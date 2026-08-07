from __future__ import annotations

import struct
import unittest

from crumble_bot import pbutil as pb
from crumble_bot.auth import AccountState
from crumble_bot.crumble_dungeon import (
    FINISH_CRUMBLE_DUNGEON_PATH,
    START_CRUMBLE_DUNGEON_PATH,
    CrumbleDungeonRunner,
    parse_signup_cookie_ids,
    start_crumble_dungeon_request,
)
from crumble_bot.grpc_client import GrpcResponse


def _signup_with_team() -> bytes:
    team0 = pb.encode_int32_field(1, 0) + pb.encode_repeated_messages(
        2,
        (
            pb.encode_int32_field(1, 0) + pb.encode_int32_field(2, 101),
        ),
    )
    team1 = pb.encode_int32_field(1, 1) + pb.encode_repeated_messages(
        2,
        (
            pb.encode_int32_field(1, 0) + pb.encode_int32_field(2, 202),
            pb.encode_int32_field(1, 1) + pb.encode_int32_field(2, 303),
        ),
    )
    typed = (
        pb.encode_int32_field(1, 0)
        + pb.encode_repeated_messages(2, (team0, team1))
        + pb.encode_int32_field(3, 1)
    )
    collections = pb.encode_repeated_messages(2, (typed,))
    return pb.encode_message_field(3, pb.encode_message_field(2, collections))


class _DungeonClient:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.messages: list[bytes] = []

    def unary(self, path, message, *, metadata=None):
        self.calls.append(path)
        self.messages.append(message)
        if path == START_CRUMBLE_DUNGEON_PATH:
            ongoing = pb.encode_string_field(1, "BATTLE-1")
            return GrpcResponse(pb.encode_message_field(3, ongoing), {}, {})
        if path == FINISH_CRUMBLE_DUNGEON_PATH:
            results = pb.encode_double_field(1, 182.0)
            return GrpcResponse(pb.encode_message_field(3, results), {}, {})
        raise AssertionError(path)


class CrumbleDungeonTests(unittest.TestCase):
    def test_signup_team_parser_uses_last_used_team(self) -> None:
        self.assertEqual(parse_signup_cookie_ids(_signup_with_team()), (202, 303))

    def test_start_request_default_is_proto_empty(self) -> None:
        self.assertEqual(start_crumble_dungeon_request(), b"")

    def test_runner_starts_and_finishes_with_account_team(self) -> None:
        client = _DungeonClient()
        session = AccountState(mid="MID", game_access_token="token").to_session()
        result = CrumbleDungeonRunner(
            client,
            session,
            cookie_ids=parse_signup_cookie_ids(_signup_with_team()),
        ).run()

        self.assertTrue(result["ok"])
        self.assertEqual(result["battle_id"], "BATTLE-1")
        self.assertEqual(result["cookie_ids"], [202, 303])
        self.assertEqual(result["result"]["total_max_score"], 182.0)
        self.assertEqual(
            client.calls,
            [START_CRUMBLE_DUNGEON_PATH, FINISH_CRUMBLE_DUNGEON_PATH],
        )
        report = bytes(pb.decode_fields(client.messages[1])[1][2])
        score_field = pb.decode_fields(report)[0]
        self.assertEqual(score_field[0:2], (1, 1))
        self.assertEqual(score_field[2], struct.pack("<d", 182.0))


if __name__ == "__main__":
    unittest.main()
