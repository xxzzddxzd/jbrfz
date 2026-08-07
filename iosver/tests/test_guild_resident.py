from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from crumble_bot import pbutil as pb
from crumble_bot.auth import AccountState
from crumble_bot.db import AccountDB
from crumble_bot.grpc_client import GrpcResponse
from crumble_bot.guild import JOIN_GUILD_PATH
from crumble_bot.guild_resident_runner import ResidentGuildRunner


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

    def test_private_fill_reports_controller_transfer_without_waiting(self) -> None:
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
            result = ResidentGuildRunner(db, self._login).fill(guild)
            self.assertFalse(result["ok"])
            self.assertEqual(result["stopped_reason"], "controller_not_master")
            self.assertEqual(
                result["next_action"]["action"],
                "transfer_master_to_controller",
            )


if __name__ == "__main__":
    unittest.main()
