"""Stage auto-production and daily bonus reward RPCs for game version 10101."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from . import pbutil as pb
from .grpc_client import GrpcClient, GrpcResponse
from .headers import Session, build_metadata
from .messages import (
    receive_stage_auto_production_rewards_request,
    receive_stage_bonus_auto_production_rewards_request,
)

RECEIVE_STAGE_AUTO_PRODUCTION_REWARDS_PATH = (
    "/cc.public.game.AdventureService/ReceiveStageAutoProductionRewards"
)
RECEIVE_STAGE_BONUS_AUTO_PRODUCTION_REWARDS_PATH = (
    "/cc.public.game.AdventureService/ReceiveStageBonusAutoProductionRewards"
)

STAGE_AUTO_PRODUCTION_RECEIVE_TYPE_ONLINE = 0
STAGE_AUTO_PRODUCTION_RECEIVE_TYPE_OFFLINE = 1
STAGE_AUTO_PRODUCTION_RECEIVE_TYPE_OFFLINE_STACK = 2

# 10101 GameSetting.LuckyBonusOfflineReward* and the captured live requests.
STAGE_BONUS_DAILY_FREE_LIMIT = 1
STAGE_BONUS_DAILY_ADVERTISEMENT_LIMIT = 3
STAGE_BONUS_ADVERTISEMENT_DATA_ID = 1246517436

_REWARD_TYPE_NAMES = {
    1: "currency",
    4: "chargeable_item",
    6: "persistent_item",
}


@dataclass(frozen=True)
class StageReward:
    reward_type: str
    item_data_id: int
    amount: int


@dataclass(frozen=True)
class StageRewardCounters:
    daily_free_count: Optional[int]
    daily_advertisement_count: Optional[int]


@dataclass(frozen=True)
class StageAutoProductionRewardResult:
    reward_count: int
    rewards: tuple[StageReward, ...]
    stacked_rewards: tuple[StageReward, ...]
    stacked_duration_millis: int

    @property
    def has_stacked_rewards(self) -> bool:
        return any(reward.amount > 0 for reward in self.stacked_rewards)


@dataclass(frozen=True)
class StageBonusAutoProductionRewardResult:
    bonus_type: int
    reward_count: int
    rewards: tuple[StageReward, ...]
    daily_free_count: Optional[int]
    daily_advertisement_count: Optional[int]


def parse_signup_stage_reward_counters(
    body: bytes,
    advertisement_data_id: int = STAGE_BONUS_ADVERTISEMENT_DATA_ID,
) -> StageRewardCounters:
    """Read stage-bonus daily counters from ``SignUpResponse.crumble.progress``."""
    crumble = _message_value(pb.decode_fields(body), 3)
    if crumble is None:
        return StageRewardCounters(None, None)
    progress = _message_value(pb.decode_fields(crumble), 5)
    if progress is None:
        return StageRewardCounters(None, None)
    daily_counters = _message_value(pb.decode_fields(progress), 2)
    if daily_counters is None:
        return StageRewardCounters(None, None)
    return _parse_daily_counters(daily_counters, advertisement_data_id)


def parse_receive_stage_auto_production_rewards_response(
    body: bytes,
) -> StageAutoProductionRewardResult:
    """Parse claimed rewards and the refreshed offline stack snapshot."""
    fields = pb.decode_fields(body)
    reward_count, rewards = _parse_rewards(fields, 2)
    auto_production = _message_value(fields, 3)
    if auto_production is None:
        return StageAutoProductionRewardResult(
            reward_count=reward_count,
            rewards=rewards,
            stacked_rewards=(),
            stacked_duration_millis=0,
        )

    auto_fields = pb.decode_fields(auto_production)
    stacked_rewards = tuple(
        StageReward(
            reward_type="stacked",
            item_data_id=item_data_id,
            amount=amount,
        )
        for item_data_id, amount in sorted(_int64_map(auto_fields, 3).items())
    )
    duration = _message_value(auto_fields, 4)
    duration_millis = (
        _int_value(pb.decode_fields(duration), 1) if duration is not None else 0
    )
    return StageAutoProductionRewardResult(
        reward_count=reward_count,
        rewards=rewards,
        stacked_rewards=stacked_rewards,
        stacked_duration_millis=duration_millis,
    )


def parse_receive_stage_bonus_auto_production_rewards_response(
    body: bytes,
    advertisement_data_id: int = STAGE_BONUS_ADVERTISEMENT_DATA_ID,
) -> StageBonusAutoProductionRewardResult:
    """Parse one free/advertisement bonus claim and its updated daily counters."""
    fields = pb.decode_fields(body)
    reward_count, rewards = _parse_rewards(fields, 3)
    counters = StageRewardCounters(None, None)
    additional = _message_value(fields, 1)
    if additional is not None:
        postprocessed = _message_value(pb.decode_fields(additional), 2)
        if postprocessed is not None:
            daily_counters = _message_value(pb.decode_fields(postprocessed), 2)
            if daily_counters is not None:
                counters = _parse_daily_counters(
                    daily_counters,
                    advertisement_data_id,
                )
    return StageBonusAutoProductionRewardResult(
        bonus_type=_int_value(fields, 2),
        reward_count=reward_count,
        rewards=rewards,
        daily_free_count=counters.daily_free_count,
        daily_advertisement_count=counters.daily_advertisement_count,
    )


def _parse_daily_counters(
    body: bytes,
    advertisement_data_id: int,
) -> StageRewardCounters:
    fields = pb.decode_fields(body)
    advertisement_counts = _int64_map(fields, 3)
    return StageRewardCounters(
        daily_free_count=_int_value(fields, 2),
        daily_advertisement_count=advertisement_counts.get(
            advertisement_data_id,
            0,
        ),
    )


def _parse_rewards(fields, target: int) -> tuple[int, tuple[StageReward, ...]]:
    reward_messages = tuple(
        bytes(value)
        for field_number, wire_type, value in fields
        if field_number == target and wire_type == 2
    )
    totals: dict[tuple[str, int], int] = {}
    for reward in reward_messages:
        for field_number, wire_type, value in pb.decode_fields(reward):
            if field_number != 1 or wire_type != 2:
                continue
            element_fields = pb.decode_fields(bytes(value))
            for element_number, element_wire, element_value in element_fields:
                reward_type = _REWARD_TYPE_NAMES.get(element_number)
                if reward_type is None or element_wire != 2:
                    continue
                item_fields = pb.decode_fields(bytes(element_value))
                item_data_id = _int_value(item_fields, 1)
                amount = _int_value(item_fields, 2)
                if item_data_id:
                    key = (reward_type, item_data_id)
                    totals[key] = totals.get(key, 0) + amount
    rewards = tuple(
        StageReward(
            reward_type=reward_type,
            item_data_id=item_data_id,
            amount=amount,
        )
        for (reward_type, item_data_id), amount in sorted(totals.items())
    )
    return len(reward_messages), rewards


def _int64_map(fields, target: int) -> dict[int, int]:
    values: dict[int, int] = {}
    for field_number, wire_type, value in fields:
        if field_number != target or wire_type != 2:
            continue
        entry_fields = pb.decode_fields(bytes(value))
        values[_int_value(entry_fields, 1)] = _int_value(entry_fields, 2)
    return values


def _message_value(fields, target: int) -> bytes | None:
    for field_number, wire_type, value in fields:
        if field_number == target and wire_type == 2:
            return bytes(value)
    return None


def _int_value(fields, target: int) -> int:
    for field_number, wire_type, value in fields:
        if field_number == target and wire_type == 0:
            return int(value)
    return 0


class StageRewards:
    """Authenticated member functions for offline and stage-bonus rewards."""

    def __init__(self, client: GrpcClient, session: Session) -> None:
        self.client = client
        self.session = session

    def receive_auto_production_rewards(
        self,
        receive_type: int,
        *,
        all_monster_kill_count: Optional[int] = None,
        stage_monster_kill_count: Optional[int] = None,
    ) -> GrpcResponse:
        if (
            isinstance(receive_type, bool)
            or not isinstance(receive_type, int)
            or receive_type
            not in {
                STAGE_AUTO_PRODUCTION_RECEIVE_TYPE_ONLINE,
                STAGE_AUTO_PRODUCTION_RECEIVE_TYPE_OFFLINE,
                STAGE_AUTO_PRODUCTION_RECEIVE_TYPE_OFFLINE_STACK,
            }
        ):
            raise ValueError("receive_type must be online(0), offline(1), or stack(2)")
        for name, value in (
            ("all_monster_kill_count", all_monster_kill_count),
            ("stage_monster_kill_count", stage_monster_kill_count),
        ):
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int) or value < 0
            ):
                raise ValueError(f"{name} must be a non-negative integer or None")
        return self._unary(
            RECEIVE_STAGE_AUTO_PRODUCTION_REWARDS_PATH,
            receive_stage_auto_production_rewards_request(
                receive_type,
                all_monster_kill_count=all_monster_kill_count,
                stage_monster_kill_count=stage_monster_kill_count,
            ),
        )

    def refresh_offline_stack(self) -> GrpcResponse:
        """Refresh the server-calculated offline stack without receiving it."""
        return self.receive_auto_production_rewards(
            STAGE_AUTO_PRODUCTION_RECEIVE_TYPE_OFFLINE_STACK
        )

    def receive_offline_rewards(
        self,
        *,
        all_monster_kill_count: Optional[int] = None,
        stage_monster_kill_count: Optional[int] = None,
    ) -> GrpcResponse:
        return self.receive_auto_production_rewards(
            STAGE_AUTO_PRODUCTION_RECEIVE_TYPE_OFFLINE,
            all_monster_kill_count=all_monster_kill_count,
            stage_monster_kill_count=stage_monster_kill_count,
        )

    def receive_bonus_reward(self) -> GrpcResponse:
        """Receive one daily free stage-bonus reward."""
        return self._unary(
            RECEIVE_STAGE_BONUS_AUTO_PRODUCTION_REWARDS_PATH,
            receive_stage_bonus_auto_production_rewards_request(),
        )

    def receive_bonus_advertisement_reward(
        self,
        advertisement_data_id: int = STAGE_BONUS_ADVERTISEMENT_DATA_ID,
        *,
        skip_count: Optional[int] = None,
    ) -> GrpcResponse:
        """Receive one stage-bonus advertisement reward."""
        if isinstance(advertisement_data_id, bool) or not isinstance(
            advertisement_data_id,
            int,
        ):
            raise ValueError("advertisement_data_id must be an integer")
        if advertisement_data_id <= 0:
            raise ValueError("advertisement_data_id must be positive")
        if skip_count is not None and (
            isinstance(skip_count, bool)
            or not isinstance(skip_count, int)
            or skip_count < 0
        ):
            raise ValueError("skip_count must be a non-negative integer or None")
        return self._unary(
            RECEIVE_STAGE_BONUS_AUTO_PRODUCTION_REWARDS_PATH,
            receive_stage_bonus_auto_production_rewards_request(
                advertisement_data_id,
                skip_count=skip_count,
            ),
        )

    def _unary(self, path: str, message: bytes) -> GrpcResponse:
        response = self.client.unary(
            path,
            message,
            metadata=build_metadata(self.session),
        )
        self.session.adopt_resource_key(response.headers)
        return response
