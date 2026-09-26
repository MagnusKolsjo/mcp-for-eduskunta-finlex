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
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

log = logging.getLogger(__name__)

_SCRIPT_DIR  = Path(__file__).parent.resolve()
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///finland_cache.db")

_pg_pool = None
_pg_pool_lock = threading.Lock()
# En SQLite-anslutning per tråd. Verktygen körs på arbetstrådar, och en
# delad anslutning skulle blanda ihop trådarnas transaktioner: en commit
# eller rollback i en tråd gäller då även den andras halvfärdiga skrivning.
_sq_lokal = threading.local()


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
        with _pg_pool_lock:
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
    """Returnerar trådens SQLite-anslutning; öppnas vid första användningen."""
    conn = getattr(_sq_lokal, "conn", None)
    if conn is None:
        db_sokvag = DATABASE_URL.replace("sqlite:///", "")
        if not Path(db_sokvag).is_absolute():
            db_sokvag = str(_SCRIPT_DIR / db_sokvag)
        conn = sqlite3.connect(db_sokvag, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        _sq_lokal.conn = conn
    return conn


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
    _migrera()
    log.info("PostgreSQL-schema finland initierat")


def sq_init():
    """Initierar SQLite-tabeller via schema_sqlite.sql."""
    sql_fil = _SCRIPT_DIR / "db" / "schema_sqlite.sql"
    if not sql_fil.exists():
        raise FileNotFoundError(f"Schemafil saknas: {sql_fil}")
    conn = _sq_anslutning()
    conn.executescript(sql_fil.read_text(encoding="utf-8"))
    conn.commit()
    _migrera()
    log.info("SQLite-schema initierat")


# Kolumner som tillkommit efter första publiceringen. Läggs till med
# ADD COLUMN, aldrig i bas-schemat, så att befintliga databaser migreras.
_TILLAGDA_KOLUMNER = {
    "chunks": [
        # Styckets position i dokumentets text per språk. Gör att en träff
        # kan adresseras i en live-hämtad text utan att texten lagras lokalt.
        ("tecken_start_fi", "INTEGER"),
        ("tecken_slut_fi",  "INTEGER"),
        ("tecken_start_sv", "INTEGER"),
        ("tecken_slut_sv",  "INTEGER"),
    ],
}


def _migrera():
    """Lägger till kolumner som saknas. Idempotent; påverkar inga befintliga värden."""
    for tabell, kolumner in _TILLAGDA_KOLUMNER.items():
        if _ar_postgres():
            with _cursor() as cur:
                for namn, typ in kolumner:
                    cur.execute(
                        f"ALTER TABLE finland.{tabell} ADD COLUMN IF NOT EXISTS {namn} {typ}"
                    )
        else:
            conn = _sq_anslutning()
            finns = {r[1] for r in conn.execute(f"PRAGMA table_info({tabell})")}
            for namn, typ in kolumner:
                if namn not in finns:
                    conn.execute(f"ALTER TABLE {tabell} ADD COLUMN {namn} {typ}")
            conn.commit()
    if _ar_postgres():
        _migrera_halfvec()


# ---------------------------------------------------------------------------
# Vektorlagring: vector eller halfvec
# ---------------------------------------------------------------------------
# Embeddings lagras som halfvec(768) (16-bitars flyttal): hälften så stort
# som vector(768), och träffsäkerheten påverkas inte mätbart för cosinus-
# sökning. Äldre databaser har vector(768) tills 06_konvertera_vektorer.py
# körts; frågorna läser därför kolumntypen och castar frågevektorn därefter.
#
# Index: HNSW (m=16, ef_construction=64). IVFFlat är mindre och snabbare att
# bygga, men dess centroider beräknas en gång vid bygget och passar allt
# sämre när den dagliga synken lägger till chunks; HNSW tål inskrivningar och
# ger högre träffsäkerhet per fråga. Mätt på 60 000 riktiga embeddings:
# recall@10 0,997 med ef_search=100 mot 0,88 för IVFFlat med probes=20.

VEKTOR_DIM = 768
HNSW_M = 16
HNSW_EF_CONSTRUCTION = 64
HNSW_EF_SEARCH = int(os.getenv("FI_HNSW_EF_SEARCH", "100"))
# Gäller bara IVFFlat-index, dvs. en databas som ännu inte konverterats.
# Varje probe läser ungefär 1/lists av alla vektorer ur TOAST: med 3 miljoner
# chunks och lists=100 tar probes=20 över en minut per fråga. Standard 1 är
# pgvectors eget standardvärde; träffsäkerheten höjs genom konverteringen.
IVFFLAT_PROBES = int(os.getenv("FI_IVFFLAT_PROBES", "1"))

# Under den här storleken konverteras kolumnerna automatiskt vid uppstart
# (ny eller nästan tom databas). Större tabeller konverteras med
# 06_konvertera_vektorer.py, eftersom omskrivningen tar tid och disk.
AUTO_KONVERTERA_MAX_RADER = 50_000


def vektortyp(sprak: str, cur=None) -> str:
    """'halfvec' eller 'vector' för embedding-kolumnen på språket."""
    kol = "embedding_fi" if sprak == "fi" else "embedding_sv"
    sql = """SELECT format_type(a.atttypid, a.atttypmod)
             FROM pg_attribute a
             WHERE a.attrelid = 'finland.chunks'::regclass AND a.attname = %s"""
    if cur is not None:
        cur.execute(sql, (kol,))
        rad = cur.fetchone()
    else:
        with _cursor() as c:
            c.execute(sql, (kol,))
            rad = c.fetchone()
    return "halfvec" if rad and rad[0].startswith("halfvec") else "vector"


def _sokinstallningar(cur) -> None:
    """Sökparametrar för vektorindexen, gäller bara transaktionen."""
    cur.execute(f"SET LOCAL hnsw.ef_search = {HNSW_EF_SEARCH}")
    cur.execute(f"SET LOCAL ivfflat.probes = {IVFFLAT_PROBES}")
    # Med ett filter (t.ex. kalla) fortsätter indexsökningen tills tillräckligt
    # många rader passerat filtret i stället för att ge för få träffar.
    cur.execute("SET LOCAL hnsw.iterative_scan = relaxed_order")


def vektorindex_namn(sprak: str) -> str:
    return f"idx_finland_chunks_emb_{'fi' if sprak == 'fi' else 'sv'}"


def bygg_vektorindex(sprak: str, minne: str | None = None, parallella: int | None = None) -> None:
    """
    Bygger om vektorindexet för ett språk som HNSW med rätt operatorklass.

    HNSW-bygget går mycket snabbare när grafen ryms i maintenance_work_mem;
    för miljontals chunks behövs flera GB. Anges minne sätts det för sessionen.
    """
    kol = "embedding_fi" if sprak == "fi" else "embedding_sv"
    namn = vektorindex_namn(sprak)
    with _cursor() as cur:
        typ = vektortyp(sprak, cur)
        ops = "halfvec_cosine_ops" if typ == "halfvec" else "vector_cosine_ops"
        if minne:
            cur.execute("SET LOCAL maintenance_work_mem = %s", (minne,))
        if parallella is not None:
            cur.execute(f"SET LOCAL max_parallel_maintenance_workers = {int(parallella)}")
        cur.execute(f"DROP INDEX IF EXISTS finland.{namn}")
        cur.execute(
            f"CREATE INDEX {namn} ON finland.chunks USING hnsw ({kol} {ops}) "
            f"WITH (m = {HNSW_M}, ef_construction = {HNSW_EF_CONSTRUCTION})"
        )


def konvertera_till_halfvec(sprak: str, cur) -> None:
    """
    Byter en embedding-kolumn till halfvec(768). Skriver om hela tabellen.

    Vektorindexet på kolumnen tas bort först: dess operatorklass gäller bara
    vector. Anroparen bygger nytt index efteråt (bygg_vektorindex).
    """
    kol = "embedding_fi" if sprak == "fi" else "embedding_sv"
    cur.execute(f"DROP INDEX IF EXISTS finland.{vektorindex_namn(sprak)}")
    cur.execute(
        f"ALTER TABLE finland.chunks ALTER COLUMN {kol} "
        f"TYPE halfvec({VEKTOR_DIM}) USING {kol}::halfvec({VEKTOR_DIM})"
    )


def _migrera_halfvec() -> None:
    """Konverterar små tabeller till halfvec vid uppstart; stora lämnas till skriptet."""
    with _cursor() as cur:
        att_konvertera = [s for s in ("fi", "sv") if vektortyp(s, cur) == "vector"]
        if not att_konvertera:
            return
        cur.execute(
            f"SELECT count(*) FROM (SELECT 1 FROM finland.chunks LIMIT {AUTO_KONVERTERA_MAX_RADER + 1}) x"
        )
        if cur.fetchone()[0] > AUTO_KONVERTERA_MAX_RADER:
            log.info(
                "finland.chunks lagrar embeddings som vector. Kör "
                "06_konvertera_vektorer.py för att byta till halfvec och HNSW."
            )
            return
        for sprak in att_konvertera:
            konvertera_till_halfvec(sprak, cur)
    for sprak in att_konvertera:
        bygg_vektorindex(sprak)
    log.info("Embeddings konverterade till halfvec(%d) med HNSW-index", VEKTOR_DIM)


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
    SQLite: returnerar trådens egen anslutning.
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
    SQLite: återanvänder trådens egen anslutning.
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

# Fulltextindex på chunks. De skapas inte vid uppstart: på en befintlig
# databas med miljontals chunks tar det lång tid, och uppstarten ska vara
# snabb. De skapas av 03_chunka_och_embedda.py --bygg-index och av
# 05_rensa_fulltext.py. Så länge de saknas söker FTS bara i dokument.
CHUNK_FTS_INDEX = {
    "fi": ("idx_finland_chunks_fts_fi", "finnish", "text_fi"),
    "sv": ("idx_finland_chunks_fts_sv", "swedish", "text_sv"),
}
_chunk_fts_finns: dict[str, bool] = {}


def har_chunk_fts(sprak: str) -> bool:
    """True om fulltextindexet på chunks finns för språket (Postgres)."""
    if not _ar_postgres():
        return False
    if _chunk_fts_finns.get(sprak):
        return True
    namn = CHUNK_FTS_INDEX[sprak][0]
    with _cursor() as cur:
        cur.execute(
            "SELECT 1 FROM pg_indexes WHERE schemaname = 'finland' AND indexname = %s",
            (namn,),
        )
        finns = cur.fetchone() is not None
    # Bara ett positivt svar cachas; ett index som byggs senare ska märkas.
    if finns:
        _chunk_fts_finns[sprak] = True
    return finns


def skapa_chunk_fts_index() -> None:
    """Skapar fulltextindexen på chunks om de saknas (tar tid på stora databaser)."""
    for sprak, (namn, konfig, kol) in CHUNK_FTS_INDEX.items():
        log.info("Skapar %s om det saknas...", namn)
        with _cursor() as cur:
            cur.execute(
                f"CREATE INDEX IF NOT EXISTS {namn} ON finland.chunks "
                f"USING GIN (to_tsvector('{konfig}', coalesce({kol}, '')))"
            )


def spara_chunks(dok_id: int, chunks: list[dict], embeddings, sprak: str) -> None:
    """
    Sparar ett språks chunks med embedding och offsets för ett dokument.

    Befintliga rader för samma dokument_id och chunk_index uppdateras bara i
    det här språkets kolumner; det andra språkets text och vektor lämnas.
    Kräver Postgres med pgvector.
    """
    s = "fi" if sprak == "fi" else "sv"
    with _cursor() as cur:
        typ = vektortyp(s, cur)
        cur.execute(
            f"UPDATE finland.chunks SET embedding_{s} = NULL WHERE dokument_id = %s",
            (dok_id,),
        )
        for ch, emb in zip(chunks, embeddings):
            vec_str = "[" + ",".join(str(float(x)) for x in emb) + "]"
            cur.execute(
                f"""
                INSERT INTO finland.chunks
                    (dokument_id, chunk_index, text_{s}, embedding_{s},
                     tecken_start_{s}, tecken_slut_{s})
                VALUES (%s, %s, %s, %s::{typ}, %s, %s)
                ON CONFLICT (dokument_id, chunk_index) DO UPDATE SET
                    text_{s}         = EXCLUDED.text_{s},
                    embedding_{s}    = EXCLUDED.embedding_{s},
                    tecken_start_{s} = EXCLUDED.tecken_start_{s},
                    tecken_slut_{s}  = EXCLUDED.tecken_slut_{s}
                """,
                (dok_id, ch["chunk_index"], ch["text"], vec_str,
                 ch.get("tecken_start"), ch.get("tecken_slut")),
            )


def antal_chunks_med_embedding(dok_id: int, sprak: str) -> int:
    """Antal chunks med embedding på språket för ett dokument (Postgres)."""
    s = "fi" if sprak == "fi" else "sv"
    with _cursor() as cur:
        cur.execute(
            f"SELECT count(*) FROM finland.chunks WHERE dokument_id = %s AND embedding_{s} IS NOT NULL",
            (dok_id,),
        )
        return cur.fetchone()[0]


def finlex_uri_med_text(uris: list[str]) -> set[str]:
    """
    De AKN-URI:er (med version) som redan finns lokalt med text på sitt språk.

    En URI räknas som lagrad när raden har råtext (även tom: dokument med
    bara titel) eller chunks på språket. Versionsbeteckningen ingår i URI:n
    (…/fin@20180817), så en ny lydelse ger en ny URI och hämtas.
    """
    p, t = _ph(), _prefix()
    funna: set[str] = set()
    for sprak, uri_kol, text_kol, chunk_kol in (
        ("fin@", "akn_uri_fi", "fulltext_fi", "text_fi"),
        ("swe@", "akn_uri_sv", "fulltext_sv", "text_sv"),
    ):
        urval = [u for u in uris if f"/{sprak}" in u]
        if not urval:
            continue
        with _cursor() as cur:
            cur.execute(
                f"""SELECT d.{uri_kol} FROM {t}dokument d
                    WHERE d.{uri_kol} IN ({', '.join([p] * len(urval))})
                      AND (d.{text_kol} IS NOT NULL
                           OR EXISTS (SELECT 1 FROM {t}chunks c
                                      WHERE c.dokument_id = d.id AND c.{chunk_kol} IS NOT NULL))""",
                urval,
            )
            funna.update(r[0] for r in cur.fetchall())
    return funna


def lagrad_textlangd(akn_uri: str) -> Optional[int]:
    """Längden på lagrad råtext för en AKN-URI på dess språk, eller None."""
    sv = "/swe@" in akn_uri
    uri_kol  = "akn_uri_sv" if sv else "akn_uri_fi"
    text_kol = "fulltext_sv" if sv else "fulltext_fi"
    with _cursor() as cur:
        cur.execute(
            f"SELECT length({text_kol}) FROM {_prefix()}dokument WHERE {uri_kol} = {_ph()}",
            (akn_uri,),
        )
        rad = cur.fetchone()
    return rad[0] if rad else None


def rensa_fulltext(dok_id: int, sprak: str) -> None:
    """Tar bort ett språks råtext ur dokument när texten finns som chunks."""
    kol = "fulltext_fi" if sprak == "fi" else "fulltext_sv"
    with _cursor() as cur:
        cur.execute(f"UPDATE {_prefix()}dokument SET {kol} = NULL WHERE id = {_ph()}", (dok_id,))


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
    """
    FTS via to_tsquery med finsk/svensk konfiguration.

    Söker i dokumentets titel och eventuell lagrad råtext, och i chunks när
    fulltextindexet på chunks finns. Dokument vars råtext rensats hittas då
    via sina chunks; varje dokument får sin bästa rank från någon av delarna.
    """
    pg_sprak  = "finnish" if sprak == "fi" else "swedish"
    text_kol  = "fulltext_fi" if sprak == "fi" else "fulltext_sv"
    titel_kol = "titel_fi"   if sprak == "fi" else "titel_sv"
    chunk_kol = "text_fi"    if sprak == "fi" else "text_sv"

    tsquery_delar = " || ".join(
        [f"plainto_tsquery('{pg_sprak}', %s)"] * len(termer)
    )

    villkor: list[str] = []
    filter_params: list = []
    if kalla_filter:
        villkor.append("d.kalla = %s")
        filter_params.append(kalla_filter)
    if typ_filter:
        villkor.append("d.typ = %s")
        filter_params.append(typ_filter)
    if ar_filter:
        villkor.append("d.ar = %s")
        filter_params.append(ar_filter)
    where_extra = ("AND " + " AND ".join(villkor)) if villkor else ""

    dok_vektor = (
        f"to_tsvector('{pg_sprak}', "
        f"coalesce(d.{titel_kol},'') || ' ' || coalesce(d.{text_kol},''))"
    )
    delar = [f"""
        SELECT d.id, ts_rank_cd({dok_vektor}, q.tsq) AS rank
        FROM   finland.dokument d, q
        WHERE  {dok_vektor} @@ q.tsq {where_extra}
    """]
    params = list(termer) + filter_params

    if har_chunk_fts(sprak):
        # Uttrycket måste vara identiskt med indexets för att indexet används.
        chunk_vektor = f"to_tsvector('{pg_sprak}', coalesce(c.{chunk_kol}, ''))"
        delar.append(f"""
            SELECT c.dokument_id AS id, ts_rank_cd({chunk_vektor}, q.tsq) AS rank
            FROM   finland.chunks c
            JOIN   finland.dokument d ON d.id = c.dokument_id, q
            WHERE  {chunk_vektor} @@ q.tsq {where_extra}
        """)
        params += filter_params

    sql = f"""
        WITH q AS (SELECT {tsquery_delar} AS tsq),
        traffar AS (
            SELECT id, max(rank) AS rank
            FROM ({" UNION ALL ".join(delar)}) x
            GROUP BY id
        )
        SELECT
            d.id, d.edk_id, d.eduskuntatunnus_fi, d.eduskuntatunnus_sv,
            d.kalla, d.typ, d.{titel_kol} AS titel,
            d.ar, d.datum, d.akn_uri_fi, t.rank
        FROM   traffar t
        JOIN   finland.dokument d ON d.id = t.id
        ORDER  BY t.rank DESC, d.datum DESC NULLS LAST
        LIMIT  %s
    """
    params.append(max_treff)

    with _cursor() as cur:
        cur.execute(sql, params)
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
            1 - (c.{emb_kol} <=> %s::{{typ}}) AS likhet,
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
        ORDER  BY c.{emb_kol} <=> %s::{{typ}}
        LIMIT  %s
    """

    try:
        with _cursor() as cur:
            _sokinstallningar(cur)
            cur.execute(sql.format(typ=vektortyp(sprak, cur)), params)
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

    emb_kol    = "embedding_fi" if sprak == "fi" else "embedding_sv"
    text_kol   = "text_fi"      if sprak == "fi" else "text_sv"
    titel_kol  = "titel_fi"     if sprak == "fi" else "titel_sv"
    sprak_kort = "fi"           if sprak == "fi" else "sv"

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
                typ = vektortyp(sprak, cur)
                cur.execute(
                    f"""SELECT chunk_index,
                               {text_kol} AS text,
                               1 - (c.{emb_kol} <=> %s::{typ}) AS likhet,
                               c.tecken_start_{sprak_kort}, c.tecken_slut_{sprak_kort}
                        FROM   finland.chunks c
                        WHERE  c.dokument_id = %s
                          AND  c.{emb_kol} IS NOT NULL
                        -- "+ 0" hindrar planeraren från vektorindexet: inom ett
                        -- dokument är en sortering av dess få tusen chunks exakt
                        -- och snabb, medan indexet plus dokumentfiltret kan ge
                        -- för få träffar.
                        ORDER  BY (c.{emb_kol} <=> %s::{typ}) + 0
                        LIMIT  %s""",
                    (vec_str, dokument_id, vec_str, max_treff)
                )
                traffar = [
                    {
                        "chunk_index":  r[0],
                        "text":         r[1],
                        "likhet":       round(float(r[2]), 4) if r[2] is not None else 0.0,
                        "tecken_start": r[3],
                        "tecken_slut":  r[4],
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

    # Källan levererar en rad per språk för samma votering. Upserten slår
    # ihop dem: ett NULL-fält i den ena raden får aldrig nolla det den andra
    # raden redan fyllt i. raw_json behåller den först inlästa raden.
    sql = f"""
        INSERT INTO {t}voteringar
            (aanestys_id, ar, vp_ar, istunto_nr, datum,
             otsikko_fi, otsikko_sv, ja_roster, nej_roster,
             tom_roster, franv_roster, resultat, kalla, raw_json)
        VALUES ({', '.join([p]*14)})
        ON CONFLICT (aanestys_id) DO UPDATE SET
            otsikko_fi   = COALESCE(EXCLUDED.otsikko_fi,   voteringar.otsikko_fi),
            otsikko_sv   = COALESCE(EXCLUDED.otsikko_sv,   voteringar.otsikko_sv),
            ja_roster    = COALESCE(EXCLUDED.ja_roster,    voteringar.ja_roster),
            nej_roster   = COALESCE(EXCLUDED.nej_roster,   voteringar.nej_roster),
            tom_roster   = COALESCE(EXCLUDED.tom_roster,   voteringar.tom_roster),
            franv_roster = COALESCE(EXCLUDED.franv_roster, voteringar.franv_roster),
            -- Resultatet räknas om ur de sammanslagna talen, så att det
            -- aldrig kan motsäga ja_roster/nej_roster.
            resultat = CASE
                WHEN COALESCE(EXCLUDED.ja_roster, voteringar.ja_roster) IS NULL
                  OR COALESCE(EXCLUDED.nej_roster, voteringar.nej_roster) IS NULL
                THEN NULL
                WHEN COALESCE(EXCLUDED.ja_roster, voteringar.ja_roster)
                   > COALESCE(EXCLUDED.nej_roster, voteringar.nej_roster) THEN 'JA'
                WHEN COALESCE(EXCLUDED.ja_roster, voteringar.ja_roster)
                   < COALESCE(EXCLUDED.nej_roster, voteringar.nej_roster) THEN 'NEJ'
                ELSE NULL
            END
    """

    with _cursor() as cur:
        cur.execute(sql, (
            aanestys_id, ar, vp_ar, istunto_nr, datum,
            otsikko_fi, otsikko_sv, ja_roster, nej_roster,
            tom_roster, franv_roster, resultat, kalla, raw
        ))


def sok_voteringar(
    fraga: Optional[str] = None,
    fran_datum: Optional[str] = None,
    till_datum: Optional[str] = None,
    vp_ar: Optional[int] = None,
    istunto_nr: Optional[int] = None,
    aanestys_id: Optional[str] = None,
    max_treff: int = 20,
    start_index: int = 0,
) -> dict:
    """
    Söker bland lokalt lagrade voteringar.

    fraga matchar delsträngar i otsikko_fi och otsikko_sv utan hänsyn till
    versaler; flera ord separerade med komma ger OR. Returnerar
    {totalt, voteringar, tackning: {fran, till, antal}}.
    """
    p, t = _ph(), _prefix()
    villkor: list[str] = []
    params: list = []
    termer = [x.strip().lower() for x in (fraga or "").split(",") if x.strip()]
    if termer:
        villkor.append("(" + " OR ".join(
            [f"(lower(coalesce(otsikko_fi,'')) LIKE {p} OR lower(coalesce(otsikko_sv,'')) LIKE {p})"]
            * len(termer)) + ")")
        for term in termer:
            params += [f"%{term}%", f"%{term}%"]
    if fran_datum:
        villkor.append(f"datum >= {p}")
        params.append(fran_datum)
    if till_datum:
        villkor.append(f"datum <= {p}")
        params.append(till_datum)
    if vp_ar is not None:
        villkor.append(f"vp_ar = {p}")
        params.append(vp_ar)
    if istunto_nr is not None:
        villkor.append(f"istunto_nr = {p}")
        params.append(istunto_nr)
    if aanestys_id:
        villkor.append(f"aanestys_id = {p}")
        params.append(aanestys_id)
    where = ("WHERE " + " AND ".join(villkor)) if villkor else ""

    with _cursor() as cur:
        cur.execute(f"SELECT count(*) FROM {t}voteringar {where}", params)
        totalt = cur.fetchone()[0]
        cur.execute(
            f"""SELECT aanestys_id, vp_ar, istunto_nr, datum, otsikko_fi, otsikko_sv,
                       ja_roster, nej_roster, tom_roster, franv_roster, resultat, kalla
                FROM {t}voteringar {where}
                ORDER BY datum, vp_ar, istunto_nr, length(aanestys_id), aanestys_id
                LIMIT {p} OFFSET {p}""",
            params + [max_treff, start_index],
        )
        rader = cur.fetchall()
        cur.execute(f"SELECT min(datum), max(datum), count(*) FROM {t}voteringar")
        fran, till, antal = cur.fetchone()

    nycklar = ("aanestys_id", "vp_ar", "istunto_nr", "datum", "otsikko_fi", "otsikko_sv",
               "ja_roster", "nej_roster", "tom_roster", "franv_roster", "resultat", "kalla")
    voteringar = []
    for r in rader:
        rad = dict(zip(nycklar, tuple(r)))
        rad["datum"] = str(rad["datum"]) if rad["datum"] else None
        voteringar.append(rad)
    return {
        "totalt": totalt,
        "voteringar": voteringar,
        "tackning": {
            "fran": str(fran) if fran else None,
            "till": str(till) if till else None,
            "antal": antal,
        },
    }


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
