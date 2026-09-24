#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Magnus Kolsjö

"""
06_konvertera_vektorer.py — Byter embeddings från vector(768) till halfvec(768)

halfvec lagrar varje komponent som 16-bitars flyttal: 1 540 byte per vektor
i stället för 3 076. Träffsäkerheten påverkas inte mätbart för cosinus-
sökning (samma topp-10 i provmätningen). Vektorindexen byggs om som HNSW.

Servern fungerar före, under (med väntan) och efter konverteringen: den läser
kolumntypen vid varje sökning. Nya och nästan tomma databaser konverteras
automatiskt vid uppstart; det här skriptet är till för befintliga databaser
med många chunks, där omskrivningen tar tid och kräver diskutrymme.

Så går det till, per körning:
  1. Vektorindexet på kolumnen tas bort (dess operatorklass gäller vector).
  2. ALTER TABLE … TYPE halfvec(768) skriver om hela finland.chunks. Tabellen
     är låst under tiden; sökningar väntar. En körning med --kolumn bada gör
     båda kolumnerna i samma omskrivning och är därför snabbast.
  3. HNSW-index byggs för de konverterade kolumnerna.

Kör:
  python3 06_konvertera_vektorer.py --torrkorning          # uppskattning, ändrar inget
  python3 06_konvertera_vektorer.py                         # båda kolumnerna
  python3 06_konvertera_vektorer.py --kolumn fi             # en kolumn i taget
  python3 06_konvertera_vektorer.py --bara-index --kolumn sv  # bygg om index
"""

import argparse
import logging
import math
import sys
import time
from pathlib import Path

_SCRIPT_DIR = Path(__file__).parent.resolve()
sys.path.insert(0, str(_SCRIPT_DIR))

from dotenv import load_dotenv
load_dotenv(_SCRIPT_DIR / ".env")

import db  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("konvertera_vektorer")

# Uppmätt på riktiga chunks (pgvector 0.8.2): 25 000 rader med text och
# 35 470 vektorer för lagringen, 60 000 vektorer för HNSW (maintenance_work_mem
# 2 GB, två parallella arbetare). Används bara för uppskattningarna.
SPARAT_PER_VEKTOR = 2_640                    # byte mindre i tabell + TOAST per vektor
HNSW_BYTE_PER_RAD = 2_100                    # indexstorlek per vektor
HNSW_SEK_60K = 40.0                          # byggtid för 60 000 vektorer
OMSKRIVNING_BYTE_PER_SEK = 100 * 1024**2     # försiktig läs+skrivtakt


def _gb(byte: float) -> str:
    return f"{byte / 1024**3:.1f} GB"


def _tid(sek: float) -> str:
    return f"{sek / 60:.0f} min" if sek < 5400 else f"{sek / 3600:.1f} h"


def _hnsw_sek(n: int) -> float:
    """HNSW-bygget växer ungefär som n·log n."""
    if n <= 0:
        return 0.0
    return HNSW_SEK_60K * (n / 60_000) * (math.log(max(n, 2)) / math.log(60_000))


def lagesbild() -> dict:
    """Läser storlekar ur katalogen och statistiken; inga tunga frågor."""
    with db._cursor() as cur:
        cur.execute("""
            SELECT c.reltuples::bigint, pg_relation_size(c.oid),
                   coalesce(pg_total_relation_size(nullif(c.reltoastrelid, 0)), 0),
                   pg_total_relation_size(c.oid)
            FROM pg_class c WHERE c.oid = 'finland.chunks'::regclass""")
        rader, heap, toast, totalt = cur.fetchone()
        if rader < 0:  # aldrig analyserad
            cur.execute("SELECT count(*) FROM finland.chunks")
            rader = cur.fetchone()[0]
        info = {"rader": rader, "heap": heap, "toast": toast, "totalt": totalt, "kolumner": {}}
        for sprak in ("fi", "sv"):
            kol = f"embedding_{sprak}"
            cur.execute("""SELECT null_frac FROM pg_stats
                           WHERE schemaname = 'finland' AND tablename = 'chunks'
                             AND attname = %s""", (kol,))
            rad = cur.fetchone()
            null_frac = rad[0] if rad else 0.0
            index = 0
            if _index_finns(cur, sprak):
                cur.execute("SELECT pg_relation_size(%s::regclass)",
                            (f"finland.{db.vektorindex_namn(sprak)}",))
                index = cur.fetchone()[0]
            info["kolumner"][sprak] = {
                "typ": db.vektortyp(sprak, cur),
                "vektorer": int(rader * (1 - null_frac)),
                "index": index,
            }
    return info


def _index_finns(cur, sprak: str) -> bool:
    cur.execute("SELECT 1 FROM pg_indexes WHERE schemaname = 'finland' AND indexname = %s",
                (db.vektorindex_namn(sprak),))
    return cur.fetchone() is not None


def torrkorning(sprak_lista: list[str]) -> None:
    info = lagesbild()
    kol = info["kolumner"]
    index_nu = sum(k["index"] for k in kol.values())
    log.info("finland.chunks: %d rader; totalt %s (tabell %s, TOAST %s, vektorindex %s)",
             info["rader"], _gb(info["totalt"]), _gb(info["heap"]), _gb(info["toast"]),
             _gb(index_nu))

    att_gora = [s for s in sprak_lista if kol[s]["typ"] == "vector"]
    for s in sprak_lista:
        log.info("[%s] %s, %d vektorer, index %s", s, kol[s]["typ"], kol[s]["vektorer"],
                 _gb(kol[s]["index"]))
    if not att_gora:
        log.info("Inget att konvertera; kolumnerna är redan halfvec.")
        return

    sparat = sum(kol[s]["vektorer"] * SPARAT_PER_VEKTOR for s in att_gora)
    tabell_efter = max(info["heap"] + info["toast"] - sparat, 0)
    ovriga_index = info["totalt"] - info["heap"] - info["toast"] - index_nu
    hnsw_efter = sum(kol[s]["vektorer"] * HNSW_BYTE_PER_RAD for s in sprak_lista)
    index_kvar = sum(kol[s]["index"] for s in sprak_lista if s not in att_gora)
    totalt_efter = tabell_efter + ovriga_index + hnsw_efter + index_kvar

    omskrivningar = 1 if len(att_gora) == 1 or len(att_gora) == len(sprak_lista) else len(att_gora)
    sek_omskrivning = omskrivningar * (info["heap"] + info["toast"]) / OMSKRIVNING_BYTE_PER_SEK
    sek_index = sum(_hnsw_sek(kol[s]["vektorer"]) for s in sprak_lista)
    storsta_graf = max(kol[s]["vektorer"] for s in sprak_lista) * HNSW_BYTE_PER_RAD

    log.info("Uppskattning (osäkerhet ungefär ±50 %):")
    log.info("  omskrivning av tabellen: %s; indexbygge: %s (med tillräckligt minne)",
             _tid(sek_omskrivning), _tid(sek_index))
    log.info("  disk under omskrivningen: ytterligare %s (ny kopia av tabellen) "
             "efter att vektorindexen (%s) tagits bort",
             _gb(tabell_efter + ovriga_index), _gb(sum(kol[s]["index"] for s in att_gora)))
    log.info("  maintenance_work_mem för snabbt HNSW-bygge: minst %s (--minne)",
             _gb(storsta_graf * 1.2))
    log.info("  slutstorlek: %s (tabell %s, HNSW %s, övriga index %s); frigör %s",
             _gb(totalt_efter), _gb(tabell_efter), _gb(hnsw_efter + index_kvar),
             _gb(ovriga_index), _gb(info["totalt"] - totalt_efter))


def konvertera(sprak_lista: list[str], minne: str, parallella: int, bara_index: bool) -> None:
    with db._cursor() as cur:
        att_gora = [s for s in sprak_lista if db.vektortyp(s, cur) == "vector"]
    if not bara_index and att_gora:
        start = time.time()
        with db._cursor() as cur:
            for s in att_gora:
                cur.execute(f"DROP INDEX IF EXISTS finland.{db.vektorindex_namn(s)}")
            # Alla kolumner i samma ALTER TABLE ger en enda omskrivning.
            delar = ", ".join(
                f"ALTER COLUMN embedding_{s} TYPE halfvec({db.VEKTOR_DIM}) "
                f"USING embedding_{s}::halfvec({db.VEKTOR_DIM})"
                for s in att_gora
            )
            log.info("Skriver om finland.chunks (%s); tabellen är låst under tiden...",
                     ", ".join(att_gora))
            cur.execute(f"ALTER TABLE finland.chunks {delar}")
        log.info("Omskrivning klar på %s", _tid(time.time() - start))
    elif not bara_index:
        log.info("Kolumnerna är redan halfvec; bygger bara index.")

    for s in sprak_lista:
        start = time.time()
        log.info("[%s] bygger HNSW-index (maintenance_work_mem=%s)...", s, minne)
        db.bygg_vektorindex(s, minne=minne, parallella=parallella)
        log.info("[%s] index klart på %s", s, _tid(time.time() - start))

    with db._cursor() as cur:
        cur.execute("ANALYZE finland.chunks")
        cur.execute("SELECT pg_total_relation_size('finland.chunks')")
        log.info("finland.chunks efter konverteringen: %s", _gb(cur.fetchone()[0]))


def main() -> None:
    parser = argparse.ArgumentParser(description="Byter embeddings till halfvec(768) med HNSW-index")
    parser.add_argument("--torrkorning", action="store_true",
                        help="Visa uppskattad tid, diskbehov och slutstorlek; ändrar inget")
    parser.add_argument("--kolumn", choices=["fi", "sv", "bada"], default="bada",
                        help="Vilken embedding-kolumn (standard: båda i samma omskrivning)")
    parser.add_argument("--minne", default="8GB",
                        help="maintenance_work_mem för HNSW-bygget (standard 8GB)")
    parser.add_argument("--parallella", type=int, default=4,
                        help="Parallella arbetare för indexbygget (standard 4)")
    parser.add_argument("--bara-index", action="store_true",
                        help="Bygg bara om HNSW-indexen, ingen typkonvertering")
    args = parser.parse_args()

    if not db.ar_postgres():
        log.error("Skriptet kräver PostgreSQL med pgvector.")
        sys.exit(1)
    sprak_lista = ["fi", "sv"] if args.kolumn == "bada" else [args.kolumn]
    if args.torrkorning:
        torrkorning(sprak_lista)
        return
    konvertera(sprak_lista, args.minne, args.parallella, args.bara_index)
    log.info("Klar.")


if __name__ == "__main__":
    main()
