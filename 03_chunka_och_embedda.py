#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Magnus Kolsjö

"""
03_chunka_och_embedda.py — Chunkning och embedding för arbetsström 15 (Finland)

Läser fulltext_fi och fulltext_sv från finland.dokument, delar upp i stycken
och genererar vektorer med två språkspecifika modeller:

  Finska:  TurkuNLP/sbert-cased-finnish-paraphrase (768 dim) → embedding_fi
  Svenska: KBLab/sentence-bert-swedish-cased       (768 dim) → embedding_sv

Vektorerna lagras i finland.chunks med kolumnerna text_fi/embedding_fi och
text_sv/embedding_sv. Om ett dokument har båda språkversionerna (t.ex. Finlex-
lagar) sparas de i samma chunk-rad med alignerat chunk_index.

Kräver PostgreSQL + pgvector — SQLite-alternativet saknar vektorsökning.

Användning:
  python3 03_chunka_och_embedda.py                    # Alla dokument utan chunks
  python3 03_chunka_och_embedda.py --kalla finlex     # Bara Finlex
  python3 03_chunka_och_embedda.py --kalla eduskunta  # Bara Eduskunta
  python3 03_chunka_och_embedda.py --sprak fi         # Bara finska embeddings
  python3 03_chunka_och_embedda.py --sprak sv         # Bara svenska embeddings
  python3 03_chunka_och_embedda.py --tvinga           # Återskapa befintliga chunks
  python3 03_chunka_och_embedda.py --bygg-index       # Bygg IVFFlat-index efteråt
"""

import argparse
import logging
import os
import re
import sys
from pathlib import Path

from dotenv import load_dotenv

_SCRIPT_DIR = Path(__file__).parent.resolve()
load_dotenv(_SCRIPT_DIR / ".env")

sys.path.insert(0, str(_SCRIPT_DIR))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# ── Konfiguration ──────────────────────────────────────────────────────────────

EMBEDDING_MODEL_FI = os.getenv("EMBEDDING_MODEL_FI", "TurkuNLP/sbert-cased-finnish-paraphrase")
EMBEDDING_MODEL_SV = os.getenv("EMBEDDING_MODEL_SV", "KBLab/sentence-bert-swedish-cased")
CHUNK_MAX_TECKEN     = int(os.getenv("CHUNK_MAX_TECKEN",           "800"))
CHUNK_MIN_TECKEN     = int(os.getenv("CHUNK_MIN_TECKEN",           "100"))
CHUNK_OVERLAP_TECKEN = int(os.getenv("CHUNK_OVERLAP_TECKEN",       "200"))
EMBEDDING_BATCH    = int(os.getenv("EMBEDDING_BATCH_STORLEK",  "32"))

_modell_fi = None
_modell_sv = None


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

def chunka_text(text: str) -> list[dict]:
    """
    Delar upp text i stycken på ~CHUNK_MAX_TECKEN tecken med CHUNK_OVERLAP_TECKEN
    tecken bakåtöverlapp mellan intilliggande chunks.

    Splittningsstrategi (i fallande prioritet):
      1. Paragrafrubriker (###) — naturliga gränser i AKN-extraherad text
      2. Dubbla radbrytningar (styckebrytning)
      3. Meningsgränser (. ? !) om stycket fortfarande är för långt
      4. Hårt snitt på CHUNK_MAX_TECKEN om inget bättre alternativ finns

    Överlapp: varje chunk inleds med de sista CHUNK_OVERLAP_TECKEN tecknen från
    föregående stycke så att kontexten bevaras vid chunkgränser.

    Returnerar lista med dicts:
      {chunk_index, text, tecken_start, tecken_slut}
    tecken_start/slut pekar på det primära styckets position i originaltexten
    (överlappstexten är inte medräknad i positionerna).
    """
    if not text:
        return []

    # Steg 1: dela på §-/kapitelrubriker
    delar = re.split(r"(?=\n###\s)", text)

    stycken: list[str] = []
    for del_ in delar:
        del_ = del_.strip()
        if not del_:
            continue
        if len(del_) <= CHUNK_MAX_TECKEN:
            stycken.append(del_)
        else:
            # Dela ytterligare på dubbla radbrytningar
            understycken = re.split(r"\n{2,}", del_)
            nuvarande = ""
            for us in understycken:
                us = us.strip()
                if not us:
                    continue
                if len(nuvarande) + len(us) + 2 <= CHUNK_MAX_TECKEN:
                    nuvarande = (nuvarande + "\n\n" + us).strip() if nuvarande else us
                else:
                    if nuvarande:
                        stycken.append(nuvarande)
                    if len(us) > CHUNK_MAX_TECKEN:
                        # Dela på meningsgränser
                        meningar = re.split(r"(?<=[.?!])\s+", us)
                        nuvarande = ""
                        for m in meningar:
                            if len(nuvarande) + len(m) + 1 <= CHUNK_MAX_TECKEN:
                                nuvarande = (nuvarande + " " + m).strip() if nuvarande else m
                            else:
                                if nuvarande:
                                    stycken.append(nuvarande)
                                # Hårt snitt om meningen är för lång
                                while len(m) > CHUNK_MAX_TECKEN:
                                    stycken.append(m[:CHUNK_MAX_TECKEN])
                                    m = m[CHUNK_MAX_TECKEN:]
                                nuvarande = m
                        if nuvarande:
                            stycken.append(nuvarande)
                            nuvarande = ""
                    else:
                        nuvarande = us
            if nuvarande:
                stycken.append(nuvarande)

    # Bygg chunks med positionsinfo och bakåtöverlapp
    chunks          = []
    pos             = 0
    index           = 0
    foregaende_text = ""
    for s in stycken:
        s = s.strip()
        if len(s) < CHUNK_MIN_TECKEN:
            continue

        # Bakåtöverlapp: inled med slutet av föregående stycke för bättre
        # kontexttäckning vid chunkgränser (t.ex. lagparagrafer som hänvisar bakåt)
        if foregaende_text and CHUNK_OVERLAP_TECKEN > 0:
            overlapp   = foregaende_text[-CHUNK_OVERLAP_TECKEN:]
            chunk_text = overlapp + "\n\n" + s
        else:
            chunk_text = s

        idx   = text.find(s[:40], pos)
        start = idx if idx >= 0 else pos
        slut  = start + len(s)
        chunks.append({
            "chunk_index":  index,
            "text":         chunk_text,
            "tecken_start": start,
            "tecken_slut":  slut,
        })
        pos             = max(pos, slut)
        index          += 1
        foregaende_text = s

    return chunks


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

    villkor_delar = [
        f"d.{text_kol} IS NOT NULL",
        f"d.{text_kol} != ''",
    ]

    if not tvinga:
        villkor_delar.append(f"""
            NOT EXISTS (
                SELECT 1 FROM finland.chunks c
                WHERE c.dokument_id = d.id
                  AND c.{emb_kol} IS NOT NULL
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
    """Hämtar fulltext_fi eller fulltext_sv för ett dokument."""
    from db import pg_anslutning, pg_returnera

    text_kol = "fulltext_fi" if sprak == "fi" else "fulltext_sv"
    conn = pg_anslutning()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT {text_kol} FROM finland.dokument WHERE id = %s",
                (dok_id,),
            )
            rad = cur.fetchone()
    finally:
        pg_returnera(conn)
    return rad[0] if rad else None


# ---------------------------------------------------------------------------
# Databas — sparning
# ---------------------------------------------------------------------------

def _spara_chunks(dok_id: int, chunks: list[dict], embeddings, sprak: str):
    """
    Sparar chunks med embeddings för ett språk.

    Om en chunk-rad redan finns (samma dokument_id + chunk_index) uppdateras
    den med det nya språkets text och embedding — de befintliga kolumnerna
    för det andra språket berörs inte.
    """
    from db import pg_anslutning, pg_returnera

    text_kol = "text_fi" if sprak == "fi" else "text_sv"
    emb_kol  = "embedding_fi" if sprak == "fi" else "embedding_sv"

    conn = pg_anslutning()
    try:
        with conn.cursor() as cur:
            # Ta bort gamla embeddings för det här språket (men behåll det andra språkets data)
            cur.execute(
                f"UPDATE finland.chunks SET {emb_kol} = NULL WHERE dokument_id = %s",
                (dok_id,)
            )

            for ch, emb in zip(chunks, embeddings):
                vec_str = "[" + ",".join(str(float(x)) for x in emb) + "]"
                cur.execute(
                    f"""
                    INSERT INTO finland.chunks
                        (dokument_id, chunk_index, {text_kol}, {emb_kol})
                    VALUES (%s, %s, %s, %s::vector)
                    ON CONFLICT (dokument_id, chunk_index) DO UPDATE SET
                        {text_kol} = EXCLUDED.{text_kol},
                        {emb_kol}  = EXCLUDED.{emb_kol}
                    """,
                    (dok_id, ch["chunk_index"], ch["text"], vec_str),
                )
        conn.commit()
    finally:
        pg_returnera(conn)


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

    _spara_chunks(dok_id, chunks, embeddings, sprak)
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
# IVFFlat-index
# ---------------------------------------------------------------------------

def bygg_ivfflat_index(lists: int = 100):
    """
    Bygger IVFFlat-index för ANN-sökning i finland.chunks.

    Bygger separata index för embedding_fi och embedding_sv.
    Ska köras EFTER att data laddats in. Bygg om när >20 % ny data tillkommer.

    Rekommenderat lists-värde: sqrt(antal_rader).
      <10k chunks  → 100
      ~100k chunks → 316
      ~500k chunks → 707
    """
    from db import pg_anslutning, pg_returnera

    log.info("Bygger IVFFlat-index för finland.chunks (lists=%d)...", lists)
    conn = pg_anslutning()
    try:
        for emb_kol, index_namn in [
            ("embedding_fi", "idx_finland_chunks_emb_fi"),
            ("embedding_sv", "idx_finland_chunks_emb_sv"),
        ]:
            # Kontrollera att det finns data att indexera
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT COUNT(*) FROM finland.chunks WHERE {emb_kol} IS NOT NULL"
                )
                antal = cur.fetchone()[0]

            if antal < 10:
                log.info("Hoppar %s — för få rader (%d) för IVFFlat.", index_namn, antal)
                continue

            log.info("Bygger %s (%d rader, lists=%d)...", index_namn, antal, lists)
            with conn.cursor() as cur:
                cur.execute(f"DROP INDEX IF EXISTS {index_namn}")
                cur.execute(
                    f"""
                    CREATE INDEX {index_namn} ON finland.chunks
                        USING ivfflat ({emb_kol} vector_cosine_ops)
                        WITH (lists = {lists})
                    """
                )
            conn.commit()
            log.info("%s byggt.", index_namn)
    finally:
        pg_returnera(conn)

    log.info("IVFFlat-index klara.")


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
        help="Bygg IVFFlat-index för embedding_fi och embedding_sv efter embedding",
    )
    parser.add_argument(
        "--lists",
        type=int,
        default=100,
        help="IVFFlat lists-parameter (standard 100, rekommenderat: sqrt(antal_chunks))",
    )
    args = parser.parse_args()

    import db
    try:
        db.init_db()
    except Exception as exc:
        log.error("Databasinitiering misslyckades: %s", exc)
        sys.exit(1)

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
        bygg_ivfflat_index(args.lists)
