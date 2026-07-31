"""SQLite account warehouse for generated guests."""
from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

from .auth import AccountState
from .constants import ENDPOINT, FALLBACK_RESOURCE_KEY

DEFAULT_DB = Path(__file__).resolve().parent.parent / "data" / "accounts.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    mid TEXT PRIMARY KEY,
    guest_secret TEXT NOT NULL DEFAULT '',
    refresh_token TEXT NOT NULL DEFAULT '',
    game_access_token TEXT NOT NULL DEFAULT '',
    oven_access_token TEXT NOT NULL DEFAULT '',
    resource_key TEXT NOT NULL DEFAULT '',
    endpoint TEXT NOT NULL DEFAULT '',
    email TEXT NOT NULL DEFAULT '',
    device_json TEXT NOT NULL DEFAULT '{}',
    inviter_mid TEXT NOT NULL DEFAULT '',
    next_stage INTEGER NOT NULL DEFAULT 1,
    used INTEGER NOT NULL DEFAULT 0,
    ready INTEGER NOT NULL DEFAULT 0,
    invalid INTEGER NOT NULL DEFAULT 0,
    note TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_accounts_used ON accounts(used);
CREATE INDEX IF NOT EXISTS idx_accounts_ready ON accounts(ready);
"""


@dataclass
class AccountRow:
    mid: str
    guest_secret: str
    refresh_token: str
    game_access_token: str
    oven_access_token: str
    resource_key: str
    endpoint: str
    email: str
    device: Dict[str, Any]
    inviter_mid: str
    next_stage: int
    used: bool
    ready: bool
    invalid: bool
    note: str
    created_at: float
    updated_at: float

    def to_state(self) -> AccountState:
        return AccountState(
            mid=self.mid,
            guest_secret=self.guest_secret,
            refresh_token=self.refresh_token,
            game_access_token=self.game_access_token,
            oven_access_token=self.oven_access_token,
            resource_key=self.resource_key or FALLBACK_RESOURCE_KEY,
            device=dict(self.device or {}),
            endpoint=self.endpoint or ENDPOINT,
            inviter_mid=self.inviter_mid,
            next_stage=int(self.next_stage or 1),
            email=self.email,
            updated_at=self.updated_at,
        )


class AccountDB:
    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path or DEFAULT_DB)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path))
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.executescript(SCHEMA)
        self._migrate()
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_accounts_pool ON accounts(used, invalid, ready)"
        )
        self._conn.commit()

    def _migrate(self) -> None:
        cols = {r[1] for r in self._conn.execute("PRAGMA table_info(accounts)").fetchall()}
        if "invalid" not in cols:
            self._conn.execute(
                "ALTER TABLE accounts ADD COLUMN invalid INTEGER NOT NULL DEFAULT 0"
            )

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "AccountDB":
        return self

    def __exit__(self, *args) -> None:
        self.close()

    def upsert_state(
        self,
        state: AccountState,
        *,
        used: Optional[bool] = None,
        ready: Optional[bool] = None,
        invalid: Optional[bool] = None,
        note: Optional[str] = None,
    ) -> None:
        now = time.time()
        existing = self.get(state.mid)
        if existing is None:
            used_v = 0 if used is None else (1 if used else 0)
            ready_v = 0 if ready is None else (1 if ready else 0)
            invalid_v = 0 if invalid is None else (1 if invalid else 0)
            note_v = note or ""
            created = now
        else:
            used_v = existing.used if used is None else used
            used_v = 1 if used_v else 0
            ready_v = existing.ready if ready is None else ready
            ready_v = 1 if ready_v else 0
            invalid_v = existing.invalid if invalid is None else invalid
            invalid_v = 1 if invalid_v else 0
            note_v = existing.note if note is None else note
            created = existing.created_at

        device_json = json.dumps(state.device or {}, ensure_ascii=False)
        self._conn.execute(
            """
            INSERT INTO accounts (
                mid, guest_secret, refresh_token, game_access_token, oven_access_token,
                resource_key, endpoint, email, device_json, inviter_mid, next_stage,
                used, ready, invalid, note, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(mid) DO UPDATE SET
                guest_secret=excluded.guest_secret,
                refresh_token=excluded.refresh_token,
                game_access_token=excluded.game_access_token,
                oven_access_token=excluded.oven_access_token,
                resource_key=excluded.resource_key,
                endpoint=excluded.endpoint,
                email=excluded.email,
                device_json=excluded.device_json,
                inviter_mid=excluded.inviter_mid,
                next_stage=excluded.next_stage,
                used=excluded.used,
                ready=excluded.ready,
                invalid=excluded.invalid,
                note=excluded.note,
                updated_at=excluded.updated_at
            """,
            (
                state.mid,
                state.guest_secret or "",
                state.refresh_token or "",
                state.game_access_token or "",
                state.oven_access_token or "",
                state.resource_key or FALLBACK_RESOURCE_KEY,
                state.endpoint or ENDPOINT,
                state.email or "",
                device_json,
                state.inviter_mid or "",
                int(state.next_stage or 1),
                used_v,
                ready_v,
                invalid_v,
                note_v,
                created,
                now,
            ),
        )
        self._conn.commit()

    def claim_unused(self, *, require_ready: bool = True) -> Optional[AccountRow]:
        """Pick oldest unused, valid account (optionally ready)."""
        sql = """
            SELECT * FROM accounts
            WHERE used=0 AND invalid=0
        """
        if require_ready:
            sql += " AND ready=1"
        sql += " ORDER BY created_at ASC LIMIT 1"
        row = self._conn.execute(sql).fetchone()
        return self._row(row) if row else None

    def mark_used(self, mid: str, used: bool = True) -> None:
        self._conn.execute(
            "UPDATE accounts SET used=?, updated_at=? WHERE mid=?",
            (1 if used else 0, time.time(), mid),
        )
        self._conn.commit()

    def mark_ready(self, mid: str, ready: bool = True) -> None:
        self._conn.execute(
            "UPDATE accounts SET ready=?, updated_at=? WHERE mid=?",
            (1 if ready else 0, time.time(), mid),
        )
        self._conn.commit()

    def mark_invalid(self, mid: str, note: str = "invalid") -> None:
        self._conn.execute(
            """
            UPDATE accounts
            SET invalid=1, used=1, note=?, updated_at=?
            WHERE mid=?
            """,
            (note[:500], time.time(), mid),
        )
        self._conn.commit()

    def mark_invited(self, mid: str, inviter_mid: str, state: Optional[AccountState] = None) -> None:
        """Success path: fill inviter target id, mark used, keep valid."""
        now = time.time()
        if state is not None:
            state.inviter_mid = inviter_mid
            self.upsert_state(state, used=True, ready=True, invalid=False, note=f"invited:{inviter_mid}")
            return
        self._conn.execute(
            """
            UPDATE accounts
            SET inviter_mid=?, used=1, invalid=0, note=?, updated_at=?
            WHERE mid=?
            """,
            (inviter_mid, f"invited:{inviter_mid}", now, mid),
        )
        self._conn.commit()

    def get(self, mid: str) -> Optional[AccountRow]:
        cur = self._conn.execute("SELECT * FROM accounts WHERE mid=?", (mid,))
        row = cur.fetchone()
        return self._row(row) if row else None

    def list_unused(
        self, *, ready_only: bool = False, limit: int = 0, include_invalid: bool = False
    ) -> List[AccountRow]:
        sql = "SELECT * FROM accounts WHERE used=0"
        if not include_invalid:
            sql += " AND invalid=0"
        if ready_only:
            sql += " AND ready=1"
        sql += " ORDER BY created_at ASC"
        if limit and limit > 0:
            sql += f" LIMIT {int(limit)}"
        return [self._row(r) for r in self._conn.execute(sql)]

    def count(self) -> Dict[str, int]:
        total = self._conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0]
        used = self._conn.execute("SELECT COUNT(*) FROM accounts WHERE used=1").fetchone()[0]
        invalid = self._conn.execute("SELECT COUNT(*) FROM accounts WHERE invalid=1").fetchone()[0]
        ready_unused = self._conn.execute(
            "SELECT COUNT(*) FROM accounts WHERE ready=1 AND used=0 AND invalid=0"
        ).fetchone()[0]
        unused = self._conn.execute(
            "SELECT COUNT(*) FROM accounts WHERE used=0 AND invalid=0"
        ).fetchone()[0]
        return {
            "total": int(total),
            "used": int(used),
            "unused": int(unused),
            "ready_unused": int(ready_unused),
            "invalid": int(invalid),
        }

    def iter_all(self) -> Iterator[AccountRow]:
        for row in self._conn.execute("SELECT * FROM accounts ORDER BY created_at ASC"):
            yield self._row(row)

    @staticmethod
    def _row(row: sqlite3.Row) -> AccountRow:
        keys = row.keys()
        try:
            device = json.loads(row["device_json"] or "{}")
        except Exception:
            device = {}
        return AccountRow(
            mid=row["mid"],
            guest_secret=row["guest_secret"],
            refresh_token=row["refresh_token"],
            game_access_token=row["game_access_token"],
            oven_access_token=row["oven_access_token"],
            resource_key=row["resource_key"],
            endpoint=row["endpoint"],
            email=row["email"],
            device=device if isinstance(device, dict) else {},
            inviter_mid=row["inviter_mid"],
            next_stage=int(row["next_stage"] or 1),
            used=bool(row["used"]),
            ready=bool(row["ready"]),
            invalid=bool(row["invalid"]) if "invalid" in keys else False,
            note=row["note"] or "",
            created_at=float(row["created_at"] or 0),
            updated_at=float(row["updated_at"] or 0),
        )
