"""
db.py — one small connection wrapper so the rest of the code doesn't care
whether it's talking to SQLite (local testing) or Postgres (hosting).

- If the DATABASE_URL environment variable is set, we connect to Postgres.
- Otherwise we use a local SQLite file.

All SQL in this project is written with `?` placeholders; for Postgres the
wrapper rewrites them to `%s`. Everything else we use (CREATE TABLE IF NOT
EXISTS, INSERT ... ON CONFLICT DO NOTHING) is valid in both databases.
"""

import os
import sqlite3


def using_postgres() -> bool:
    return bool(os.environ.get("DATABASE_URL"))


class Conn:
    def __init__(self, raw, is_pg: bool):
        self.raw = raw
        self.is_pg = is_pg

    def execute(self, sql: str, params=()):
        if self.is_pg:
            sql = sql.replace("?", "%s")
        return self.raw.execute(sql, params)

    def commit(self):
        self.raw.commit()

    def close(self):
        self.raw.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            if exc_type is None:
                self.raw.commit()
        finally:
            self.raw.close()
        return False


def connect(sqlite_path: str = "flaky.db") -> Conn:
    url = os.environ.get("DATABASE_URL")
    if url:
        import psycopg  # imported lazily so local SQLite use needs no extra package

        return Conn(psycopg.connect(url, connect_timeout=15), is_pg=True)
    return Conn(sqlite3.connect(sqlite_path), is_pg=False)
