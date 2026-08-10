# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Magnus Kolsjö

"""
db.py — Databaslager för finsk riksdags- och rättsdata

Hanterar initiering och anslutning till:
  - PostgreSQL + pgvector (schema: finland)
  - SQLite                (fil: finland_cache.db)

Databasens typ styrs av DATABASE_URL i .env:
  postgresql://user:pass@localhost:5432/riksdagstryck   → PostgreSQL
  sqlite:///finland_cache.db                            → SQLite

Interna konventioner:
  _ar_postgres()  — detekterar backend
  _hamta_db()     — kontexthanterare för rätt anslutning
  _ph()           — platshållar-tecken (%s / ?)
  _prefix()       — schemaprefix (finland. / '')
  PostgreSQL-anslutningar hanteras via ThreadedConnectionPool —
  trådsäkert för http-transport med flera klienter.
"""

import json
import logging
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

log = logging.getLogger(__name__)

_SCRIPT_DIR  = Path(__file__).parent.resolve()
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///finland_cache.db")

_pg_pool = None
_sq_conn = None


# ---------------------------------------------------------------------------
# Detektera databastyp
# ---------------------------------------------------------------------------

def _ar_postgres() -> bool:
    """Returnerar True om DATABASE_URL pekar på PostgreSQL."""
    return DATABASE_URL.startswith("postgresql://") or DATABASE_URL.startswith("postgres://")


# Publik alias — bibehålls för externa anropare (mcp_server.py, synkskript)
ar_postgres = _ar_postgres


def db_typ() -> str:
    """Returnerar 'postgres' eller 'sqlite' baserat på DATABASE_URL."""
    return "postgres" if _ar_postgres() else "sqlite"


# ---------------------------------------------------------------------------
# PostgreSQL — connection pool
# ---------------------------------------------------------------------------

def _pg_hamta_pool():
    """Skapar och returnerar ThreadedConnectionPool för PostgreSQL (lazy init)."""
    global _pg_pool
    if _pg_pool is None:
        import psycopg2.pool
        _pg_pool = psycopg2.pool.ThreadedConnectionPool(1, 5, DATABASE_URL)
    return _pg_pool


@contextmanager
def _pg_anslutning():
    """Kontexthanterare som hämtar en PostgreSQL-anslutning ur poolen och återlämnar den."""
    pool = _pg_hamta_pool()
    conn = pool.getconn()
    try:
        yield conn
    finally:
        pool.putconn(conn)


def pg_anslutning():
    """
    Publik funktion för externa skript — returnerar en anslutning ur poolen.
    Anroparen ansvarar för att anropa pg_returnera(conn) när anslutningen
    inte längre behövs.
    """
    return _pg_hamta_pool().getconn()


def pg_returnera(conn):
    """Återlämnar en PostgreSQL-anslutning till poolen (komplement till pg_anslutning)."""
    if _pg_pool:
        _pg_pool.putconn(conn)


# ---------------------------------------------------------------------------
# SQLite
# ---------------------------------------------------------------------------

def _sq_anslutning():
    """Returnerar en aktiv SQLite-anslutning (singleton per process)."""
    global _sq_conn
    if _sq_conn is None:
        db_sokvag = DATABASE_URL.replace("sqlite:///", "")
        if not Path(db_sokvag).is_absolute():
            db_sokvag = str(_SCRIPT_DIR / db_sokvag)
        _sq_conn = sqlite3.connect(db_sokvag, check_same_thread=False)
        _sq_conn.row_factory = sqlite3.Row
        _sq_conn.execute("PRAGMA journal_mode=WAL")
    return _sq_conn


# Bakåtkompatibelt alias
sq_anslutning = _sq_anslutning


# ---------------------------------------------------------------------------
# Initiering
# ---------------------------------------------------------------------------

def pg_init():
    """Initierar PostgreSQL-schema finland via schema_postgres.sql."""
    sql_fil = _SCRIPT_DIR / "db" / "schema_postgres.sql"
    if not sql_fil.exists():
        raise FileNotFoundError(f"Schemafil saknas: {sql_fil}")
    with _pg_anslutning() as conn:
        with conn.cursor() as cur:
            cur.execute(sql_fil.read_text(encoding="utf-8"))
        conn.commit()
    log.info("PostgreSQL-schema finland initierat")


def sq_init():
    """Initierar SQLite-tabeller via schema_sqlite.sql."""
    sql_fil = _SCRIPT_DIR / "db" / "schema_sqlite.sql"
    if not sql_fil.exists():
        raise FileNotFoundError(f"Schemafil saknas: {sql_fil}")
    conn = _sq_anslutning()
    conn.executescript(sql_fil.read_text(encoding="utf-8"))
    conn.commit()
    log.info("SQLite-schema initierat")


def init_db():
    """Initierar rätt databas baserat på DATABASE_URL."""
    if _ar_postgres():
        pg_init()
    else:
        sq_init()
    log.info("Databas redo (%s)", db_typ())


# ---------------------------------------------------------------------------
# Gemensamma hjälpfunktioner
# ---------------------------------------------------------------------------

@contextmanager
def _hamta_db():
    """
    Kontexthanterare som ger rätt databasanslutning per backend.
    PostgreSQL: hämtar från pool och återlämnar automatiskt.
    SQLite: returnerar singletonen.
    """
    if _ar_postgres():
        with _pg_anslutning() as conn:
            yield conn
    else:
        yield _sq_anslutning()


@contextmanager
def _cursor():
    """
    Kontexthanterare som ger en databasmarkör och committar/rollbackar.
    PostgreSQL: hämtar anslutning ur poolen, återlämnar i finally.
    SQLite: återanvänder singleton-anslutningen.
    """
    if _ar_postgres():
        with _pg_anslutning() as conn:
            cur = conn.cursor()
            try:
                yield cur
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                cur.close()
    else:
        conn = _sq_anslutning()
        cur  = conn.cursor()
        try:
            yield cur
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            cur.close()


def _ph() -> str:
    """Platshållar-tecken: '%s' för PostgreSQL, '?' för SQLite."""
    return "%s" if _ar_postgres() else "?"


def _prefix() -> str:
    """Schemaprefix: 'finland.' för PostgreSQL, '' för SQLite."""
    return "finland." if _ar_postgres() else ""


def _now() -> str:
    """NOW()-syntax per databas."""
    return "NOW()" if _ar_postgres() else "datetime('now')"


# ---------------------------------------------------------------------------
# Dokument
# ---------------------------------------------------------------------------

def upsert_dokument(
    kalla: str,
    edk_id: Optional[str] = None,
    eduskuntatunnus_fi: Optional[str] = None,
    eduskuntatunnus_sv: Optional[str] = None,
    akn_uri_fi: Optional[str] = None,
    akn_uri_sv: Optional[str] = None,
    eli: Optional[str] = None,
    typ: Optional[str] = None,
    finlex_hierarki: Optional[str] = None,
    finlex_typ: Optional[str] = None,
    titel_fi: Optional[str] = None,
    titel_sv: Optional[str] = None,
    ar: Optional[int] = None,
    nummer: Optional[str] = None,
    datum: Optional[str] = None,
    sprak: Optional[str] = None,
    html_saatavilla: bool = False,
    fulltext_fi: Optional[str] = None,
    fulltext_sv: Optional[str] = None,
) -> int:
    """
    Infogar eller uppdaterar ett dokument. Returnerar postens id.
    Konfliktnyckel: edk_id (Eduskunta) eller akn_uri_fi (Finlex).
    """
    p = _ph()
    t = _prefix()
    n = _now()

    # Välj konfliktnyckel
    if edk_id:
        konflikt = "edk_id"
        konflikt_val = edk_id
    elif akn_uri_fi:
        konflikt = "akn_uri_fi"
        konflikt_val = akn_uri_fi
    else:
        konflikt = None
        konflikt_val = None

    # PostgreSQL vill ha True/False, SQLite vill ha 1/0
    html_saat_val = bool(html_saatavilla) if _ar_postgres() else (1 if html_saatavilla else 0)

    varden = (
        kalla, edk_id, eduskuntatunnus_fi, eduskuntatunnus_sv,
        akn_uri_fi, akn_uri_sv, eli, typ, finlex_hierarki, finlex_typ,
        titel_fi, titel_sv, ar, nummer, datum, sprak,
        html_saat_val,
        fulltext_fi, fulltext_sv,
    )

    if _ar_postgres():
        konflikt_klausul = ""
        if konflikt:
            konflikt_klausul = f"""
            ON CONFLICT ({konflikt}) DO UPDATE SET
                titel_fi           = EXCLUDED.titel_fi,
                titel_sv           = EXCLUDED.titel_sv,
                fulltext_fi        = COALESCE(EXCLUDED.fulltext_fi, finland.dokument.fulltext_fi),
                fulltext_sv        = COALESCE(EXCLUDED.fulltext_sv, finland.dokument.fulltext_sv),
                html_saatavilla    = EXCLUDED.html_saatavilla,
                eduskuntatunnus_fi = COALESCE(EXCLUDED.eduskuntatunnus_fi, finland.dokument.eduskuntatunnus_fi),
                eduskuntatunnus_sv = COALESCE(EXCLUDED.eduskuntatunnus_sv, finland.dokument.eduskuntatunnus_sv),
                akn_uri_sv         = COALESCE(EXCLUDED.akn_uri_sv, finland.dokument.akn_uri_sv),
                senast_hamtad      = NOW()
            RETURNING id
            """
        else:
            konflikt_klausul = "RETURNING id"

        sql = f"""
            INSERT INTO {t}dokument
                (kalla, edk_id, eduskuntatunnus_fi, eduskuntatunnus_sv,
                 akn_uri_fi, akn_uri_sv, eli, typ, finlex_hierarki, finlex_typ,
                 titel_fi, titel_sv, ar, nummer, datum, sprak,
                 html_saatavilla, fulltext_fi, fulltext_sv, senast_hamtad)
            VALUES ({', '.join([p]*19)}, NOW())
            {konflikt_klausul}
        """
        with _cursor() as cur:
            cur.execute(sql, varden)
            rad = cur.fetchone()
        return rad[0] if rad else -1

    else:
        # SQLite
        konflikt_klausul = ""
        if konflikt:
            konflikt_klausul = f"""
            ON CONFLICT ({konflikt}) DO UPDATE SET
                titel_fi        = excluded.titel_fi,
                titel_sv        = excluded.titel_sv,
                fulltext_fi     = coalesce(excluded.fulltext_fi, fulltext_fi),
                fulltext_sv     = coalesce(excluded.fulltext_sv, fulltext_sv),
                html_saatavilla = excluded.html_saatavilla,
                senast_hamtad   = datetime('now')
            """
        sql = f"""
            INSERT INTO {t}dokument
                (kalla, edk_id, eduskuntatunnus_fi, eduskuntatunnus_sv,
                 akn_uri_fi, akn_uri_sv, eli, typ, finlex_hierarki, finlex_typ,
                 titel_fi, titel_sv, ar, nummer, datum, sprak,
                 html_saatavilla, fulltext_fi, fulltext_sv, senast_hamtad)
            VALUES ({', '.join(['?']*19)}, datetime('now'))
            {konflikt_klausul}
        """
        with _cursor() as cur:
            cur.execute(sql, varden)
            if cur.lastrowid:
                return cur.lastrowid
        # Hämta befintligt id
        conn = _sq_anslutning()
        if konflikt:
            rad = conn.execute(
                f"SELECT id FROM dokument WHERE {konflikt}=?", (konflikt_val,)
            ).fetchone()
            return rad["id"] if rad else -1
        return -1


def hamta_dokument_via_edk_id(edk_id: str) -> Optional[dict]:
    """Hämtar ett cachat dokument via edktunnus. Returnerar dict eller None."""
    if not _ar_postgres():
        conn = _sq_anslutning()
        rad = conn.execute(
            "SELECT * FROM dokument WHERE edk_id=?", (edk_id,)
        ).fetchone()
        return dict(rad) if rad else None

    import psycopg2.extras
    with _pg_anslutning() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT * FROM finland.dokument WHERE edk_id=%s", (edk_id,)
            )
            rad = cur.fetchone()
    return dict(rad) if rad else None


def hamta_dokument_via_akn_uri(akn_uri_fi: str) -> Optional[dict]:
    """Hämtar ett cachat Finlex-dokument via AKN URI. Returnerar dict eller None."""
    if not _ar_postgres():
        conn = _sq_anslutning()
        rad = conn.execute(
            "SELECT * FROM dokument WHERE akn_uri_fi=?", (akn_uri_fi,)
        ).fetchone()
        return dict(rad) if rad else None

    import psycopg2.extras
    with _pg_anslutning() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT * FROM finland.dokument WHERE akn_uri_fi=%s", (akn_uri_fi,)
            )
            rad = cur.fetchone()
    return dict(rad) if rad else None


def hamta_dokument_via_eduskuntatunnus(eduskuntatunnus: str) -> Optional[dict]:
    """
    Hämtar ett cachat Eduskunta-dokument via riksdagsbeteckning (fi eller sv).
    Söker i eduskuntatunnus_fi och eduskuntatunnus_sv. Returnerar dict eller None.
    """
    if not _ar_postgres():
        conn = _sq_anslutning()
        rad = conn.execute(
            "SELECT * FROM dokument WHERE eduskuntatunnus_fi=? OR eduskuntatunnus_sv=?",
            (eduskuntatunnus, eduskuntatunnus),
        ).fetchone()
        return dict(rad) if rad else None

    import psycopg2.extras
    with _pg_anslutning() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """SELECT * FROM finland.dokument
                   WHERE eduskuntatunnus_fi=%s OR eduskuntatunnus_sv=%s
                   LIMIT 1""",
                (eduskuntatunnus, eduskuntatunnus),
            )
            rad = cur.fetchone()
    return dict(rad) if rad else None


# ---------------------------------------------------------------------------
# FTS-sökning
# ---------------------------------------------------------------------------

def fts_sok(
    fraga: str,
    sprak: str = "fi",
    kalla_filter: Optional[str] = None,
    typ_filter: Optional[str] = None,
    ar_filter: Optional[int] = None,
    max_treff: int = 10,
) -> list[dict]:
    """
    Fulltextsökning i finland.dokument.

    sprak: "fi" (finska, standard) | "sv" (svenska)
    Kommaseparerade termer tolkas som OR-logik.
    """
    termer = [t.strip() for t in fraga.split(",") if t.strip()]
    if not termer:
        return []

    if _ar_postgres():
        return _pg_fts_sok(termer, sprak, kalla_filter, typ_filter, ar_filter, max_treff)
    else:
        return _sq_fts_sok(termer, kalla_filter, typ_filter, ar_filter, max_treff)


def _pg_fts_sok(termer, sprak, kalla_filter, typ_filter, ar_filter, max_treff) -> list[dict]:
    """FTS via to_tsquery med finsk/svensk konfiguration."""
    pg_sprak  = "finnish" if sprak == "fi" else "swedish"
    text_kol  = "fulltext_fi" if sprak == "fi" else "fulltext_sv"
    titel_kol = "titel_fi"   if sprak == "fi" else "titel_sv"

    tsquery_delar = " || ".join(
        [f"plainto_tsquery('{pg_sprak}', %s)"] * len(termer)
    )

    villkor  = []
    params   = list(termer)  # för tsquery-CTE

    if kalla_filter:
        villkor.append("kalla = %s")
        params.append(kalla_filter)
    if typ_filter:
        villkor.append("typ = %s")
        params.append(typ_filter)
    if ar_filter:
        villkor.append("ar = %s")
        params.append(ar_filter)

    where_extra = ("AND " + " AND ".join(villkor)) if villkor else ""
    rank_params = list(termer) + params[len(termer):] + [max_treff]

    sql = f"""
        WITH q AS (
            SELECT {tsquery_delar} AS tsq
        )
        SELECT
            d.id, d.edk_id, d.eduskuntatunnus_fi, d.eduskuntatunnus_sv,
            d.kalla, d.typ, d.{titel_kol} AS titel,
            d.ar, d.datum, d.akn_uri_fi,
            ts_rank_cd(
                to_tsvector('{pg_sprak}',
                    coalesce(d.{titel_kol},'') || ' ' || coalesce(d.{text_kol},'')),
                q.tsq
            ) AS rank
        FROM   finland.dokument d, q
        WHERE  to_tsvector('{pg_sprak}',
                   coalesce(d.{titel_kol},'') || ' ' || coalesce(d.{text_kol},''))
               @@ q.tsq
        {where_extra}
        ORDER  BY rank DESC, d.datum DESC NULLS LAST
        LIMIT  %s
    """

    with _cursor() as cur:
        cur.execute(sql, rank_params)
        rader = cur.fetchall()

    return [
        {
            "id":                  r[0],
            "edk_id":              r[1],
            "eduskuntatunnus_fi":  r[2],
            "eduskuntatunnus_sv":  r[3],
            "kalla":               r[4],
            "typ":                 r[5],
            "titel":               r[6],
            "ar":                  r[7],
            "datum":               str(r[8]) if r[8] else None,
            "akn_uri_fi":          r[9],
            "rank":                float(r[10]) if r[10] is not None else 0.0,
        }
        for r in rader
    ]


def _sq_fts_sok(termer, kalla_filter, typ_filter, ar_filter, max_treff) -> list[dict]:
    """ILIKE-sökning för SQLite."""
    villkor = []
    params  = []

    or_delar = " OR ".join(
        ["(titel_fi LIKE ? OR titel_sv LIKE ? OR fulltext_fi LIKE ? OR fulltext_sv LIKE ?)"] * len(termer)
    )
    for t in termer:
        params += [f"%{t}%", f"%{t}%", f"%{t}%", f"%{t}%"]
    villkor.append(f"({or_delar})")

    if kalla_filter:
        villkor.append("kalla = ?")
        params.append(kalla_filter)
    if typ_filter:
        villkor.append("typ = ?")
        params.append(typ_filter)
    if ar_filter:
        villkor.append("ar = ?")
        params.append(ar_filter)

    params.append(max_treff)
    sql = f"""
        SELECT id, edk_id, eduskuntatunnus_fi, eduskuntatunnus_sv,
               kalla, typ, titel_fi AS titel, ar, datum, akn_uri_fi, 0.0 AS rank
        FROM   dokument
        WHERE  {' AND '.join(villkor)}
        ORDER  BY datum DESC
        LIMIT  ?
    """

    conn = _sq_anslutning()
    rader = conn.execute(sql, params).fetchall()
    return [
        {
            "id":                 r[0],
            "edk_id":             r[1],
            "eduskuntatunnus_fi": r[2],
            "eduskuntatunnus_sv": r[3],
            "kalla":              r[4],
            "typ":                r[5],
            "titel":              r[6],
            "ar":                 r[7],
            "datum":              r[8],
            "akn_uri_fi":         r[9],
            "rank":               0.0,
        }
        for r in rader
    ]


# ---------------------------------------------------------------------------
# Semantisk (vektor) sökning
# ---------------------------------------------------------------------------

def vektor_sok(
    embedding: list[float],
    sprak: str = "fi",
    kalla_filter: Optional[str] = None,
    max_treff: int = 10,
) -> list[dict]:
    """
    Semantisk sökning via pgvector (cosinuslikhet).

    sprak: "fi" → embedding_fi, "sv" → embedding_sv
    Kräver PostgreSQL — returnerar tom lista vid SQLite.
    """
    if not _ar_postgres():
        log.warning("vektor_sok: pgvector kräver PostgreSQL — returnerar tom lista.")
        return []

    emb_kol   = "embedding_fi" if sprak == "fi" else "embedding_sv"
    titel_kol = "titel_fi" if sprak == "fi" else "titel_sv"

    vec_str = "[" + ",".join(str(float(x)) for x in embedding) + "]"

    villkor = [f"c.{emb_kol} IS NOT NULL"]
    params: list = [vec_str]

    if kalla_filter:
        villkor.append("d.kalla = %s")
        params.append(kalla_filter)

    params += [vec_str, max_treff]
    where_extra = " AND ".join(villkor)

    sql = f"""
        SELECT
            c.dokument_id,
            c.chunk_index,
            {'c.text_fi' if sprak == 'fi' else 'c.text_sv'} AS text,
            1 - (c.{emb_kol} <=> %s::vector) AS likhet,
            d.kalla,
            d.typ,
            d.{titel_kol} AS titel,
            d.ar,
            d.datum,
            d.edk_id,
            d.akn_uri_fi
        FROM   finland.chunks c
        JOIN   finland.dokument d ON d.id = c.dokument_id
        WHERE  {where_extra}
        ORDER  BY c.{emb_kol} <=> %s::vector
        LIMIT  %s
    """

    try:
        with _pg_anslutning() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                rader = cur.fetchall()
    except Exception as exc:
        log.error("vektor_sok misslyckades: %s", exc)
        return []

    return [
        {
            "dok_id":      r[0],
            "chunk_index": r[1],
            "text":        r[2],
            "likhet":      round(float(r[3]), 4) if r[3] is not None else 0.0,
            "kalla":       r[4],
            "typ":         r[5],
            "titel":       r[6],
            "ar":          r[7],
            "datum":       str(r[8]) if r[8] else None,
            "edk_id":      r[9],
            "akn_uri_fi":  r[10],
        }
        for r in rader
    ]


def vektor_sok_i_dokument(
    dokument_id: int,
    embedding: list[float],
    sprak: str = "fi",
    max_treff: int = 5,
) -> dict:
    """
    Semantisk sökning via pgvector inom ett enskilt cachat dokument.

    Returnerar dokumentmetadata + topp-N chunk-träffar sorterade efter
    cosinus-likhet. Kräver PostgreSQL med pgvector — SQLite-läge har
    inga embeddings och stöds inte.

    dokument_id — intern PK i finland.dokument
    embedding   — frågans vektorkod (768 dim, TurkuNLP eller KBLab)
    sprak       — "fi" → embedding_fi + text_fi, "sv" → embedding_sv + text_sv
    max_treff   — max antal chunk-träffar att returnera (standard 5)
    """
    if not _ar_postgres():
        return {"fel": "Semantisk sökning kräver PostgreSQL med pgvector — SQLite-läge stöds inte."}

    emb_kol   = "embedding_fi" if sprak == "fi" else "embedding_sv"
    text_kol  = "text_fi"      if sprak == "fi" else "text_sv"
    titel_kol = "titel_fi"     if sprak == "fi" else "titel_sv"

    # Hämta dokumentmetadata och räkna chunks
    with _pg_anslutning() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""SELECT id, edk_id, eduskuntatunnus_fi, eduskuntatunnus_sv,
                           kalla, typ, {titel_kol} AS titel, ar, datum
                    FROM finland.dokument WHERE id = %s""",
                (dokument_id,)
            )
            rad = cur.fetchone()
            if not rad:
                return {"fel": f"Dokument med id={dokument_id} finns inte i databasen."}
            dok_meta = {
                "dokument_id":       rad[0],
                "edk_id":            rad[1],
                "eduskuntatunnus_fi": rad[2],
                "eduskuntatunnus_sv": rad[3],
                "kalla":             rad[4],
                "typ":               rad[5],
                "titel":             rad[6],
                "ar":                rad[7],
                "datum":             str(rad[8]) if rad[8] else None,
            }

            cur.execute(
                f"SELECT COUNT(*) FROM finland.chunks WHERE dokument_id = %s AND {emb_kol} IS NOT NULL",
                (dokument_id,)
            )
            antal_chunks = cur.fetchone()[0]

    if antal_chunks == 0:
        return {
            **dok_meta,
            "antal_chunks": 0,
            "fel": (
                "Dokumentet har inga chunks med embeddings — "
                "fulltext saknas eller chunkning/indexering ej körd."
            ),
        }

    vec_str = "[" + ",".join(str(float(x)) for x in embedding) + "]"

    try:
        with _pg_anslutning() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""SELECT chunk_index,
                               {text_kol} AS text,
                               1 - (c.{emb_kol} <=> %s::vector) AS likhet
                        FROM   finland.chunks c
                        WHERE  c.dokument_id = %s
                          AND  c.{emb_kol} IS NOT NULL
                        ORDER  BY c.{emb_kol} <=> %s::vector
                        LIMIT  %s""",
                    (vec_str, dokument_id, vec_str, max_treff)
                )
                traffar = [
                    {
                        "chunk_index": r[0],
                        "text":        r[1],
                        "likhet":      round(float(r[2]), 4) if r[2] is not None else 0.0,
                    }
                    for r in cur.fetchall()
                ]
    except Exception as exc:
        log.error("vektor_sok_i_dokument misslyckades (id=%s): %s", dokument_id, exc)
        return {**dok_meta, "fel": str(exc)}

    return {
        **dok_meta,
        "fraga_sprak":   sprak,
        "antal_chunks":  antal_chunks,
        "antal_traffar": len(traffar),
        "traffar":       traffar,
    }


# ---------------------------------------------------------------------------
# Voteringar
# ---------------------------------------------------------------------------

def upsert_votering(
    aanestys_id: str,
    ar: Optional[int],
    vp_ar: Optional[int],
    istunto_nr: Optional[int],
    datum: Optional[str],
    otsikko_fi: Optional[str],
    otsikko_sv: Optional[str],
    ja_roster: Optional[int],
    nej_roster: Optional[int],
    tom_roster: Optional[int],
    franv_roster: Optional[int],
    resultat: Optional[str],
    kalla: str = "eduskunta",
    raw_json: Optional[dict] = None,
):
    """Infogar eller uppdaterar en votering."""
    p = _ph()
    t = _prefix()
    raw = json.dumps(raw_json) if raw_json else None

    sql = f"""
        INSERT INTO {t}voteringar
            (aanestys_id, ar, vp_ar, istunto_nr, datum,
             otsikko_fi, otsikko_sv, ja_roster, nej_roster,
             tom_roster, franv_roster, resultat, kalla, raw_json)
        VALUES ({', '.join([p]*14)})
        ON CONFLICT (aanestys_id) DO UPDATE SET
            otsikko_fi  = EXCLUDED.otsikko_fi,
            otsikko_sv  = EXCLUDED.otsikko_sv,
            ja_roster   = EXCLUDED.ja_roster,
            nej_roster  = EXCLUDED.nej_roster,
            resultat    = EXCLUDED.resultat
    """ if _ar_postgres() else f"""
        INSERT INTO {t}voteringar
            (aanestys_id, ar, vp_ar, istunto_nr, datum,
             otsikko_fi, otsikko_sv, ja_roster, nej_roster,
             tom_roster, franv_roster, resultat, kalla, raw_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (aanestys_id) DO UPDATE SET
            otsikko_fi = excluded.otsikko_fi,
            otsikko_sv = excluded.otsikko_sv,
            ja_roster  = excluded.ja_roster,
            nej_roster = excluded.nej_roster,
            resultat   = excluded.resultat
    """

    with _cursor() as cur:
        cur.execute(sql, (
            aanestys_id, ar, vp_ar, istunto_nr, datum,
            otsikko_fi, otsikko_sv, ja_roster, nej_roster,
            tom_roster, franv_roster, resultat, kalla, raw
        ))


# ---------------------------------------------------------------------------
# Synkstatus
# ---------------------------------------------------------------------------

def hamta_sync_status(kalla: str) -> dict:
    """Returnerar synkstatus för en källa, eller tomt dict om ingen finns."""
    p = _ph()
    t = _prefix()
    if _ar_postgres():
        with _pg_anslutning() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT kalla, sist_synkad, senaste_ar, antal_poster, detaljer FROM {t}sync_status WHERE kalla={p}",
                    (kalla,)
                )
                rad = cur.fetchone()
        if not rad:
            return {}
        return {
            "kalla": rad[0], "sist_synkad": str(rad[1]),
            "senaste_ar": rad[2], "antal_poster": rad[3], "detaljer": rad[4]
        }
    else:
        conn = _sq_anslutning()
        rad = conn.execute(
            "SELECT kalla, sist_synkad, senaste_ar, antal_poster, detaljer FROM sync_status WHERE kalla=?",
            (kalla,)
        ).fetchone()
        if not rad:
            return {}
        return dict(rad)


def set_sync_status(
    kalla: str,
    senaste_ar: Optional[int] = None,
    antal_poster: Optional[int] = None,
    detaljer: Optional[dict] = None,
):
    """Uppdaterar (eller infogar) synkstatus för en källa."""
    p = _ph()
    t = _prefix()
    det_str = json.dumps(detaljer) if isinstance(detaljer, dict) else detaljer

    if _ar_postgres():
        with _pg_anslutning() as conn:
            with conn.cursor() as cur:
                cur.execute(f"""
                    INSERT INTO {t}sync_status (kalla, sist_synkad, senaste_ar, antal_poster, detaljer)
                    VALUES ({p}, NOW(), {p}, {p}, {p}::jsonb)
                    ON CONFLICT (kalla) DO UPDATE SET
                        sist_synkad  = NOW(),
                        senaste_ar   = COALESCE(EXCLUDED.senaste_ar, {t}sync_status.senaste_ar),
                        antal_poster = COALESCE(EXCLUDED.antal_poster, {t}sync_status.antal_poster),
                        detaljer     = COALESCE(EXCLUDED.detaljer, {t}sync_status.detaljer)
                """, (kalla, senaste_ar, antal_poster, det_str))
            conn.commit()
    else:
        conn = _sq_anslutning()
        conn.execute("""
            INSERT INTO sync_status (kalla, sist_synkad, senaste_ar, antal_poster, detaljer)
            VALUES (?, datetime('now'), ?, ?, ?)
            ON CONFLICT (kalla) DO UPDATE SET
                sist_synkad  = datetime('now'),
                senaste_ar   = coalesce(excluded.senaste_ar, senaste_ar),
                antal_poster = coalesce(excluded.antal_poster, antal_poster),
                detaljer     = coalesce(excluded.detaljer, detaljer)
        """, (kalla, senaste_ar, antal_poster, det_str))
        conn.commit()
