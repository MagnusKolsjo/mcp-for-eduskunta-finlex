#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Magnus Kolsjö

"""
02_synka_voteringar_historik.py — Voteringshistorik 1996–2014 från avoindata.eduskunta.fi

Eduskuntas nya API (api.eduskunta.fi) har voteringar fr.o.m. plenum 94/2008
(2008-10-17). Allt äldre, och därmed hela perioden 1996–2014 i ett svep,
hämtas här ur Eduskuntas gamla öppna datatjänst avoindata.eduskunta.fi,
som exponerar tabelldata via GET /api/v1/tables/.

Den gamla tjänsten ska enligt Eduskunta ersättas av den nya "vid utgången
av 2026". Inget datum för nedstängning är angivet. Redan synkade rader
ligger kvar i den lokala databasen även om källan försvinner; synken kan
däremot inte köras om. Ett nedstängt eller omgjort API ger ett tydligt fel
och exitkod 1, aldrig en tyst körning med noll rader.

Tabeller:
  SaliDBAanestys        — voteringsresultat per omröstning, en rad per språk
  SaliDBAanestysEdustaja — enskild ledamots röst per votering (8,6M rader)

OBS: SaliDBAanestysEdustaja (enskilda röster) synkas inte. Flaggan
--med-roster finns kvar men skriver bara ut en varning.

Kör:
  python3 02_synka_voteringar_historik.py          # Bara voteringsresultat
  python3 02_synka_voteringar_historik.py --fran-sida 120  # Återuppta
"""

import argparse
import json
import logging
import sys
import time
from pathlib import Path

# ── Konfiguration ──────────────────────────────────────────────────────────────

_SCRIPT_DIR = Path(__file__).parent.resolve()
sys.path.insert(0, str(_SCRIPT_DIR))

from dotenv import load_dotenv
load_dotenv(_SCRIPT_DIR / ".env")

import db

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("synka_voteringar_historik")

import httpx

AVOINDATA_BASE = "https://avoindata.eduskunta.fi/api/v1/tables"
USER_AGENT     = "mcp-for-eduskunta-finlex/1.0 (+https://github.com/MagnusKolsjo/mcp-for-eduskunta-finlex)"
SIDSTORLEK     = 100


# ---------------------------------------------------------------------------
# Hämtning från avoindata.eduskunta.fi
# ---------------------------------------------------------------------------

class KallanOtillganglig(RuntimeError):
    """Den gamla datatjänsten svarar inte som väntat: nedstängd, flyttad eller omgjord."""


def _kalla_fel(orsak: str) -> KallanOtillganglig:
    return KallanOtillganglig(
        f"avoindata.eduskunta.fi svarar inte som väntat ({orsak}). "
        "Tjänsten ska enligt Eduskunta ersättas av api.eduskunta.fi vid "
        "utgången av 2026 och kan ha stängts. Redan synkade voteringar ligger "
        "kvar i databasen. Voteringar fr.o.m. 2008-10-17 finns i det nya API:t "
        "och nås live via fi_hamta_aanestys; äldre saknas där."
    )


def hamta_sida(tabell: str, sida: int) -> dict:
    """
    Hämtar en sida från avoindata.eduskunta.fi.
    GET /api/v1/tables/{tabell}/rows?perPage=100&page={sida}

    Tillfälliga fel (nätverk, 429, 5xx) prövas tre gånger. Ett svar som inte
    har tabelltjänstens form — fel statuskod, HTML i stället för JSON eller
    saknade kolumnnamn — betyder att tjänsten har ändrats eller stängts, och
    ger KallanOtillganglig direkt.
    """
    url = f"{AVOINDATA_BASE}/{tabell}/rows"
    for forsok in range(3):
        try:
            r = httpx.get(
                url,
                params={"perPage": SIDSTORLEK, "page": sida},
                headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
                timeout=30,
            )
        except httpx.HTTPError as exc:
            if forsok < 2:
                log.warning("Nätverksfel sida %d (försök %d): %s", sida, forsok + 1, exc)
                time.sleep(5 * (forsok + 1))
                continue
            raise _kalla_fel(f"nätverksfel: {exc}") from exc

        if r.status_code == 429 or r.status_code >= 500:
            if forsok < 2:
                log.warning("HTTP %d sida %d (försök %d)", r.status_code, sida, forsok + 1)
                time.sleep(5 * (forsok + 1))
                continue
            raise _kalla_fel(f"HTTP {r.status_code}")
        if r.status_code != 200:
            raise _kalla_fel(f"HTTP {r.status_code} för {r.url}")

        try:
            svar = r.json()
        except ValueError as exc:
            typ = r.headers.get("content-type", "okänd typ")
            raise _kalla_fel(f"svaret är inte JSON ({typ})") from exc
        if not isinstance(svar, dict) or "columnNames" not in svar or "rowData" not in svar:
            raise _kalla_fel("svaret saknar columnNames/rowData")
        return svar
    raise _kalla_fel("inga fler försök")


# ---------------------------------------------------------------------------
# Parsning av SaliDBAanestys
# ---------------------------------------------------------------------------

def _resultat(ja: int | None, nej: int | None) -> str | None:
    """JA eller NEJ ur rösttalen; None vid lika eller när något tal saknas."""
    if ja is None or nej is None or ja == nej:
        return None
    return "JA" if ja > nej else "NEJ"


def parsad_aanestys(rad: dict) -> dict | None:
    """
    Konverterar en rad från SaliDBAanestys till voteringsfält.

    Faktiska kolumnnamn (verifierat 2026-05-17 mot live-API):
      AanestysId, KieliId, IstuntoVPVuosi, IstuntoNumero, IstuntoPvm,
      IstuntoIlmoitettuAlkuaika, IstuntoAlkuaika, PJOtsikko, AanestysNumero,
      AanestysAlkuaika, AanestysLoppuaika, AanestysMitatoity, AanestysOtsikko,
      AanestysLisaOtsikko, PaaKohtaTunniste, PaaKohtaOtsikko, PaaKohtaHuomautus,
      KohtaKasittelyOtsikko, KohtaKasittelyVaihe, KohtaJarjestys, KohtaTunniste,
      KohtaOtsikko, KohtaHuomautus, AanestysTulosJaa, AanestysTulosEi,
      AanestysTulosTyhjia, AanestysTulosPoissa, AanestysTulosYhteensa,
      Url, AanestysPoytakirja, AanestysPoytakirjaUrl, AanestysValtiopaivaasia,
      AanestysValtiopaivaasiaUrl, AliKohtaTunniste, Imported

    KieliId: 1 = fi, 2 = sv. Varje votering finns som två rader, en per språk,
    med samma aanestystunnus men olika AanestysId.

    Rubriker: AanestysOtsikko (voteringens egen rubrik, t.ex. "Pöydällepano,
    Pulliainen/Rosendahl") finns bara på finska och är identisk i båda
    raderna. Det språkberoende fältet är KohtaOtsikko (ärendets rubrik).
    Därför blir otsikko_fi "KohtaOtsikko – AanestysOtsikko" ur den finska
    raden och otsikko_sv KohtaOtsikko ur den svenska raden. Varje rad fyller
    bara sitt eget språkfält; upserten slår ihop dem.

    Inget Tulos-fält — resultat beräknas från Jaa vs Ei.
    """
    if not rad:
        return None

    try:
        aanestys_id_raw = rad.get("AanestysId", "")
        aanestys_nr_raw = rad.get("AanestysNumero")
        istunto_nr      = rad.get("IstuntoNumero")
        vp_ar_raw       = rad.get("IstuntoVPVuosi")
        aika            = rad.get("AanestysAlkuaika") or rad.get("IstuntoPvm", "")
        aanestys_otsikko = (rad.get("AanestysOtsikko") or "").strip()
        kohta_otsikko    = (rad.get("KohtaOtsikko") or rad.get("PaaKohtaOtsikko") or "").strip()
        ja              = rad.get("AanestysTulosJaa")
        nej             = rad.get("AanestysTulosEi")
        tom             = rad.get("AanestysTulosTyhjia")
        franv           = rad.get("AanestysTulosPoissa")
        kieli_id        = rad.get("KieliId", "1")

        # KieliId: "1" = finska, "2" = svenska
        kieli = "sv" if str(kieli_id) == "2" else "fi"

        # Bygg aanestystunnus: {vp_ar}-{istunto}-{aanestys_nr}
        vp_ar      = int(vp_ar_raw)      if vp_ar_raw      else None
        istunto    = int(istunto_nr)     if istunto_nr      else None
        aanestys_nr = int(aanestys_nr_raw) if aanestys_nr_raw else None

        if vp_ar and istunto and aanestys_nr:
            aanestystunnus = f"{vp_ar}-{istunto}-{aanestys_nr}"
        else:
            aanestystunnus = str(aanestys_id_raw)

        # Datum från tidsstämpel
        datum = aika[:10] if aika and len(aika) >= 10 else None

        # Resultat beräknas från röstantal (inget Tulos-fält i denna tabell).
        # Saknas något av talen lämnas resultatet tomt i stället för att
        # gissas; upserten räknar om det ur de sammanslagna talen.
        ja_int  = int(ja)  if ja  is not None else None
        nej_int = int(nej) if nej is not None else None
        resultat = _resultat(ja_int, nej_int)

        return {
            "aanestystunnus": aanestystunnus,
            "vp_ar":          vp_ar,
            "istunto_nr":     istunto,
            "datum":          datum,
            "otsikko_fi":     " – ".join(x for x in (kohta_otsikko, aanestys_otsikko) if x) or None
                              if kieli == "fi" else None,
            "otsikko_sv":     (kohta_otsikko or None) if kieli == "sv" else None,
            "ja_roster":      ja_int,
            "nej_roster":     nej_int,
            "tom_roster":     int(tom)   if tom   is not None else None,
            "franv_roster":   int(franv) if franv is not None else None,
            "resultat":       resultat,
            "raw":            rad,
        }
    except Exception as exc:
        log.warning("Kunde inte parsa rad: %s — %s", rad, exc)
        return None


# ---------------------------------------------------------------------------
# Synk av voteringsresultat
# ---------------------------------------------------------------------------

def synka_voteringsresultat(fran_sida: int = 1, max_sidor: int = 0) -> int:
    """
    Synkar SaliDBAanestys (voteringsresultat) till lokal databas.

    max_sidor > 0 begränsar körningen till så många sidor (för provkörning).
    Returnerar antal synkade poster. Kastar KallanOtillganglig om källan
    inte svarar som väntat.
    """
    log.info("Synkar voteringsresultat (SaliDBAanestys) fr.o.m. sida %d", fran_sida)
    totalt = 0
    sida   = fran_sida

    while True:
        log.info("Hämtar sida %d (totalt: %d)", sida, totalt)
        # Fel från källan avbryter synken och propagerar till main(), så att
        # körningen slutar med exitkod 1 i stället för att se lyckad ut.
        svar     = hamta_sida("SaliDBAanestys", sida)
        kolumner = svar.get("columnNames", [])
        rader    = [dict(zip(kolumner, r)) for r in svar.get("rowData", [])]

        if not rader:
            if sida == fran_sida:
                # En tom startsida är normal när en omstart pekar förbi sista
                # sidan. Källan räknas som otillgänglig bara om även sida 1 är tom.
                if sida == 1 or not hamta_sida("SaliDBAanestys", 1).get("rowData"):
                    raise _kalla_fel("tabellen är tom")
                log.info("Sida %d är tom: inget nytt efter föregående körning", sida)
            else:
                log.info("Tom sida %d — synk klar", sida)
            break

        for rad in rader:
            parsad = parsad_aanestys(rad)
            if not parsad:
                continue

            ar_rad = parsad["datum"][:4] if parsad["datum"] else None
            ar_int = int(ar_rad) if ar_rad else None

            # Hoppa över voteringar efter 2014 (nås live via det nya API:et)
            if ar_int and ar_int > 2014:
                log.debug("Hoppar vp_ar=%s (täcks av ny API)", parsad.get("vp_ar"))
                continue

            db.upsert_votering(
                aanestys_id  = parsad["aanestystunnus"],
                ar           = ar_int,
                vp_ar        = parsad["vp_ar"],
                istunto_nr   = parsad["istunto_nr"],
                datum        = parsad["datum"],
                otsikko_fi   = parsad["otsikko_fi"],
                otsikko_sv   = parsad["otsikko_sv"],
                ja_roster    = parsad["ja_roster"],
                nej_roster   = parsad["nej_roster"],
                tom_roster   = parsad["tom_roster"],
                franv_roster = parsad["franv_roster"],
                resultat     = parsad["resultat"],
                kalla        = "avoindata",
                raw_json     = parsad["raw"],
            )
            totalt += 1

        # Spara checkpoint
        db.set_sync_status(
            kalla="voteringar_historik",
            antal_poster=totalt,
            detaljer={"senaste_sida": sida},
        )

        sista = len(rader) < SIDSTORLEK or svar.get("hasMore") is False
        if sista or (max_sidor and sida - fran_sida + 1 >= max_sidor):
            log.info("Sista sidan för körningen nådd (sida %d, %d rader)", sida, len(rader))
            break

        sida += 1
        time.sleep(0.5)  # Vänta för att inte hammra API:et

    return totalt


# ---------------------------------------------------------------------------
# Huvudprogram
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Synkar voteringshistorik 1996–2014 från avoindata.eduskunta.fi"
    )
    parser.add_argument("--fran-sida",  type=int, default=1,
                        help="Startsida (för att återuppta avbruten synk)")
    parser.add_argument("--max-sidor", type=int, default=0,
                        help="Hämta högst så många sidor (0 = alla); för provkörning")
    parser.add_argument("--ja", action="store_true",
                        help="Kör om utan att fråga, även om synken redan körts")
    parser.add_argument("--med-roster", action="store_true",
                        help="Synka även enskilda ledamotsröster (SaliDBAanestysEdustaja, ~8,6M rader)")
    args = parser.parse_args()

    db.init_db()

    # Kontrollera om synken redan körts
    status = db.hamta_sync_status("voteringar_historik")
    if status and status.get("antal_poster", 0) > 0 and args.fran_sida == 1 and not args.ja:
        log.info(
            "Voteringshistorik redan synkad (%d poster, senast: %s). "
            "Kör med --fran-sida för att fortsätta eller tvinga om.",
            status["antal_poster"], status.get("sist_synkad", "?")
        )
        svar = input("Kör om ändå? (j/N) ").strip().lower()
        if svar != "j":
            sys.exit(0)

    start  = time.time()
    try:
        antal = synka_voteringsresultat(fran_sida=args.fran_sida, max_sidor=args.max_sidor)
    except KallanOtillganglig as exc:
        log.error("%s", exc)
        detaljer = (db.hamta_sync_status("voteringar_historik") or {}).get("detaljer") or {}
        if isinstance(detaljer, str):  # SQLite lagrar JSON som text
            detaljer = json.loads(detaljer)
        sida = detaljer.get("senaste_sida")
        if sida:
            log.error("Senast sparade sida: %s. Återuppta med --fran-sida %s.", sida, sida + 1)
        sys.exit(1)
    elapsed = time.time() - start

    log.info(
        "Voteringsresultat synkade: %d poster på %.0f s",
        antal, elapsed
    )

    if args.med_roster:
        log.warning(
            "Synk av enskilda ledamotsröster (SaliDBAanestysEdustaja) är inte implementerat "
            "i denna version. Tabellen innehåller 8,6M rader och kräver ett eget schema. "
            "Implementera vid behov."
        )

    log.info("Synk av voteringshistorik klar.")


if __name__ == "__main__":
    main()
