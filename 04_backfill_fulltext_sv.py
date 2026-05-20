#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Magnus Kolsjö

"""
04_backfill_fulltext_sv.py — Backfill av fulltext_fi/fulltext_sv för Finlex-dokument

Hämtar saknad fulltext för Finlex-dokument som synkades innan tvåspråkighetsstödet
implementerades. Hanterar både fulltext_fi och fulltext_sv i ett pass.

Kör:
  python3 04_backfill_fulltext_sv.py            (backfill alla med saknad fulltext)
  python3 04_backfill_fulltext_sv.py --torr     (visa antal utan att ladda ned)
  python3 04_backfill_fulltext_sv.py --limit 20 (testa med de första 20 dokumenten)
"""

import argparse
import logging
import sys
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
log = logging.getLogger("backfill_fulltext_sv")


# ---------------------------------------------------------------------------
# Databashjälpare
# ---------------------------------------------------------------------------

def hamta_saknade(conn, limit: int | None = None) -> list[dict]:
    """Hämtar Finlex-dokument där fulltext_fi eller fulltext_sv saknas, sorterade per år."""
    begransning = f"LIMIT {limit}" if limit else ""
    cur = conn.cursor()
    cur.execute(f"""
        SELECT id, akn_uri_fi, ar,
               (fulltext_fi IS NULL) AS saknar_fi,
               (fulltext_sv IS NULL) AS saknar_sv
        FROM finland.dokument
        WHERE kalla = 'finlex'
          AND akn_uri_fi IS NOT NULL
          AND (fulltext_fi IS NULL OR fulltext_sv IS NULL)
        ORDER BY ar, id
        {begransning}
    """)
    return [
        {"id": r[0], "akn_uri_fi": r[1], "ar": r[2], "saknar_fi": r[3], "saknar_sv": r[4]}
        for r in cur.fetchall()
    ]


def uppdatera_fulltext(conn, dok_id: int, fulltext_fi: str | None, fulltext_sv: str | None) -> None:
    """Uppdaterar bara de fulltext-fält som skickats in — rör inga andra fält."""
    delar = []
    varden = []
    if fulltext_fi is not None:
        delar.append("fulltext_fi = %s")
        varden.append(fulltext_fi)
    if fulltext_sv is not None:
        delar.append("fulltext_sv = %s")
        varden.append(fulltext_sv)
    if not delar:
        return
    delar.append("senast_hamtad = NOW()")
    varden.append(dok_id)
    cur = conn.cursor()
    cur.execute(
        f"UPDATE finland.dokument SET {', '.join(delar)} WHERE id = %s",
        varden,
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Backfill
# ---------------------------------------------------------------------------

def _hamta_fulltext(uri: str) -> str | None:
    """Hämtar och extraherar fulltext från en AKN-URI. Returnerar None vid 404 eller tom text."""
    rot = fx.hamta_akn_dokument(uri)
    if rot is None:
        return None
    text = fx.extrahera_fulltext(rot)
    return text if text and text.strip() else None


def backfill(torrkoring: bool = False, limit: int | None = None) -> None:
    conn = db.pg_anslutning()
    try:
        saknade = hamta_saknade(conn, limit=limit)
        totalt = len(saknade)

        log.info(
            "Hittade %d dokument med saknad fulltext%s",
            totalt, f" (begränsat till {limit})" if limit else "",
        )

        if torrkoring or totalt == 0:
            if torrkoring:
                log.info("Torrkörning — inga hämtningar görs")
            return

        uppdaterade  = 0
        hoppade_over = 0

        for i, dok in enumerate(saknade, 1):
            ny_fi = None
            ny_sv = None

            try:
                if dok["saknar_fi"]:
                    ny_fi = _hamta_fulltext(dok["akn_uri_fi"])
                    if ny_fi is None:
                        log.debug("[%d/%d] Ingen finsk fulltext: %s", i, totalt, dok["akn_uri_fi"])

                if dok["saknar_sv"]:
                    uri_sv = fx.byt_sprak_i_uri(dok["akn_uri_fi"], fx.SPRAK_SV)
                    ny_sv = _hamta_fulltext(uri_sv)
                    if ny_sv is None:
                        log.debug("[%d/%d] Ingen svensk version: %s", i, totalt, uri_sv)

                if ny_fi is not None or ny_sv is not None:
                    uppdatera_fulltext(conn, dok["id"], ny_fi, ny_sv)
                    uppdaterade += 1
                else:
                    hoppade_over += 1

                if uppdaterade % 100 == 0 and uppdaterade > 0:
                    log.info(
                        "[%d/%d] %d uppdaterade, %d hoppade över",
                        i, totalt, uppdaterade, hoppade_over,
                    )

            except Exception as exc:
                log.error("[%d/%d] Fel för dokument id=%s: %s", i, totalt, dok["id"], exc)

    finally:
        db.pg_returnera(conn)

    log.info(
        "Klar — %d uppdaterade, %d hoppade över (ingen version hittad eller tom text)",
        uppdaterade, hoppade_over,
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Backfill fulltext_sv för Finlex-dokument som saknar svensk fulltext"
    )
    parser.add_argument(
        "--torr", action="store_true",
        help="Torrkörning: visa antal utan att ladda ned",
    )
    parser.add_argument(
        "--limit", type=int, default=None, metavar="N",
        help="Begränsa till de första N dokumenten (för test)",
    )
    args = parser.parse_args()
    backfill(torrkoring=args.torr, limit=args.limit)


if __name__ == "__main__":
    main()
