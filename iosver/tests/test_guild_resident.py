from __future__ import annotations

import io
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from crumble_bot import cli, pbutil as pb
from crumble_bot.auth import AccountState
from crumble_bot.db import AccountDB
from crumble_bot.grpc_client import GrpcResponse
from crumble_bot.guild_calendar import guild_daily_count, guild_day_key
from crumble_bot.guild import (
    APPLY_GUILD_PATH,
    GET_GUILD_APPLICATIONS_FOR_USER_PATH,
    GET_GUILD_SUPPORT_REQUESTS_PATH,
    JOIN_GUILD_PATH,
    GuildActionResult,
    GuildMemberStateSnapshot,
)
from crumble_bot.guild_resident_runner import ResidentGuildRunner, resident_day_key
from crumble_bot.guild_runner import GuildProgress
from crumble_bot.social import GET_USER_SOCIAL_INFO_PATH


def _join_response() -> bytes:
    member_state = b"".join(
        (
            pb.encode_int32_field(7, 1),
            pb.encode_int32_field(10, 0),
            pb.encode_int32_field(12, 0),
        )
    )
    return pb.encode_message_field(2, member_state)


class _PublicJoinClient:
    def __init__(self, _endpoint: str) -> None:
        pass

    def __enter__(self) -> "_PublicJoinClient":
        return self

    def __exit__(self, *args) -> None:
        return None

    def unary(self, path: str, _message: bytes, *, metadata=None) -> GrpcResponse:
        if path != JOIN_GUILD_PATH:
            raise AssertionError(path)
        return GrpcResponse(_join_response(), {}, {})


class _PrivateApplyClient:
    def __init__(self, _endpoint: str) -> None:
        pass

    def __enter__(self) -> "_PrivateApplyClient":
        return self

    def __exit__(self, *args) -> None:
        return None

    def unary(self, path: str, _message: bytes, *, metadata=None) -> GrpcResponse:
        if path == GET_USER_SOCIAL_INFO_PATH:
            entries = []
            for index in range(4):
                entries.append(
                    pb.encode_message_field(
                        1,
                        b"".join(
                            (
                                pb.encode_string_field(1, f"BOT{index}"),
                                pb.encode_int32_field(2, 31),
                                pb.encode_string_field(3, f"bot-{index}"),
                            )
                        ),
                    )
                )
            return GrpcResponse(b"".join(entries), {}, {})
        if path == GET_GUILD_APPLICATIONS_FOR_USER_PATH:
            return GrpcResponse(b"", {}, {})
        if path == APPLY_GUILD_PATH:
            return GrpcResponse(pb.encode_string_field(1, "APP1"), {}, {})
        raise AssertionError(path)


def _pending_application_response(guild_id: str = "G1") -> bytes:
    settings = pb.encode_int32_field(4, 1)
    master = b"".join(
        (
            pb.encode_string_field(1, "OWNER"),
            pb.encode_string_field(2, "owner"),
        )
    )
    guild = b"".join(
        (
            pb.encode_string_field(1, guild_id),
            pb.encode_string_field(2, "ahhhha"),
            pb.encode_message_field(3, settings),
            pb.encode_int32_field(4, 1),
            pb.encode_message_field(5, master),
        )
    )
    application = b"".join(
        (
            pb.encode_string_field(1, "APP-SERVER"),
            pb.encode_message_field(3, guild),
        )
    )
    return pb.encode_message_field(1, application)


class _ExistingPrivateApplicationClient:
    apply_calls = 0

    def __init__(self, _endpoint: str) -> None:
        pass

    def __enter__(self) -> "_ExistingPrivateApplicationClient":
        return self

    def __exit__(self, *args) -> None:
        return None

    def unary(self, path: str, _message: bytes, *, metadata=None) -> GrpcResponse:
        if path == GET_GUILD_APPLICATIONS_FOR_USER_PATH:
            return GrpcResponse(_pending_application_response(), {}, {})
        if path == APPLY_GUILD_PATH:
            type(self).apply_calls += 1
            raise AssertionError("existing application must not be submitted again")
        raise AssertionError(path)


class ResidentGuildTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tempdir.name) / "accounts.db"

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    @staticmethod
    def _login(row):
        return row.to_state()

    def _add_accounts(self, db: AccountDB, count: int = 4) -> None:
        for index in range(count):
            db.upsert_state(
                AccountState(
                    mid=f"BOT{index}",
                    guest_secret="secret",
                    game_access_token="token",
                    resource_key="game-data-test",
                    next_stage=31,
                ),
                ready=True,
                invalid=False,
            )

    def test_guild_day_rolls_over_at_kst_midnight(self) -> None:
        before = datetime(2026, 8, 10, 14, 59, tzinfo=timezone.utc).timestamp()
        after = datetime(2026, 8, 10, 15, 0, tzinfo=timezone.utc).timestamp()

        self.assertEqual(guild_day_key(before), "2026-08-10")
        self.assertEqual(guild_day_key(after), "2026-08-11")
        self.assertEqual(resident_day_key(after), "2026-08-11")

    def test_guild_daily_count_uses_server_action_day(self) -> None:
        previous_day = int(
            datetime(2026, 8, 10, 14, 59, tzinfo=timezone.utc).timestamp()
            * 1000
        )
        current_day = int(
            datetime(2026, 8, 10, 15, 1, tzinfo=timezone.utc).timestamp()
            * 1000
        )
        now = datetime(2026, 8, 10, 15, 2, tzinfo=timezone.utc).timestamp()

        self.assertEqual(guild_daily_count(26, previous_day, now=now), 0)
        self.assertEqual(guild_daily_count(2, current_day, now=now), 2)
        self.assertEqual(guild_daily_count(2, 0, now=now), 2)

    def test_guild_progress_resets_stale_server_counters(self) -> None:
        previous_day = int(
            datetime(2026, 8, 10, 14, 59, tzinfo=timezone.utc).timestamp()
            * 1000
        )
        now = datetime(2026, 8, 10, 15, 2, tzinfo=timezone.utc).timestamp()
        action = GuildActionResult(
            member_state=GuildMemberStateSnapshot(
                guild_level=7,
                daily_free_research_count=4,
                daily_paid_research_count=26,
                last_free_researched_at_millis=previous_day,
                last_paid_researched_at_millis=previous_day,
            )
        )
        progress = GuildProgress()

        with patch("crumble_bot.guild_calendar.time.time", return_value=now):
            progress.observe_action(action, initial=True)

        self.assertEqual(progress.daily_free_research_count_before, 0)
        self.assertEqual(progress.daily_donation_count_before, 0)
        self.assertEqual(progress.next_donation_diamond_cost_before, 10)

    def test_legacy_daily_action_is_replayed_after_daily_sop_upgrade(self) -> None:
        self.assertFalse(
            ResidentGuildRunner._daily_action_has_account_workflows(
                {"status": "done", "details_json": '{"workflow": {}, "support": {}}'}
            )
        )
        self.assertTrue(
            ResidentGuildRunner._daily_action_has_account_workflows(
                {
                    "status": "done",
                    "details_json": (
                        '{"account_daily": {}, "crumble_dungeon": {}, '
                        '"workflow": {}, "support": {}}'
                    ),
                }
            )
        )

    def test_schema_and_target_default_keep_two_slots(self) -> None:
        with AccountDB(self.db_path) as db:
            self._add_accounts(db)
            guild = db.upsert_managed_guild(
                guild_id="G1",
                gname="ahhhha",
                gmname="absdbld",
                capacity=6,
            )
            self.assertEqual(guild.target_managed_count, 4)
            tables = {
                row[0]
                for row in db._conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            self.assertTrue(
                {
                    "guilds",
                    "guild_memberships",
                    "guild_daily_actions",
                    "guild_support_actions",
                }.issubset(tables)
            )

    def test_public_fill_retries_slots_and_marks_roster(self) -> None:
        with AccountDB(self.db_path) as db:
            self._add_accounts(db)
            guild = db.upsert_managed_guild(
                guild_id="G1",
                gname="ahhhha",
                gmname="absdbld",
                capacity=5,
            )
            runner = ResidentGuildRunner(
                db,
                self._login,
                client_factory=_PublicJoinClient,
            )
            result = runner.fill(guild)
            self.assertTrue(result["ok"])
            self.assertEqual(result["joined"], 3)
            memberships = db.list_guild_memberships(guild.id, status="active")
            self.assertEqual([row.slot_no for row in memberships], [1, 2, 3])
            self.assertTrue(all(db.get(row.mid).used for row in memberships))

    def test_fill_reserve_zero_fills_around_external_member(self) -> None:
        with AccountDB(self.db_path) as db:
            self._add_accounts(db)
            guild = db.upsert_managed_guild(
                guild_id="G1",
                gname="ahhhha",
                gmname="absdbld",
                capacity=5,
            )
            db.upsert_guild_membership(
                guild.id,
                "OWNER",
                member_type="external",
                status="active",
                role=0,
                details={"name": "owner"},
            )
            runner = ResidentGuildRunner(
                db,
                self._login,
                client_factory=_PublicJoinClient,
            )

            guild = runner.set_reserve_slots(guild, 0)
            result = runner.fill(guild)
            status = runner.status(db.get_managed_guild("G1"))

            self.assertTrue(result["ok"])
            self.assertEqual(result["joined"], 4)
            self.assertEqual(status["guild"]["reserve_slots"], 0)
            self.assertEqual(status["roster"]["configured_managed_target"], 5)
            self.assertEqual(status["roster"]["managed_target"], 4)
            self.assertEqual(status["roster"]["managed_active"], 4)
            self.assertEqual(status["roster"]["vacancy"], 0)
            controlled = {
                item["mid"]: item["controlled"] for item in status["members"]
            }
            self.assertFalse(controlled["OWNER"])
            self.assertTrue(controlled["BOT0"])

    def test_fill_reserve_slots_parser_and_member_control_output(self) -> None:
        args = cli.build_parser().parse_args(
            [
                "guild",
                "fill",
                "--gname",
                "ahhhha",
                "--reserve-slots",
                "0",
            ]
        )
        self.assertEqual(args.reserve_slots, 0)

        output = cli._guild_human_output(
            {
                "ok": True,
                "mode": "resident",
                "fill": {
                    "ok": True,
                    "requested": 0,
                    "joined": 0,
                    "applied": 0,
                    "results": [],
                },
                "status": {
                    "guild": {
                        "name": "ahhhha",
                        "capacity": 2,
                        "member_count": 2,
                        "reserve_slots": 0,
                    },
                    "roster": {
                        "managed_active": 1,
                        "managed_target": 1,
                    },
                    "members": [
                        {
                            "mid": "BOT0",
                            "name": "bot-0",
                            "member_type": "managed",
                            "controlled": True,
                            "status": "active",
                        },
                        {
                            "mid": "OWNER",
                            "name": "owner",
                            "member_type": "external",
                            "controlled": False,
                            "status": "active",
                        },
                    ],
                },
            }
        )
        self.assertIn("预留位置：0", output)
        self.assertIn("成员控制状态：受控 1，非受控 1", output)
        self.assertIn("[受控] bot-0（BOT0）", output)
        self.assertIn("[非受控] owner（OWNER）", output)

    def test_private_fill_submits_applications_without_controller(self) -> None:
        with AccountDB(self.db_path) as db:
            self._add_accounts(db)
            guild = db.upsert_managed_guild(
                guild_id="G1",
                gname="ahhhha",
                gmname="absdbld",
                join_method=1,
                controller_mid="BOT0",
                capacity=5,
                details={"search_summary": {"master_user_id": "ORIGINAL"}},
            )
            db.upsert_guild_membership(
                guild.id,
                "BOT0",
                member_type="reserved",
                status="active",
                role=1,
            )
            runner = ResidentGuildRunner(
                db,
                self._login,
                client_factory=_PrivateApplyClient,
            )
            result = runner.fill(guild)
            self.assertTrue(result["ok"])
            self.assertEqual(result["joined"], 0)
            self.assertEqual(result["applied"], 3)
            self.assertEqual(
                [item["name"] for item in result["results"]],
                ["bot-1", "bot-2", "bot-3"],
            )
            self.assertEqual(
                result["next_action"]["action"],
                "approve_applications",
            )
            self.assertEqual(
                len(db.list_guild_memberships(guild.id, status="applied")),
                3,
            )
            refresh = runner.enrich_member_names(guild)
            self.assertEqual(refresh["updated"], 1)
            self.assertEqual(
                [
                    item.details["name"]
                    for item in db.list_guild_memberships(guild.id, status="applied")
                ],
                ["bot-1", "bot-2", "bot-3"],
            )

    def test_private_fill_reapplies_stale_local_application(self) -> None:
        with AccountDB(self.db_path) as db:
            self._add_accounts(db, count=1)
            guild = db.upsert_managed_guild(
                guild_id="G1",
                gname="ahhhha",
                gmname="absdbld",
                join_method=1,
                capacity=3,
                reserve_slots=2,
            )
            db.upsert_guild_membership(
                guild.id,
                "BOT0",
                slot_no=1,
                member_type="managed",
                status="applied",
                details={"application_id": "APP-STALE", "name": "bot-0"},
            )
            runner = ResidentGuildRunner(
                db,
                self._login,
                client_factory=_PrivateApplyClient,
            )

            result = runner.fill(guild)

            self.assertTrue(result["ok"])
            self.assertEqual(result["requested"], 1)
            self.assertEqual(result["applied"], 1)
            self.assertTrue(result["results"][0]["reapplied"])
            self.assertEqual(result["results"][0]["mid"], "BOT0")
            self.assertEqual(result["pending_validation"]["invalidated"], 1)
            membership = db.get_guild_membership(guild.id, "BOT0")
            self.assertEqual(membership.status, "applied")
            self.assertEqual(membership.details["application_id"], "APP1")

    def test_private_fill_keeps_server_pending_application(self) -> None:
        _ExistingPrivateApplicationClient.apply_calls = 0
        with AccountDB(self.db_path) as db:
            self._add_accounts(db, count=1)
            guild = db.upsert_managed_guild(
                guild_id="G1",
                gname="ahhhha",
                gmname="absdbld",
                join_method=1,
                capacity=3,
                reserve_slots=2,
            )
            db.upsert_guild_membership(
                guild.id,
                "BOT0",
                slot_no=1,
                member_type="managed",
                status="applied",
                details={"application_id": "APP-LOCAL", "name": "bot-0"},
            )
            runner = ResidentGuildRunner(
                db,
                self._login,
                client_factory=_ExistingPrivateApplicationClient,
            )

            result = runner.fill(guild)

            self.assertTrue(result["ok"])
            self.assertEqual(result["state"], "awaiting_approval")
            self.assertEqual(result["requested"], 0)
            self.assertEqual(result["pending_validation"]["confirmed"], 1)
            self.assertEqual(_ExistingPrivateApplicationClient.apply_calls, 0)
            membership = db.get_guild_membership(guild.id, "BOT0")
            self.assertEqual(membership.details["application_id"], "APP-SERVER")

    def test_support_is_standalone_and_does_not_create_daily_action(self) -> None:
        with AccountDB(self.db_path) as db:
            self._add_accounts(db, count=2)
            guild = db.upsert_managed_guild(
                guild_id="G1",
                gname="ahhhha",
                gmname="absdbld",
                capacity=5,
            )
            db.upsert_guild_membership(
                guild.id,
                "BOT0",
                slot_no=1,
                member_type="managed",
                status="active",
                details={"name": "bot-0"},
            )
            db.upsert_guild_membership(
                guild.id,
                "BOT1",
                slot_no=2,
                member_type="managed",
                status="active",
                details={"name": "bot-1"},
            )

            request = b"".join(
                (
                    pb.encode_string_field(1, "REQ1"),
                    pb.encode_message_field(
                        2, pb.encode_string_field(1, "OWNER")
                    ),
                )
            )
            rpc_paths = []

            class _SupportClient:
                def __init__(self, _endpoint: str) -> None:
                    pass

                def __enter__(self):
                    return self

                def __exit__(self, *args) -> None:
                    return None

                def unary(self, path: str, _message: bytes, *, metadata=None):
                    rpc_paths.append(path)
                    if path == GET_GUILD_SUPPORT_REQUESTS_PATH:
                        return GrpcResponse(
                            pb.encode_message_field(1, request), {}, {}
                        )
                    raise AssertionError(path)

            runner = ResidentGuildRunner(
                db,
                self._login,
                client_factory=_SupportClient,
            )
            calls = []

            def fake_support(guild_row, supporter_mid, api, day_key, requests=None):
                calls.append(
                    (guild_row.guild_id, supporter_mid, day_key, requests)
                )
                return {
                    "ok": True,
                    "attempted": 1,
                    "count": 1,
                    "available": len(requests or []),
                    "requests": [],
                }

            runner._support_one = fake_support
            progress = []
            result = runner.support(guild, on_progress=progress.append)

            self.assertTrue(result["ok"])
            self.assertEqual(result["count"], 2)
            self.assertEqual(result["attempted"], 2)
            self.assertEqual(result["accounts_attempted"], 2)
            self.assertEqual(rpc_paths, [GET_GUILD_SUPPORT_REQUESTS_PATH])
            self.assertEqual(
                [item["phase"] for item in progress],
                ["querying", "queried", "account", "account", "done"],
            )
            self.assertEqual(progress[-1]["processed"], 2)
            self.assertEqual(progress[-1]["support_count"], 2)
            self.assertIn(
                "[████████████████] 2/2｜成功 2｜失败 0｜完成",
                cli._guild_support_progress_line(progress[-1]),
            )
            self.assertEqual(calls[0][0:2], ("G1", "BOT0"))
            self.assertEqual(calls[1][0:2], ("G1", "BOT1"))
            self.assertEqual(
                [item.support_request_id for item in calls[0][3]], ["REQ1"]
            )
            self.assertIsNone(
                db.get_daily_guild_action(guild.id, result["day"], "BOT0")
            )
            membership = db.get_guild_membership(guild.id, "BOT0")
            self.assertEqual(membership.details["name"], "bot-0")
            self.assertEqual(membership.details["last_support_day"], result["day"])

    def test_support_progress_display_rewrites_tty_line(self) -> None:
        class TtyBuffer(io.StringIO):
            def isatty(self) -> bool:
                return True

        output = TtyBuffer()
        display = cli._GuildSupportProgressDisplay(output)
        display.update(
            {
                "phase": "account",
                "processed": 1,
                "total": 22,
                "support_count": 1,
                "failed": 0,
                "name": "bot-1",
                "status": "ok",
            }
        )
        display.update(
            {
                "phase": "done",
                "processed": 1,
                "total": 22,
                "support_count": 1,
                "failed": 0,
                "stopped_reason": "support_limit",
            }
        )

        rendered = output.getvalue()
        self.assertEqual(rendered.count("\n"), 1)
        self.assertEqual(rendered.count("\r\x1b[2K"), 2)
        self.assertIn("1/22｜成功 1", rendered)
        self.assertIn("完成（支援已达上限）", rendered)

    def test_support_progress_display_throttles_non_tty_output(self) -> None:
        output = io.StringIO()
        display = cli._GuildSupportProgressDisplay(output)
        for processed in range(1, 13):
            display.update(
                {
                    "phase": "account",
                    "processed": processed,
                    "total": 22,
                    "support_count": processed,
                    "failed": 0,
                    "mid": f"BOT{processed}",
                    "status": "ok",
                }
            )
        display.update(
            {
                "phase": "done",
                "processed": 12,
                "total": 22,
                "support_count": 12,
                "failed": 0,
            }
        )

        lines = output.getvalue().splitlines()
        self.assertEqual(len(lines), 4)
        self.assertIn("1/22", lines[0])
        self.assertIn("5/22", lines[1])
        self.assertIn("10/22", lines[2])
        self.assertIn("12/22", lines[3])


if __name__ == "__main__":
    unittest.main()
