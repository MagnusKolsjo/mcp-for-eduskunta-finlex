-- schema_sqlite.sql — SQLite-schema för arbetsström 15 (Finland)
-- SPDX-License-Identifier: AGPL-3.0-or-later
-- Copyright (C) 2026 Magnus Kolsjö
--
-- OBS: pgvector saknas i SQLite — embedding_fi och embedding_sv lagras inte.
-- Semantisk sökning är inaktiverad i SQLite-läget.

-- ─── Dokument ────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS dokument (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    edk_id              TEXT UNIQUE,
    eduskuntatunnus_fi  TEXT,
    eduskuntatunnus_sv  TEXT,
    akn_uri_fi          TEXT UNIQUE,
    akn_uri_sv          TEXT,
    eli                 TEXT,
    kalla               TEXT NOT NULL,
    typ                 TEXT,
    finlex_hierarki     TEXT,
    finlex_typ          TEXT,
    titel_fi            TEXT,
    titel_sv            TEXT,
    ar                  INTEGER,
    nummer              TEXT,
    datum               TEXT,
    sprak               TEXT,
    html_saatavilla     INTEGER DEFAULT 0,
    fulltext_fi         TEXT,
    fulltext_sv         TEXT,
    senast_hamtad       TEXT,
    skapad              TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_sq_dok_typ   ON dokument (typ);
CREATE INDEX IF NOT EXISTS idx_sq_dok_ar    ON dokument (ar);
CREATE INDEX IF NOT EXISTS idx_sq_dok_kalla ON dokument (kalla);
CREATE INDEX IF NOT EXISTS idx_sq_dok_datum ON dokument (datum DESC);

-- ─── Chunks (utan vektorkolumner — SQLite stöder inte pgvector) ─────────────

CREATE TABLE IF NOT EXISTS chunks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    dokument_id INTEGER NOT NULL REFERENCES dokument(id) ON DELETE CASCADE,
    chunk_index INTEGER NOT NULL,
    text_fi     TEXT,
    text_sv     TEXT,
    UNIQUE (dokument_id, chunk_index)
);

-- ─── Voteringar ─────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS voteringar (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    aanestys_id TEXT UNIQUE NOT NULL,
    ar          INTEGER,
    vp_ar       INTEGER,
    istunto_nr  INTEGER,
    datum       TEXT,
    otsikko_fi  TEXT,
    otsikko_sv  TEXT,
    ja_roster   INTEGER,
    nej_roster  INTEGER,
    tom_roster  INTEGER,
    franv_roster INTEGER,
    resultat    TEXT,
    kalla       TEXT DEFAULT 'eduskunta',
    raw_json    TEXT
);

-- ─── Ledamöter ──────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS ledamoter (
    henkilonro  TEXT PRIMARY KEY,
    efternamn   TEXT,
    fornamn     TEXT,
    parti       TEXT,
    vaalikaudet TEXT,
    uppdaterad  TEXT DEFAULT (datetime('now'))
);

-- ─── Synkstatus ─────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS sync_status (
    kalla       TEXT PRIMARY KEY,
    sist_synkad TEXT,
    senaste_ar  INTEGER,
    antal_poster INTEGER DEFAULT 0,
    detaljer    TEXT
);
