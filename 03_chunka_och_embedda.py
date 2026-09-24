#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Magnus Kolsjö

"""
03_chunka_och_embedda.py — Chunkning och embedding för finsk riksdags- och rättsdata

Läser dokumentens text, delar upp i stycken och genererar vektorer med två
språkspecifika modeller. Texten tas ur fulltext_fi/fulltext_sv när synken
lagrat den, annars hämtas den live från Finlex eller Eduskunta:

  Finska:  TurkuNLP/sbert-cased-finnish-paraphrase (768 dim) → embedding_fi
  Svenska: KBLab/sentence-bert-swedish-cased       (768 dim) → embedding_sv

Vektorerna lagras i finland.chunks med kolumnerna text_fi/embedding_fi och
text_sv/embedding_sv. Om ett dokument har båda språkversionerna (t.ex. Finlex-
lagar) sparas de i samma chunk-rad med alignerat chunk_index. Varje chunk får
också sin position i texten (tecken_start/tecken_slut per språk).

Smal cache: när fulltextindexen på chunks finns (--bygg-index) tas råtexten
bort ur finland.dokument så snart ett språk är chunkat och embeddat. Texten
finns då kvar som chunks för sökning och hämtas live när den ska läsas.
--behall-fulltext stänger av det.

Kräver PostgreSQL + pgvector — SQLite-alternativet saknar vektorsökning.

Användning:
  python3 03_chunka_och_embedda.py                    # Alla dokument utan chunks
  python3 03_chunka_och_embedda.py --kalla finlex     # Bara Finlex
  python3 03_chunka_och_embedda.py --kalla eduskunta  # Bara Eduskunta
  python3 03_chunka_och_embedda.py --sprak fi         # Bara finska embeddings
  python3 03_chunka_och_embedda.py --sprak sv         # Bara svenska embeddings
  python3 03_chunka_och_embedda.py --tvinga           # Återskapa befintliga chunks
  python3 03_chunka_och_embedda.py --bygg-index       # Bygg HNSW- och FTS-index efteråt
  python3 03_chunka_och_embedda.py --behall-fulltext  # Behåll råtexten i dokument
"""

import argparse
import logging
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

_SCRIPT_DIR = Path(__file__).parent.resolve()
load_dotenv(_SCRIPT_DIR / ".env")

sys.path.insert(0, str(_SCRIPT_DIR))

from chunkning import chunka_text  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# ── Konfiguration ──────────────────────────────────────────────────────────────

EMBEDDING_MODEL_FI = os.getenv("EMBEDDING_MODEL_FI", "TurkuNLP/sbert-cased-finnish-paraphrase")
EMBEDDING_MODEL_SV = os.getenv("EMBEDDING_MODEL_SV", "KBLab/sentence-bert-swedish-cased")
EMBEDDING_BATCH    = int(os.getenv("EMBEDDING_BATCH_STORLEK",  "32"))

_modell_fi = None
_modell_sv = None

# Sätts av --behall-fulltext. Råtexten tas bara bort när fulltextindexet på
# chunks finns; annars skulle dokumentet försvinna ur FTS-sökningen.
_RENSA_FULLTEXT = True


# ---------------------------------------------------------------------------
# Modell-laddning (FD1-skyddad)
# ---------------------------------------------------------------------------

def _hamta_modell(sprak: str):
    """
    Laddar embeddingmodell lazily. Skyddar FD 1 mot tqdm/transformers-utskrifter
    som annars kraschar MCP stdio-protokollet om skriptet körs via MCP.
    """
    global _modell_fi, _modell_sv

    if sprak == "fi" and _modell_fi is not None:
        return _modell_fi
    if sprak == "sv" and _modell_sv is not None:
        return _modell_sv

    modellnamn = EMBEDDING_MODEL_FI if sprak == "fi" else EMBEDDING_MODEL_SV
    log.info("Laddar embeddingmodell (%s): %s", sprak, modellnamn)

    log_sokvag = _SCRIPT_DIR / "logs" / "embedding.log"
    log_sokvag.parent.mkdir(parents=True, exist_ok=True)
    log_fd   = os.open(str(log_sokvag), os.O_WRONLY | os.O_APPEND | os.O_CREAT)
    save_fd1 = os.dup(1)
    try:
        os.dup2(log_fd, 1)
        from sentence_transformers import SentenceTransformer
        modell = SentenceTransformer(modellnamn)
    finally:
        os.dup2(save_fd1, 1)
        os.close(save_fd1)
        os.close(log_fd)

    if sprak == "fi":
        _modell_fi = modell
    else:
        _modell_sv = modell

    log.info("Embeddingmodell (%s) laddad", sprak)
    return modell


# ---------------------------------------------------------------------------
# Chunkning
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Databas — hämtning
# ---------------------------------------------------------------------------

def _hamta_dokument_att_embeda(
    kalla: str | None,
    sprak: str,
    tvinga: bool,
) -> list[dict]:
    """
    Returnerar dokument som saknar embedding för det angivna språket.
    Hoppas över dokument som saknar fulltext för det språket.
    """
    from db import pg_anslutning, pg_returnera, ar_postgres

    if not ar_postgres():
        log.error("Embedding kräver PostgreSQL — SQLite stöder inte pgvector.")
        return []

    text_kol  = "fulltext_fi" if sprak == "fi" else "fulltext_sv"
    emb_kol   = "embedding_fi" if sprak == "fi" else "embedding_sv"

    if tvinga:
        # Omchunkning går även för dokument vars råtext rensats; texten
        # hämtas då live via akn-URI eller edk_id.
        villkor_delar = [
            f"(coalesce(d.{text_kol}, '') <> '' OR d.akn_uri_fi IS NOT NULL OR d.edk_id IS NOT NULL)"
        ]
    else:
        villkor_delar = [
            f"d.{text_kol} IS NOT NULL",
            f"d.{text_kol} != ''",
        ]

    if not tvinga:
        # Två fall ska med: dokument som aldrig chunkats för språket (inga
        # rader med språkets text) och dokument där en körning avbröts mitt i
        # (rader med text men utan embedding). Rader som bara bär det andra
        # språkets text räknas inte; de saknar alltid det här språkets vektor.
        text_c = "text_fi" if sprak == "fi" else "text_sv"
        villkor_delar.append(f"""
            (
                NOT EXISTS (
                    SELECT 1 FROM finland.chunks c
                    WHERE c.dokument_id = d.id AND c.{text_c} IS NOT NULL
                )
                OR EXISTS (
                    SELECT 1 FROM finland.chunks c
                    WHERE c.dokument_id = d.id
                      AND c.{text_c} IS NOT NULL
                      AND c.{emb_kol} IS NULL
                )
            )
        """)

    if kalla and kalla != "alla":
        villkor_delar.append("d.kalla = %s")

    where = "WHERE " + " AND ".join(villkor_delar)
    params = (kalla,) if (kalla and kalla != "alla") else ()

    conn = pg_anslutning()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT d.id, d.kalla, d.typ, d.edk_id,
                       d.titel_fi, d.titel_sv,
                       length(d.{text_kol}) AS teckenlangd
                FROM   finland.dokument d
                {where}
                ORDER  BY d.id
                """,
                params,
            )
            rader = cur.fetchall()
    finally:
        pg_returnera(conn)

    return [
        {
            "id":          r[0],
            "kalla":       r[1],
            "typ":         r[2],
            "edk_id":      r[3],
            "titel_fi":    r[4],
            "titel_sv":    r[5],
            "teckenlangd": r[6],
        }
        for r in rader
    ]


def _hamta_fulltext(dok_id: int, sprak: str) -> str | None:
    """
    Dokumentets text på språket: lagrad råtext om den finns, annars live.

    Live-hämtningen gör att dokument vars råtext rensats kan chunkas om.
    """
    import psycopg2.extras
    from db import pg_anslutning, pg_returnera
    import texthamtning

    text_kol = "fulltext_fi" if sprak == "fi" else "fulltext_sv"
    conn = pg_anslutning()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                f"""SELECT {text_kol} AS text, kalla, edk_id, eduskuntatunnus_fi,
                           eduskuntatunnus_sv, akn_uri_fi, akn_uri_sv
                    FROM finland.dokument WHERE id = %s""",
                (dok_id,),
            )
            rad = cur.fetchone()
    finally:
        pg_returnera(conn)
    if not rad:
        return None
    if rad["text"]:
        return rad["text"]
    return texthamtning.dokumenttext(dict(rad), sprak)


# ---------------------------------------------------------------------------
# Databas — sparning
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Embedding av ett dokument
# ---------------------------------------------------------------------------

def embeda_dokument(dok_id: int, titel_fi: str | None, titel_sv: str | None, sprak: str) -> int:
    """
    Chunkar och embeddar ett enstaka dokument för ett språk.
    Returnerar antal genererade chunks (0 vid fel eller tom text).
    """
    text = _hamta_fulltext(dok_id, sprak)
    if not text:
        return 0

    chunks = chunka_text(text)
    if not chunks:
        return 0

    modell = _hamta_modell(sprak)
    titel  = titel_fi if sprak == "fi" else titel_sv

    # Titeln preprenderas för bättre semantisk precision
    texter = [
        f"{titel}\n\n{ch['text']}" if titel else ch["text"]
        for ch in chunks
    ]

    log_sokvag = _SCRIPT_DIR / "logs" / "embedding.log"
    log_fd   = os.open(str(log_sokvag), os.O_WRONLY | os.O_APPEND | os.O_CREAT)
    save_fd1 = os.dup(1)
    try:
        os.dup2(log_fd, 1)
        embeddings = modell.encode(
            texter,
            batch_size=EMBEDDING_BATCH,
            show_progress_bar=False,
            normalize_embeddings=True,
        )
    finally:
        os.dup2(save_fd1, 1)
        os.close(save_fd1)
        os.close(log_fd)

    import db
    db.spara_chunks(dok_id, chunks, embeddings, sprak)
    if _RENSA_FULLTEXT and db.har_chunk_fts(sprak):
        db.rensa_fulltext(dok_id, sprak)
    return len(chunks)


# ---------------------------------------------------------------------------
# Huvudkörning
# ---------------------------------------------------------------------------

def kor_embedding(
    kalla: str | None = None,
    sprak: str = "bada",
    tvinga: bool = False,
) -> dict:
    """
    Embeddar alla dokument som saknar chunks för det angivna språket.

    sprak: "fi" | "sv" | "bada" (standard)

    Returnerar statistik:
      {fi: {total, lyckade, hoppade, fel, chunks}, sv: {...}}
    """
    sprak_lista = []
    if sprak in ("fi", "bada"):
        sprak_lista.append("fi")
    if sprak in ("sv", "bada"):
        sprak_lista.append("sv")

    stat: dict = {}

    for s in sprak_lista:
        log.info("=== Embeddar %s-text ===", s.upper())
        dokument = _hamta_dokument_att_embeda(kalla, s, tvinga)

        if not dokument:
            log.info("Inga dokument att embeda (%s).", s)
            stat[s] = {"total": 0, "lyckade": 0, "hoppade": 0, "fel": 0, "chunks": 0}
            continue

        log.info(
            "Embeddar %d dokument (%s-text, kalla=%s, tvinga=%s)",
            len(dokument), s, kalla or "alla", tvinga,
        )

        s_stat = {"total": len(dokument), "lyckade": 0, "hoppade": 0, "fel": 0, "chunks": 0}

        for dok in dokument:
            try:
                antal = embeda_dokument(
                    dok["id"],
                    dok.get("titel_fi"),
                    dok.get("titel_sv"),
                    s,
                )
                if antal == 0:
                    s_stat["hoppade"] += 1
                    log.debug("Hoppad (tom text): id=%s", dok["id"])
                else:
                    s_stat["lyckade"] += 1
                    s_stat["chunks"]  += antal
                    log.debug(
                        "OK  id=%-6s  %3d chunks  [%s]  %s",
                        dok["id"], antal, s,
                        (dok.get("titel_fi") or dok.get("titel_sv") or "")[:55],
                    )
            except Exception as exc:
                s_stat["fel"] += 1
                log.warning("FEL  id=%s [%s]: %s", dok["id"], s, exc)

        log.info(
            "[%s] Klar — lyckade: %d, hoppade: %d, fel: %d, chunks: %d",
            s.upper(), s_stat["lyckade"], s_stat["hoppade"], s_stat["fel"], s_stat["chunks"],
        )
        stat[s] = s_stat

    return stat


# ---------------------------------------------------------------------------
# Index
# ---------------------------------------------------------------------------

def bygg_index(minne: str | None = None):
    """
    Bygger om vektorindexen (HNSW) och skapar fulltextindexen på chunks.

    Vektorindexen byggs om från grunden; kör efter större inläsningar. HNSW tål
    senare inskrivningar, så det behövs inte efter varje daglig synk.
    """
    import db

    for sprak in ("fi", "sv"):
        kol = "embedding_fi" if sprak == "fi" else "embedding_sv"
        with db._cursor() as cur:
            cur.execute(f"SELECT count(*) FROM finland.chunks WHERE {kol} IS NOT NULL")
            antal = cur.fetchone()[0]
        if antal == 0:
            log.info("Hoppar vektorindex för %s — inga embeddings.", sprak)
            continue
        log.info("Bygger HNSW-index för %s (%d rader)...", kol, antal)
        db.bygg_vektorindex(sprak, minne=minne)
    log.info("Vektorindex klara.")
    db.skapa_chunk_fts_index()
    log.info("Fulltextindex på chunks klara.")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description=(
            "Chunkar och embeddar finska riksdags- och rättsdokument.\n"
            "Finska:  TurkuNLP/sbert-cased-finnish-paraphrase → embedding_fi\n"
            "Svenska: KBLab/sentence-bert-swedish-cased       → embedding_sv"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--kalla",
        default="alla",
        choices=["alla", "finlex", "eduskunta"],
        help="Källfilter (standard: alla)",
    )
    parser.add_argument(
        "--sprak",
        default="bada",
        choices=["fi", "sv", "bada"],
        help="Språk att embeda (standard: bada)",
    )
    parser.add_argument(
        "--tvinga",
        action="store_true",
        help="Återskapa chunks även för dokument som redan är embeddade",
    )
    parser.add_argument(
        "--bygg-index",
        action="store_true",
        help="Bygg HNSW-vektorindex och fulltextindex på chunks efter embedding",
    )
    parser.add_argument(
        "--minne",
        default=None,
        help="maintenance_work_mem för indexbygget, t.ex. 6GB (snabbare HNSW-bygge)",
    )
    parser.add_argument(
        "--behall-fulltext",
        action="store_true",
        help="Behåll dokumentens råtext i finland.dokument efter chunkning",
    )
    args = parser.parse_args()

    import db
    try:
        db.init_db()
    except Exception as exc:
        log.error("Databasinitiering misslyckades: %s", exc)
        sys.exit(1)

    _RENSA_FULLTEXT = not args.behall_fulltext
    kalla_arg = None if args.kalla == "alla" else args.kalla
    stat = kor_embedding(kalla=kalla_arg, sprak=args.sprak, tvinga=args.tvinga)

    print("\n── Resultat ──────────────────────────────────────────")
    for s, s_stat in stat.items():
        print(
            f"  [{s.upper()}]  lyckade: {s_stat['lyckade']}, "
            f"hoppade: {s_stat['hoppade']}, fel: {s_stat['fel']}, "
            f"chunks: {s_stat['chunks']}"
        )
    print()

    if args.bygg_index:
        bygg_index(args.minne)
