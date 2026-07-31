"""Protobuf request builders for stage / invite RPCs."""
from __future__ import annotations

import random
from typing import Optional, Sequence

from . import pbutil as pb

WAVE = 0
BOSS = 1

DEFAULT_COOKIE_IDS = (
    1218479743,
    1229712006,
    1273860791,
    1398196995,
)


def duration_millis(ms: int) -> bytes:
    return pb.encode_int64_field(1, int(ms))


def cookie_battle_report(
    cookie_data_id: int,
    *,
    total_damage: float = 100.0,
    damage_received: float = 10.0,
    healing_done: float = 0.0,
    healing_received: float = 10.0,
    max_single_damage: float = 20.0,
    first_atk: float = 100.0,
    first_def: float = 100.0,
    first_hp: float = 2000.0,
    kill_count: int = 5,
) -> bytes:
    return b"".join(
        [
            pb.encode_int32_field(1, cookie_data_id),
            pb.encode_double_field(2, total_damage),
            pb.encode_double_field(3, damage_received),
            pb.encode_double_field(4, healing_done),
            pb.encode_double_field(5, healing_received),
            pb.encode_double_field(7, max_single_damage),
            pb.encode_double_field(8, first_atk),
            pb.encode_double_field(9, first_def),
            pb.encode_double_field(10, first_hp),
            pb.encode_double_field(11, float(kill_count)),
        ]
    )


def battle_team_report(cookie_ids: Sequence[int]) -> bytes:
    msgs = [
        cookie_battle_report(
            cid,
            total_damage=random.uniform(80, 400),
            damage_received=random.uniform(0, 50),
            healing_received=random.uniform(0, 40),
            max_single_damage=random.uniform(15, 60),
            first_atk=random.uniform(90, 160),
            first_def=random.uniform(90, 180),
            first_hp=random.uniform(1300, 2800),
            kill_count=random.randint(3, 20),
        )
        for cid in cookie_ids
    ]
    return pb.encode_repeated_messages(1, msgs)


def stage_clear_report(
    *,
    random_seed: Optional[int] = None,
    battle_time_ms: int = 20000,
    cookie_ids: Sequence[int] = DEFAULT_COOKIE_IDS,
    raw_template: Optional[bytes] = None,
) -> bytes:
    if raw_template is not None:
        if random_seed is None:
            return raw_template
        return _rewrite_varint_field1(raw_template, random_seed)

    seed = random_seed if random_seed is not None else random.randint(-(1 << 31), (1 << 31) - 1)
    seed_u = seed & ((1 << 64) - 1)
    seed_bytes = pb.tag(1, 0) + pb._varint(seed_u)
    return b"".join(
        [
            seed_bytes,
            pb.encode_message_field(2, duration_millis(battle_time_ms)),
            pb.encode_message_field(3, battle_team_report(cookie_ids)),
        ]
    )


def _rewrite_varint_field1(buf: bytes, seed: int) -> bytes:
    fields = pb.decode_fields(buf)
    rest = []
    replaced = False
    seed_u = seed & ((1 << 64) - 1)
    for fn, wt, val in fields:
        if not replaced and fn == 1 and wt == 0:
            rest.append(pb.tag(1, 0) + pb._varint(seed_u))
            replaced = True
            continue
        if wt == 0:
            rest.append(pb.tag(fn, 0) + pb._varint(int(val)))
        elif wt == 2:
            rest.append(pb.encode_bytes_field(fn, bytes(val)))
        elif wt == 1:
            rest.append(pb.tag(fn, 1) + bytes(val))
        elif wt == 5:
            rest.append(pb.tag(fn, 5) + bytes(val))
    if not replaced:
        rest.insert(0, pb.tag(1, 0) + pb._varint(seed_u))
    return b"".join(rest)


def client_battle_report_minimal(owner_mid: str, cookie_ids: Sequence[int] = DEFAULT_COOKIE_IDS) -> bytes:
    entities = []
    for i, cid in enumerate(cookie_ids, start=1):
        ent = b"".join(
            [
                pb.encode_int32_field(1, 1000 + i),
                pb.encode_int32_field(3, cid),
                pb.encode_string_field(4, owner_mid),
            ]
        )
        entities.append(ent)
    entities.append(
        b"".join(
            [
                pb.encode_int32_field(1, 2000),
                pb.encode_int32_field(2, 1),
                pb.encode_int32_field(3, 1750600616),
            ]
        )
    )
    return pb.encode_repeated_messages(1, entities)


def start_stage_request(
    stage_index: int,
    *,
    team_index: int = 1,
    start_point: int = WAVE,
    start_trigger_case: int = 0,
) -> bytes:
    """Build StartStageRequest.

    StageStartTrigger is field 4 message; its oneof case id is nested inside.
    """
    parts = [
        pb.encode_int32_field(1, stage_index),
        pb.encode_int32_field(2, team_index),
        pb.encode_int32_field(3, start_point),
    ]
    if start_trigger_case:
        inner = pb.encode_message_field(start_trigger_case, b"")
        parts.append(pb.encode_message_field(4, inner))
    return b"".join(parts)


def complete_stage_request(
    stage_index: int,
    *,
    start_point: int,
    clear_report: bytes,
    client_report: bytes,
) -> bytes:
    return b"".join(
        [
            pb.encode_int32_field(1, stage_index),
            pb.encode_int32_field(2, start_point),
            pb.encode_message_field(3, clear_report),
            pb.encode_message_field(4, client_report),
        ]
    )


def register_friend_inviter_request(inviter_id: str) -> bytes:
    return pb.encode_string_field(1, inviter_id)


def sign_up_request() -> bytes:
    return b""
