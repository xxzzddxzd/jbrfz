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
from .grpc_client import GrpcClient, GrpcResponse
from .headers import Session, build_metadata

log = logging.getLogger(__name__)

PAY_ASSETS_FORCIBLY_PATH = "/cc.public.game.CheatService/PayAssetsForcibly"

_INT32_MAX = (1 << 31) - 1
_INT64_MAX = (1 << 63) - 1


@dataclass(frozen=True)
class PayAssetsForciblyCommand:
    """One asset payment in ``PayAssetsForciblyRequest.commands``."""

    asset_data_id: int
    amount: int


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
        response = self.client.unary(
            PAY_ASSETS_FORCIBLY_PATH,
            body,
            metadata=build_metadata(self.session),
        )
        if self.session.adopt_resource_key(response.headers):
            log.debug("resource_key <- %s", self.session.resource_key)
        return response


def _positive_int(value: int, name: str, *, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer")
    if value <= 0:
        raise ValueError(f"{name} must be positive")
    if value > maximum:
        raise ValueError(f"{name} exceeds protobuf integer range")
    return value
