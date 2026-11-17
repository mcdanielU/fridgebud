"""Launch data that sits beside the engine's tables.

The engine knows food. This knows the account: which voice a household was
assigned (the Bud vs plain test), where they are in onboarding, when and how
they consented (CASL record), their invite code, who else gets the texts for
the same fridge, and trial/payment state.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from .store import Store

SCHEMA = """
CREATE TABLE IF NOT EXISTS profiles (
    household_id INTEGER PRIMARY KEY REFERENCES households(id),
    voice TEXT NOT NULL,              -- 'bud' or 'plain'
    stage TEXT NOT NULL DEFAULT 'new',-- new | size | active
    size INTEGER,                     -- people eating from this fridge
    source TEXT NOT NULL,             -- sms | web | legacy
    created_at TEXT NOT NULL,
    consented_at TEXT,
    code TEXT UNIQUE NOT NULL,        -- invite / referral code
    referred_by INTEGER,              -- household id of the referrer
    awaiting TEXT,                    -- e.g. 'dollars' after a receipt asks for an estimate
    last_receipt_on TEXT,
    paywall_sent_at TEXT,
    paid_at TEXT
);
-- Other people who eat from the same fridge and get the same texts.
CREATE TABLE IF NOT EXISTS members (
    phone TEXT PRIMARY KEY,
    household_id INTEGER NOT NULL REFERENCES households(id),
    status TEXT NOT NULL DEFAULT 'pending',  -- pending | active | stopped
    invited_at TEXT NOT NULL,
    consented_at TEXT
);
"""

_DIGITS = "0123456789ABCDEFGHJKLMNPQRSTUVWXYZ"  # no I or O: easier to read aloud


def code_for(household_id: int) -> str:
    """Short, unique, non-sequential-looking code, e.g. household 3 -> 'B4T'."""
    n, out = household_id * 131 + 1009, ""
    while n:
        n, r = divmod(n, len(_DIGITS))
        out = _DIGITS[r] + out
    return "B" + out


def _dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


@dataclass
class Profile:
    household_id: int
    voice: str
    stage: str
    size: int | None
    source: str
    created_at: datetime
    consented_at: datetime | None
    code: str
    referred_by: int | None
    awaiting: str | None
    last_receipt_on: date | None
    paywall_sent_at: datetime | None
    paid_at: datetime | None


@dataclass
class Member:
    phone: str
    household_id: int
    status: str


class Accounts:
    def __init__(self, store: Store):
        self.db = store.db
        self.db.executescript(SCHEMA)

    # profiles ---------------------------------------------------------
    def get(self, household_id: int) -> Profile | None:
        row = self.db.execute("SELECT * FROM profiles WHERE household_id = ?", (household_id,)).fetchone()
        return self._profile(row) if row else None

    def by_code(self, code: str) -> Profile | None:
        row = self.db.execute("SELECT * FROM profiles WHERE code = ?", (code.upper(),)).fetchone()
        return self._profile(row) if row else None

    def create(self, household_id: int, source: str, now: datetime, referred_by: int | None = None,
               stage: str = "new", consented_at: datetime | None = None) -> Profile:
        # Alternate by id: an even 50/50 split without storing a random seed.
        voice = "bud" if household_id % 2 == 0 else "plain"
        self.db.execute(
            "INSERT INTO profiles (household_id, voice, stage, source, created_at, consented_at, code, referred_by)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (household_id, voice, stage, source, now.isoformat(),
             consented_at.isoformat() if consented_at else None, code_for(household_id), referred_by),
        )
        self.db.commit()
        return self.get(household_id)

    def save(self, p: Profile) -> None:
        self.db.execute(
            """UPDATE profiles SET voice=?, stage=?, size=?, consented_at=?, referred_by=?, awaiting=?,
               last_receipt_on=?, paywall_sent_at=?, paid_at=? WHERE household_id=?""",
            (p.voice, p.stage, p.size, p.consented_at.isoformat() if p.consented_at else None, p.referred_by,
             p.awaiting, p.last_receipt_on.isoformat() if p.last_receipt_on else None,
             p.paywall_sent_at.isoformat() if p.paywall_sent_at else None,
             p.paid_at.isoformat() if p.paid_at else None, p.household_id),
        )
        self.db.commit()

    def all(self) -> list[Profile]:
        return [self._profile(r) for r in self.db.execute("SELECT * FROM profiles ORDER BY household_id")]

    @staticmethod
    def _profile(row) -> Profile:
        return Profile(
            household_id=row["household_id"], voice=row["voice"], stage=row["stage"], size=row["size"],
            source=row["source"], created_at=_dt(row["created_at"]), consented_at=_dt(row["consented_at"]),
            code=row["code"], referred_by=row["referred_by"], awaiting=row["awaiting"],
            last_receipt_on=date.fromisoformat(row["last_receipt_on"]) if row["last_receipt_on"] else None,
            paywall_sent_at=_dt(row["paywall_sent_at"]), paid_at=_dt(row["paid_at"]),
        )

    # members ----------------------------------------------------------
    def member(self, phone: str) -> Member | None:
        row = self.db.execute("SELECT * FROM members WHERE phone = ?", (phone,)).fetchone()
        return Member(row["phone"], row["household_id"], row["status"]) if row else None

    def invite_member(self, phone: str, household_id: int, now: datetime) -> Member:
        self.db.execute("INSERT INTO members (phone, household_id, invited_at) VALUES (?,?,?)",
                        (phone, household_id, now.isoformat()))
        self.db.commit()
        return self.member(phone)

    def set_member_status(self, phone: str, status: str, now: datetime) -> None:
        if status == "active":
            self.db.execute("UPDATE members SET status = ?, consented_at = ? WHERE phone = ?",
                            (status, now.isoformat(), phone))
        else:
            self.db.execute("UPDATE members SET status = ? WHERE phone = ?", (status, phone))
        self.db.commit()

    def active_member_phones(self, household_id: int) -> list[str]:
        rows = self.db.execute("SELECT phone FROM members WHERE household_id = ? AND status = 'active' ORDER BY phone",
                               (household_id,))
        return [r["phone"] for r in rows]

    def member_count(self, household_id: int) -> int:
        return len(self.active_member_phones(household_id))
