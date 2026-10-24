from contextlib import contextmanager

import psycopg
from pgvector.psycopg import register_vector
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from . import config

SCHEMA = f"""
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS memos (
    id           bigserial PRIMARY KEY,
    created_at   timestamptz NOT NULL DEFAULT now(),
    mime         text,
    audio        bytea,                            -- deleted once the memo is sorted
    duration_s   real,
    status       text NOT NULL DEFAULT 'pending',  -- pending | done | failed
    transcript   text,
    error        text,
    processed_at timestamptz
);

ALTER TABLE memos ADD COLUMN IF NOT EXISTS audio bytea;
ALTER TABLE memos DROP COLUMN IF EXISTS audio_path;

CREATE TABLE IF NOT EXISTS items (
    id         bigserial PRIMARY KEY,
    memo_id    bigint NOT NULL REFERENCES memos(id) ON DELETE CASCADE,
    created_at timestamptz NOT NULL DEFAULT now(),
    kind       text NOT NULL,                      -- todo | event | note
    title      text NOT NULL,
    detail     text NOT NULL DEFAULT '',
    when_text  text NOT NULL DEFAULT '',
    due_at     timestamptz,
    status     text NOT NULL DEFAULT 'new',        -- new | approved | done | dismissed
    embedding  vector({config.EMBED_DIM}),
    tsv        tsvector GENERATED ALWAYS AS
               (to_tsvector('english', title || ' ' || detail)) STORED
);
CREATE INDEX IF NOT EXISTS items_tsv_idx ON items USING gin (tsv);
CREATE INDEX IF NOT EXISTS items_status_idx ON items (status, created_at DESC);

CREATE TABLE IF NOT EXISTS reviews (
    id            bigserial PRIMARY KEY,
    created_at    timestamptz NOT NULL DEFAULT now(),
    model         text NOT NULL,
    items_read    int NOT NULL,
    input_tokens  int NOT NULL,
    output_tokens int NOT NULL,
    cost_usd      numeric(12, 6) NOT NULL,
    body          jsonb NOT NULL
);

CREATE TABLE IF NOT EXISTS naps (
    id          bigserial PRIMARY KEY,
    started_at  timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz,
    model       text NOT NULL,
    memos       int NOT NULL DEFAULT 0,
    items       int NOT NULL DEFAULT 0,
    audio_s     real NOT NULL DEFAULT 0,
    words       int NOT NULL DEFAULT 0,
    failed      int NOT NULL DEFAULT 0
);
"""

_pool: ConnectionPool | None = None


def _configure(conn: psycopg.Connection) -> None:
    register_vector(conn)


def init() -> None:
    global _pool
    with psycopg.connect(config.DATABASE_URL, autocommit=True) as conn:
        conn.execute(SCHEMA)
    _pool = ConnectionPool(
        config.DATABASE_URL,
        min_size=1,
        max_size=5,
        configure=_configure,
        kwargs={"row_factory": dict_row},
        open=True,
    )


def close() -> None:
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None


@contextmanager
def connect():
    if _pool is None:
        init()
    with _pool.connection() as conn:
        yield conn
