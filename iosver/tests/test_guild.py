from __future__ import annotations

import argparse
import io
import json
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from crumble_bot import cli, pbutil as pb
from crumble_bot.auth import AccountState
from crumble_bot.currency import DIAMOND_CURRENCY_DATA_ID
from crumble_bot.db import AccountDB
from crumble_bot.grpc_client import GrpcError, GrpcResponse
from crumble_bot.guild import (
    ATTEND_GUILD_PATH,
    CONDUCT_FREE_GUILD_LAB_RESEARCH_PATH,
    CONDUCT_PAID_GUILD_LAB_RESEARCH_PATH,
    GET_GUILD_PATH,
    JOIN_GUILD_PATH,
    LEAVE_GUILD_PATH,
    parse_guild_detail_response,
    parse_guild_search_response,
)
from crumble_bot.guild_runner import GuildRunner, GuildWorkflowResult
from crumble_bot.stage_runner import SIGNUP_PATH


def signup_response(diamonds: int) -> bytes:
    currency = b"".join(
        (
            pb.encode_int32_field(1, DIAMOND_CURRENCY_DATA_ID),
            pb.encode_int64_field(2, diamonds),
        )
    )
    inventory = pb.encode_message_field(1, currency)
    crumble = pb.encode_message_field(3, inventory)
    return pb.encode_message_field(3, crumble)


def payment_response(amount: int) -> bytes:
    currency_payment = b"".join(
        (
            pb.encode_int32_field(1, DIAMOND_CURRENCY_DATA_ID),
            pb.encode_int64_field(2, amount),
        )
    )
    payment = pb.encode_message_field(1, currency_payment)
    return pb.encode_message_field(2, payment)


def guild_detail_response() -> bytes:
    settings = b"".join(
        (
            pb.encode_int32_field(1, 101),
            pb.encode_int32_field(2, 202),
            pb.encode_string_field(3, "description"),
        )
    )
    members = pb.encode_repeated_messages(
        1,
        (
            pb.encode_string_field(1, "MASTER"),
            pb.encode_string_field(1, "MEMBER"),
        ),
    )
    experiences = pb.encode_int64_field(1, 33)
    guild = b"".join(
        (
            pb.encode_string_field(1, "ahhhha"),
            pb.encode_message_field(2, settings),
            pb.encode_message_field(3, members),
            pb.encode_message_field(6, experiences),
        )
    )
    return b"".join(
        (
            pb.encode_message_field(2, guild),
            pb.encode_double_field(3, 12345),
            pb.encode_string_field(4, "absdbld"),
        )
    )


def guild_search_response() -> bytes:
    settings = b"".join(
        (
            pb.encode_int32_field(1, 101),
            pb.encode_int32_field(2, 202),
            pb.encode_string_field(3, "description"),
        )
    )
    master = b"".join(
        (
            pb.encode_string_field(1, "MASTER"),
            pb.encode_string_field(2, "absdbld"),
            pb.encode_int32_field(7, 55),
        )
    )
    summary = b"".join(
        (
            pb.encode_string_field(1, "G-ID"),
            pb.encode_string_field(2, "ahhhha"),
            pb.encode_message_field(3, settings),
            pb.encode_int32_field(4, 1),
            pb.encode_message_field(5, master),
            pb.encode_int32_field(6, 1),
            pb.encode_double_field(7, 12345),
        )
    )
    return pb.encode_message_field(1, summary)


class GuildParserTests(unittest.TestCase):
    def test_search_and_detail_parsers(self) -> None:
        settings = b"".join(
            (
                pb.encode_int32_field(1, 101),
                pb.encode_int32_field(2, 202),
                pb.encode_string_field(3, "description"),
                pb.encode_int32_field(4, 1),
            )
        )
        master = b"".join(
            (
                pb.encode_string_field(1, "MASTER"),
                pb.encode_string_field(2, "absdbld"),
                pb.encode_int32_field(7, 55),
            )
        )
        summary = b"".join(
            (
                pb.encode_string_field(1, "G00000000-0000-0000-0000-000000000000"),
                pb.encode_string_field(2, "ahhhha"),
                pb.encode_message_field(3, settings),
                pb.encode_int32_field(4, 3),
                pb.encode_message_field(5, master),
                pb.encode_int32_field(6, 2),
                pb.encode_double_field(7, 12345),
            )
        )
        parsed = parse_guild_search_response(pb.encode_message_field(1, summary))
        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0].name, "ahhhha")
        self.assertEqual(parsed[0].master_name, "absdbld")
        self.assertEqual(parsed[0].member_count, 2)
        self.assertEqual(parsed[0].join_method, 1)
        self.assertEqual(parsed[0].total_combat_power, 12345)

        detail = parse_guild_detail_response(guild_detail_response())
        self.assertEqual(detail.master_name, "absdbld")
        self.assertEqual(detail.member_ids, ("MASTER", "MEMBER"))
        self.assertEqual(detail.total_experience, 33)


class AccountDBGuildTests(unittest.TestCase):
    def test_cooldown_and_target_cache(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "accounts.db"
            with AccountDB(db_path) as db:
                for mid in ("RECENT", "OLD", "NEVER"):
                    db.upsert_state(
                        AccountState(mid=mid, next_stage=31),
                        used=True,
                        ready=True,
                        invalid=False,
                    )
                db.mark_guild_left("RECENT", left_at=100_000)
                db.mark_guild_left("OLD", left_at=1_000)

                eligible = db.list_guild_eligible(now=100_100)
                self.assertEqual([row.mid for row in eligible], ["NEVER", "OLD"])
                status = db.guild_pool_status(now=100_100)
                self.assertEqual(status["eligible"], 2)
                self.assertEqual(status["cooling"], 1)

                target = db.upsert_guild_target(
                    gname="ahhhha",
                    gmname="absdbld",
                    guild_id="G-ID",
                    guild_level=1,
                    member_count=2,
                    details={"confirmed": True},
                )
                confirmed_at = target.confirmed_at
                updated = db.upsert_guild_target(
                    gname="ahhhha",
                    gmname="absdbld",
                    guild_id="G-ID",
                    guild_level=2,
                    member_count=3,
                    details={"members": ["A", "B"]},
                )
                self.assertEqual(updated.confirmed_at, confirmed_at)
                self.assertEqual(updated.guild_level, 2)
                self.assertEqual(updated.details["members"], ["A", "B"])


class FakeWorkflowClient:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.signup_count = 0
        self.paid_count = 0

    def unary(self, path, message, metadata=None):
        self.calls.append(path)
        if path == SIGNUP_PATH:
            self.signup_count += 1
            balance = 900 if self.signup_count == 1 else 860
            return GrpcResponse(signup_response(balance), {}, {})
        if path == GET_GUILD_PATH:
            return GrpcResponse(guild_detail_response(), {}, {})
        if path == CONDUCT_PAID_GUILD_LAB_RESEARCH_PATH:
            self.paid_count += 1
            if self.paid_count == 1:
                return GrpcResponse(payment_response(40), {}, {})
            raise GrpcError(
                9,
                "Not enough resources for payment Some(1464007916). "
                "Owned amount: 860, using amount: 900.",
            )
        return GrpcResponse(b"", {}, {})


class GuildRunnerTests(unittest.TestCase):
    def test_full_sop_until_insufficient_then_leave(self) -> None:
        client = FakeWorkflowClient()
        balances: list[int] = []
        runner = GuildRunner(
            client,
            AccountState(mid="MID", game_access_token="token").to_session(),
            sleep_seconds=0,
            on_balance=balances.append,
        )
        result = runner.run("G00000000-0000-0000-0000-000000000000")
        self.assertTrue(result.ok)
        self.assertEqual(result.free_research_count, 3)
        self.assertEqual(result.paid_research_count, 1)
        self.assertEqual(result.diamond_spent, 40)
        self.assertEqual(result.diamond_balance_final, 860)
        self.assertIn(JOIN_GUILD_PATH, client.calls)
        self.assertIn(ATTEND_GUILD_PATH, client.calls)
        self.assertEqual(client.calls.count(CONDUCT_FREE_GUILD_LAB_RESEARCH_PATH), 3)
        self.assertIn(LEAVE_GUILD_PATH, client.calls)
        self.assertEqual(balances, [900, 860, 860])


class DummyClient:
    def __init__(self, *args, **kwargs) -> None:
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args) -> None:
        pass


class FakeRunner:
    def __init__(self, client, session, *, on_balance=None, **kwargs) -> None:
        self.on_balance = on_balance

    def sync_diamond_balance(self) -> int:
        if self.on_balance:
            self.on_balance(600)
        return 600

    def run(self, guild_id: str) -> GuildWorkflowResult:
        if self.on_balance:
            self.on_balance(100)
        return GuildWorkflowResult(
            joined=True,
            attendance_claimed=True,
            free_research_count=3,
            paid_research_count=1,
            diamond_balance_before_paid=600,
            diamond_spent=500,
            diamond_balance_final=100,
            left_guild=True,
            paid_stop_status=9,
            paid_stop_message="not enough resources 1464007916",
        )


class FakeSearchGuild:
    calls = 0

    def __init__(self, client, session) -> None:
        pass

    def search_guilds(self, query: str) -> GrpcResponse:
        type(self).calls += 1
        return GrpcResponse(guild_search_response(), {}, {})


class GuildCommandTests(unittest.TestCase):
    def test_legacy_database_is_migrated_before_guild_pool_query(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "accounts.db"
            with sqlite3.connect(db_path) as conn:
                conn.executescript(
                    """
                    CREATE TABLE accounts (
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
                        note TEXT NOT NULL DEFAULT '',
                        created_at REAL NOT NULL,
                        updated_at REAL NOT NULL
                    );
                    INSERT INTO accounts (
                        mid, guest_secret, next_stage, used, ready, note,
                        created_at, updated_at
                    ) VALUES (
                        'LEGACY', 'secret', 1, 1, 1, 'keep-me', 100, 200
                    );
                    """
                )

            args = argparse.Namespace(
                gname="ahhhha",
                gmname="absdbld",
                count=1,
                db=str(db_path),
            )
            output = io.StringIO()
            with redirect_stdout(output):
                code = cli.cmd_guild(args)

            self.assertEqual(code, 0)
            summary = json.loads(output.getvalue())
            self.assertEqual(summary["stopped_reason"], "all_accounts_cooling")

            with sqlite3.connect(db_path) as conn:
                columns = {
                    row[1] for row in conn.execute("PRAGMA table_info(accounts)")
                }
                self.assertTrue(
                    {"invalid", "diamond_balance", "guild"}.issubset(columns)
                )
                self.assertIsNotNone(
                    conn.execute(
                        "SELECT 1 FROM sqlite_master "
                        "WHERE type='table' AND name='guild_targets'"
                    ).fetchone()
                )
                legacy = conn.execute(
                    "SELECT note, invalid, diamond_balance, guild "
                    "FROM accounts WHERE mid='LEGACY'"
                ).fetchone()
                self.assertEqual(legacy, ("keep-me", 0, 0, 0.0))

    def test_cached_target_reuses_id_and_marks_two_accounts_cooling(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "accounts.db"
            with AccountDB(db_path) as db:
                for mid in ("A", "B", "COOLING"):
                    db.upsert_state(
                        AccountState(
                            mid=mid,
                            guest_secret="secret",
                            game_access_token="token",
                            next_stage=31,
                        ),
                        used=True,
                        ready=True,
                        invalid=False,
                    )
                db.mark_guild_left("COOLING")
                db.upsert_guild_target(
                    gname="ahhhha",
                    gmname="absdbld",
                    guild_id="G-ID",
                    guild_level=1,
                    member_count=1,
                )

            args = argparse.Namespace(
                gname="ahhhha",
                gmname="absdbld",
                count=2,
                db=str(db_path),
            )
            output = io.StringIO()
            with (
                patch.object(cli, "GrpcClient", DummyClient),
                patch.object(cli, "GuildRunner", FakeRunner),
                patch.object(cli, "_login_guild_account", side_effect=lambda row: row.to_state()),
                patch.object(cli, "_confirm_guild", side_effect=AssertionError("must use cache")),
                redirect_stdout(output),
            ):
                code = cli.cmd_guild(args)

            self.assertEqual(code, 0)
            summary = json.loads(output.getvalue())
            self.assertEqual(summary["count"], 2)
            self.assertEqual(summary["attempted"], 2)
            self.assertEqual(summary["guild"]["source"], "cache")
            with AccountDB(db_path) as db:
                self.assertGreater(db.get("A").guild, 0)
                self.assertGreater(db.get("B").guild, 0)
                self.assertEqual(db.guild_pool_status()["cooling"], 3)

    def test_uncached_target_searches_and_confirms_once(self) -> None:
        FakeSearchGuild.calls = 0
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "accounts.db"
            with AccountDB(db_path) as db:
                db.upsert_state(
                    AccountState(
                        mid="A",
                        guest_secret="secret",
                        game_access_token="token",
                        next_stage=31,
                    ),
                    used=True,
                    ready=True,
                    invalid=False,
                )

            args = argparse.Namespace(
                gname="ahhhha",
                gmname="absdbld",
                count=1,
                db=str(db_path),
            )
            output = io.StringIO()
            with (
                patch.object(cli, "GrpcClient", DummyClient),
                patch.object(cli, "GuildRunner", FakeRunner),
                patch.object(cli, "Guild", FakeSearchGuild),
                patch.object(cli, "_login_guild_account", side_effect=lambda row: row.to_state()),
                patch.object(cli, "_confirm_guild", return_value=True) as confirm,
                redirect_stdout(output),
            ):
                code = cli.cmd_guild(args)

            self.assertEqual(code, 0)
            self.assertEqual(FakeSearchGuild.calls, 1)
            self.assertEqual(confirm.call_count, 1)
            confirmation = confirm.call_args.args[0]
            self.assertEqual(confirmation["name"], "ahhhha")
            self.assertEqual(confirmation["master_name"], "absdbld")
            self.assertEqual(confirmation["guild_level"], 1)
            with AccountDB(db_path) as db:
                target = db.get_guild_target("ahhhha", "absdbld")
                self.assertIsNotNone(target)
                self.assertEqual(target.guild_id, "G-ID")


if __name__ == "__main__":
    unittest.main()
