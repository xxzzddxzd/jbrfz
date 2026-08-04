"""SQLite account warehouse for generated guests."""
from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional
from zoneinfo import ZoneInfo

from .auth import AccountState
from .constants import ENDPOINT, FALLBACK_RESOURCE_KEY

DEFAULT_DB = Path(__file__).resolve().parent.parent / "data" / "accounts.db"
GUILD_COOLDOWN_SECONDS = 24 * 60 * 60
DAILY_TIMEZONE = ZoneInfo("Asia/Shanghai")

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
    diamond_balance INTEGER NOT NULL DEFAULT 0,
    guild REAL NOT NULL DEFAULT 0,
    daily REAL NOT NULL DEFAULT 0,
    guild_last_id TEXT NOT NULL DEFAULT '',
    guild_joined_at REAL NOT NULL DEFAULT 0,
    guild_left_at REAL NOT NULL DEFAULT 0,
    guild_free_research_total INTEGER NOT NULL DEFAULT 0,
    guild_paid_research_total INTEGER NOT NULL DEFAULT 0,
    guild_effective_research_total INTEGER NOT NULL DEFAULT 0,
    guild_super_success_total INTEGER NOT NULL DEFAULT 0,
    guild_diamond_spent_total INTEGER NOT NULL DEFAULT 0,
    used INTEGER NOT NULL DEFAULT 0,
    ready INTEGER NOT NULL DEFAULT 0,
    invalid INTEGER NOT NULL DEFAULT 0,
    note TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_accounts_used ON accounts(used);
CREATE INDEX IF NOT EXISTS idx_accounts_ready ON accounts(ready);

CREATE TABLE IF NOT EXISTS guild_targets (
    gname TEXT NOT NULL,
    gmname TEXT NOT NULL,
    guild_id TEXT NOT NULL,
    guild_level INTEGER NOT NULL DEFAULT 0,
    member_count INTEGER NOT NULL DEFAULT 0,
    master_user_id TEXT NOT NULL DEFAULT '',
    original_master_mid TEXT NOT NULL DEFAULT '',
    details_json TEXT NOT NULL DEFAULT '{}',
    confirmed_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    PRIMARY KEY (gname, gmname)
);

CREATE TABLE IF NOT EXISTS guild_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mid TEXT NOT NULL,
    guild_id TEXT NOT NULL DEFAULT '',
    joined_at REAL NOT NULL DEFAULT 0,
    left_at REAL NOT NULL DEFAULT 0,
    free_research_count INTEGER NOT NULL DEFAULT 0,
    paid_research_count INTEGER NOT NULL DEFAULT 0,
    free_effective_count INTEGER NOT NULL DEFAULT 0,
    paid_effective_count INTEGER NOT NULL DEFAULT 0,
    effective_research_count INTEGER NOT NULL DEFAULT 0,
    free_super_success_count INTEGER NOT NULL DEFAULT 0,
    paid_super_success_count INTEGER NOT NULL DEFAULT 0,
    super_success_count INTEGER NOT NULL DEFAULT 0,
    diamond_spent INTEGER NOT NULL DEFAULT 0,
    stop_reason TEXT NOT NULL DEFAULT '',
    ok INTEGER NOT NULL DEFAULT 0,
    error TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_guild_runs_mid ON guild_runs(mid, id);

CREATE TABLE IF NOT EXISTS guild_private_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id TEXT NOT NULL,
    gname TEXT NOT NULL,
    gmname TEXT NOT NULL,
    original_master_mid TEXT NOT NULL DEFAULT '',
    controller_mid TEXT NOT NULL,
    application_id TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'created',
    paid_count_per_account INTEGER NOT NULL DEFAULT 0,
    total_count_limit INTEGER NOT NULL DEFAULT 0,
    effective_count INTEGER NOT NULL DEFAULT 0,
    master_acquired_at REAL NOT NULL DEFAULT 0,
    completed_at REAL NOT NULL DEFAULT 0,
    error TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_guild_private_jobs_active
ON guild_private_jobs(guild_id, controller_mid, status, id);

CREATE TABLE IF NOT EXISTS guild_private_accounts (
    job_id INTEGER NOT NULL,
    mid TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'selected',
    invitation_id TEXT NOT NULL DEFAULT '',
    member_state_json TEXT NOT NULL DEFAULT '{}',
    invited_at REAL NOT NULL DEFAULT 0,
    accepted_at REAL NOT NULL DEFAULT 0,
    left_at REAL NOT NULL DEFAULT 0,
    guild_run_id INTEGER NOT NULL DEFAULT 0,
    error TEXT NOT NULL DEFAULT '',
    updated_at REAL NOT NULL,
    PRIMARY KEY (job_id, mid)
);
CREATE INDEX IF NOT EXISTS idx_guild_private_accounts_state
ON guild_private_accounts(job_id, state, mid);
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
    diamond_balance: int
    guild: float
    daily: float
    guild_last_id: str
    guild_joined_at: float
    guild_left_at: float
    guild_free_research_total: int
    guild_paid_research_total: int
    guild_effective_research_total: int
    guild_super_success_total: int
    guild_diamond_spent_total: int
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
            diamond_balance=int(self.diamond_balance),
            email=self.email,
            updated_at=self.updated_at,
        )


@dataclass(frozen=True)
class GuildTargetRow:
    gname: str
    gmname: str
    guild_id: str
    guild_level: int
    member_count: int
    master_user_id: str
    original_master_mid: str
    details: Dict[str, Any]
    confirmed_at: float
    updated_at: float


@dataclass(frozen=True)
class GuildPrivateJobRow:
    id: int
    guild_id: str
    gname: str
    gmname: str
    original_master_mid: str
    controller_mid: str
    application_id: str
    status: str
    paid_count_per_account: int
    total_count_limit: int
    effective_count: int
    master_acquired_at: float
    completed_at: float
    error: str
    created_at: float
    updated_at: float


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
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_accounts_guild ON accounts(guild)"
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_accounts_daily ON accounts(daily)"
        )
        self._conn.commit()

    def _migrate(self) -> None:
        cols = {
            r[1] for r in self._conn.execute("PRAGMA table_info(accounts)").fetchall()
        }
        migrations = {
            "invalid": "INTEGER NOT NULL DEFAULT 0",
            "diamond_balance": "INTEGER NOT NULL DEFAULT 0",
            "guild": "REAL NOT NULL DEFAULT 0",
            "daily": "REAL NOT NULL DEFAULT 0",
            "guild_last_id": "TEXT NOT NULL DEFAULT ''",
            "guild_joined_at": "REAL NOT NULL DEFAULT 0",
            "guild_left_at": "REAL NOT NULL DEFAULT 0",
            "guild_free_research_total": "INTEGER NOT NULL DEFAULT 0",
            "guild_paid_research_total": "INTEGER NOT NULL DEFAULT 0",
            "guild_effective_research_total": "INTEGER NOT NULL DEFAULT 0",
            "guild_super_success_total": "INTEGER NOT NULL DEFAULT 0",
            "guild_diamond_spent_total": "INTEGER NOT NULL DEFAULT 0",
        }
        for column, definition in migrations.items():
            if column not in cols:
                self._conn.execute(
                    f"ALTER TABLE accounts ADD COLUMN {column} {definition}"
                )
        guild_target_cols = {
            r[1]
            for r in self._conn.execute(
                "PRAGMA table_info(guild_targets)"
            ).fetchall()
        }
        if "original_master_mid" not in guild_target_cols:
            self._conn.execute(
                "ALTER TABLE guild_targets ADD COLUMN "
                "original_master_mid TEXT NOT NULL DEFAULT ''"
            )
        self._conn.execute(
            """
            UPDATE guild_targets
            SET original_master_mid=master_user_id
            WHERE original_master_mid='' AND master_user_id<>''
            """
        )
        private_account_cols = {
            r[1]
            for r in self._conn.execute(
                "PRAGMA table_info(guild_private_accounts)"
            ).fetchall()
        }
        if (
            private_account_cols
            and "member_state_json" not in private_account_cols
        ):
            self._conn.execute(
                "ALTER TABLE guild_private_accounts ADD COLUMN "
                "member_state_json TEXT NOT NULL DEFAULT '{}'"
            )
        self._conn.execute(
            """
            UPDATE accounts
            SET guild_left_at=guild
            WHERE guild_left_at<=0 AND guild>0
            """
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
        if state.diamond_balance is None:
            diamond_balance = existing.diamond_balance if existing is not None else 0
        else:
            diamond_balance = max(0, int(state.diamond_balance))
        self._conn.execute(
            """
            INSERT INTO accounts (
                mid, guest_secret, refresh_token, game_access_token, oven_access_token,
                resource_key, endpoint, email, device_json, inviter_mid, next_stage,
                diamond_balance, used, ready, invalid, note, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                diamond_balance=excluded.diamond_balance,
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
                diamond_balance,
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

    def mark_invited(
        self, mid: str, inviter_mid: str, state: Optional[AccountState] = None
    ) -> None:
        """Success path: fill inviter target id, mark used, keep valid."""
        now = time.time()
        if state is not None:
            state.inviter_mid = inviter_mid
            self.upsert_state(
                state,
                used=True,
                ready=True,
                invalid=False,
                note=f"invited:{inviter_mid}",
            )
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

    def list_guild_eligible(
        self,
        *,
        now: Optional[float] = None,
        cooldown_seconds: int = GUILD_COOLDOWN_SECONDS,
    ) -> List[AccountRow]:
        """Ready accounts whose last guild exit is outside the cooldown."""
        current = time.time() if now is None else float(now)
        cutoff = current - max(0, int(cooldown_seconds))
        rows = self._conn.execute(
            """
            SELECT * FROM accounts
            WHERE ready=1 AND invalid=0 AND next_stage>30
              AND guild_joined_at<=guild_left_at
              AND (guild<=0 OR guild<=?)
            ORDER BY
              CASE WHEN guild<=0 THEN 0 ELSE 1 END,
              guild ASC,
              created_at ASC
            """,
            (cutoff,),
        )
        return [self._row(row) for row in rows]

    def list_daily_accounts(
        self,
        *,
        now: Optional[float] = None,
        limit: int = 0,
    ) -> List[AccountRow]:
        """Ready, valid accounts not successfully processed today.

        ``used`` and the guild cooldown intentionally do not affect this pool.
        A day follows the account login timezone, Asia/Shanghai.
        """
        day_start, _ = self._daily_window(now)
        sql = """
            SELECT * FROM accounts
            WHERE ready=1 AND invalid=0 AND next_stage>30
              AND daily<?
            ORDER BY created_at ASC
        """
        params: tuple[float | int, ...] = (day_start,)
        if limit > 0:
            sql += " LIMIT ?"
            params = (day_start, int(limit))
        return [self._row(row) for row in self._conn.execute(sql, params)]

    def daily_pool_status(self, *, now: Optional[float] = None) -> Dict[str, Any]:
        """Return today's eligible and completed daily-account counts."""
        day_start, next_day_start = self._daily_window(now)
        base = "ready=1 AND invalid=0 AND next_stage>30"
        total = self._conn.execute(
            f"SELECT COUNT(*) FROM accounts WHERE {base}"
        ).fetchone()[0]
        eligible = self._conn.execute(
            f"SELECT COUNT(*) FROM accounts WHERE {base} AND daily<?",
            (day_start,),
        ).fetchone()[0]
        completed_today = self._conn.execute(
            f"SELECT COUNT(*) FROM accounts WHERE {base} AND daily>=? AND daily<?",
            (day_start, next_day_start),
        ).fetchone()[0]
        return {
            "day": datetime.fromtimestamp(day_start, DAILY_TIMEZONE).date().isoformat(),
            "timezone": str(DAILY_TIMEZONE),
            "total": int(total),
            "eligible": int(eligible),
            "completed_today": int(completed_today),
        }

    def mark_daily_completed(
        self,
        mid: str,
        *,
        completed_at: Optional[float] = None,
    ) -> float:
        """Record the time a complete daily workflow succeeded."""
        timestamp = time.time() if completed_at is None else float(completed_at)
        self._conn.execute(
            "UPDATE accounts SET daily=?, updated_at=? WHERE mid=?",
            (timestamp, timestamp, mid),
        )
        self._conn.commit()
        return timestamp

    def guild_pool_status(
        self,
        *,
        now: Optional[float] = None,
        cooldown_seconds: int = GUILD_COOLDOWN_SECONDS,
    ) -> Dict[str, Any]:
        current = time.time() if now is None else float(now)
        cutoff = current - max(0, int(cooldown_seconds))
        eligible = self._conn.execute(
            """
            SELECT COUNT(*) FROM accounts
            WHERE ready=1 AND invalid=0 AND next_stage>30
              AND guild_joined_at<=guild_left_at
              AND (guild<=0 OR guild<=?)
            """,
            (cutoff,),
        ).fetchone()[0]
        cooling = self._conn.execute(
            """
            SELECT COUNT(*) FROM accounts
            WHERE ready=1 AND invalid=0 AND next_stage>30
              AND guild_joined_at<=guild_left_at AND guild>?
            """,
            (cutoff,),
        ).fetchone()[0]
        next_row = self._conn.execute(
            """
            SELECT MIN(guild + ?) FROM accounts
            WHERE ready=1 AND invalid=0 AND next_stage>30
              AND guild_joined_at<=guild_left_at AND guild>?
            """,
            (max(0, int(cooldown_seconds)), cutoff),
        ).fetchone()
        return {
            "eligible": int(eligible),
            "cooling": int(cooling),
            "currently_joined": int(
                self._conn.execute(
                    """
                    SELECT COUNT(*) FROM accounts
                    WHERE ready=1 AND invalid=0 AND next_stage>30
                      AND guild_joined_at>guild_left_at
                    """
                ).fetchone()[0]
            ),
            "next_available_at": float(next_row[0])
            if next_row and next_row[0]
            else None,
        }

    def mark_guild_joined(
        self,
        mid: str,
        guild_id: str,
        *,
        joined_at: Optional[float] = None,
    ) -> float:
        """Record an accepted/joined membership without starting exit cooldown."""
        timestamp = time.time() if joined_at is None else float(joined_at)
        self._conn.execute(
            """
            UPDATE accounts
            SET guild_last_id=?, guild_joined_at=?, updated_at=?
            WHERE mid=?
            """,
            (guild_id, timestamp, timestamp, mid),
        )
        self._conn.commit()
        return timestamp

    def mark_guild_left(self, mid: str, *, left_at: Optional[float] = None) -> float:
        timestamp = time.time() if left_at is None else float(left_at)
        self._conn.execute(
            "UPDATE accounts SET guild=?, guild_left_at=?, updated_at=? WHERE mid=?",
            (timestamp, timestamp, timestamp, mid),
        )
        self._conn.commit()
        return timestamp

    def record_guild_run(
        self,
        mid: str,
        *,
        guild_id: str,
        joined_at: Optional[float],
        left_at: Optional[float],
        free_research_count: int,
        paid_research_count: int,
        free_effective_count: int,
        paid_effective_count: int,
        free_super_success_count: int,
        paid_super_success_count: int,
        diamond_spent: int,
        stop_reason: str,
        ok: bool,
        error: str = "",
    ) -> int:
        """Persist one guild workflow and update the account's guild totals."""
        account = self.get(mid)
        if account is None:
            raise KeyError(f"account not found: {mid}")

        joined = max(0.0, float(joined_at or 0))
        left = max(0.0, float(left_at or 0))
        free_count = max(0, int(free_research_count))
        paid_count = max(0, int(paid_research_count))
        free_effective = max(0, int(free_effective_count))
        paid_effective = max(0, int(paid_effective_count))
        effective = free_effective + paid_effective
        free_super = max(0, int(free_super_success_count))
        paid_super = max(0, int(paid_super_success_count))
        super_success = free_super + paid_super
        spent = max(0, int(diamond_spent))
        now = time.time()

        cursor = self._conn.execute(
            """
            INSERT INTO guild_runs (
                mid, guild_id, joined_at, left_at,
                free_research_count, paid_research_count,
                free_effective_count, paid_effective_count,
                effective_research_count,
                free_super_success_count, paid_super_success_count,
                super_success_count, diamond_spent, stop_reason, ok, error,
                created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                mid,
                guild_id,
                joined,
                left,
                free_count,
                paid_count,
                free_effective,
                paid_effective,
                effective,
                free_super,
                paid_super,
                super_success,
                spent,
                stop_reason,
                1 if ok else 0,
                error[:1000],
                now,
            ),
        )
        self._conn.execute(
            """
            UPDATE accounts
            SET guild_last_id=?,
                guild_joined_at=?,
                guild_left_at=?,
                guild=?,
                guild_free_research_total=guild_free_research_total+?,
                guild_paid_research_total=guild_paid_research_total+?,
                guild_effective_research_total=guild_effective_research_total+?,
                guild_super_success_total=guild_super_success_total+?,
                guild_diamond_spent_total=guild_diamond_spent_total+?,
                updated_at=?
            WHERE mid=?
            """,
            (
                guild_id or account.guild_last_id,
                joined or account.guild_joined_at,
                left or account.guild_left_at,
                left or account.guild,
                free_count,
                paid_count,
                effective,
                super_success,
                spent,
                now,
                mid,
            ),
        )
        self._conn.commit()
        return int(cursor.lastrowid)

    def list_guild_runs(self, mid: str, *, limit: int = 20) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM guild_runs WHERE mid=? ORDER BY id DESC"
        params: tuple[Any, ...] = (mid,)
        if limit > 0:
            sql += " LIMIT ?"
            params = (mid, int(limit))
        return [dict(row) for row in self._conn.execute(sql, params)]

    @staticmethod
    def _daily_window(now: Optional[float] = None) -> tuple[float, float]:
        timestamp = time.time() if now is None else float(now)
        current = datetime.fromtimestamp(timestamp, DAILY_TIMEZONE)
        day_start = current.replace(hour=0, minute=0, second=0, microsecond=0)
        next_day_start = day_start + timedelta(days=1)
        return day_start.timestamp(), next_day_start.timestamp()

    def get_guild_target(self, gname: str, gmname: str) -> Optional[GuildTargetRow]:
        row = self._conn.execute(
            "SELECT * FROM guild_targets WHERE gname=? AND gmname=?",
            (gname, gmname),
        ).fetchone()
        return self._guild_target_row(row) if row else None

    def upsert_guild_target(
        self,
        *,
        gname: str,
        gmname: str,
        guild_id: str,
        guild_level: int = 0,
        member_count: int = 0,
        master_user_id: str = "",
        original_master_mid: str = "",
        details: Optional[Dict[str, Any]] = None,
    ) -> GuildTargetRow:
        now = time.time()
        self._conn.execute(
            """
            INSERT INTO guild_targets (
                gname, gmname, guild_id, guild_level, member_count,
                master_user_id, original_master_mid, details_json,
                confirmed_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(gname, gmname) DO UPDATE SET
                guild_id=excluded.guild_id,
                guild_level=excluded.guild_level,
                member_count=excluded.member_count,
                master_user_id=excluded.master_user_id,
                original_master_mid=CASE
                    WHEN guild_targets.original_master_mid<>''
                    THEN guild_targets.original_master_mid
                    ELSE excluded.original_master_mid
                END,
                details_json=excluded.details_json,
                updated_at=excluded.updated_at
            """,
            (
                gname,
                gmname,
                guild_id,
                max(0, int(guild_level)),
                max(0, int(member_count)),
                master_user_id,
                original_master_mid or master_user_id,
                json.dumps(details or {}, ensure_ascii=False),
                now,
                now,
            ),
        )
        self._conn.commit()
        row = self.get_guild_target(gname, gmname)
        if row is None:
            raise RuntimeError("failed to persist guild target")
        return row

    def get_private_job(self, job_id: int) -> Optional[GuildPrivateJobRow]:
        row = self._conn.execute(
            "SELECT * FROM guild_private_jobs WHERE id=?",
            (int(job_id),),
        ).fetchone()
        return self._private_job_row(row) if row else None

    def list_private_jobs(
        self,
        *,
        status: Optional[str] = None,
    ) -> List[GuildPrivateJobRow]:
        """List private-guild jobs using their persistent database ids."""
        sql = "SELECT * FROM guild_private_jobs"
        params: tuple[Any, ...] = ()
        if status is not None:
            sql += " WHERE status=?"
            params = (str(status),)
        sql += " ORDER BY id"
        return [
            self._private_job_row(row)
            for row in self._conn.execute(sql, params)
        ]

    def get_active_private_job(
        self,
        guild_id: str,
        controller_mid: str,
    ) -> Optional[GuildPrivateJobRow]:
        row = self._conn.execute(
            """
            SELECT * FROM guild_private_jobs
            WHERE guild_id=? AND controller_mid=?
              AND status NOT IN ('complete', 'cancelled')
            ORDER BY id DESC LIMIT 1
            """,
            (guild_id, controller_mid),
        ).fetchone()
        return self._private_job_row(row) if row else None

    def get_latest_private_job(
        self,
        guild_id: str,
        controller_mid: str,
    ) -> Optional[GuildPrivateJobRow]:
        """Return the newest job, including completed and cancelled jobs."""
        row = self._conn.execute(
            """
            SELECT * FROM guild_private_jobs
            WHERE guild_id=? AND controller_mid=?
            ORDER BY id DESC LIMIT 1
            """,
            (guild_id, controller_mid),
        ).fetchone()
        return self._private_job_row(row) if row else None

    def create_private_job(
        self,
        *,
        guild_id: str,
        gname: str,
        gmname: str,
        original_master_mid: str,
        controller_mid: str,
        paid_count_per_account: int,
        total_count_limit: int,
    ) -> GuildPrivateJobRow:
        existing = self.get_active_private_job(guild_id, controller_mid)
        if existing is not None:
            return existing
        now = time.time()
        cursor = self._conn.execute(
            """
            INSERT INTO guild_private_jobs (
                guild_id, gname, gmname, original_master_mid, controller_mid,
                paid_count_per_account, total_count_limit,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                guild_id,
                gname,
                gmname,
                original_master_mid,
                controller_mid,
                max(0, int(paid_count_per_account)),
                max(0, int(total_count_limit)),
                now,
                now,
            ),
        )
        self._conn.commit()
        job = self.get_private_job(int(cursor.lastrowid))
        if job is None:
            raise RuntimeError("failed to create private guild job")
        return job

    def update_private_job(self, job_id: int, **changes: Any) -> GuildPrivateJobRow:
        allowed = {
            "application_id",
            "status",
            "effective_count",
            "master_acquired_at",
            "completed_at",
            "error",
        }
        fields = {key: value for key, value in changes.items() if key in allowed}
        if not fields:
            job = self.get_private_job(job_id)
            if job is None:
                raise KeyError(f"private guild job not found: {job_id}")
            return job
        fields["updated_at"] = time.time()
        assignments = ", ".join(f"{key}=?" for key in fields)
        values = list(fields.values()) + [int(job_id)]
        self._conn.execute(
            f"UPDATE guild_private_jobs SET {assignments} WHERE id=?",
            values,
        )
        self._conn.commit()
        job = self.get_private_job(job_id)
        if job is None:
            raise KeyError(f"private guild job not found: {job_id}")
        return job

    def reserve_private_account(self, job_id: int, mid: str) -> Dict[str, Any]:
        now = time.time()
        self._conn.execute(
            """
            INSERT INTO guild_private_accounts (job_id, mid, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(job_id, mid) DO NOTHING
            """,
            (int(job_id), mid, now),
        )
        self._conn.commit()
        row = self.get_private_account(job_id, mid)
        if row is None:
            raise RuntimeError("failed to reserve private guild account")
        return row

    def get_private_account(
        self,
        job_id: int,
        mid: str,
    ) -> Optional[Dict[str, Any]]:
        row = self._conn.execute(
            "SELECT * FROM guild_private_accounts WHERE job_id=? AND mid=?",
            (int(job_id), mid),
        ).fetchone()
        return self._private_account_dict(row) if row else None

    def list_private_accounts(self, job_id: int) -> List[Dict[str, Any]]:
        rows = self._conn.execute(
            """
            SELECT * FROM guild_private_accounts
            WHERE job_id=? ORDER BY updated_at, mid
            """,
            (int(job_id),),
        )
        return [self._private_account_dict(row) for row in rows]

    def update_private_account(
        self,
        job_id: int,
        mid: str,
        **changes: Any,
    ) -> Dict[str, Any]:
        self.reserve_private_account(job_id, mid)
        allowed = {
            "state",
            "invitation_id",
            "member_state_json",
            "invited_at",
            "accepted_at",
            "left_at",
            "guild_run_id",
            "error",
        }
        fields = {key: value for key, value in changes.items() if key in allowed}
        if "member_state" in changes:
            fields["member_state_json"] = json.dumps(
                changes["member_state"] or {},
                ensure_ascii=False,
            )
        fields["updated_at"] = time.time()
        assignments = ", ".join(f"{key}=?" for key in fields)
        values = list(fields.values()) + [int(job_id), mid]
        self._conn.execute(
            f"""
            UPDATE guild_private_accounts SET {assignments}
            WHERE job_id=? AND mid=?
            """,
            values,
        )
        self._conn.commit()
        row = self.get_private_account(job_id, mid)
        if row is None:
            raise RuntimeError("failed to update private guild account")
        return row

    def active_private_account_mids(self) -> set[str]:
        rows = self._conn.execute(
            """
            SELECT DISTINCT a.mid
            FROM guild_private_accounts AS a
            JOIN guild_private_jobs AS j ON j.id=a.job_id
            WHERE j.status NOT IN ('complete', 'cancelled')
              AND a.state NOT IN ('complete', 'failed')
            """
        )
        return {str(row[0]) for row in rows}

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
        used = self._conn.execute(
            "SELECT COUNT(*) FROM accounts WHERE used=1"
        ).fetchone()[0]
        invalid = self._conn.execute(
            "SELECT COUNT(*) FROM accounts WHERE invalid=1"
        ).fetchone()[0]
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
            diamond_balance=int(row["diamond_balance"] or 0),
            guild=float(row["guild"] or 0),
            daily=float(row["daily"] or 0) if "daily" in keys else 0,
            guild_last_id=(
                row["guild_last_id"] or "" if "guild_last_id" in keys else ""
            ),
            guild_joined_at=(
                float(row["guild_joined_at"] or 0)
                if "guild_joined_at" in keys
                else 0
            ),
            guild_left_at=(
                float(row["guild_left_at"] or 0)
                if "guild_left_at" in keys
                else 0
            ),
            guild_free_research_total=(
                int(row["guild_free_research_total"] or 0)
                if "guild_free_research_total" in keys
                else 0
            ),
            guild_paid_research_total=(
                int(row["guild_paid_research_total"] or 0)
                if "guild_paid_research_total" in keys
                else 0
            ),
            guild_effective_research_total=(
                int(row["guild_effective_research_total"] or 0)
                if "guild_effective_research_total" in keys
                else 0
            ),
            guild_super_success_total=(
                int(row["guild_super_success_total"] or 0)
                if "guild_super_success_total" in keys
                else 0
            ),
            guild_diamond_spent_total=(
                int(row["guild_diamond_spent_total"] or 0)
                if "guild_diamond_spent_total" in keys
                else 0
            ),
            used=bool(row["used"]),
            ready=bool(row["ready"]),
            invalid=bool(row["invalid"]) if "invalid" in keys else False,
            note=row["note"] or "",
            created_at=float(row["created_at"] or 0),
            updated_at=float(row["updated_at"] or 0),
        )

    @staticmethod
    def _guild_target_row(row: sqlite3.Row) -> GuildTargetRow:
        try:
            details = json.loads(row["details_json"] or "{}")
        except Exception:
            details = {}
        return GuildTargetRow(
            gname=row["gname"],
            gmname=row["gmname"],
            guild_id=row["guild_id"],
            guild_level=int(row["guild_level"] or 0),
            member_count=int(row["member_count"] or 0),
            master_user_id=row["master_user_id"] or "",
            original_master_mid=(
                row["original_master_mid"] or ""
                if "original_master_mid" in row.keys()
                else row["master_user_id"] or ""
            ),
            details=details if isinstance(details, dict) else {},
            confirmed_at=float(row["confirmed_at"] or 0),
            updated_at=float(row["updated_at"] or 0),
        )

    @staticmethod
    def _private_job_row(row: sqlite3.Row) -> GuildPrivateJobRow:
        return GuildPrivateJobRow(
            id=int(row["id"]),
            guild_id=row["guild_id"] or "",
            gname=row["gname"] or "",
            gmname=row["gmname"] or "",
            original_master_mid=row["original_master_mid"] or "",
            controller_mid=row["controller_mid"] or "",
            application_id=row["application_id"] or "",
            status=row["status"] or "",
            paid_count_per_account=int(row["paid_count_per_account"] or 0),
            total_count_limit=int(row["total_count_limit"] or 0),
            effective_count=int(row["effective_count"] or 0),
            master_acquired_at=float(row["master_acquired_at"] or 0),
            completed_at=float(row["completed_at"] or 0),
            error=row["error"] or "",
            created_at=float(row["created_at"] or 0),
            updated_at=float(row["updated_at"] or 0),
        )

    @staticmethod
    def _private_account_dict(row: sqlite3.Row) -> Dict[str, Any]:
        payload = dict(row)
        try:
            member_state = json.loads(payload.pop("member_state_json") or "{}")
        except Exception:
            member_state = {}
        payload["member_state"] = (
            member_state if isinstance(member_state, dict) else {}
        )
        return payload
