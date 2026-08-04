from __future__ import annotations

import argparse
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from crumble_bot import cli, pbutil as pb
from crumble_bot.auth import AccountState
from crumble_bot.currency import DIAMOND_CURRENCY_DATA_ID
from crumble_bot.daily_runner import (
    DailyRunner,
    DailyWorkflowResult,
    MailAdvertisementProgress,
    MailboxProgress,
)
from crumble_bot.db import DAILY_TIMEZONE, AccountDB
from crumble_bot.grpc_client import GrpcResponse
from crumble_bot.mailbox import (
    MAIL_ADVERTISEMENT_DATA_ID,
    RECEIVE_MAIL_ADVERTISEMENT_REWARD_PATH,
    RECEIVE_MAIL_REWARDS_PATH,
    REFRESH_MAIL_BOX_PATH,
    MailReward,
)
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


class FakeDailyClient:
    def __init__(self, advertisement_count: int = 0) -> None:
        self.calls: list[str] = []
        self.signup_count = 0
        self.advertisement_count = advertisement_count
        self.signup_balances = (900, 2000) if advertisement_count == 0 else (900, 1000)

    def unary(self, path, message, metadata=None):
        self.calls.append(path)
        if path == SIGNUP_PATH:
            balance = self.signup_balances[self.signup_count]
            self.signup_count += 1
            return GrpcResponse(
                signup_response(balance, self.advertisement_count), {}, {}
            )
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
            viewed = bytes(pb.decode_fields(message)[0][2])
            if pb.decode_fields(viewed) != [(1, 0, MAIL_ADVERTISEMENT_DATA_ID)]:
                raise AssertionError("unexpected advertisement request")
            return GrpcResponse(receive_mail_advertisement_reward_response(), {}, {})
        raise AssertionError(f"unexpected path: {path}")


class DailyRunnerTests(unittest.TestCase):
    def test_daily_login_mail_and_advertisement(self) -> None:
        client = FakeDailyClient()
        balances: list[int] = []
        result = DailyRunner(
            client,
            AccountState(mid="MID", game_access_token="token").to_session(),
            on_balance=balances.append,
        ).run()

        self.assertTrue(result.ok)
        self.assertTrue(result.login_completed)
        self.assertEqual(result.diamond_balance_final, 2000)
        self.assertEqual(result.mailbox.mail_count, 3)
        self.assertEqual(result.mailbox.claimable_count, 1)
        self.assertEqual(result.mailbox.claimed_count, 1)
        self.assertEqual(result.mailbox.diamond_balance_before, 900)
        self.assertEqual(result.mailbox.diamond_balance_after, 2000)
        self.assertEqual(result.mailbox.claimed_rewards, [MailReward(1464007916, 100)])
        advertisement = result.mailbox.advertisement
        self.assertTrue(advertisement.ok)
        self.assertEqual(advertisement.daily_view_count_before, 0)
        self.assertEqual(advertisement.daily_view_count_after, 1)
        self.assertEqual(advertisement.claimed_count, 1)
        self.assertEqual(advertisement.diamond_reward_amount, 1000)
        self.assertEqual(
            client.calls,
            [
                SIGNUP_PATH,
                REFRESH_MAIL_BOX_PATH,
                RECEIVE_MAIL_REWARDS_PATH,
                RECEIVE_MAIL_ADVERTISEMENT_REWARD_PATH,
                SIGNUP_PATH,
            ],
        )
        self.assertEqual(balances, [900, 2000])

    def test_advertisement_at_daily_limit_is_skipped(self) -> None:
        client = FakeDailyClient(advertisement_count=1)
        result = DailyRunner(
            client,
            AccountState(mid="MID", game_access_token="token").to_session(),
        ).run()

        self.assertTrue(result.ok)
        advertisement = result.mailbox.advertisement
        self.assertEqual(advertisement.daily_view_count_before, 1)
        self.assertEqual(advertisement.claimable_count, 0)
        self.assertEqual(advertisement.claim_requested_count, 0)
        self.assertNotIn(RECEIVE_MAIL_ADVERTISEMENT_REWARD_PATH, client.calls)


class DummyClient:
    def __init__(self, *args, **kwargs) -> None:
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args) -> None:
        pass


class FakeDailyRunner:
    def __init__(self, client, session, *, on_balance=None) -> None:
        self.on_balance = on_balance

    def run(self) -> DailyWorkflowResult:
        if self.on_balance:
            self.on_balance(600)
        return DailyWorkflowResult(
            login_completed=True,
            diamond_balance_final=600,
            mailbox=MailboxProgress(
                checked=True,
                diamond_balance_before=600,
                diamond_balance_after=600,
                advertisement=MailAdvertisementProgress(
                    checked=True,
                    daily_view_count_before=1,
                    daily_view_count_after=1,
                ),
            ),
        )


class DailyCommandTests(unittest.TestCase):
    def test_daily_pool_uses_shanghai_calendar_day(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "accounts.db"
            with AccountDB(db_path) as db:
                for mid in ("NEVER", "TODAY", "YESTERDAY"):
                    db.upsert_state(
                        AccountState(mid=mid, next_stage=31),
                        ready=True,
                        invalid=False,
                    )

                now = datetime(2026, 8, 4, 16, tzinfo=DAILY_TIMEZONE).timestamp()
                today = datetime(2026, 8, 4, 8, tzinfo=DAILY_TIMEZONE).timestamp()
                yesterday = datetime(
                    2026, 8, 3, 23, 59, tzinfo=DAILY_TIMEZONE
                ).timestamp()
                db.mark_daily_completed("TODAY", completed_at=today)
                db.mark_daily_completed("YESTERDAY", completed_at=yesterday)

                rows = db.list_daily_accounts(now=now)
                status = db.daily_pool_status(now=now)

            self.assertEqual([row.mid for row in rows], ["NEVER", "YESTERDAY"])
            self.assertEqual(status["day"], "2026-08-04")
            self.assertEqual(status["timezone"], "Asia/Shanghai")
            self.assertEqual(status["total"], 3)
            self.assertEqual(status["eligible"], 2)
            self.assertEqual(status["completed_today"], 1)

    def test_daily_uses_ready_accounts_regardless_of_used_or_guild_cooldown(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "accounts.db"
            with AccountDB(db_path) as db:
                for mid in ("A", "B"):
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
                db.mark_guild_left("B")

            args = argparse.Namespace(db=str(db_path))
            output = io.StringIO()
            with (
                patch.object(cli, "GrpcClient", DummyClient),
                patch.object(cli, "DailyRunner", FakeDailyRunner),
                patch.object(
                    cli, "_login_account", side_effect=lambda row: row.to_state()
                ),
                redirect_stdout(output),
            ):
                code = cli.cmd_daily(args)

            self.assertEqual(code, 0)
            summary = json.loads(output.getvalue())
            self.assertEqual(summary["count"], 2)
            self.assertEqual(summary["attempted"], 2)
            self.assertEqual(summary["skipped_today"], 0)
            self.assertEqual(summary["pool_after"]["completed_today"], 2)
            self.assertEqual(summary["totals"]["login_completed_count"], 2)
            self.assertEqual(summary["totals"]["mailbox_checked_count"], 2)
            self.assertNotIn("guild_progress", summary["results"][0])
            with AccountDB(db_path) as db:
                self.assertEqual(db.get("A").diamond_balance, 600)
                self.assertEqual(db.get("B").diamond_balance, 600)
                self.assertGreater(db.get("A").daily, 0)
                self.assertGreater(db.get("B").daily, 0)

            second_output = io.StringIO()
            with (
                patch.object(
                    cli,
                    "_login_account",
                    side_effect=AssertionError("today's accounts must be skipped"),
                ),
                redirect_stdout(second_output),
            ):
                second_code = cli.cmd_daily(args)

            self.assertEqual(second_code, 0)
            second_summary = json.loads(second_output.getvalue())
            self.assertEqual(second_summary["count"], 0)
            self.assertEqual(second_summary["attempted"], 0)
            self.assertEqual(second_summary["skipped_today"], 2)
            self.assertEqual(
                second_summary["stopped_reason"],
                "all_accounts_completed_today",
            )

    def test_daily_command_has_no_count_argument(self) -> None:
        args = cli.build_parser().parse_args(["daily"])
        self.assertEqual(args.cmd, "daily")
        self.assertFalse(hasattr(args, "count"))


if __name__ == "__main__":
    unittest.main()
