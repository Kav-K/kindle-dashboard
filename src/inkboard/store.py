"""Private bounded SQLite cache and at-most-once command dispatch receipts."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import threading
import time


def utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def encode(value) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"), sort_keys=True)


class Store:
    def __init__(self, directory: Path):
        from .config import check_private_access, private_directory
        if directory.is_symlink():
            raise ValueError("State directory must not be a symbolic link")
        if directory.exists():
            check_private_access(directory)
        else:
            private_directory(directory)
        path = directory / "inkboard.sqlite3"
        if path.is_symlink():
            raise ValueError("State database must not be a symbolic link")
        if path.exists():
            check_private_access(path)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(path, check_same_thread=False, timeout=5)
        if os.name != "nt":
            os.chmod(path, 0o600)
        else:
            check_private_access(path)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA busy_timeout=5000")
        self.db.executescript("""
          CREATE TABLE IF NOT EXISTS snapshots (id TEXT PRIMARY KEY, updated REAL NOT NULL, document TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS commands (id TEXT PRIMARY KEY, created REAL NOT NULL, fingerprint TEXT NOT NULL, document TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS command_ids (id TEXT PRIMARY KEY, created REAL NOT NULL, fingerprint TEXT NOT NULL, identity TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS notices (id TEXT PRIMARY KEY, updated REAL NOT NULL, document TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS settings (id TEXT PRIMARY KEY, updated REAL NOT NULL, document TEXT NOT NULL);
        """)
        # A crash after send and before acknowledgement is never a retry signal.
        for ident, document in self.db.execute("SELECT id, document FROM commands").fetchall():
            value = json.loads(document)
            if value["state"] in {"queued", "claimed"}:
                value.update(state="unknown", detail="Hub restarted before confirmation. Check the device before sending a new command.", updated_at=utc())
                self.db.execute("UPDATE commands SET document=? WHERE id=?", (encode(value), ident))
        for ident, created, fingerprint, document in self.db.execute("SELECT id,created,fingerprint,document FROM commands").fetchall():
            value = json.loads(document)
            identity = {key: value[key] for key in ("id", "printer_id", "module_id", "action")}
            self.db.execute("INSERT OR IGNORE INTO command_ids VALUES(?,?,?,?)", (ident, created, fingerprint, encode(identity)))
        self.db.commit()

    def snapshot(self, module_id: str, document: dict) -> None:
        data = encode(document)
        if len(data.encode("utf-8")) > 262144:
            raise ValueError("Adapter snapshot exceeds the size limit")
        with self.lock, self.db:
            self.db.execute("INSERT INTO snapshots VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET updated=excluded.updated,document=excluded.document", (module_id, time.time(), data))

    def snapshots(self) -> dict:
        with self.lock:
            return {ident: json.loads(document) for ident, document in self.db.execute("SELECT id,document FROM snapshots")}

    def create_command(self, ident: str, fingerprint: str, value: dict) -> tuple[dict, bool]:
        with self.lock, self.db:
            prior = self.known_command(ident, fingerprint)
            if prior:
                return prior, False
            if self.db.execute("SELECT count(*) FROM command_ids").fetchone()[0] >= 100000:
                raise ValueError("Command identity capacity reached. No further commands can be sent until an operator archives this setup safely.")
            identity = {key: value[key] for key in ("id", "printer_id", "module_id", "action")}
            self.db.execute("INSERT INTO command_ids VALUES(?,?,?,?)", (ident, time.time(), fingerprint, encode(identity)))
            self.db.execute("INSERT INTO commands VALUES(?,?,?,?)", (ident, time.time(), fingerprint, encode(value)))
            # Pending operations are never pruned to make room for more commands.
            self.db.execute("DELETE FROM commands WHERE id IN (SELECT id FROM commands ORDER BY created DESC LIMIT -1 OFFSET 1000) AND json_extract(document,'$.state') NOT IN ('queued','claimed')")
            return value, True

    def known_command(self, ident: str, fingerprint: str | None = None) -> dict | None:
        with self.lock:
            known = self.db.execute("SELECT fingerprint,identity FROM command_ids WHERE id=?", (ident,)).fetchone()
            if known is None:
                return None
            if fingerprint is not None and known[0] != fingerprint:
                raise ValueError("This request id already describes a different command")
            row = self.db.execute("SELECT document FROM commands WHERE id=?", (ident,)).fetchone()
            if row:
                return json.loads(row[0])
            value = json.loads(known[1])
            value.update(state="expired", detail="Detailed receipt expired. This request ID is retained and will never be dispatched again.")
            return value

    def set_command(self, ident: str, **fields) -> dict:
        with self.lock, self.db:
            row = self.db.execute("SELECT document FROM commands WHERE id=?", (ident,)).fetchone()
            if not row:
                raise KeyError(ident)
            value = json.loads(row[0])
            value.update(fields, updated_at=utc())
            self.db.execute("UPDATE commands SET document=? WHERE id=?", (encode(value), ident))
            return value

    def command(self, ident: str) -> dict | None:
        return self.known_command(ident)

    def notices(self):
        with self.lock:
            return {ident: json.loads(document) for ident, document in self.db.execute("SELECT id,document FROM notices")}

    def notice(self, ident, document):
        with self.lock, self.db:
            self.db.execute("INSERT INTO notices VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET updated=excluded.updated,document=excluded.document", (ident, time.time(), encode(document)))
            self.db.execute("DELETE FROM notices WHERE id IN (SELECT id FROM notices ORDER BY updated DESC LIMIT -1 OFFSET 128)")

    def setting(self, ident, document=None):
        with self.lock:
            if document is not None:
                with self.db:
                    self.db.execute("INSERT INTO settings VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET updated=excluded.updated,document=excluded.document", (ident, time.time(), encode(document)))
                    self.db.execute("DELETE FROM settings WHERE id IN (SELECT id FROM settings WHERE id LIKE 'cutoff:%' ORDER BY updated DESC LIMIT -1 OFFSET 256)")
                return document
            row = self.db.execute("SELECT document FROM settings WHERE id=?", (ident,)).fetchone()
            return json.loads(row[0]) if row else None

    def close(self) -> None:
        with self.lock:
            self.db.close()
