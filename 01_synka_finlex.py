#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Magnus Kolsjö

"""
01_synka_finlex.py — Bulk-synkronisering av Finlex till lokal databas

Hämtar AKN XML via Finlex öppna data-API och lagrar metadata + fulltext
i den lokala PostgreSQL- eller SQLite-databasen.

Täcker:
  act/statute              — originallagar fr.o.m. 1929
  act/statute-consolidated — gällande konsoliderad lagtext
  doc/government-proposal  — propositioner (HE/RP) fr.o.m. 1992
  doc/treaty               — fördragssamlingen

Inkrementell synk: /list med publishedSince ger alla dokument som publicerats
eller ändrats sedan senaste lyckade körning, oavsett dokumentets år. Det fångar
också nya konsoliderade lydelser av gamla lagar. Starttidpunkten för varje
lyckad körning sparas per dokumenttyp i sync_status (detaljer.publicerad_sedan)
och nästa körning börjar en dag före den, som marginal mot klockskillnader.
Saknas tidpunkten används sist_synkad; saknas även den görs en årsvis synk.

Kör scriptet manuellt för initial bulk-synk (kan ta timmar):
  python3 01_synka_finlex.py --alla

Kör för inkrementell synk (daglig):
  python3 01_synka_finlex.py

Flaggor:
  --alla       Synka fr.o.m. äldsta kända år (1929 för statute, 1992 för proposal)
  --typ TYP    Synka bara en specifik typ (t.ex. --typ statute)
  --ar AR      Synka ett specifikt år
  --sedan TID  Inkrementell synk från en given tidpunkt (ISO 8601, t.ex. 2026-05-01)
  --trad N     Antal parallella trådar (default 2 — respektera rate limit)
  --torr       Torrkörning: hämta /list men ladda inte ned XML
"""

import argparse
import json
import logging
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ── Konfiguration ──────────────────────────────────────────────────────────────

_SCRIPT_DIR = Path(__file__).parent.resolve()
sys.path.insert(0, str(_SCRIPT_DIR))

from dotenv import load_dotenv
load_dotenv(_SCRIPT_DIR / ".env")

import finlex_client as fx
import db

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("synka_finlex")

# Startar äldst kända år per typ
ALDSTA_AR = {
    "statute":              1929,
    "statute-consolidated": 2000,   # konsoliderade finns från nutid bakåt
    "government-proposal":  1992,
    "treaty":               1950,
}

SYNK_KALLOR = [
    ("act", "statute"),
    ("act", "statute-consolidated"),
    ("doc", "government-proposal"),
    ("doc", "treaty"),
]


# ---------------------------------------------------------------------------
# Synk av en dokumenttyp
# ---------------------------------------------------------------------------

def synka_typ(
    hierarki: str,
    typ: str,
    fran_ar: int,
    till_ar: int,
    torrkoring: bool = False,
    max_trad: int = 2,
) -> int:
    """
    Synkroniserar en dokumenttyp för ett årsintervall.
    Returnerar antal behandlade dokument.
    """
    kalla_nyckel = f"finlex_{typ.replace('-', '_')}"
    log.info("Synkar %s/%s, år %d–%d", hierarki, typ, fran_ar, till_ar)

    # Hämta lista per år för att hålla nere minnet
    totalt = 0
    for ar in range(fran_ar, till_ar + 1):
        poster = fx.hamta_alla_i_lista(
            hierarki=hierarki,
            typ=typ,
            start_ar=ar,
            slut_ar=ar,
        )

        if not poster:
            log.debug("%s/%s år %d: inga poster", hierarki, typ, ar)
            continue

        log.info("%s/%s år %d: %d poster hittade", hierarki, typ, ar, len(poster))

        if torrkoring:
            totalt += len(poster)
            continue

        # Hämta och lagra parallellt
        utfall: dict = {OK: 0, SAKNAS: 0, FEL: []}
        with ThreadPoolExecutor(max_workers=max_trad) as pool:
            for framtid in as_completed(
                [pool.submit(_behandla_poster, grupp) for grupp in _chunk(poster, max_trad)]
            ):
                _sla_ihop(utfall, framtid.result())
        totalt += utfall[OK]
        if utfall[FEL]:
            # Året markeras inte som klart; nästa körning börjar om från det.
            log.error("%s/%s år %d: %d dokument kunde inte hämtas (Finlex svarade inte); "
                      "året markeras inte som klart", hierarki, typ, ar, len(utfall[FEL]))
            return totalt

        db.set_sync_status(
            kalla=kalla_nyckel,
            senaste_ar=ar,
            antal_poster=totalt,
        )
        log.info("%s/%s år %d klar — totalt %d dokument synkade", hierarki, typ, ar, totalt)

    return totalt


def _chunk(lista: list, storlek: int) -> list:
    """Delar en lista i grupper av angiven storlek."""
    for i in range(0, len(lista), storlek):
        yield lista[i:i + storlek]


# Utfall per hämtad URI. FEL betyder att Finlex inte svarade (nätverk, timeout,
# 5xx): dokumentet kan finnas och ska prövas igen, och checkpointen får inte
# flyttas förbi det. SAKNAS är ett faktiskt 404.
OK, SAKNAS, FEL = "ok", "saknas", "fel"


def _behandla_uri(akn_uri: str) -> str:
    """Hämtar och lagrar ett dokument. Returnerar OK, SAKNAS eller FEL."""
    sprak    = fx.sprak_av_uri(akn_uri)
    hierarki = _hierarki_av_uri(akn_uri)
    typ      = _typ_av_uri(akn_uri)
    try:
        rot = fx.hamta_akn_dokument(akn_uri, strikt=True)
    except fx.FinlexOtillganglig as exc:
        log.warning("Finlex svarade inte för %s (%s); prövas igen senare", akn_uri, exc)
        return FEL
    if rot is None:
        log.info("404 för %s", akn_uri)
        return SAKNAS

    try:
        meta     = fx.parsad_akn_metadata(rot)
        fulltext = fx.extrahera_fulltext(rot)

        # Bygg URI för det andra språket (för lagring)
        if sprak == "fi":
            akn_uri_fi = akn_uri
            akn_uri_sv = fx.byt_sprak_i_uri(akn_uri, fx.SPRAK_SV)
        else:
            akn_uri_sv = akn_uri
            akn_uri_fi = fx.byt_sprak_i_uri(akn_uri, fx.SPRAK_FI)

        db.upsert_dokument(
            kalla="finlex",
            akn_uri_fi=akn_uri_fi,
            akn_uri_sv=akn_uri_sv,
            eli=meta.get("eli"),
            typ=typ,
            finlex_hierarki=hierarki,
            finlex_typ=typ,
            titel_fi=meta.get("titel_fi"),
            titel_sv=meta.get("titel_sv"),
            ar=meta.get("ar"),
            nummer=meta.get("nummer"),
            sprak=sprak,
            fulltext_fi=fulltext if sprak == "fi" else None,
            fulltext_sv=fulltext if sprak == "sv" else None,
        )
    except Exception as exc:
        log.error("Kunde inte lagra %s: %s", akn_uri, exc)
        return FEL
    return OK


def _behandla_poster(poster: list) -> dict:
    """Hämtar och lagrar en batch med listposter. Körs i trådpool.

    Returnerar {OK: antal, SAKNAS: antal, FEL: [uri, ...]}.
    """
    utfall: dict = {OK: 0, SAKNAS: 0, FEL: []}
    for post in poster:
        akn_uri = post.get("akn_uri", "") if isinstance(post, dict) else post
        if not akn_uri:
            continue
        r = _behandla_uri(akn_uri)
        if r == FEL:
            utfall[FEL].append(akn_uri)
        else:
            utfall[r] += 1
    return utfall


def _sla_ihop(summa: dict, del_: dict) -> None:
    summa[OK] += del_[OK]
    summa[SAKNAS] += del_[SAKNAS]
    summa[FEL].extend(del_[FEL])


def _hierarki_av_uri(uri: str) -> str:
    """Extraherar 'act' eller 'doc' från AKN-URI."""
    if "/akn/fi/act/" in uri:
        return "act"
    if "/akn/fi/doc/" in uri:
        return "doc"
    return "okand"


def _typ_av_uri(uri: str) -> str:
    """Extraherar dokumenttyp från AKN-URI (t.ex. 'statute')."""
    for hierarki in ["act", "doc"]:
        markör = f"/akn/fi/{hierarki}/"
        if markör in uri:
            efter  = uri.split(markör)[1]
            delar  = efter.split("/")
            return delar[0] if delar else "okand"
    return "okand"


# ---------------------------------------------------------------------------
# Inkrementell synk via publishedSince
# ---------------------------------------------------------------------------

MARGINAL = timedelta(days=1)


def _iso_utc(t: datetime) -> str:
    """Tidpunkt i det format Finlex kräver: UTC med Z-suffix."""
    return t.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _tolka_tid(varde) -> datetime | None:
    """Tolkar en sparad eller angiven tidpunkt; datum utan tid och tid utan zon blir UTC."""
    if varde is None:
        return None
    if isinstance(varde, datetime):
        t = varde
    else:
        text = str(varde).strip().replace("Z", "+00:00")
        try:
            t = datetime.fromisoformat(text)
        except ValueError:
            return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def bestam_publicerad_sedan(typ: str) -> datetime | None:
    """Tidpunkt att synka från: senaste lyckade körning, annars sist_synkad."""
    status = db.hamta_sync_status(f"finlex_{typ.replace('-', '_')}") or {}
    detaljer = status.get("detaljer") or {}
    if isinstance(detaljer, str):  # SQLite lagrar JSON som text
        detaljer = json.loads(detaljer)
    t = _tolka_tid(detaljer.get("publicerad_sedan")) or _tolka_tid(status.get("sist_synkad"))
    return t - MARGINAL if t else None


def synka_sedan(
    hierarki: str,
    typ: str,
    sedan: datetime,
    torrkoring: bool = False,
    max_trad: int = 2,
) -> int:
    """
    Synkar alla dokument av en typ som publicerats eller ändrats sedan `sedan`.

    Körningens starttid sparas först när alla poster är behandlade, så att en
    avbruten körning upprepas från samma tidpunkt nästa gång.
    """
    kalla_nyckel = f"finlex_{typ.replace('-', '_')}"
    korningens_start = datetime.now(timezone.utc)
    poster = fx.hamta_alla_i_lista(
        hierarki=hierarki, typ=typ, publicerad_sedan=_iso_utc(sedan),
    )
    log.info("%s/%s: %d poster publicerade eller ändrade sedan %s",
             hierarki, typ, len(poster), _iso_utc(sedan))
    if torrkoring or not poster:
        if not torrkoring:
            db.set_sync_status(kalla=kalla_nyckel,
                               detaljer={"publicerad_sedan": _iso_utc(korningens_start)})
        return len(poster)

    utfall: dict = {OK: 0, SAKNAS: 0, FEL: []}
    with ThreadPoolExecutor(max_workers=max_trad) as pool:
        for framtid in as_completed(
            [pool.submit(_behandla_poster, grupp) for grupp in _chunk(poster, max_trad)]
        ):
            _sla_ihop(utfall, framtid.result())
    totalt = utfall[OK]

    if utfall[FEL]:
        log.error("%s/%s: %d dokument kunde inte hämtas (Finlex svarade inte); "
                  "tidpunkten flyttas inte fram", hierarki, typ, len(utfall[FEL]))
    else:
        db.set_sync_status(kalla=kalla_nyckel, antal_poster=totalt,
                           detaljer={"publicerad_sedan": _iso_utc(korningens_start)})
    return totalt


# ---------------------------------------------------------------------------
# Årsvis synk (full synk och första körning)
# ---------------------------------------------------------------------------

def bestam_startaar(hierarki: str, typ: str, tvinga_alla: bool) -> int:
    """Bestämmer från vilket år synken ska börja."""
    if tvinga_alla:
        return ALDSTA_AR.get(typ, 2000)

    kalla_nyckel = f"finlex_{typ.replace('-', '_')}"
    status = db.hamta_sync_status(kalla_nyckel)
    if status and status.get("senaste_ar"):
        # Börja ett år bakåt för att fånga modifieringar
        return max(ALDSTA_AR.get(typ, 2000), int(status["senaste_ar"]) - 1)

    return ALDSTA_AR.get(typ, 2000)


# ---------------------------------------------------------------------------
# Huvudprogram
# ---------------------------------------------------------------------------

def installera_schema(shell_sokvag: str):
    """Installerar schemalagt jobb baserat på SCHEMALAGGARE i .env.

    Stöder: cron (Linux och macOS) och launchd (macOS).
    Det schemalagda jobbet anropar synk_daglig.sh (kör finlex-synk + embedding).
    """
    import platform
    import subprocess
    from pathlib import Path

    schemalaggare = os.getenv("SCHEMALAGGARE", "cron").lower()
    cron_schema   = os.getenv("CRON_SCHEMA", "15 4 * * *")
    shell_abs     = str(Path(shell_sokvag).resolve())

    if schemalaggare == "launchd":
        if platform.system() != "Darwin":
            log.error("launchd är bara tillgängligt på macOS. Byt till SCHEMALAGGARE=cron.")
            return

        plist_dir = Path.home() / "Library" / "LaunchAgents"
        plist_fil = plist_dir / "se.magnuskolsjo.mcp-finland-synk.plist"
        plist_dir.mkdir(parents=True, exist_ok=True)

        delar  = cron_schema.split()
        minut  = delar[0]
        timme  = delar[1]

        plist = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>se.magnuskolsjo.mcp-finland-synk</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>{shell_abs}</string>
    </array>
    <key>StartCalendarInterval</key>
    <dict>
        <key>Hour</key>  <integer>{timme}</integer>
        <key>Minute</key><integer>{minut}</integer>
    </dict>
    <key>RunAtLoad</key><false/>
    <key>StandardOutPath</key>
    <string>{str(Path.home())}/Library/Logs/finland-synk.log</string>
    <key>StandardErrorPath</key>
    <string>{str(Path.home())}/Library/Logs/finland-synk-fel.log</string>
</dict>
</plist>"""
        with open(plist_fil, "w", encoding="utf-8") as fh:
            fh.write(plist)

        subprocess.run(["launchctl", "load", str(plist_fil)], check=True)
        log.info("launchd-jobb installerat: %s", plist_fil)
        log.info("Kör dagligen kl. %s:%s. Loggar: ~/Library/Logs/", timme, minut)

    else:
        # cron — fungerar på Linux och macOS
        rad = f"{cron_schema} /bin/bash {shell_abs}\n"

        befintlig = subprocess.run(
            ["crontab", "-l"], capture_output=True, text=True
        ).stdout

        if shell_abs in befintlig:
            log.info("Cron-jobb finns redan. Ingen ändring gjord.")
            return

        ny_crontab = befintlig + rad
        proc = subprocess.run(["crontab", "-"], input=ny_crontab, text=True)
        if proc.returncode == 0:
            log.info("Cron-jobb tillagt: %s", rad.strip())
        else:
            log.error("Kunde inte uppdatera crontab.")


def main():
    parser = argparse.ArgumentParser(
        description="Synkar Finlex öppna data till lokal databas"
    )
    parser.add_argument("--alla",             action="store_true", help="Synka fr.o.m. äldsta år (full synk)")
    parser.add_argument("--typ",              type=str,            help="Synka bara en specifik typ (t.ex. statute)")
    parser.add_argument("--ar",               type=int,            help="Synka ett specifikt år")
    parser.add_argument("--sedan",            type=str,            help="Inkrementell synk från tidpunkt (ISO 8601)")
    parser.add_argument("--trad",             type=int, default=2, help="Antal parallella trådar (default 2)")
    parser.add_argument("--torr",             action="store_true", help="Torrkörning (hämtar /list men ingen XML)")
    parser.add_argument("--installera-schema", action="store_true", help="Installera schemalagt jobb via cron eller launchd (se .env)")
    args = parser.parse_args()

    if args.installera_schema:
        script_dir  = Path(__file__).parent.resolve()
        shell_skript = script_dir / "synk_daglig.sh"
        installera_schema(str(shell_skript))
        return

    db.init_db()
    innevarande_ar = datetime.now().year

    kallor = SYNK_KALLOR
    if args.typ:
        kallor = [(h, t) for h, t in SYNK_KALLOR if t == args.typ]
        if not kallor:
            log.error("Okänd typ: %s. Tillgängliga: %s", args.typ, [t for _, t in SYNK_KALLOR])
            sys.exit(1)

    for hierarki, typ in kallor:
        if not args.alla and not args.ar:
            sedan = _tolka_tid(args.sedan) if args.sedan else bestam_publicerad_sedan(typ)
            if args.sedan and sedan is None:
                log.error("Ogiltig tidpunkt för --sedan: %s", args.sedan)
                sys.exit(1)
            if sedan is not None:
                log.info("=== Synkar %s/%s, ändrat sedan %s ===", hierarki, typ, _iso_utc(sedan))
                start = time.time()
                antal = synka_sedan(hierarki, typ, sedan, args.torr, args.trad)
                log.info("=== %s/%s klar: %d dokument på %.0f s ===",
                         hierarki, typ, antal, time.time() - start)
                continue

        if args.ar:
            fran_ar = args.ar
            till_ar = args.ar
        else:
            fran_ar = bestam_startaar(hierarki, typ, args.alla)
            till_ar = innevarande_ar

        log.info("=== Synkar %s/%s, år %d–%d ===", hierarki, typ, fran_ar, till_ar)
        start = time.time()

        antal = synka_typ(
            hierarki=hierarki,
            typ=typ,
            fran_ar=fran_ar,
            till_ar=till_ar,
            torrkoring=args.torr,
            max_trad=args.trad,
        )

        elapsed = time.time() - start
        log.info(
            "=== %s/%s klar: %d dokument på %.0f s (%.1f dok/s) ===",
            hierarki, typ, antal, elapsed, antal / elapsed if elapsed else 0
        )

    log.info("Synk av Finlex klar.")


if __name__ == "__main__":
    main()
