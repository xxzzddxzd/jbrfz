"""Server-state red-dot scanner and zero-cost reward cleaner.

The Unity ``RedDotTree`` itself is client-local.  This module reproduces the
actionable, zero-cost subset from SignUp state plus the live patch tables and
then calls the same reward RPCs used by the client.
"""
from __future__ import annotations

import struct
from datetime import datetime
from typing import Any, Iterable, Mapping, Sequence
from zoneinfo import ZoneInfo

from . import pbutil as pb
from .crumble_dungeon import parse_signup_cookie_ids
from .daily_runner import DailyRunner
from .grpc_client import GrpcClient, GrpcResponse
from .headers import Session, build_metadata
from .patch_data import PatchData

SIGNUP_PATH = "/cc.public.game.CrumbleService/SignUp"
CLEAR_MISSION_REQUIREMENTS_PATH = (
    "/cc.public.game.TaskService/ClearMissionRequirements"
)
CLEAR_MISSION_ACHIEVEMENTS_PATH = (
    "/cc.public.game.TaskService/ClearMissionAchievements"
)
RECEIVE_MISSION_LEVEL_REWARDS_PATH = (
    "/cc.public.game.TaskService/ReceiveMissionLevelRewards"
)
RECEIVE_CRUMBLE_LEVEL_REWARDS_PATH = (
    "/cc.public.game.UserService/ReceiveCrumbleLevelRewards"
)
RECEIVE_CRUMBLE_DUNGEON_ACHIEVEMENT_REWARDS_PATH = (
    "/cc.public.game.DungeonService/ReceiveCrumbleDungeonAchievementRewards"
)
COLLECT_PET_CAMP_GIFTS_PATH = (
    "/cc.public.game.CollectionService/CollectPetCampGifts"
)
RECEIVE_PASS_REWARDS_PATH = "/cc.public.game.PassService/ReceivePassRewards"
RECEIVE_SWEET_BLESSING_DAILY_REWARD_PATH = (
    "/cc.public.game.SweetBlessingService/ReceiveSweetBlessingDailyReward"
)
GET_GUILD_GIFTS_PATH = "/cc.public.game.GuildMemberService/GetGuildGifts"
CLAIM_GUILD_GIFTS_PATH = "/cc.public.game.GuildMemberService/ClaimGuildGifts"

MISSION_POINT_ITEM_ID = 2076950184
CRUMBLE_EXP_ITEM_ID = 2004501366
BLESSING_POINT_ITEM_ID = 1593848422
_LOCAL_TIMEZONE = ZoneInfo("Asia/Shanghai")


class RedDotRunner:
    """Clear the safe red-dot subset for one authenticated account."""

    def __init__(
        self,
        client: GrpcClient,
        session: Session,
        patch_data: PatchData,
        *,
        include_daily: bool = True,
    ) -> None:
        self.client = client
        self.session = session
        self.patch_data = patch_data
        self.include_daily = bool(include_daily)
        self._items = {
            int(row["id"]): row
            for row in patch_data.rows("items")
            if _as_int(row.get("id")) > 0
        }

    def run(self) -> dict[str, Any]:
        signup_before = self.signup()
        before_assets = parse_signup_assets(signup_before)
        actions: list[dict[str, Any]] = []

        if self.include_daily:
            daily = DailyRunner(
                self.client,
                self.session,
                include_crumble_dungeon=True,
            ).run()
            actions.append(
                {
                    "key": "daily_free_actions",
                    "ok": daily.ok,
                    "attempted": True,
                    "details": daily.to_dict(),
                    "error": daily.error,
                }
            )

        # These actions can unlock one another.  Refresh SignUp between each
        # category and make a second mission pass for chained achievements.
        for _ in range(3):
            signup = self.signup()
            mission_actions = self._claim_missions(signup)
            actions.extend(mission_actions)
            if not any(action.get("attempted") for action in mission_actions):
                break

        signup = self.signup()
        actions.extend(self._claim_mission_levels(signup))
        signup = self.signup()
        actions.extend(self._claim_crumble_level_rewards(signup))
        signup = self.signup()
        actions.extend(self._claim_crumble_dungeon_achievements(signup))
        signup = self.signup()
        actions.extend(self._collect_pet_camp_gifts(signup))
        signup = self.signup()
        actions.extend(self._claim_season_pass_rewards(signup))
        signup = self.signup()
        actions.extend(self._claim_sweet_blessing_daily(signup))
        signup = self.signup()
        actions.extend(self._claim_guild_gifts(signup))

        signup_after = self.signup()
        after_assets = parse_signup_assets(signup_after)
        gains = _asset_delta(before_assets, after_assets)
        remaining = self.scan(signup_after)
        attempted = sum(1 for action in actions if action.get("attempted"))
        failed = sum(
            1
            for action in actions
            if action.get("attempted") and not action.get("ok", False)
        )
        reward_totals = _merge_reward_totals(
            action.get("rewards", []) for action in actions
        )
        return {
            "ok": failed == 0,
            "mid": self.session.mid,
            "name": parse_signup_profile_name(signup_after),
            "resource_key": self.patch_data.resource_key,
            "actions_detected": sum(
                int(action.get("detected_count") or 0) for action in actions
            ),
            "actions_attempted": attempted,
            "actions_failed": failed,
            "actions": actions,
            "assets_before": self._describe_assets(before_assets),
            "assets_after": self._describe_assets(after_assets),
            "gains": self._describe_assets(gains),
            "response_reward_totals": self._describe_assets(reward_totals),
            "remaining_safe_red_dots": remaining,
            "remaining_safe_count": sum(
                int(candidate.get("count") or 0) for candidate in remaining
            ),
            "cookie_ids": list(parse_signup_cookie_ids(signup_after)),
        }

    def signup(self) -> bytes:
        return self._unary(SIGNUP_PATH, b"").message

    def scan(self, signup: bytes) -> list[dict[str, Any]]:
        """Return safe, currently claimable actions without mutating state."""
        candidates: list[dict[str, Any]] = []
        mission = self._mission_candidates(signup)
        for key in ("daily", "weekly", "achievement"):
            values = mission[key]
            if values:
                candidates.append(
                    {"key": f"mission_{key}", "count": len(values), "ids": values}
                )
        levels = self._mission_level_candidates(signup)
        if levels:
            candidates.append({"key": "mission_level", "count": len(levels), "levels": levels})
        orders = self._crumble_level_reward_candidates(signup)
        if orders:
            candidates.append({"key": "crumble_level_reward", "count": len(orders), "orders": orders})
        orders = self._crumble_dungeon_achievement_candidates(signup)
        if orders:
            candidates.append({"key": "crumble_dungeon_achievement", "count": len(orders), "orders": orders})
        gift_amount = self._pet_camp_gift_amount(signup)
        if gift_amount > 0:
            candidates.append({"key": "pet_camp_gift", "count": gift_amount})
        for candidate in self._season_pass_candidates(signup):
            candidates.append(
                {
                    "key": "season_pass_reward",
                    "count": len(candidate["free_levels"]) + len(candidate["special_levels"]),
                    **candidate,
                }
            )
        if self._sweet_blessing_daily_claimable(signup):
            candidates.append({"key": "sweet_blessing_daily", "count": 1})
        guild_id = parse_signup_guild_id(signup)
        if guild_id and _message(_crumble(signup), 16):
            # Gift ids require the explicit GetGuildGifts refresh and are
            # therefore discovered during execution, not a SignUp-only scan.
            member_state = _message(_message(_crumble(signup), 16), 1)
            if _message(member_state, 16):
                candidates.append({"key": "guild_gift_refresh", "count": 1})
        return candidates

    def _claim_missions(self, signup: bytes) -> list[dict[str, Any]]:
        candidates = self._mission_candidates(signup)
        actions: list[dict[str, Any]] = []
        for key, mission_type in (("daily", 0), ("weekly", 1)):
            mission_ids = candidates[key]
            if not mission_ids:
                continue
            request = b"".join(
                (
                    pb.encode_packed_int32_field(1, mission_ids),
                    pb.encode_int32_field(2, mission_type),
                )
            )
            actions.append(
                self._execute(
                    f"mission_{key}",
                    CLEAR_MISSION_REQUIREMENTS_PATH,
                    request,
                    detected_count=len(mission_ids),
                    identifiers=mission_ids,
                )
            )
        achievements = candidates["achievement"]
        if achievements:
            actions.append(
                self._execute(
                    "mission_achievement",
                    CLEAR_MISSION_ACHIEVEMENTS_PATH,
                    pb.encode_packed_int32_field(1, achievements),
                    detected_count=len(achievements),
                    identifiers=achievements,
                )
            )
        return actions

    def _claim_mission_levels(self, signup: bytes) -> list[dict[str, Any]]:
        levels = self._mission_level_candidates(signup)
        if not levels:
            return []
        return [
            self._execute(
                "mission_level",
                RECEIVE_MISSION_LEVEL_REWARDS_PATH,
                pb.encode_packed_int32_field(1, levels),
                detected_count=len(levels),
                identifiers=levels,
            )
        ]

    def _claim_crumble_level_rewards(self, signup: bytes) -> list[dict[str, Any]]:
        orders = self._crumble_level_reward_candidates(signup)
        if not orders:
            return []
        return [
            self._execute(
                "crumble_level_reward",
                RECEIVE_CRUMBLE_LEVEL_REWARDS_PATH,
                pb.encode_packed_int32_field(1, orders),
                detected_count=len(orders),
                identifiers=orders,
            )
        ]

    def _claim_crumble_dungeon_achievements(self, signup: bytes) -> list[dict[str, Any]]:
        orders = self._crumble_dungeon_achievement_candidates(signup)
        if not orders:
            return []
        return [
            self._execute(
                "crumble_dungeon_achievement",
                RECEIVE_CRUMBLE_DUNGEON_ACHIEVEMENT_REWARDS_PATH,
                pb.encode_packed_int32_field(1, orders),
                detected_count=len(orders),
                identifiers=orders,
            )
        ]

    def _collect_pet_camp_gifts(self, signup: bytes) -> list[dict[str, Any]]:
        amount = self._pet_camp_gift_amount(signup)
        if amount <= 0:
            return []
        return [
            self._execute(
                "pet_camp_gift",
                COLLECT_PET_CAMP_GIFTS_PATH,
                pb.encode_int64_field(1, amount),
                detected_count=amount,
                identifiers=[amount],
            )
        ]

    def _claim_season_pass_rewards(self, signup: bytes) -> list[dict[str, Any]]:
        actions: list[dict[str, Any]] = []
        for candidate in self._season_pass_candidates(signup):
            request = b"".join(
                (
                    pb.encode_int32_field(1, candidate["pass_id"]),
                    pb.encode_packed_int32_field(2, candidate["free_levels"]),
                    pb.encode_packed_int32_field(3, candidate["special_levels"]),
                )
            )
            actions.append(
                self._execute(
                    "season_pass_reward",
                    RECEIVE_PASS_REWARDS_PATH,
                    request,
                    detected_count=(
                        len(candidate["free_levels"])
                        + len(candidate["special_levels"])
                    ),
                    identifiers=[candidate],
                )
            )
        return actions

    def _claim_sweet_blessing_daily(self, signup: bytes) -> list[dict[str, Any]]:
        if not self._sweet_blessing_daily_claimable(signup):
            return []
        return [
            self._execute(
                "sweet_blessing_daily",
                RECEIVE_SWEET_BLESSING_DAILY_REWARD_PATH,
                b"",
                detected_count=1,
                identifiers=[],
            )
        ]

    def _claim_guild_gifts(self, signup: bytes) -> list[dict[str, Any]]:
        guild_id = parse_signup_guild_id(signup)
        if not guild_id:
            return []
        try:
            response = self._unary(
                GET_GUILD_GIFTS_PATH,
                pb.encode_string_field(1, guild_id),
            )
            gift_ids = []
            for gift in _messages(response.message, 1):
                gift_id = _string(gift, 1)
                claimed = bool(_varint(gift, 6))
                if gift_id and not claimed:
                    gift_ids.append(gift_id)
            if not gift_ids:
                return []
            request = pb.encode_string_field(1, guild_id) + b"".join(
                pb.encode_string_field(2, gift_id) for gift_id in gift_ids
            )
            return [
                self._execute(
                    "guild_gift",
                    CLAIM_GUILD_GIFTS_PATH,
                    request,
                    detected_count=len(gift_ids),
                    identifiers=gift_ids,
                )
            ]
        except Exception as error:  # noqa: BLE001 - preserve per-action result
            return [
                {
                    "key": "guild_gift",
                    "ok": False,
                    "attempted": True,
                    "detected_count": 0,
                    "error": f"{type(error).__name__}: {error}",
                }
            ]

    def _mission_candidates(self, signup: bytes) -> dict[str, list[int]]:
        tasks = _message(_crumble(signup), 8)
        missions_state = _message(tasks, 3)
        requirements = {
            _varint(requirement, 1): tuple(
                _varint(unit, 1) for unit in _messages(requirement, 2)
            )
            for requirement in _messages(tasks, 1)
        }
        requirements.pop(0, None)
        daily_ids = set(_repeated_ints(_message(missions_state, 1), 1))
        weekly_ids = set(_repeated_ints(_message(missions_state, 2), 1))
        received_achievements = set(_repeated_ints(missions_state, 3))
        result: dict[str, list[int]] = {
            "daily": [],
            "weekly": [],
            "achievement": [],
        }
        for row in self.patch_data.rows("missions"):
            mission_id = _as_int(row.get("id"))
            if mission_id not in requirements:
                continue
            required = tuple(
                _as_int(value) for value in row.get("clearRequirementValues", [])
            )
            current = requirements[mission_id]
            if not required or len(current) < len(required):
                continue
            if not all(now >= target for now, target in zip(current, required)):
                continue
            mission_type = str(row.get("missionType") or "")
            if mission_id in daily_ids:
                result["daily"].append(mission_id)
            elif mission_id in weekly_ids:
                result["weekly"].append(mission_id)
            elif (
                mission_type == "MISSIONTYPE_ACHIEVEMENT_MISSION"
                and mission_id not in received_achievements
            ):
                result["achievement"].append(mission_id)
        for values in result.values():
            values.sort()
        return result

    def _mission_level_candidates(self, signup: bytes) -> list[int]:
        crumble = _crumble(signup)
        missions_state = _message(_message(crumble, 8), 3)
        received = set(_repeated_ints(missions_state, 4))
        points = parse_signup_assets(signup).get(MISSION_POINT_ITEM_ID, 0)
        return sorted(
            _as_int(row.get("missionLevel"))
            for row in self.patch_data.rows("missionLevels")
            if _as_int(row.get("missionLevel")) not in received
            and _as_int(row.get("cumulativeRequiredAmount")) <= points
            and _as_int(row.get("rewardAmount")) > 0
        )

    def _crumble_level_reward_candidates(self, signup: bytes) -> list[int]:
        crumble = _crumble(signup)
        received = set(_repeated_ints(_message(crumble, 1), 5))
        experience = parse_signup_assets(signup).get(CRUMBLE_EXP_ITEM_ID, 0)
        level = max(
            (
                _as_int(row.get("crumbleLevel"))
                for row in self.patch_data.rows("crumbleLevels")
                if _as_int(row.get("cumulativeRequiredAmount")) <= experience
            ),
            default=1,
        )
        return sorted(
            _as_int(row.get("rewardOrder"))
            for row in self.patch_data.rows("crumbleLevelRewards")
            if _as_int(row.get("rewardOrder")) not in received
            and _as_int(row.get("requiredCrumbleLevel")) <= level
        )

    def _crumble_dungeon_achievement_candidates(self, signup: bytes) -> list[int]:
        dungeon = _message(_message(_message(_crumble(signup), 6), 2), 1)
        received = set(_repeated_ints(dungeon, 2))
        score = _double(_message(dungeon, 3), 1)
        return sorted(
            _as_int(row.get("rewardOrder"))
            for row in self.patch_data.rows("crumbleAchievementRewards")
            if _as_int(row.get("rewardOrder")) not in received
            and float(_as_int(row.get("achievementScore"))) <= score
        )

    @staticmethod
    def _pet_camp_gift_amount(signup: bytes) -> int:
        pet_camp = _message(_message(_crumble(signup), 2), 4)
        return _varint(_message(pet_camp, 3), 1)

    def _season_pass_candidates(self, signup: bytes) -> list[dict[str, Any]]:
        passes = _message(_crumble(signup), 13)
        candidates: list[dict[str, Any]] = []
        rewards_by_pass: dict[int, list[dict[str, Any]]] = {}
        for row in self.patch_data.rows("seasonPassRewards"):
            rewards_by_pass.setdefault(_as_int(row.get("id")), []).append(row)
        for season_pass in _messages(passes, 1):
            pass_id = _varint(season_pass, 1)
            points = _varint(season_pass, 2)
            received_free = set(_repeated_ints(season_pass, 3))
            received_special = set(_repeated_ints(season_pass, 4))
            can_receive_special = bool(_varint(season_pass, 5))
            free_levels: list[int] = []
            special_levels: list[int] = []
            for row in rewards_by_pass.get(pass_id, []):
                level = _as_int(row.get("level"))
                required = _as_int(row.get("cumulativeRequiredPassPointAmount"))
                if required > points:
                    continue
                if level not in received_free:
                    free_levels.append(level)
                if can_receive_special and level not in received_special:
                    special_levels.append(level)
            if free_levels or special_levels:
                candidates.append(
                    {
                        "pass_id": pass_id,
                        "free_levels": sorted(free_levels),
                        "special_levels": sorted(special_levels),
                    }
                )
        return candidates

    @staticmethod
    def _sweet_blessing_daily_claimable(signup: bytes) -> bool:
        shops = _message(_crumble(signup), 9)
        sweet_blessing = _message(shops, 3)
        if not sweet_blessing:
            return False
        claimed_millis = _varint(_message(sweet_blessing, 2), 1)
        if claimed_millis <= 0:
            return True
        claimed_day = datetime.fromtimestamp(
            claimed_millis / 1000,
            tz=_LOCAL_TIMEZONE,
        ).date()
        return claimed_day < datetime.now(_LOCAL_TIMEZONE).date()

    def _execute(
        self,
        key: str,
        path: str,
        request: bytes,
        *,
        detected_count: int,
        identifiers: Sequence[Any],
    ) -> dict[str, Any]:
        result: dict[str, Any] = {
            "key": key,
            "ok": False,
            "attempted": True,
            "detected_count": int(detected_count),
            "identifiers": list(identifiers),
        }
        try:
            response = self._unary(path, request)
            result["ok"] = True
            result["rewards"] = parse_reward_response(response.message)
        except Exception as error:  # noqa: BLE001 - preserve per-action result
            result["error"] = f"{type(error).__name__}: {error}"
        return result

    def _unary(self, path: str, body: bytes) -> GrpcResponse:
        response = self.client.unary(
            path,
            body,
            metadata=build_metadata(self.session),
        )
        self.session.adopt_resource_key(response.headers)
        return response

    def _describe_assets(self, assets: Mapping[int, int]) -> list[dict[str, Any]]:
        result = []
        for data_id, amount in sorted(assets.items()):
            if amount == 0:
                continue
            item = self._items.get(int(data_id), {})
            name = item.get("name") if isinstance(item.get("name"), dict) else {}
            result.append(
                {
                    "data_id": int(data_id),
                    "name": str(name.get("originalText") or ""),
                    "tag": str(item.get("itemHardcodingTag") or ""),
                    "amount": int(amount),
                }
            )
        return result


def parse_signup_assets(body: bytes) -> dict[int, int]:
    """Read currency, chargeable and persistent item balances from SignUp."""
    inventory = _message(_crumble(body), 3)
    assets: dict[int, int] = {}
    for field_number in (1, 3, 5):
        for item in _messages(inventory, field_number):
            data_id = _varint(item, 1)
            if data_id:
                assets[data_id] = _varint(item, 2)
    return assets


def parse_signup_profile_name(body: bytes) -> str:
    return _string(_message(_crumble(body), 1), 1)


def parse_signup_guild_id(body: bytes) -> str:
    affiliation = _message(_crumble(body), 16)
    return _string(_message(affiliation, 1), 1)


def parse_reward_response(body: bytes) -> list[dict[str, int]]:
    """Aggregate item-like rewards from a standard mutation response."""
    totals: dict[int, int] = {}
    for reward in _messages(body, 2):
        elements = list(_messages(reward, 1))
        for extra_field in (2, 3):
            extra = _message(reward, extra_field)
            elements.extend(_messages(extra, 1))
        for element in elements:
            for field_number, wire_type, value in pb.decode_fields(element):
                if wire_type != 2:
                    continue
                value = bytes(value)
                if field_number in (1, 4, 5, 6):
                    data_id = _varint(value, 1)
                    amount = _varint(value, 2, default=1)
                elif field_number == 9:
                    data_id = _varint(value, 1)
                    amount = _varint(value, 3)
                elif field_number == 2:
                    preexisting = _message(value, 2)
                    data_id = _varint(preexisting, 1)
                    amount = _varint(preexisting, 2)
                elif field_number == 3:
                    preexisting = _message(value, 2)
                    data_id = _varint(preexisting, 1)
                    amount = _varint(preexisting, 2)
                else:
                    continue
                if data_id and amount:
                    totals[data_id] = totals.get(data_id, 0) + amount
    return [
        {"data_id": data_id, "amount": amount}
        for data_id, amount in sorted(totals.items())
    ]


def _crumble(signup: bytes) -> bytes:
    return _message(signup, 3)


def _message(body: bytes, target: int) -> bytes:
    for field_number, wire_type, value in pb.decode_fields(body or b""):
        if field_number == target and wire_type == 2:
            return bytes(value)
    return b""


def _messages(body: bytes, target: int) -> list[bytes]:
    return [
        bytes(value)
        for field_number, wire_type, value in pb.decode_fields(body or b"")
        if field_number == target and wire_type == 2
    ]


def _varint(body: bytes, target: int, default: int = 0) -> int:
    for field_number, wire_type, value in pb.decode_fields(body or b""):
        if field_number == target and wire_type == 0:
            return int(value)
    return int(default)


def _repeated_ints(body: bytes, target: int) -> list[int]:
    values: list[int] = []
    for field_number, wire_type, value in pb.decode_fields(body or b""):
        if field_number != target:
            continue
        if wire_type == 0:
            values.append(int(value))
        elif wire_type == 2:
            values.extend(pb.decode_packed_varints(bytes(value)))
    return values


def _string(body: bytes, target: int) -> str:
    value = _message(body, target)
    if not value:
        return ""
    try:
        return value.decode("utf-8")
    except UnicodeDecodeError:
        return ""


def _double(body: bytes, target: int) -> float:
    for field_number, wire_type, value in pb.decode_fields(body or b""):
        if field_number == target and wire_type == 1 and len(value) == 8:
            return float(struct.unpack("<d", bytes(value))[0])
    return 0.0


def _as_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _asset_delta(before: Mapping[int, int], after: Mapping[int, int]) -> dict[int, int]:
    return {
        data_id: after.get(data_id, 0) - before.get(data_id, 0)
        for data_id in before.keys() | after.keys()
        if after.get(data_id, 0) != before.get(data_id, 0)
    }


def _merge_reward_totals(groups: Iterable[Iterable[Mapping[str, Any]]]) -> dict[int, int]:
    totals: dict[int, int] = {}
    for rewards in groups:
        for reward in rewards:
            data_id = _as_int(reward.get("data_id"))
            amount = _as_int(reward.get("amount"))
            if data_id and amount:
                totals[data_id] = totals.get(data_id, 0) + amount
    return totals
