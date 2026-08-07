"""Crumble dungeon (碎屑副本) RPCs.

The dungeon is a two-step server transaction: the client starts a battle and
the response supplies an opaque battle id, then it submits a result for that
id.  The game normally fills the battle report from the selected team; the
bot keeps the same protobuf shape and uses the account's configured team ids
when they are available, falling back to the small default team used by the
stage runner.
"""
from __future__ import annotations

import logging
import struct
from typing import Iterable, Sequence

from . import messages as msg
from . import pbutil as pb
from .grpc_client import GrpcClient, GrpcResponse
from .headers import Session, build_metadata

log = logging.getLogger(__name__)

START_CRUMBLE_DUNGEON_PATH = (
    "/cc.public.game.DungeonService/StartCrumbleDungeonBattle"
)
FINISH_CRUMBLE_DUNGEON_PATH = (
    "/cc.public.game.DungeonService/FinishCrumbleDungeonBattle"
)

# These are the values used by the previously captured finish request.  The
# score is deliberately explicit: the server accepts the score as a double
# and this is the validated score used by the automation workflow.
DEFAULT_CRUMBLE_DUNGEON_SCORE = 182.0
DEFAULT_BATTLE_DURATION_MILLIS = 20_000
DEFAULT_END_REASON = 0  # CrumbleDungeonBattleEndReason.Timeout (proto default)
DEFAULT_EQUIPMENT_PRESET_INDEX = 0


def start_crumble_dungeon_request(
    equipment_preset_index: int = DEFAULT_EQUIPMENT_PRESET_INDEX,
) -> bytes:
    """Build ``StartCrumbleDungeonBattleRequest``."""
    return pb.encode_int32_field(1, int(equipment_preset_index))


def crumble_dungeon_battle_report(
    *,
    score: float = DEFAULT_CRUMBLE_DUNGEON_SCORE,
    battle_duration_millis: int = DEFAULT_BATTLE_DURATION_MILLIS,
    end_reason: int = DEFAULT_END_REASON,
    cookie_ids: Sequence[int] = msg.DEFAULT_COOKIE_IDS,
    owner_mid: str = "",
    battle_team_report: bytes | None = None,
    client_battle_report: bytes | None = None,
) -> bytes:
    """Build ``CrumbleDungeonBattleReport``.

    ``battle_team_report`` and ``client_battle_report`` are accepted as raw
    protobuf messages so callers that have fetched the account's current
    team can submit it without the builder knowing the rest of the schema.
    """
    normalized_ids = _normalize_cookie_ids(cookie_ids)
    team_report = battle_team_report or msg.battle_team_report(normalized_ids)
    client_report = client_battle_report or msg.client_battle_report_minimal(
        owner_mid,
        normalized_ids,
    )
    parts = [
        pb.encode_double_field(1, float(score)),
        pb.encode_message_field(2, msg.duration_millis(int(battle_duration_millis))),
        pb.encode_int32_field(3, int(end_reason)),
        pb.encode_message_field(4, team_report),
        pb.encode_message_field(5, client_report),
    ]
    return b"".join(parts)


def finish_crumble_dungeon_request(
    battle_id: str,
    *,
    score: float = DEFAULT_CRUMBLE_DUNGEON_SCORE,
    battle_duration_millis: int = DEFAULT_BATTLE_DURATION_MILLIS,
    end_reason: int = DEFAULT_END_REASON,
    cookie_ids: Sequence[int] = msg.DEFAULT_COOKIE_IDS,
    owner_mid: str = "",
    battle_team_report: bytes | None = None,
    client_battle_report: bytes | None = None,
) -> bytes:
    """Build ``FinishCrumbleDungeonBattleRequest``."""
    if not isinstance(battle_id, str) or not battle_id.strip():
        raise ValueError("battle_id must not be empty")
    report = crumble_dungeon_battle_report(
        score=score,
        battle_duration_millis=battle_duration_millis,
        end_reason=end_reason,
        cookie_ids=cookie_ids,
        owner_mid=owner_mid,
        battle_team_report=battle_team_report,
        client_battle_report=client_battle_report,
    )
    return b"".join(
        (
            pb.encode_string_field(1, battle_id.strip()),
            pb.encode_message_field(2, report),
        )
    )


def parse_start_crumble_dungeon_response(body: bytes) -> dict:
    """Parse the opaque battle id and metadata returned by the start RPC."""
    fields = pb.decode_fields(body)
    ongoing = _message_value(fields, 3)
    if ongoing is None:
        raise ValueError("StartCrumbleDungeonBattleResponse has no ongoing battle")
    ongoing_fields = pb.decode_fields(ongoing)
    battle_id = _string_value(ongoing_fields, 1)
    if not battle_id:
        raise ValueError("ongoing crumble dungeon battle has no id")
    return {
        "battle_id": battle_id,
        "random_seed": _int_value(ongoing_fields, 2),
        "epoch_day": _int_value(ongoing_fields, 3),
        "first_epoch_day_of_week": _int_value(ongoing_fields, 4),
        "equipment_preset_index": _int_value(ongoing_fields, 5),
    }


def parse_signup_cookie_ids(
    body: bytes,
    *,
    battle_team_type: int = 0,
) -> tuple[int, ...]:
    """Extract the account's last-used stage team from ``SignUpResponse``.

    ``Crumble.collections.teams`` contains ``BattleTeamsForType`` entries;
    each entry contains the saved teams and (when present) its last-used team
    index.  Keeping this parser here lets the dungeon report use the actual
    account cookies without an extra collection RPC.  Older/partial signup
    responses simply return an empty tuple and the runner falls back to its
    safe default team.
    """
    try:
        crumble = _message_value(pb.decode_fields(body), 3)
        collections = _message_value(pb.decode_fields(crumble or b""), 2)
        if collections is None:
            return ()
        typed_teams = [
            bytes(value)
            for field_number, wire_type, value in pb.decode_fields(collections)
            if field_number == 2 and wire_type == 2
        ]
        for typed in typed_teams:
            typed_fields = pb.decode_fields(typed)
            if _int_value(typed_fields, 1) != int(battle_team_type):
                continue
            team_bodies = [
                bytes(value)
                for field_number, wire_type, value in typed_fields
                if field_number == 2 and wire_type == 2
            ]
            if not team_bodies:
                continue
            last_used = _int_value(typed_fields, 3)
            selected = team_bodies[0]
            for team in team_bodies:
                if _int_value(pb.decode_fields(team), 1) == last_used:
                    selected = team
                    break
            cookie_ids: list[int] = []
            for field_number, wire_type, value in pb.decode_fields(selected):
                if field_number != 2 or wire_type != 2:
                    continue
                cookie_id = _int_value(pb.decode_fields(bytes(value)), 2)
                if cookie_id > 0 and cookie_id not in cookie_ids:
                    cookie_ids.append(cookie_id)
            if cookie_ids:
                return tuple(cookie_ids)
    except (TypeError, ValueError):
        log.debug("signup response did not contain a parseable stage team")
    return ()


def parse_finish_crumble_dungeon_response(body: bytes) -> dict:
    """Parse result summary and reward count from the finish RPC."""
    fields = pb.decode_fields(body)
    results = _message_value(fields, 3)
    payload: dict = {
        "reward_count": sum(
            1 for field_number, wire_type, _ in fields
            if field_number == 2 and wire_type == 2
        ),
    }
    if results is None:
        return payload
    result_fields = pb.decode_fields(results)
    payload.update(
        {
            "total_max_score": _double_value(result_fields, 1),
            "settled_best_weekly_rank": _int_value(result_fields, 4),
            "daily_max_score_count": sum(
                1 for field_number, wire_type, _ in result_fields
                if field_number == 2 and wire_type == 2
            ),
            "weekly_max_score_count": sum(
                1 for field_number, wire_type, _ in result_fields
                if field_number == 3 and wire_type == 2
            ),
        }
    )
    return payload


class CrumbleDungeonRunner:
    """Start and immediately settle one crumble dungeon battle."""

    def __init__(
        self,
        client: GrpcClient,
        session: Session,
        *,
        score: float = DEFAULT_CRUMBLE_DUNGEON_SCORE,
        battle_duration_millis: int = DEFAULT_BATTLE_DURATION_MILLIS,
        end_reason: int = DEFAULT_END_REASON,
        cookie_ids: Sequence[int] = msg.DEFAULT_COOKIE_IDS,
    ) -> None:
        self.client = client
        self.session = session
        self.score = float(score)
        self.battle_duration_millis = max(0, int(battle_duration_millis))
        self.end_reason = int(end_reason)
        self.cookie_ids = _normalize_cookie_ids(cookie_ids)

    def run(
        self,
        *,
        equipment_preset_index: int = DEFAULT_EQUIPMENT_PRESET_INDEX,
        battle_team_report: bytes | None = None,
        client_battle_report: bytes | None = None,
    ) -> dict:
        payload = {
            "ok": False,
            "started": False,
            "finished": False,
            "score": self.score,
            "equipment_preset_index": int(equipment_preset_index),
            "battle_id": "",
            "cookie_ids": list(self.cookie_ids),
        }
        try:
            start_response = self._unary(
                START_CRUMBLE_DUNGEON_PATH,
                start_crumble_dungeon_request(equipment_preset_index),
            )
            started = parse_start_crumble_dungeon_response(start_response.message)
            payload.update({"started": True, **started})

            finish_response = self._unary(
                FINISH_CRUMBLE_DUNGEON_PATH,
                finish_crumble_dungeon_request(
                    started["battle_id"],
                    score=self.score,
                    battle_duration_millis=self.battle_duration_millis,
                    end_reason=self.end_reason,
                    cookie_ids=self.cookie_ids,
                    owner_mid=self.session.mid,
                    battle_team_report=battle_team_report,
                    client_battle_report=client_battle_report,
                ),
            )
            payload.update(
                {
                    "finished": True,
                    "result": parse_finish_crumble_dungeon_response(
                        finish_response.message
                    ),
                }
            )
            payload["ok"] = True
        except Exception as error:  # noqa: BLE001 - preserve per-account result
            message = f"{type(error).__name__}: {error}"
            if _is_already_completed_error(message):
                payload.update(
                    {
                        "ok": True,
                        "skipped": True,
                        "reason": "already_completed_today",
                    }
                )
                log.info("crumble dungeon already completed mid=%s", self.session.mid)
            else:
                payload["error"] = message
                log.error("crumble dungeon failed mid=%s: %s", self.session.mid, message)
        return payload

    def _unary(self, path: str, body: bytes) -> GrpcResponse:
        response = self.client.unary(
            path,
            body,
            metadata=build_metadata(self.session),
        )
        if self.session.adopt_resource_key(response.headers):
            log.debug("resource_key <- %s", self.session.resource_key)
        return response


def _normalize_cookie_ids(cookie_ids: Iterable[int]) -> tuple[int, ...]:
    values: list[int] = []
    for value in cookie_ids:
        try:
            number = int(value)
        except (TypeError, ValueError):
            continue
        if number > 0 and number not in values:
            values.append(number)
    return tuple(values) or tuple(msg.DEFAULT_COOKIE_IDS)


def _message_value(fields, target: int) -> bytes | None:
    for field_number, wire_type, value in fields:
        if field_number == target and wire_type == 2:
            return bytes(value)
    return None


def _string_value(fields, target: int) -> str:
    value = _message_value(fields, target)
    if value is None:
        return ""
    try:
        return value.decode("utf-8")
    except UnicodeDecodeError:
        return ""


def _int_value(fields, target: int) -> int:
    for field_number, wire_type, value in fields:
        if field_number == target and wire_type == 0:
            return int(value)
    return 0


def _double_value(fields, target: int) -> float:
    for field_number, wire_type, value in fields:
        if field_number == target and wire_type == 1:
            raw = bytes(value)
            if len(raw) == 8:
                return float(struct.unpack("<d", raw)[0])
    return 0.0


def _is_already_completed_error(message: str) -> bool:
    text = str(message).lower()
    if "already" not in text:
        return False
    return any(
        token in text
        for token in ("complete", "claim", "daily", "limit", "attempt", "record")
    )
