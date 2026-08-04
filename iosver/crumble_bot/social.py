"""Read-only social profile lookup helpers."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from . import pbutil as pb
from .grpc_client import GrpcClient, GrpcResponse
from .headers import Session, build_metadata
from .messages import get_user_social_info_request

GET_USER_SOCIAL_INFO_PATH = (
    "/cc.public.game.SocialPureService/GetUserSocialInfo"
)


@dataclass(frozen=True)
class UserSocialInfo:
    user_id: str
    crumble_level: int
    name: str


def _string_value(fields: list[tuple[int, int, object]], number: int) -> str:
    for field_number, wire_type, value in fields:
        if field_number == number and wire_type == 2:
            return bytes(value).decode("utf-8")
    return ""


def _int_value(fields: list[tuple[int, int, object]], number: int) -> int:
    for field_number, wire_type, value in fields:
        if field_number == number and wire_type == 0:
            return int(value)
    return 0


def parse_get_user_social_info_response(body: bytes) -> tuple[UserSocialInfo, ...]:
    """Parse repeated UserSocialInfo entries: MID, level, and game name."""
    results: list[UserSocialInfo] = []
    for field_number, wire_type, value in pb.decode_fields(body):
        if field_number != 1 or wire_type != 2:
            continue
        fields = pb.decode_fields(bytes(value))
        user_id = _string_value(fields, 1)
        if not user_id:
            continue
        results.append(
            UserSocialInfo(
                user_id=user_id,
                crumble_level=_int_value(fields, 2),
                name=_string_value(fields, 3),
            )
        )
    return tuple(results)


class Social:
    """SocialPureService facade bound to an authenticated game session."""

    def __init__(self, client: GrpcClient, session: Session) -> None:
        self.client = client
        self.session = session

    def get_user_social_info(self, user_ids: Sequence[str]) -> GrpcResponse:
        normalized = tuple(
            value.strip()
            for value in user_ids
            if isinstance(value, str) and value.strip()
        )
        if not normalized:
            raise ValueError("user_ids must contain at least one MID")
        response = self.client.unary(
            GET_USER_SOCIAL_INFO_PATH,
            get_user_social_info_request(normalized),
            metadata=build_metadata(self.session),
        )
        self.session.adopt_resource_key(response.headers)
        return response
