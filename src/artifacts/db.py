from collections.abc import Iterator
from contextlib import contextmanager
from importlib import resources

import psycopg

SCHEMA_VERSION = 1


class Database:
    def __init__(self, dsn: str) -> None:
        self.dsn = dsn

    @contextmanager
    def connect(self) -> Iterator[psycopg.Connection]:
        with psycopg.connect(self.dsn, autocommit=True) as conn:
            yield conn

    def migrate(self) -> None:
        sql = resources.files("artifacts").joinpath("schema.sql").read_text()
        with self.connect() as conn:
            conn.execute(sql)

    def reset(self) -> None:
        with self.connect() as conn:
            conn.execute("drop schema public cascade; create schema public;")
        self.migrate()
