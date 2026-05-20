#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Magnus Kolsjö

"""
01_synka_voteringar_historik.py — Voteringshistorik 1996–2014 från avoindata.eduskunta.fi

Det nya API:et (api.eduskunta.fi) täcker voteringar fr.o.m. ca 2015.
Äldre voteringshistorik (1996–2014) hämtas från den sekundära källan
avoindata.eduskunta.fi som exponerar tabelldata via GET /api/v1/tables/.

Tabeller:
  SaliDBAanestys        — voteringsresultat per omröstning (43 208 rader)
  SaliDBAanestysEdustaja — enskild ledamots röst per votering (8,6M rader)

OBS: Det historiska voteringsarkivet är stort. SaliDBAanestysEdustaja (enskilda röster)
hoppas över i standardläge för att spara lagringsutrymme. Aktivera med --med-roster.

Kör:
  python3 01_synka_voteringar_historik.py          # Bara voteringsresultat
  python3 01_synka_voteringar_historik.py --med-roster  # Inkl. enskilda röster (3–5 GB)
"""

import argparse
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

def hamta_sida(tabell: str, sida: int) -> dict:
    """
    Hämtar en sida från avoindata.eduskunta.fi.
    GET /api/v1/tables/{tabell}/rows?perPage=100&page={sida}
    """
    url = f"{AVOINDATA_BASE}/{tabell}/rows"
    for forsok in range(3):
        try:
            r = httpx.get(
                url,
                params={"perPage": SIDSTORLEK, "page": sida},
                headers={"User-Agent": USER_AGENT},
                timeout=30,
            )
            r.raise_for_status()
            return r.json()
        except Exception as exc:
            if forsok < 2:
                log.warning("Fel vid hämtning sida %d (försök %d): %s", sida, forsok + 1, exc)
                time.sleep(5 * (forsok + 1))
            else:
                raise


def hamta_antal_rader(tabell: str) -> int:
    """Hämtar totalt antal rader i en tabell."""
    try:
        svar = hamta_sida(tabell, 1)
        return svar.get("rowData", {}).get("rowCount", 0) or svar.get("rowCount", 0) or 0
    except Exception as exc:
        log.error("Kunde inte hämta antal rader för %s: %s", tabell, exc)
        return 0


# ---------------------------------------------------------------------------
# Parsning av SaliDBAanestys
# ---------------------------------------------------------------------------

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

    KieliId: 1 = fi, 2 = sv (kontrollerat mot titlar i svaren)
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
        otsikko         = rad.get("AanestysOtsikko") or rad.get("PaaKohtaOtsikko", "")
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

        # Resultat beräknas från röstantal (inget Tulos-fält i denna tabell)
        ja_int  = int(ja)  if ja  is not None else 0
        nej_int = int(nej) if nej is not None else 0
        if ja_int > nej_int:
            resultat = "JA"
        elif nej_int > ja_int:
            resultat = "NEJ"
        else:
            resultat = None

        return {
            "aanestystunnus": aanestystunnus,
            "vp_ar":          vp_ar,
            "istunto_nr":     istunto,
            "datum":          datum,
            "otsikko_fi":     otsikko if kieli == "fi" else None,
            "otsikko_sv":     otsikko if kieli == "sv" else None,
            "ja_roster":      ja_int  if ja  is not None else None,
            "nej_roster":     nej_int if nej is not None else None,
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

def synka_voteringsresultat(fran_sida: int = 1) -> int:
    """
    Synkar SaliDBAanestys (voteringsresultat) till lokal databas.
    Returnerar antal synkade poster.
    """
    log.info("Synkar voteringsresultat (SaliDBAanestys) fr.o.m. sida %d", fran_sida)
    totalt = 0
    sida   = fran_sida

    while True:
        log.info("Hämtar sida %d (totalt: %d)", sida, totalt)
        try:
            svar     = hamta_sida("SaliDBAanestys", sida)
            kolumner = svar.get("columnNames", [])
            rader    = [dict(zip(kolumner, r)) for r in svar.get("rowData", [])]

            if not rader:
                log.info("Tom sida %d — synk klar", sida)
                break

            for rad in rader:
                parsad = parsad_aanestys(rad)
                if not parsad:
                    continue

                ar_rad = parsad["datum"][:4] if parsad["datum"] else None
                ar_int = int(ar_rad) if ar_rad else None

                # Hoppa över voteringar efter 2014 (täcks av det nya API:et)
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

            if len(rader) < SIDSTORLEK:
                log.info("Sista sidan nådd (%d rader)", len(rader))
                break

            sida += 1
            time.sleep(0.5)  # Vänta för att inte hammra API:et

        except Exception as exc:
            log.error("Fel på sida %d: %s", sida, exc)
            log.info("Synk avbruten vid sida %d. Kör om med --fran-sida %d", sida, sida)
            break

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
    parser.add_argument("--med-roster", action="store_true",
                        help="Synka även enskilda ledamotsröster (SaliDBAanestysEdustaja, ~8,6M rader)")
    args = parser.parse_args()

    db.init_db()

    # Kontrollera om synken redan körts
    status = db.hamta_sync_status("voteringar_historik")
    if status and status.get("antal_poster", 0) > 0 and args.fran_sida == 1:
        log.info(
            "Voteringshistorik redan synkad (%d poster, senast: %s). "
            "Kör med --fran-sida för att fortsätta eller tvinga om.",
            status["antal_poster"], status.get("sist_synkad", "?")
        )
        svar = input("Kör om ändå? (j/N) ").strip().lower()
        if svar != "j":
            sys.exit(0)

    start  = time.time()
    antal  = synka_voteringsresultat(fran_sida=args.fran_sida)
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
