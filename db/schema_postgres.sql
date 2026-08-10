-- schema_postgres.sql — PostgreSQL-schema för finsk riksdags- och rättsdata
-- SPDX-License-Identifier: AGPL-3.0-or-later
-- Copyright (C) 2026 Magnus Kolsjö
--
-- Kräver pgvector-tillägget (pgvector/pgvector:pg16 i Docker).
-- Körs automatiskt av db.py vid uppstart.

CREATE EXTENSION IF NOT EXISTS vector;
CREATE SCHEMA  IF NOT EXISTS finland;

-- ─── Dokument (on-demand cache + Finlex bulk) ────────────────────────────────

CREATE TABLE IF NOT EXISTS finland.dokument (
    id                  SERIAL PRIMARY KEY,

    -- Identifikatorer
    edk_id              TEXT,           -- Eduskunta edktunnus, t.ex. "EDK-2026-AK-8746"
    eduskuntatunnus_fi  TEXT,           -- t.ex. "HE 15/2026 vp"
    eduskuntatunnus_sv  TEXT,           -- t.ex. "RP 15/2026 rd"
    akn_uri_fi          TEXT,           -- Finlex fin@-URI
    akn_uri_sv          TEXT,           -- Finlex swe@-URI
    eli                 TEXT,           -- ELI-identifierare (Finlex)

    -- Klassificering
    kalla               TEXT NOT NULL,  -- "eduskunta" | "finlex"
    typ                 TEXT,           -- asiakirjatyyppikoodi (HE, RP, KK...) eller finlex-typ
    finlex_hierarki     TEXT,           -- "act" | "doc"
    finlex_typ          TEXT,           -- "statute" | "government-proposal" | ...

    -- Innehåll
    titel_fi            TEXT,
    titel_sv            TEXT,
    ar                  INTEGER,
    nummer              TEXT,
    datum               DATE,
    sprak               TEXT,           -- primärspråk: "fi" | "sv"

    -- Fulltext (HTML → text)
    html_saatavilla     BOOLEAN DEFAULT FALSE,
    fulltext_fi         TEXT,           -- extraherad text på finska
    fulltext_sv         TEXT,           -- extraherad text på svenska

    -- Teknisk metadata
    senast_hamtad       TIMESTAMPTZ,
    skapad              TIMESTAMPTZ DEFAULT NOW(),

    CONSTRAINT uq_edk_id    UNIQUE (edk_id),
    CONSTRAINT uq_akn_fi    UNIQUE (akn_uri_fi)
);

CREATE INDEX IF NOT EXISTS idx_finland_dok_typ   ON finland.dokument (typ);
CREATE INDEX IF NOT EXISTS idx_finland_dok_ar    ON finland.dokument (ar);
CREATE INDEX IF NOT EXISTS idx_finland_dok_kalla ON finland.dokument (kalla);
CREATE INDEX IF NOT EXISTS idx_finland_dok_datum ON finland.dokument (datum DESC NULLS LAST);

-- FTS-index (finska och svenska)
CREATE INDEX IF NOT EXISTS idx_finland_dok_fts_fi ON finland.dokument
    USING GIN (to_tsvector('finnish', coalesce(titel_fi,'') || ' ' || coalesce(fulltext_fi,'')));
CREATE INDEX IF NOT EXISTS idx_finland_dok_fts_sv ON finland.dokument
    USING GIN (to_tsvector('swedish', coalesce(titel_sv,'') || ' ' || coalesce(fulltext_sv,'')));

-- ─── Chunks för RAG (dubbel embedding: finska + svenska) ────────────────────

CREATE TABLE IF NOT EXISTS finland.chunks (
    id           SERIAL PRIMARY KEY,
    dokument_id  INTEGER NOT NULL REFERENCES finland.dokument(id) ON DELETE CASCADE,
    chunk_index  INTEGER NOT NULL,
    text_fi      TEXT,                      -- finskt textblock
    text_sv      TEXT,                      -- svenskt textblock
    embedding_fi VECTOR(768),               -- TurkuNLP/sbert-cased-finnish-paraphrase
    embedding_sv VECTOR(768),               -- KBLab/sentence-bert-swedish-cased
    UNIQUE (dokument_id, chunk_index)
);

CREATE INDEX IF NOT EXISTS idx_finland_chunks_dok ON finland.chunks (dokument_id);

-- Vektorindex (IVFFlat — skapas när data finns)
-- CREATE INDEX IF NOT EXISTS idx_finland_chunks_emb_fi ON finland.chunks
--     USING ivfflat (embedding_fi vector_cosine_ops) WITH (lists = 100);
-- CREATE INDEX IF NOT EXISTS idx_finland_chunks_emb_sv ON finland.chunks
--     USING ivfflat (embedding_sv vector_cosine_ops) WITH (lists = 100);

-- ─── Voteringar ─────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS finland.voteringar (
    id              SERIAL PRIMARY KEY,
    aanestys_id     TEXT UNIQUE NOT NULL,   -- "{vpvuosi}-{istuntonr}-{aanestysnr}"
    ar              INTEGER,
    vp_ar           INTEGER,                -- riksdagsår (valtiopaivavuosi)
    istunto_nr      INTEGER,
    datum           DATE,
    otsikko_fi      TEXT,                   -- rubrik på finska
    otsikko_sv      TEXT,                   -- rubrik på svenska
    ja_roster       INTEGER,
    nej_roster      INTEGER,
    tom_roster      INTEGER,
    franv_roster    INTEGER,
    resultat        TEXT,                   -- "JA" | "NEJ" | "OAVGJORT"
    kalla           TEXT DEFAULT 'eduskunta',  -- "eduskunta" (ny API) | "avoindata" (1996-2014)
    raw_json        JSONB
);

CREATE INDEX IF NOT EXISTS idx_finland_vot_datum ON finland.voteringar (datum DESC NULLS LAST);
CREATE INDEX IF NOT EXISTS idx_finland_vot_ar    ON finland.voteringar (vp_ar, istunto_nr);

-- ─── Ledamöter ──────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS finland.ledamoter (
    henkilonro      TEXT PRIMARY KEY,
    efternamn       TEXT,
    fornamn         TEXT,
    parti           TEXT,
    vaalikaudet     JSONB,              -- lista med valperioder
    uppdaterad      TIMESTAMPTZ DEFAULT NOW()
);

-- ─── Synkstatus (checkpoint per källa) ──────────────────────────────────────

CREATE TABLE IF NOT EXISTS finland.sync_status (
    kalla           TEXT PRIMARY KEY,   -- "finlex_statute", "finlex_proposal", "eduskunta_search", ...
    sist_synkad     TIMESTAMPTZ,
    senaste_ar      INTEGER,            -- senaste synkade år (för inkrementell synk)
    antal_poster    INTEGER DEFAULT 0,
    detaljer        JSONB
);
