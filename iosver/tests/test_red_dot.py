from __future__ import annotations

import unittest

from crumble_bot import pbutil as pb
from crumble_bot.headers import Session
from crumble_bot.patch_data import PatchData, compute_zip_password
from crumble_bot.red_dot import RedDotRunner, parse_signup_assets


def _message(field: int, value: bytes) -> bytes:
    return pb.encode_message_field(field, value)


class RedDotWireTests(unittest.TestCase):
    def test_packed_int32_round_trip(self) -> None:
        body = pb.encode_packed_int32_field(4, [1, 127, 128, 123456789])
        fields = pb.decode_fields(body)
        self.assertEqual(fields[0][0:2], (4, 2))
        self.assertEqual(
            pb.decode_packed_varints(bytes(fields[0][2])),
            [1, 127, 128, 123456789],
        )

    def test_live_zip_password_algorithm(self) -> None:
        self.assertEqual(
            compute_zip_password("game-data-185237-ee36b3"),
            "8236108BEB91B2182293307168C2883BB2DC3848",
        )

    def test_scan_finds_completed_daily_mission(self) -> None:
        mission_id = 123456
        requirement = b"".join(
            (
                pb.encode_int32_field(1, mission_id),
                _message(2, pb.encode_int64_field(1, 1)),
            )
        )
        periodic = pb.encode_packed_int32_field(1, [mission_id])
        missions = _message(1, periodic)
        tasks = _message(1, requirement) + _message(3, missions)
        inventory = _message(
            1,
            pb.encode_int32_field(1, 1464007916)
            + pb.encode_int64_field(2, 500),
        )
        signup = _message(
            3,
            _message(3, inventory) + _message(8, tasks),
        )
        patch = PatchData(
            resource_hash="hash",
            resource_key="game-data-test",
            tables={
                "items": [],
                "missions": [
                    {
                        "id": mission_id,
                        "missionType": "MISSIONTYPE_DAILY_MISSION",
                        "clearRequirementValues": ["1"],
                    }
                ],
            },
        )
        runner = RedDotRunner(
            client=None,  # type: ignore[arg-type]
            session=Session(mid="TEST", game_access_token="token"),
            patch_data=patch,
            include_daily=False,
        )

        self.assertEqual(parse_signup_assets(signup), {1464007916: 500})
        self.assertEqual(
            runner.scan(signup),
            [{"key": "mission_daily", "count": 1, "ids": [mission_id]}],
        )


if __name__ == "__main__":
    unittest.main()
