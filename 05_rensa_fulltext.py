#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Magnus Kolsjö

"""
05_rensa_fulltext.py — Tar bort redundant råtext ur en befintlig databas

Dokumentens råtext (finland.dokument.fulltext_fi/_sv) behövs inte när texten
finns som chunks med embeddings: sökningen går mot chunks, och texten hämtas
live från källan när den ska läsas. Skriptet tar bort råtexten för varje
dokument och språk där alla chunks med text också har embedding.

Ordning:
  1. Fulltextindexen på chunks skapas om de saknas, så att dokumenten
     fortsätter att hittas med FTS när råtexten är borta.
  2. Råtexten sätts till NULL i omgångar om --batch dokument.
  3. Med --vacuum-full skrivs tabellen om så att utrymmet lämnas tillbaka
     till filsystemet. Det låser finland.dokument under körningen.

Dokument utan chunks (t.ex. text kortare än minsta chunk) rörs inte.
Kräver PostgreSQL. Ändringen går inte att ångra lokalt, men texten kan
alltid hämtas på nytt från källan (03_chunka_och_embedda.py --tvinga).

Kör:
  python3 05_rensa_fulltext.py --torrkorning     # Visar vad som frigörs, ändrar inget
  python3 05_rensa_fulltext.py                   # Rensar
  python3 05_rensa_fulltext.py --vacuum-full     # Rensar och lämnar tillbaka utrymmet
"""

import argparse
import logging
import sys
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
log = logging.getLogger("rensa_fulltext")


def _villkor(sprak: str) -> str:
    """Dokument vars text på språket finns som chunks, alla med embedding."""
    return f"""
        d.fulltext_{sprak} IS NOT NULL
        AND EXISTS (
            SELECT 1 FROM finland.chunks c
            WHERE c.dokument_id = d.id AND c.text_{sprak} IS NOT NULL
        )
        AND NOT EXISTS (
            SELECT 1 FROM finland.chunks c
            WHERE c.dokument_id = d.id
              AND c.text_{sprak} IS NOT NULL
              AND c.embedding_{sprak} IS NULL
        )
    """


def _storlek(byte: int | None) -> str:
    byte = byte or 0
    for enhet in ("B", "kB", "MB", "GB"):
        if byte < 1024 or enhet == "GB":
            return f"{byte:.1f} {enhet}" if enhet != "B" else f"{byte} B"
        byte /= 1024
    return f"{byte} B"


def rapportera() -> dict:
    """Visar hur mycket råtext som kan tas bort, per språk. Ändrar inget."""
    resultat = {}
    with db._cursor() as cur:
        cur.execute("SELECT pg_total_relation_size('finland.dokument')")
        totalt = cur.fetchone()[0]
        log.info("finland.dokument inklusive TOAST och index: %s", _storlek(totalt))
        for sprak in ("fi", "sv"):
            cur.execute(f"""
                SELECT count(*), sum(pg_column_size(d.fulltext_{sprak}))
                FROM finland.dokument d WHERE {_villkor(sprak)}
            """)
            antal, byte = cur.fetchone()
            cur.execute(f"""
                SELECT count(*) FROM finland.dokument d
                WHERE d.fulltext_{sprak} IS NOT NULL AND NOT ({_villkor(sprak)})
            """)
            kvar = cur.fetchone()[0]
            resultat[sprak] = (antal or 0, byte or 0, kvar)
            log.info(
                "[%s] kan rensas: %d dokument, %s lagrad text (komprimerad). "
                "Behåller råtext för %d dokument utan fullständiga chunks.",
                sprak, antal or 0, _storlek(byte), kvar,
            )
    for sprak in ("fi", "sv"):
        if not db.har_chunk_fts(sprak):
            log.info("[%s] fulltextindexet på chunks saknas och skapas först "
                     "(ett GIN-index över alla chunks; tar tid och utrymme).", sprak)
    return resultat


def rensa(batch: int) -> int:
    """Sätter råtexten till NULL i omgångar. Returnerar antal ändrade fält."""
    db.skapa_chunk_fts_index()
    totalt = 0
    for sprak in ("fi", "sv"):
        while True:
            with db._cursor() as cur:
                cur.execute(f"""
                    UPDATE finland.dokument SET fulltext_{sprak} = NULL
                    WHERE id IN (
                        SELECT d.id FROM finland.dokument d
                        WHERE {_villkor(sprak)}
                        LIMIT %s
                    )
                """, (batch,))
                n = cur.rowcount
            totalt += n
            log.info("[%s] rensade %d (totalt %d)", sprak, n, totalt)
            if n < batch:
                break
    return totalt


def vacuum_full() -> None:
    """Skriver om finland.dokument så att frigjort utrymme lämnas tillbaka."""
    import psycopg2
    conn = psycopg2.connect(db.DATABASE_URL)
    try:
        conn.autocommit = True  # VACUUM kan inte köras i en transaktion
        with conn.cursor() as cur:
            log.info("VACUUM FULL finland.dokument (låser tabellen)...")
            cur.execute("VACUUM FULL finland.dokument")
    finally:
        conn.close()
    with db._cursor() as cur:
        cur.execute("SELECT pg_total_relation_size('finland.dokument')")
        log.info("finland.dokument efter VACUUM FULL: %s", _storlek(cur.fetchone()[0]))


def main() -> None:
    parser = argparse.ArgumentParser(description="Tar bort redundant råtext ur finland.dokument")
    parser.add_argument("--torrkorning", action="store_true",
                        help="Visa vad som frigörs utan att ändra något")
    parser.add_argument("--batch", type=int, default=2000,
                        help="Antal dokument per omgång (standard 2000)")
    parser.add_argument("--vacuum-full", action="store_true",
                        help="Kör VACUUM FULL efteråt så att utrymmet lämnas tillbaka")
    args = parser.parse_args()

    if not db.ar_postgres():
        log.error("Skriptet kräver PostgreSQL. I SQLite-läget används råtexten för sökning.")
        sys.exit(1)

    rapportera()
    if args.torrkorning:
        log.info("Torrkörning: inget ändrat.")
        return
    rensa(args.batch)
    if args.vacuum_full:
        vacuum_full()
    log.info("Klar.")


if __name__ == "__main__":
    main()
