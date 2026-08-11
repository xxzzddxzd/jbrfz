from __future__ import annotations

import unittest

from crumble_bot import pbutil as pb
from crumble_bot.cheat import (
    CHEAT_PURE_SERVICE_METHODS,
    CHEAT_PURE_SERVICE_NAME,
    CHEAT_SERVICE_METHODS,
    CHEAT_SERVICE_NAME,
    PAY_ASSETS_FORCIBLY_PATH,
    Cheat,
    PayAssetsForciblyCommand,
    pay_assets_forcibly_request,
)
from crumble_bot.grpc_client import GrpcError, GrpcResponse
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


class _ProbeClient:
    def __init__(self) -> None:
        self.calls = []

    def unary(self, path, message, *, metadata=None):
        self.calls.append((path, message, metadata))
        if path.endswith("/PayAssetsForcibly"):
            raise GrpcError(12, f"Method not found: {path.removeprefix('/')}")
        if path.endswith("/GetContextResourceKey"):
            return GrpcResponse(b"", {}, {})
        raise GrpcError(13, "Error parsing request message")


class CheatTests(unittest.TestCase):
    def test_generated_method_registry_is_complete(self) -> None:
        self.assertEqual(len(CHEAT_SERVICE_METHODS), 50)
        self.assertEqual(len(CHEAT_PURE_SERVICE_METHODS), 14)
        self.assertIn("PayAssetsForcibly", CHEAT_SERVICE_METHODS)
        self.assertIn("GetContextResourceKey", CHEAT_PURE_SERVICE_METHODS)

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

    def test_probe_uses_malformed_body_and_classifies_routes(self) -> None:
        client = _ProbeClient()
        cheat = Cheat(client, Session(mid="MID1", game_access_token="token"))

        missing = cheat.probe_method("PayAssetsForcibly")
        parsing = cheat.probe_method("GetGameBoosts")
        success = cheat.probe_method(
            "GetContextResourceKey",
            service=CHEAT_PURE_SERVICE_NAME,
        )

        self.assertFalse(missing.exists)
        self.assertEqual(missing.grpc_status, 12)
        self.assertTrue(parsing.exists)
        self.assertEqual(parsing.grpc_status, 13)
        self.assertTrue(success.exists)
        self.assertEqual(success.grpc_status, 0)
        self.assertEqual([call[1] for call in client.calls], [b"\x80"] * 3)
        self.assertEqual(
            client.calls[0][0],
            f"/{CHEAT_SERVICE_NAME}/PayAssetsForcibly",
        )

    def test_probe_rejects_unknown_service_or_method(self) -> None:
        cheat = Cheat(_ProbeClient(), Session(mid="MID1", game_access_token="token"))

        with self.assertRaises(ValueError):
            cheat.probe_method("NotGenerated")
        with self.assertRaises(ValueError):
            cheat.probe_method("Fail", service="cc.public.game.Other")


if __name__ == "__main__":
    unittest.main()
