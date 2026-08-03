"""Guild-related game RPCs."""
from __future__ import annotations

import logging
import struct
from dataclasses import dataclass
from typing import Callable

from . import pbutil as pb
from .grpc_client import GrpcClient, GrpcResponse
from .headers import Session, build_metadata
from .messages import (
    attend_guild_request,
    conduct_free_guild_lab_research_request,
    conduct_paid_guild_lab_research_request,
    get_guild_request,
    join_guild_request,
    leave_guild_request,
    search_guilds_request,
)

log = logging.getLogger(__name__)

SEARCH_GUILDS_PATH = "/cc.public.game.GuildDiscoveryService/SearchGuilds"
JOIN_GUILD_PATH = "/cc.public.game.GuildDiscoveryService/JoinGuild"
LEAVE_GUILD_PATH = "/cc.public.game.GuildMemberService/LeaveGuild"
GET_GUILD_PATH = "/cc.public.game.GuildMemberService/GetGuild"
CONDUCT_FREE_GUILD_LAB_RESEARCH_PATH = (
    "/cc.public.game.GuildMemberService/ConductFreeGuildLabResearch"
)
CONDUCT_PAID_GUILD_LAB_RESEARCH_PATH = (
    "/cc.public.game.GuildMemberService/ConductPaidGuildLabResearch"
)
ATTEND_GUILD_PATH = "/cc.public.game.GuildMemberService/AttendGuild"

GuildIdRequestBuilder = Callable[[str], bytes]


@dataclass(frozen=True)
class GuildSearchSummary:
    guild_id: str
    name: str
    master_user_id: str
    master_name: str
    guild_level: int
    member_count: int
    description: str = ""
    join_method: int = 0
    total_combat_power: float = 0.0
    master_crumble_level: int = 0
    master_profile_image_data_id: int = 0
    master_profile_frame_data_id: int = 0
    master_profile_title_data_id: int = 0
    master_channel_id: int = 0
    emblem_symbol_data_id: int = 0
    emblem_badge_data_id: int = 0


@dataclass(frozen=True)
class GuildDetail:
    name: str
    master_name: str
    description: str
    join_method: int
    total_combat_power: float
    member_ids: tuple[str, ...]
    total_experience: int = 0
    announcement: str = ""


def parse_guild_search_response(body: bytes) -> list[GuildSearchSummary]:
    """Parse the identifying fields from SearchGuildsResponse."""
    summaries: list[GuildSearchSummary] = []
    for field_number, wire_type, value in pb.decode_fields(body):
        if field_number != 1 or wire_type != 2:
            continue
        fields = pb.decode_fields(bytes(value))
        master = _message_value(fields, 5)
        master_fields = pb.decode_fields(master) if master is not None else []
        settings = _message_value(fields, 3)
        settings_fields = pb.decode_fields(settings) if settings is not None else []
        guild_id = _string_value(fields, 1)
        name = _string_value(fields, 2)
        if not guild_id:
            continue
        summaries.append(
            GuildSearchSummary(
                guild_id=guild_id,
                name=name,
                master_user_id=_string_value(master_fields, 1),
                master_name=_string_value(master_fields, 2),
                guild_level=_int_value(fields, 4),
                member_count=_int_value(fields, 6),
                description=_string_value(settings_fields, 3),
                join_method=_int_value(settings_fields, 4),
                total_combat_power=_double_value(fields, 7),
                master_crumble_level=_int_value(master_fields, 7),
                master_profile_image_data_id=_int_value(master_fields, 3),
                master_profile_frame_data_id=_int_value(master_fields, 4),
                master_profile_title_data_id=_int_value(master_fields, 5),
                master_channel_id=_int_value(master_fields, 6),
                emblem_symbol_data_id=_int_value(settings_fields, 1),
                emblem_badge_data_id=_int_value(settings_fields, 2),
            )
        )
    return summaries


def parse_guild_detail_response(body: bytes) -> GuildDetail:
    """Parse fields exposed by GuildMemberService.GetGuild."""
    fields = pb.decode_fields(body)
    guild = _message_value(fields, 2)
    guild_fields = pb.decode_fields(guild) if guild is not None else []
    settings = _message_value(guild_fields, 2)
    settings_fields = pb.decode_fields(settings) if settings is not None else []

    member_ids: list[str] = []
    members = _message_value(guild_fields, 3)
    if members is not None:
        for field_number, wire_type, value in pb.decode_fields(members):
            if field_number != 1 or wire_type != 2:
                continue
            member_fields = pb.decode_fields(bytes(value))
            user_id = _string_value(member_fields, 1)
            if user_id:
                member_ids.append(user_id)

    announcement = ""
    announcements = _message_value(guild_fields, 5)
    if announcements is not None:
        announcement_message = _message_value(pb.decode_fields(announcements), 1)
        if announcement_message is not None:
            announcement = _string_value(pb.decode_fields(announcement_message), 1)

    experiences = _message_value(guild_fields, 6)
    experience_fields = pb.decode_fields(experiences) if experiences is not None else []
    return GuildDetail(
        name=_string_value(guild_fields, 1),
        master_name=_string_value(fields, 4),
        description=_string_value(settings_fields, 3),
        join_method=_int_value(settings_fields, 4),
        total_combat_power=_double_value(fields, 3),
        member_ids=tuple(member_ids),
        total_experience=_int_value(experience_fields, 1),
        announcement=announcement,
    )


def _string_value(fields, target: int) -> str:
    for field_number, wire_type, value in fields:
        if field_number == target and wire_type == 2:
            try:
                return bytes(value).decode("utf-8")
            except UnicodeDecodeError:
                return ""
    return ""


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


def _double_value(fields, target: int) -> float:
    for field_number, wire_type, value in fields:
        if field_number == target and wire_type == 1:
            raw = bytes(value)
            if len(raw) == 8:
                return float(struct.unpack("<d", raw)[0])
    return 0.0


class Guild:
    """Guild RPC facade bound to one authenticated account session.

    Args:
        client: Connected game gRPC client.
        session: Account identity, access token, resource key, and device ids.
    """

    def __init__(self, client: GrpcClient, session: Session) -> None:
        self.client = client
        self.session = session

    def search_guilds(self, query: str) -> GrpcResponse:
        """Search guilds by name or keyword.

        Args:
            query: Non-empty guild name or search keyword. It is encoded as
                field 1 of ``SearchGuildsRequest``.

        Returns:
            The complete response. ``SearchGuildsResponse`` contains repeated
            ``guild_summaries`` entries with each guild's ``id`` and ``name``.
        """
        if not isinstance(query, str):
            raise ValueError("query must be a string")
        query = query.strip()
        if not query:
            raise ValueError("query must not be empty")
        return self._unary(SEARCH_GUILDS_PATH, search_guilds_request(query))

    def join_guild(self, guild_id: str) -> GrpcResponse:
        """Join the guild identified by ``guild_id``.

        Args:
            guild_id: Server guild id, for example ``G...``. It is encoded as
                field 1 of ``JoinGuildRequest``.

        Returns:
            The complete gRPC response, including message, headers, and trailers.

        Raises:
            ValueError: If ``guild_id`` is empty or is not a string.
            GrpcError: If the game server rejects the request.
        """
        return self._guild_id_rpc(JOIN_GUILD_PATH, join_guild_request, guild_id)

    def leave_guild(self, guild_id: str) -> GrpcResponse:
        """Leave the guild identified by ``guild_id``.

        ``guild_id`` is field 1 of ``LeaveGuildRequest``. Eligibility and any
        rejoin or guild-creation cooldown are enforced by the server.
        """
        return self._guild_id_rpc(LEAVE_GUILD_PATH, leave_guild_request, guild_id)

    def get_guild(self, guild_id: str) -> GrpcResponse:
        """Fetch guild details; the authenticated account must be a member."""
        return self._guild_id_rpc(GET_GUILD_PATH, get_guild_request, guild_id)

    def conduct_free_guild_lab_research(self, guild_id: str) -> GrpcResponse:
        """Conduct one free guild-lab research action.

        ``guild_id`` is the only request field. The server tracks and enforces
        the daily free allowance (currently three actions).
        """
        return self._guild_id_rpc(
            CONDUCT_FREE_GUILD_LAB_RESEARCH_PATH,
            conduct_free_guild_lab_research_request,
            guild_id,
        )

    def conduct_paid_guild_lab_research(self, guild_id: str) -> GrpcResponse:
        """Conduct one diamond-paid guild-lab research action.

        ``guild_id`` is the only request field. The diamond price is not sent
        by the client; the server determines it from the member's daily paid
        research count and returns the charged payment in the response.
        """
        return self._guild_id_rpc(
            CONDUCT_PAID_GUILD_LAB_RESEARCH_PATH,
            conduct_paid_guild_lab_research_request,
            guild_id,
        )

    def attend_guild(self, guild_id: str) -> GrpcResponse:
        """Attend the guild once and receive the available attendance reward.

        ``guild_id`` is the only request field; attendance eligibility and the
        reward contents are determined by the server.
        """
        return self._guild_id_rpc(ATTEND_GUILD_PATH, attend_guild_request, guild_id)

    def _guild_id_rpc(
        self,
        path: str,
        request_builder: GuildIdRequestBuilder,
        guild_id: str,
    ) -> GrpcResponse:
        if not isinstance(guild_id, str):
            raise ValueError("guild_id must be a string")
        guild_id = guild_id.strip()
        if not guild_id:
            raise ValueError("guild_id must not be empty")

        return self._unary(path, request_builder(guild_id))

    def _unary(self, path: str, body: bytes) -> GrpcResponse:
        response = self.client.unary(
            path,
            body,
            metadata=build_metadata(self.session),
        )
        if self.session.adopt_resource_key(response.headers):
            log.debug("resource_key <- %s", self.session.resource_key)
        return response
