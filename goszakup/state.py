"""SQLite checkpoint. One database per date and mode; writes are transactional."""
import json
import sqlite3
from pathlib import Path


class State:
    def __init__(self, path: Path, signature: str, resume: bool):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS announcements (id INTEGER PRIMARY KEY, payload TEXT NOT NULL, done INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS candidates (id INTEGER PRIMARY KEY, announcement_id INTEGER, payload TEXT NOT NULL, done INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS lots (id INTEGER PRIMARY KEY, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS review (id INTEGER PRIMARY KEY, payload TEXT NOT NULL, reason TEXT NOT NULL);
        """)
        if not resume:
            with self.db:
                for table in ("meta", "announcements", "candidates", "lots", "review"):
                    self.db.execute(f"DELETE FROM {table}")
        old = self.get("signature")
        if old is not None and old != signature:
            self.db.close()
            raise ValueError("Checkpoint создан для других параметров; запустите без --resume")
        self.set("signature", signature)

    def get(self, key, default=None):
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set(self, key, value):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (key, json.dumps(value, ensure_ascii=False)))

    def prune_platform(self, allowed_ids):
        """Remove previously checkpointed records from unverified platforms."""
        removed = {}
        with self.db:
            for table in ("announcements", "candidates", "lots", "review"):
                invalid = []
                for row_id, payload in self.db.execute(f"SELECT id,payload FROM {table}"):
                    row = json.loads(payload)
                    valid = row.get("systemId") in allowed_ids
                    if valid and table in {"lots", "review"}:
                        announcement = row.get("TrdBuy")
                        valid = isinstance(announcement, dict) and announcement.get("systemId") in allowed_ids
                    if not valid:
                        invalid.append((row_id,))
                if invalid:
                    self.db.executemany(f"DELETE FROM {table} WHERE id=?", invalid)
                removed[table] = len(invalid)
        return removed

    def add_announcements(self, rows, cursor, scanned=None):
        with self.db:
            inserted = self.db.executemany("INSERT OR IGNORE INTO announcements(id,payload) VALUES (?,?)",
                                           ((r["id"], json.dumps(r, ensure_ascii=False)) for r in rows)).rowcount
            duplicates = self.get("duplicate_ids", 0) + len(rows) - inserted
            self.db.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", ("duplicate_ids", json.dumps(duplicates)))
            scanned_total = self.get("announcements_scanned", 0) + (len(rows) if scanned is None else scanned)
            self.db.execute("INSERT OR REPLACE INTO meta VALUES (?,?)",
                            ("announcements_scanned", json.dumps(scanned_total)))
            self.db.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", ("announcement_cursor", json.dumps(cursor)))

    def advance_announcement_batch(self, next_index):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO meta VALUES (?,?)",
                            ("kato_batch_index", json.dumps(next_index)))
            self.db.execute("DELETE FROM meta WHERE key='announcement_cursor'")

    def pending_announcements(self, limit):
        rows = self.db.execute("SELECT id,payload FROM announcements WHERE done=0 ORDER BY id LIMIT ?", (limit,)).fetchall()
        return [(id_, json.loads(payload)) for id_, payload in rows]

    def add_candidates(self, rows, announcement_ids, scanned=None):
        with self.db:
            inserted = self.db.executemany("INSERT OR IGNORE INTO candidates(id,announcement_id,payload) VALUES (?,?,?)",
                                           ((r["id"], r.get("trdBuyId"), json.dumps(r, ensure_ascii=False)) for r in rows)).rowcount
            duplicates = self.get("duplicate_ids", 0) + len(rows) - inserted
            self.db.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", ("duplicate_ids", json.dumps(duplicates)))
            scanned_total = self.get("lots_scanned", 0) + (len(rows) if scanned is None else scanned)
            self.db.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", ("lots_scanned", json.dumps(scanned_total)))
            self.db.executemany("UPDATE announcements SET done=1 WHERE id=?", ((i,) for i in announcement_ids))

    def pending_candidates(self, limit):
        rows = self.db.execute("SELECT id,payload FROM candidates WHERE done=0 ORDER BY id LIMIT ?", (limit,)).fetchall()
        return [(id_, json.loads(payload)) for id_, payload in rows]

    def excluded_candidates(self):
        rows = self.db.execute("""SELECT payload FROM candidates
                                  WHERE id NOT IN (SELECT id FROM lots)
                                    AND id NOT IN (SELECT id FROM review)""")
        return [json.loads(payload) for (payload,) in rows]

    def add_result(self, lot_id, payload, classification, reason=""):
        with self.db:
            if classification == "match":
                self.db.execute("INSERT OR REPLACE INTO lots VALUES (?,?)", (lot_id, json.dumps(payload, ensure_ascii=False)))
            elif classification == "review":
                self.db.execute("INSERT OR REPLACE INTO review VALUES (?,?,?)",
                                (lot_id, json.dumps(payload, ensure_ascii=False), reason))
            self.db.execute("UPDATE candidates SET done=1 WHERE id=?", (lot_id,))

    def promote_review(self, lot_id, payload):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO lots VALUES (?,?)",
                            (lot_id, json.dumps(payload, ensure_ascii=False)))
            self.db.execute("DELETE FROM review WHERE id=?", (lot_id,))

    def results(self, table):
        if table == "review":
            return [(json.loads(payload), reason) for payload, reason in
                    self.db.execute("SELECT payload,reason FROM review ORDER BY id")]
        if table == "lots":
            return [json.loads(payload) for (payload,) in self.db.execute("SELECT payload FROM lots ORDER BY id")]
        raise ValueError(table)

    def count(self, table):
        if table not in {"announcements", "candidates", "lots", "review"}:
            raise ValueError(table)
        return self.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

    def close(self):
        self.db.close()

