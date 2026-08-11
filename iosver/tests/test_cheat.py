from __future__ import annotations

import unittest

from crumble_bot import pbutil as pb
from crumble_bot.cheat import (
    PAY_ASSETS_FORCIBLY_PATH,
    Cheat,
    PayAssetsForciblyCommand,
    pay_assets_forcibly_request,
)
from crumble_bot.grpc_client import GrpcResponse
from crumble_bot.headers import Session


def _command_values(body: bytes) -> list[tuple[int, int]]:
    values = []
    for field_number, wire_type, value in pb.decode_fields(body):
        if field_number != 1 or wire_type != 2:
            continue
        command = {
            current_field: int(current_value)
            for current_field, current_wire, current_value in pb.decode_fields(
                bytes(value)
            )
            if current_wire == 0
        }
        values.append((command.get(1, 0), command.get(2, 0)))
    return values


class _CheatClient:
    def __init__(self) -> None:
        self.calls = []

    def unary(self, path, message, *, metadata=None):
        self.calls.append((path, message, metadata))
        return GrpcResponse(
            message=b"response",
            headers={"crumble-resource-key": "game-data-new"},
            trailers={},
        )


class CheatTests(unittest.TestCase):
    def test_pay_assets_request_supports_repeated_commands(self) -> None:
        body = pay_assets_forcibly_request(
            (
                PayAssetsForciblyCommand(1464007916, 100),
                PayAssetsForciblyCommand(2008613512, 4),
            )
        )

        self.assertEqual(
            _command_values(body),
            [(1464007916, 100), (2008613512, 4)],
        )

    def test_pay_assets_member_sends_one_authenticated_command(self) -> None:
        client = _CheatClient()
        session = Session(
            mid="MID1",
            game_access_token="token",
            resource_key="game-data-old",
        )

        response = Cheat(client, session).pay_assets_forcibly(
            1464007916,
            5000,
        )

        self.assertEqual(response.message, b"response")
        self.assertEqual(len(client.calls), 1)
        path, body, metadata = client.calls[0]
        self.assertEqual(path, PAY_ASSETS_FORCIBLY_PATH)
        self.assertEqual(_command_values(body), [(1464007916, 5000)])
        self.assertEqual(metadata["crumble-user-id"], "MID1")
        self.assertEqual(metadata["crumble-access-token"], "token")
        self.assertEqual(metadata["crumble-resource-key"], "game-data-old")
        self.assertEqual(session.resource_key, "game-data-new")

    def test_pay_assets_request_rejects_invalid_values(self) -> None:
        invalid_commands = (
            (),
            (PayAssetsForciblyCommand(0, 1),),
            (PayAssetsForciblyCommand(True, 1),),
            (PayAssetsForciblyCommand(1 << 31, 1),),
            (PayAssetsForciblyCommand(1, 0),),
            (PayAssetsForciblyCommand(1, True),),
            (PayAssetsForciblyCommand(1, 1 << 63),),
        )
        for commands in invalid_commands:
            with self.subTest(commands=commands), self.assertRaises(ValueError):
                pay_assets_forcibly_request(commands)

        with self.assertRaises(ValueError):
            pay_assets_forcibly_request(((1, 2),))


if __name__ == "__main__":
    unittest.main()
