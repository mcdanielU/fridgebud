"""SQLite storage. One file per deployment; ':memory:' for tests."""
from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime

from .models import Household, Item

SCHEMA = """
CREATE TABLE IF NOT EXISTS households (
    id INTEGER PRIMARY KEY,
    phone TEXT UNIQUE NOT NULL,
    name TEXT DEFAULT '',
    timezone TEXT DEFAULT 'America/Toronto',
    nudge_hour INTEGER DEFAULT 16,
    nudge_minute INTEGER DEFAULT 30,
    dislikes TEXT DEFAULT '[]',
    diet_notes TEXT DEFAULT '',
    persona TEXT DEFAULT '{}',        -- what they love, avoid and when they eat, from a voice note
    opted_in INTEGER DEFAULT 1,
    last_nudge_on TEXT,
    last_suggestion TEXT
);
CREATE TABLE IF NOT EXISTS items (
    id INTEGER PRIMARY KEY,
    household_id INTEGER NOT NULL REFERENCES households(id),
    name TEXT NOT NULL,
    key TEXT,
    category TEXT,
    kind TEXT NOT NULL,
    added_on TEXT NOT NULL,
    portions INTEGER,
    status TEXT NOT NULL DEFAULT 'active',
    closed_on TEXT,
    confirmed_good_on TEXT,
    price REAL
);
-- Learned spoil rates: the per-home data a general assistant does not have.
CREATE TABLE IF NOT EXISTS spoil_rates (
    household_id INTEGER NOT NULL,
    category TEXT NOT NULL,
    multiplier REAL NOT NULL DEFAULT 1.0,
    observations INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (household_id, category)
);
-- Every inbound/outbound message and state change, for the scorecard.
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    household_id INTEGER,
    at TEXT NOT NULL,
    type TEXT NOT NULL,
    payload TEXT DEFAULT '{}'
);
"""


def _d(value: str | None) -> date | None:
    return date.fromisoformat(value) if value else None


class Store:
    def __init__(self, path: str = ":memory:"):
        # The web server handles texts on worker threads; the service layer
        # serialises access with one lock, so sharing the connection is safe.
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        cols = {r["name"] for r in self.db.execute("PRAGMA table_info(items)")}
        if "price" not in cols:  # databases created before photo capture
            self.db.execute("ALTER TABLE items ADD COLUMN price REAL")
            self.db.commit()
        hh_cols = {r["name"] for r in self.db.execute("PRAGMA table_info(households)")}
        if "diet_notes" not in hh_cols:  # databases created before diet/cuisine notes
            self.db.execute("ALTER TABLE households ADD COLUMN diet_notes TEXT DEFAULT ''")
            self.db.commit()
        if "persona" not in hh_cols:  # databases created before the voice-note food persona
            self.db.execute("ALTER TABLE households ADD COLUMN persona TEXT DEFAULT '{}'")
            self.db.commit()

    # households -------------------------------------------------------
    def get_household_by_phone(self, phone: str) -> Household | None:
        row = self.db.execute("SELECT * FROM households WHERE phone = ?", (phone,)).fetchone()
        return self._household(row) if row else None

    def create_household(self, phone: str) -> Household:
        cur = self.db.execute("INSERT INTO households (phone) VALUES (?)", (phone,))
        self.db.commit()
        return self.get_household(cur.lastrowid)

    def get_household(self, household_id: int) -> Household:
        row = self.db.execute("SELECT * FROM households WHERE id = ?", (household_id,)).fetchone()
        return self._household(row)

    def all_households(self) -> list[Household]:
        return [self._household(r) for r in self.db.execute("SELECT * FROM households")]

    def save_household(self, h: Household) -> None:
        self.db.execute(
            """UPDATE households SET name=?, timezone=?, nudge_hour=?, nudge_minute=?, dislikes=?, diet_notes=?, persona=?,
               opted_in=?, last_nudge_on=?, last_suggestion=? WHERE id=?""",
            (h.name, h.timezone, h.nudge_hour, h.nudge_minute, json.dumps(h.dislikes), h.diet_notes, json.dumps(h.persona), int(h.opted_in),
             h.last_nudge_on.isoformat() if h.last_nudge_on else None,
             json.dumps(h.last_suggestion) if h.last_suggestion else None, h.id),
        )
        self.db.commit()

    @staticmethod
    def _household(row: sqlite3.Row) -> Household:
        return Household(
            id=row["id"], phone=row["phone"], name=row["name"], timezone=row["timezone"],
            nudge_hour=row["nudge_hour"], nudge_minute=row["nudge_minute"],
            dislikes=json.loads(row["dislikes"] or "[]"), diet_notes=row["diet_notes"] or "", persona=json.loads(row["persona"] or "{}"), opted_in=bool(row["opted_in"]),
            last_nudge_on=_d(row["last_nudge_on"]),
            last_suggestion=json.loads(row["last_suggestion"]) if row["last_suggestion"] else None,
        )

    # items ------------------------------------------------------------
    def add_item(self, household_id: int, name: str, key: str | None, category: str | None,
                 kind: str, added_on: date, portions: int | None = None, price: float | None = None) -> Item:
        cur = self.db.execute(
            "INSERT INTO items (household_id, name, key, category, kind, added_on, portions, price) VALUES (?,?,?,?,?,?,?,?)",
            (household_id, name, key, category, kind, added_on.isoformat(), portions, price),
        )
        self.db.commit()
        return self.get_item(cur.lastrowid)

    def get_item(self, item_id: int) -> Item:
        return self._item(self.db.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone())

    def active_items(self, household_id: int) -> list[Item]:
        rows = self.db.execute(
            "SELECT * FROM items WHERE household_id = ? AND status = 'active' ORDER BY added_on, id",
            (household_id,),
        )
        return [self._item(r) for r in rows]

    def items_closed_between(self, household_id: int, start: date, end: date) -> list[Item]:
        rows = self.db.execute(
            "SELECT * FROM items WHERE household_id = ? AND status != 'active' AND closed_on BETWEEN ? AND ?",
            (household_id, start.isoformat(), end.isoformat()),
        )
        return [self._item(r) for r in rows]

    def close_item(self, item_id: int, status: str, on: date) -> None:
        self.db.execute("UPDATE items SET status = ?, closed_on = ? WHERE id = ?", (status, on.isoformat(), item_id))
        self.db.commit()

    def confirm_good(self, item_id: int, on: date) -> None:
        self.db.execute("UPDATE items SET confirmed_good_on = ? WHERE id = ?", (on.isoformat(), item_id))
        self.db.commit()

    @staticmethod
    def _item(row: sqlite3.Row) -> Item:
        return Item(
            id=row["id"], household_id=row["household_id"], name=row["name"], key=row["key"],
            category=row["category"], kind=row["kind"], added_on=date.fromisoformat(row["added_on"]),
            portions=row["portions"], status=row["status"], closed_on=_d(row["closed_on"]),
            confirmed_good_on=_d(row["confirmed_good_on"]), price=row["price"],
        )

    # learned spoil rates ---------------------------------------------
    def get_rate(self, household_id: int, category: str) -> tuple[float, int]:
        row = self.db.execute(
            "SELECT multiplier, observations FROM spoil_rates WHERE household_id = ? AND category = ?",
            (household_id, category),
        ).fetchone()
        return (row["multiplier"], row["observations"]) if row else (1.0, 0)

    def set_rate(self, household_id: int, category: str, multiplier: float, observations: int) -> None:
        self.db.execute(
            """INSERT INTO spoil_rates (household_id, category, multiplier, observations) VALUES (?,?,?,?)
               ON CONFLICT(household_id, category) DO UPDATE SET multiplier=excluded.multiplier,
               observations=excluded.observations""",
            (household_id, category, multiplier, observations),
        )
        self.db.commit()

    # events -----------------------------------------------------------
    def log(self, household_id: int | None, at: datetime, type_: str, **payload) -> None:
        self.db.execute(
            "INSERT INTO events (household_id, at, type, payload) VALUES (?,?,?,?)",
            (household_id, at.isoformat(), type_, json.dumps(payload, default=str)),
        )
        self.db.commit()

    def events(self, household_id: int | None = None, type_: str | None = None) -> list[dict]:
        sql, args = "SELECT * FROM events WHERE 1=1", []
        if household_id is not None:
            sql += " AND household_id = ?"
            args.append(household_id)
        if type_:
            sql += " AND type = ?"
            args.append(type_)
        return [dict(r) | {"payload": json.loads(r["payload"])} for r in self.db.execute(sql + " ORDER BY id", args)]
