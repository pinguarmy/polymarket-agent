"""SQLite database helpers for the BTC 5-minute collector."""

import sqlite3
from pathlib import Path


class Database:
    """Small SQLite database manager for collector storage."""

    def __init__(self, db_path: str = "data/btc5m.db"):
        self.project_root = Path(__file__).resolve().parent.parent
        path = Path(db_path)
        if not path.is_absolute():
            path = self.project_root / path
        self.db_path = path

    def get_connection(self) -> sqlite3.Connection:
        """Return a SQLite connection with dict-like row access enabled."""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def init_schema(self) -> None:
        """Create all database tables from the root schema.sql file."""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        schema_path = self.project_root / "schema.sql"

        with open(schema_path, encoding="utf-8") as schema_file:
            schema_sql = schema_file.read()

        with self.get_connection() as conn:
            conn.executescript(schema_sql)
            conn.commit()
