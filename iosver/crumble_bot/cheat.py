"""Internal CheatService RPC helpers.

The generated 10101 client exposes this service, but production accounts may
not be authorized to call it.  This module only builds and submits the wire
request; server permission checks remain authoritative.
"""
from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass

from . import pbutil as pb
from .grpc_client import GrpcClient, GrpcError, GrpcResponse
from .headers import Session, build_metadata

log = logging.getLogger(__name__)

CHEAT_SERVICE_NAME = "cc.public.game.CheatService"
CHEAT_PURE_SERVICE_NAME = "cc.public.game.CheatPureService"

CHEAT_SERVICE_METHODS = (
    "GetCommonStatBoostsMap",
    "GetGameBoosts",
    "GetStatBoosts",
    "GetBattleTeamDetailStatsForDebug",
    "GetArenaChecksumDebug",
    "ReceiveAssetsForcibly",
    "PayAssetsForcibly",
    "ChangePersistentItemsForcibly",
    "ChangeCrumbleNameForcibly",
    "CompleteStageForcibly",
    "ResetAllPromotesForcibly",
    "ChangeArenaRatingForcibly",
    "ResetArenaOpponentPoolForcibly",
    "ReplaceArenaOpponentForcibly",
    "ChangeCookieSpecsForcibly",
    "ChangePetSpecsForcibly",
    "ChangeCompletedLabResearchesForcibly",
    "ChangeRushPowerForcibly",
    "ChangeFameForcibly",
    "UpgradePlateWithResult",
    "ChangePlateGradeForcibly",
    "ChangeContentsUnlockHistoryForcibly",
    "ChangeCutsceneHistoryForcibly",
    "ChangeAdventureNoteHistoryForcibly",
    "ChangeTutorialHistoryForcibly",
    "ChangePetCampLevelForcibly",
    "PatchBattleTeamsForcibly",
    "ChangeDailyActionCountersForcibly",
    "ChangeActionCountersForcibly",
    "ChangePeriodicMissionsForcibly",
    "ChangeGuidesForcibly",
    "ChangeAchievementMissionsForcibly",
    "ChangeOvenLevelForcibly",
    "ChangeEquipmentsForcibly",
    "AttendForcibly",
    "PurgeInvalidStatesForcibly",
    "ChangeEventMissionsForcibly",
    "ResetSweetBlessingForcibly",
    "ResetContentsShopsForcibly",
    "ChangeRequirementUnitCountsForcibly",
    "FireNotification",
    "RemoveGuildCreationCooldownForcibly",
    "RemoveGuildJoinCooldownForcibly",
    "CreateDummyGuildInvitationForcibly",
    "GainGuildMasterRoleForcibly",
    "ChangeMercenaryBandLevelForcibly",
    "UnlockMercenaryBandPerksForcibly",
    "ChangeConstellationStarshardsForcibly",
    "ChangeDailyDungeonLevelForcibly",
    "ChangeImplantTowerDungeonLevelForcibly",
)

CHEAT_PURE_SERVICE_METHODS = (
    "Fail",
    "GetContextResourceKey",
    "GetGuestSecret",
    "SetGuestSecret",
    "GetUserSnapshot",
    "SetUserSnapshot",
    "ChangeGuildExperienceForcibly",
    "ChangeGuildLabResearchPointForcibly",
    "ChangeGuildMemberContributionForcibly",
    "ResetGuildDailyBanishCount",
    "ResetGuildDailyRecruitmentCount",
    "CreateDummyGuildForcibly",
    "AddDummyGuildMembersForcibly",
    "CalculateArenaBotCombatPowers",
)

CHEAT_SERVICE_METHODS_BY_NAME = {
    CHEAT_SERVICE_NAME: CHEAT_SERVICE_METHODS,
    CHEAT_PURE_SERVICE_NAME: CHEAT_PURE_SERVICE_METHODS,
}

PAY_ASSETS_FORCIBLY_PATH = (
    f"/{CHEAT_SERVICE_NAME}/PayAssetsForcibly"
)

_INT32_MAX = (1 << 31) - 1
_INT64_MAX = (1 << 63) - 1


@dataclass(frozen=True)
class PayAssetsForciblyCommand:
    """One asset payment in ``PayAssetsForciblyRequest.commands``."""

    asset_data_id: int
    amount: int


@dataclass(frozen=True)
class CheatMethodProbe:
    """Non-mutating route-discovery result for one generated RPC."""

    service: str
    method: str
    path: str
    exists: bool
    grpc_status: int
    message: str


def pay_assets_forcibly_request(
    commands: Iterable[PayAssetsForciblyCommand],
) -> bytes:
    """Encode a 10101 ``PayAssetsForciblyRequest``.

    The outer request contains repeated ``commands`` at field 1.  Each command
    contains ``asset_data_id`` (int32 field 1) and ``amount`` (int64 field 2).
    """
    if isinstance(commands, (str, bytes)):
        raise ValueError("commands must be an iterable of payment commands")

    encoded_commands: list[bytes] = []
    for index, command in enumerate(commands):
        if not isinstance(command, PayAssetsForciblyCommand):
            raise ValueError(
                f"commands[{index}] must be PayAssetsForciblyCommand"
            )
        asset_data_id = _positive_int(
            command.asset_data_id,
            f"commands[{index}].asset_data_id",
            maximum=_INT32_MAX,
        )
        amount = _positive_int(
            command.amount,
            f"commands[{index}].amount",
            maximum=_INT64_MAX,
        )
        encoded_commands.append(
            pb.encode_int32_field(1, asset_data_id)
            + pb.encode_int64_field(2, amount)
        )

    if not encoded_commands:
        raise ValueError("commands must not be empty")
    return pb.encode_repeated_messages(1, encoded_commands)


class Cheat:
    """CheatService facade bound to one authenticated game session."""

    def __init__(self, client: GrpcClient, session: Session) -> None:
        self.client = client
        self.session = session

    def pay_assets_forcibly(
        self,
        asset_data_id: int,
        amount: int,
    ) -> GrpcResponse:
        """Force payment of ``amount`` units of one asset.

        Args:
            asset_data_id: Positive 32-bit game asset data ID.
            amount: Positive 64-bit quantity to deduct.

        The normal 10101 ``CheatApi.RemoveCurrencyAsync`` wrapper sends one
        command per call, which this member function mirrors.
        """
        body = pay_assets_forcibly_request(
            (PayAssetsForciblyCommand(asset_data_id, amount),)
        )
        return self.call(
            "PayAssetsForcibly",
            body,
        )

    def call(
        self,
        method: str,
        body: bytes,
        *,
        service: str = CHEAT_SERVICE_NAME,
    ) -> GrpcResponse:
        """Call one registered generated cheat RPC with an encoded request."""
        path = _method_path(service, method)
        if not isinstance(body, bytes):
            raise ValueError("body must be bytes")
        response = self.client.unary(
            path,
            body,
            metadata=build_metadata(self.session),
        )
        if self.session.adopt_resource_key(response.headers):
            log.debug("resource_key <- %s", self.session.resource_key)
        return response

    def probe_method(
        self,
        method: str,
        *,
        service: str = CHEAT_SERVICE_NAME,
    ) -> CheatMethodProbe:
        """Safely test whether a generated RPC route exists on the server.

        A deliberately truncated protobuf varint is sent so an existing route
        fails during request deserialization before its handler can mutate
        account state.  The live server's exact ``UNIMPLEMENTED`` +
        ``Method not found`` response is classified as missing; every other
        response proves that routing reached the named method.
        """
        path = _method_path(service, method)
        try:
            response = self.client.unary(
                path,
                b"\x80",
                metadata=build_metadata(self.session),
            )
        except GrpcError as error:
            message = str(error.message or "")
            missing = error.status == 12 and "method not found" in message.lower()
            return CheatMethodProbe(
                service=service,
                method=method,
                path=path,
                exists=not missing,
                grpc_status=int(error.status),
                message=message,
            )

        self.session.adopt_resource_key(response.headers)
        return CheatMethodProbe(
            service=service,
            method=method,
            path=path,
            exists=True,
            grpc_status=0,
            message="unexpected_success",
        )

    def probe_methods(self) -> tuple[CheatMethodProbe, ...]:
        """Probe all 10101 CheatService and CheatPureService method routes."""
        return tuple(
            self.probe_method(method, service=service)
            for service, methods in CHEAT_SERVICE_METHODS_BY_NAME.items()
            for method in methods
        )


def _positive_int(value: int, name: str, *, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer")
    if value <= 0:
        raise ValueError(f"{name} must be positive")
    if value > maximum:
        raise ValueError(f"{name} exceeds protobuf integer range")
    return value


def _method_path(service: str, method: str) -> str:
    methods = CHEAT_SERVICE_METHODS_BY_NAME.get(service)
    if methods is None:
        raise ValueError(f"unknown cheat service: {service}")
    if method not in methods:
        raise ValueError(f"unknown {service} method: {method}")
    return f"/{service}/{method}"
