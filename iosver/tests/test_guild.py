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
from crumble_bot.guild_runner import (
    GuildProgress,
    GuildRunner,
    GuildWorkflowResult,
)
from crumble_bot.mailbox import (
    MAIL_ADVERTISEMENT_DATA_ID,
    RECEIVE_MAIL_ADVERTISEMENT_REWARD_PATH,
    RECEIVE_MAIL_REWARDS_PATH,
    REFRESH_MAIL_BOX_PATH,
    MailReward,
    parse_receive_mail_advertisement_reward_response,
    parse_receive_mail_rewards_response,
    parse_refresh_mail_box_response,
    parse_signup_mail_advertisement_view_count,
)
from crumble_bot.messages import receive_mail_advertisement_reward_request
from crumble_bot.stage_runner import SIGNUP_PATH


def signup_response(diamonds: int, advertisement_count: int = 0) -> bytes:
    currency = b"".join(
        (
            pb.encode_int32_field(1, DIAMOND_CURRENCY_DATA_ID),
            pb.encode_int64_field(2, diamonds),
        )
    )
    inventory = pb.encode_message_field(1, currency)
    advertisement_counter = b"".join(
        (
            pb.encode_int32_field(1, MAIL_ADVERTISEMENT_DATA_ID),
            pb.encode_int64_field(2, advertisement_count),
        )
    )
    daily_counters = pb.encode_message_field(3, advertisement_counter)
    progress = pb.encode_message_field(2, daily_counters)
    crumble = b"".join(
        (
            pb.encode_message_field(3, inventory),
            pb.encode_message_field(5, progress),
        )
    )
    return pb.encode_message_field(3, crumble)


def guild_member_state(
    *,
    level: int,
    free_count: int,
    paid_count: int,
) -> bytes:
    return b"".join(
        (
            pb.encode_int32_field(7, level),
            pb.encode_int32_field(10, free_count),
            pb.encode_int32_field(12, paid_count),
        )
    )


def guild_progression(
    previous_experience: int,
    current_experience: int,
    previous_contribution: int,
    current_contribution: int,
) -> bytes:
    return b"".join(
        (
            pb.encode_int64_field(1, previous_experience),
            pb.encode_int64_field(2, current_experience),
            pb.encode_int64_field(3, previous_contribution),
            pb.encode_int64_field(4, current_contribution),
        )
    )


def guild_lab_research(previous_point: int, current_point: int) -> bytes:
    return b"".join(
        (
            pb.encode_int64_field(1, previous_point),
            pb.encode_int64_field(2, current_point),
        )
    )


def join_guild_response() -> bytes:
    return pb.encode_message_field(
        2,
        guild_member_state(level=1, free_count=0, paid_count=0),
    )


def attend_guild_response() -> bytes:
    return b"".join(
        (
            pb.encode_message_field(3, guild_progression(20, 21, 0, 1)),
            pb.encode_message_field(
                4,
                guild_member_state(level=1, free_count=0, paid_count=0),
            ),
        )
    )


def free_research_response(index: int) -> bytes:
    previous_experience = 20 + index
    previous_contribution = index
    previous_point = 90 + index * 10
    return b"".join(
        (
            pb.encode_message_field(
                2,
                guild_progression(
                    previous_experience,
                    previous_experience + 1,
                    previous_contribution,
                    previous_contribution + 1,
                ),
            ),
            pb.encode_message_field(
                3,
                guild_lab_research(previous_point, previous_point + 10),
            ),
            pb.encode_message_field(
                4,
                guild_member_state(level=1, free_count=index, paid_count=0),
            ),
            pb.encode_bool_field(5, index == 2),
        )
    )


def payment_response(amount: int) -> bytes:
    currency_payment = b"".join(
        (
            pb.encode_int32_field(1, DIAMOND_CURRENCY_DATA_ID),
            pb.encode_int64_field(2, amount),
        )
    )
    payment = pb.encode_message_field(1, currency_payment)
    return b"".join(
        (
            pb.encode_message_field(2, payment),
            pb.encode_message_field(3, guild_progression(24, 30, 4, 10)),
            pb.encode_message_field(4, guild_lab_research(130, 150)),
            pb.encode_bool_field(5, True),
            pb.encode_message_field(
                6,
                guild_member_state(level=2, free_count=3, paid_count=1),
            ),
        )
    )


def guild_detail_response(total_experience: int = 33) -> bytes:
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
    experiences = pb.encode_int64_field(1, total_experience)
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


def mail_entry(
    mail_id: str,
    *,
    rewards: tuple[tuple[int, int], ...] = (),
    is_rewarded: bool = False,
) -> bytes:
    reward_messages = (
        b"".join(
            (
                pb.encode_int32_field(1, item_data_id),
                pb.encode_int64_field(2, amount),
            )
        )
        for item_data_id, amount in rewards
    )
    return b"".join(
        (
            pb.encode_string_field(1, mail_id),
            pb.encode_repeated_messages(3, reward_messages),
            pb.encode_bool_field(4, is_rewarded),
        )
    )


def refresh_mail_box_response() -> bytes:
    mail_box = pb.encode_repeated_messages(
        1,
        (
            mail_entry(
                "MAIL-1",
                rewards=((DIAMOND_CURRENCY_DATA_ID, 100),),
            ),
            mail_entry(
                "MAIL-2",
                rewards=((DIAMOND_CURRENCY_DATA_ID, 10),),
                is_rewarded=True,
            ),
            mail_entry("MAIL-3"),
        ),
    )
    return pb.encode_message_field(1, mail_box)


def receive_mail_rewards_response() -> bytes:
    generic_reward = pb.encode_int32_field(1, 1)
    updated_mail = mail_entry(
        "MAIL-1",
        rewards=((DIAMOND_CURRENCY_DATA_ID, 100),),
        is_rewarded=True,
    )
    return b"".join(
        (
            pb.encode_message_field(2, generic_reward),
            pb.encode_message_field(3, updated_mail),
        )
    )


def receive_mail_advertisement_reward_response() -> bytes:
    currency_reward = b"".join(
        (
            pb.encode_int32_field(1, DIAMOND_CURRENCY_DATA_ID),
            pb.encode_int64_field(2, 1000),
        )
    )
    reward_element = pb.encode_message_field(1, currency_reward)
    reward = pb.encode_message_field(1, reward_element)
    return pb.encode_message_field(2, reward)


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

    def test_mailbox_parsers_find_only_unclaimed_attachment_mail(self) -> None:
        snapshot = parse_refresh_mail_box_response(refresh_mail_box_response())
        self.assertEqual(len(snapshot.mails), 3)
        self.assertEqual(
            [mail.mail_id for mail in snapshot.claimable_mails],
            ["MAIL-1"],
        )
        self.assertEqual(
            snapshot.claimable_mails[0].rewards,
            (MailReward(DIAMOND_CURRENCY_DATA_ID, 100),),
        )

        received = parse_receive_mail_rewards_response(receive_mail_rewards_response())
        self.assertEqual(received.reward_count, 1)
        self.assertEqual(len(received.updated_mails), 1)
        self.assertTrue(received.updated_mails[0].is_rewarded)

        self.assertEqual(
            parse_signup_mail_advertisement_view_count(signup_response(900, 1)),
            1,
        )
        advertisement = parse_receive_mail_advertisement_reward_response(
            receive_mail_advertisement_reward_response()
        )
        self.assertEqual(advertisement.reward_count, 1)
        self.assertEqual(
            advertisement.currency_rewards,
            (MailReward(DIAMOND_CURRENCY_DATA_ID, 1000),),
        )

    def test_mail_advertisement_request_exposes_optional_skip_count(self) -> None:
        request = receive_mail_advertisement_reward_request(MAIL_ADVERTISEMENT_DATA_ID)
        root_fields = pb.decode_fields(request)
        self.assertEqual(len(root_fields), 1)
        viewed_fields = pb.decode_fields(bytes(root_fields[0][2]))
        self.assertEqual(viewed_fields, [(1, 0, MAIL_ADVERTISEMENT_DATA_ID)])

        with_explicit_zero = receive_mail_advertisement_reward_request(
            MAIL_ADVERTISEMENT_DATA_ID,
            skip_count=0,
        )
        viewed_fields = pb.decode_fields(
            bytes(pb.decode_fields(with_explicit_zero)[0][2])
        )
        self.assertEqual(
            viewed_fields,
            [(1, 0, MAIL_ADVERTISEMENT_DATA_ID), (2, 0, 0)],
        )


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
        self.get_guild_count = 0
        self.free_count = 0
        self.paid_count = 0
        self.requests: list[tuple[str, bytes]] = []
        self.signup_balances = (900, 860)

    def unary(self, path, message, metadata=None):
        self.calls.append(path)
        self.requests.append((path, message))
        if path == SIGNUP_PATH:
            self.signup_count += 1
            balance = self.signup_balances[self.signup_count - 1]
            return GrpcResponse(signup_response(balance), {}, {})
        if path == REFRESH_MAIL_BOX_PATH:
            return GrpcResponse(refresh_mail_box_response(), {}, {})
        if path == RECEIVE_MAIL_REWARDS_PATH:
            mail_ids = [
                bytes(value).decode("utf-8")
                for field_number, wire_type, value in pb.decode_fields(message)
                if field_number == 1 and wire_type == 2
            ]
            if mail_ids != ["MAIL-1"]:
                raise AssertionError(f"unexpected mail ids: {mail_ids}")
            return GrpcResponse(receive_mail_rewards_response(), {}, {})
        if path == RECEIVE_MAIL_ADVERTISEMENT_REWARD_PATH:
            root_fields = pb.decode_fields(message)
            if len(root_fields) != 1 or root_fields[0][:2] != (1, 2):
                raise AssertionError("unexpected advertisement request envelope")
            viewed_fields = pb.decode_fields(bytes(root_fields[0][2]))
            if viewed_fields != [(1, 0, MAIL_ADVERTISEMENT_DATA_ID)]:
                raise AssertionError(
                    f"unexpected viewed advertisement: {viewed_fields}"
                )
            return GrpcResponse(receive_mail_advertisement_reward_response(), {}, {})
        if path == JOIN_GUILD_PATH:
            return GrpcResponse(join_guild_response(), {}, {})
        if path == GET_GUILD_PATH:
            self.get_guild_count += 1
            experience = 20 if self.get_guild_count == 1 else 30
            return GrpcResponse(guild_detail_response(experience), {}, {})
        if path == ATTEND_GUILD_PATH:
            return GrpcResponse(attend_guild_response(), {}, {})
        if path == CONDUCT_FREE_GUILD_LAB_RESEARCH_PATH:
            self.free_count += 1
            return GrpcResponse(free_research_response(self.free_count), {}, {})
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
            initial_diamond_balance=900,
        )
        result = runner.run("G00000000-0000-0000-0000-000000000000")
        self.assertTrue(result.ok)
        self.assertEqual(result.free_research_count, 3)
        self.assertEqual(result.paid_research_count, 1)
        self.assertEqual(result.diamond_spent, 40)
        self.assertEqual(result.diamond_balance_before_paid, 900)
        self.assertEqual(result.diamond_balance_final, 860)
        self.assertNotIn("mailbox", result.to_dict())
        self.assertEqual(result.to_dict()["donation_count"], 1)
        self.assertEqual(result.guild_progress.level_before, 1)
        self.assertEqual(result.guild_progress.level_after, 2)
        self.assertEqual(result.guild_progress.experience_before, 20)
        self.assertEqual(result.guild_progress.experience_after, 30)
        self.assertEqual(result.guild_progress.member_contribution_before, 0)
        self.assertEqual(result.guild_progress.member_contribution_after, 10)
        self.assertEqual(result.guild_progress.research_point_before, 100)
        self.assertEqual(result.guild_progress.research_point_after, 150)
        self.assertEqual(result.guild_progress.daily_free_research_count_after, 3)
        self.assertEqual(result.guild_progress.daily_donation_count_after, 1)
        self.assertEqual(result.guild_progress.super_success_count, 2)
        progress = result.to_dict()["guild_progress"]
        self.assertEqual(progress["level_change"], 1)
        self.assertEqual(progress["experience_gained"], 10)
        self.assertEqual(progress["member_contribution_gained"], 10)
        self.assertEqual(progress["research_point_gained"], 50)
        self.assertEqual(client.calls[0], JOIN_GUILD_PATH)
        self.assertNotIn(REFRESH_MAIL_BOX_PATH, client.calls)
        self.assertNotIn(RECEIVE_MAIL_REWARDS_PATH, client.calls)
        self.assertNotIn(RECEIVE_MAIL_ADVERTISEMENT_REWARD_PATH, client.calls)
        self.assertNotIn(SIGNUP_PATH, client.calls)
        self.assertIn(JOIN_GUILD_PATH, client.calls)
        self.assertIn(ATTEND_GUILD_PATH, client.calls)
        self.assertEqual(client.calls.count(CONDUCT_FREE_GUILD_LAB_RESEARCH_PATH), 3)
        self.assertEqual(client.calls.count(GET_GUILD_PATH), 2)
        self.assertIn(LEAVE_GUILD_PATH, client.calls)
        self.assertEqual(balances, [860, 860])


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
            guild_progress=GuildProgress(
                level_before=1,
                level_after=2,
                experience_before=20,
                experience_after=30,
                member_contribution_before=0,
                member_contribution_after=10,
                research_point_before=100,
                research_point_after=150,
                daily_free_research_count_before=0,
                daily_free_research_count_after=3,
                daily_donation_count_before=0,
                daily_donation_count_after=1,
                super_success_count=1,
            ),
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
                    {"invalid", "diamond_balance", "guild", "daily"}.issubset(columns)
                )
                self.assertIsNotNone(
                    conn.execute(
                        "SELECT 1 FROM sqlite_master "
                        "WHERE type='table' AND name='guild_targets'"
                    ).fetchone()
                )
                legacy = conn.execute(
                    "SELECT note, invalid, diamond_balance, guild, daily "
                    "FROM accounts WHERE mid='LEGACY'"
                ).fetchone()
                self.assertEqual(legacy, ("keep-me", 0, 0, 0.0, 0.0))

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
                patch.object(
                    cli, "_login_account", side_effect=lambda row: row.to_state()
                ),
                patch.object(
                    cli, "_confirm_guild", side_effect=AssertionError("must use cache")
                ),
                redirect_stdout(output),
            ):
                code = cli.cmd_guild(args)

            self.assertEqual(code, 0)
            summary = json.loads(output.getvalue())
            self.assertEqual(summary["count"], 2)
            self.assertEqual(summary["attempted"], 2)
            self.assertEqual(summary["guild"]["source"], "cache")
            self.assertEqual(summary["guild"]["level_before"], 1)
            self.assertEqual(summary["guild"]["level_after"], 2)
            self.assertEqual(summary["guild"]["level_change"], 1)
            self.assertEqual(summary["totals"]["donation_count"], 2)
            self.assertNotIn("mailbox_checked_count", summary["totals"])
            self.assertNotIn("mailbox", summary["results"][0])
            self.assertEqual(summary["totals"]["diamond_spent"], 1000)
            self.assertEqual(summary["totals"]["guild_experience_gained"], 20)
            self.assertEqual(summary["totals"]["research_point_gained"], 100)
            self.assertEqual(summary["results"][0]["donation_count"], 1)
            self.assertEqual(
                summary["results"][0]["guild_progress"]["experience_gained"],
                10,
            )
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
                patch.object(
                    cli, "_login_account", side_effect=lambda row: row.to_state()
                ),
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
