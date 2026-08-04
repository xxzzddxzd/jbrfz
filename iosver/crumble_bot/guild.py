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
    accept_guild_invitation_request,
    apply_guild_request,
    attend_guild_request,
    conduct_free_guild_lab_research_request,
    conduct_paid_guild_lab_research_request,
    get_guild_applications_for_user_request,
    get_guild_invitations_for_user_request,
    get_guild_request,
    invite_user_to_guild_request,
    join_guild_request,
    leave_guild_request,
    search_guilds_request,
    transfer_guild_master_request,
)

log = logging.getLogger(__name__)

SEARCH_GUILDS_PATH = "/cc.public.game.GuildDiscoveryService/SearchGuilds"
JOIN_GUILD_PATH = "/cc.public.game.GuildDiscoveryService/JoinGuild"
APPLY_GUILD_PATH = "/cc.public.game.GuildDiscoveryService/ApplyGuild"
LEAVE_GUILD_PATH = "/cc.public.game.GuildMemberService/LeaveGuild"
GET_GUILD_PATH = "/cc.public.game.GuildMemberService/GetGuild"
CONDUCT_FREE_GUILD_LAB_RESEARCH_PATH = (
    "/cc.public.game.GuildMemberService/ConductFreeGuildLabResearch"
)
CONDUCT_PAID_GUILD_LAB_RESEARCH_PATH = (
    "/cc.public.game.GuildMemberService/ConductPaidGuildLabResearch"
)
ATTEND_GUILD_PATH = "/cc.public.game.GuildMemberService/AttendGuild"
INVITE_USER_TO_GUILD_PATH = (
    "/cc.public.game.GuildMemberService/InviteUserToGuild"
)
GET_GUILD_INVITATIONS_FOR_USER_PATH = (
    "/cc.public.game.GuildDiscoveryService/GetGuildInvitationsForUser"
)
ACCEPT_GUILD_INVITATION_PATH = (
    "/cc.public.game.GuildDiscoveryService/AcceptGuildInvitation"
)
GET_GUILD_APPLICATIONS_FOR_USER_PATH = (
    "/cc.public.game.GuildDiscoveryService/GetGuildApplicationsForUser"
)
TRANSFER_GUILD_MASTER_PATH = (
    "/cc.public.game.GuildMemberService/TransferGuildMaster"
)

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
class GuildInvitationSummary:
    invitation_id: str
    invited_at_millis: int
    guild: GuildSearchSummary


@dataclass(frozen=True)
class GuildApplicationSummary:
    application_id: str
    applied_at_millis: int
    guild: GuildSearchSummary


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


@dataclass(frozen=True)
class GuildMemberStateSnapshot:
    guild_level: int
    daily_free_research_count: int
    daily_paid_research_count: int
    role: int | None = None
    guild_id: str = ""
    guild_name: str = ""


@dataclass(frozen=True)
class GuildProgressionChanges:
    previous_experience: int
    current_experience: int
    previous_contribution: int
    current_contribution: int


@dataclass(frozen=True)
class GuildLabResearchChanges:
    previous_research_point: int
    current_research_point: int


@dataclass(frozen=True)
class GuildActionResult:
    member_state: GuildMemberStateSnapshot | None = None
    progression: GuildProgressionChanges | None = None
    lab_research: GuildLabResearchChanges | None = None
    is_super_success: bool | None = None


def parse_guild_search_response(body: bytes) -> list[GuildSearchSummary]:
    """Parse the identifying fields from SearchGuildsResponse."""
    summaries: list[GuildSearchSummary] = []
    for field_number, wire_type, value in pb.decode_fields(body):
        if field_number != 1 or wire_type != 2:
            continue
        summary = _parse_guild_summary(bytes(value))
        if summary is not None:
            summaries.append(summary)
    return summaries


def parse_invite_user_to_guild_response(body: bytes) -> str:
    """Return the server-created invitation id (response field 1)."""
    return _string_value(pb.decode_fields(body), 1)


def parse_apply_guild_response(body: bytes) -> str:
    """Return the server-created guild application id (response field 1)."""
    return _string_value(pb.decode_fields(body), 1)


def parse_guild_invitations_for_user_response(
    body: bytes,
) -> list[GuildInvitationSummary]:
    """Parse all pending invitations visible to the authenticated user."""
    invitations: list[GuildInvitationSummary] = []
    for field_number, wire_type, value in pb.decode_fields(body):
        if field_number != 1 or wire_type != 2:
            continue
        fields = pb.decode_fields(bytes(value))
        guild_body = _message_value(fields, 3)
        guild = _parse_guild_summary(guild_body) if guild_body is not None else None
        invitation_id = _string_value(fields, 1)
        if not invitation_id or guild is None:
            continue
        invited_at = _message_value(fields, 2)
        invited_at_fields = pb.decode_fields(invited_at) if invited_at else []
        invitations.append(
            GuildInvitationSummary(
                invitation_id=invitation_id,
                invited_at_millis=_int_value(invited_at_fields, 1),
                guild=guild,
            )
        )
    return invitations


def parse_accept_guild_invitation_response(body: bytes) -> GuildActionResult:
    """Parse AcceptGuildInvitationResponse.member_state (field 2)."""
    return _parse_guild_action_response(body, member_state_field=2)


def parse_guild_applications_for_user_response(
    body: bytes,
) -> list[GuildApplicationSummary]:
    """Parse pending guild applications visible to the applicant."""
    applications: list[GuildApplicationSummary] = []
    for field_number, wire_type, value in pb.decode_fields(body):
        if field_number != 1 or wire_type != 2:
            continue
        fields = pb.decode_fields(bytes(value))
        guild_body = _message_value(fields, 3)
        guild = _parse_guild_summary(guild_body) if guild_body is not None else None
        application_id = _string_value(fields, 1)
        if not application_id or guild is None:
            continue
        applied_at = _message_value(fields, 2)
        applied_at_fields = pb.decode_fields(applied_at) if applied_at else []
        applications.append(
            GuildApplicationSummary(
                application_id=application_id,
                applied_at_millis=_int_value(applied_at_fields, 1),
                guild=guild,
            )
        )
    return applications


def parse_transfer_guild_master_response(body: bytes) -> GuildActionResult:
    """Parse the former master's member state after transferring ownership."""
    return _parse_guild_action_response(body, member_state_field=2)


def _parse_guild_summary(body: bytes) -> GuildSearchSummary | None:
    fields = pb.decode_fields(body)
    master = _message_value(fields, 5)
    master_fields = pb.decode_fields(master) if master is not None else []
    settings = _message_value(fields, 3)
    settings_fields = pb.decode_fields(settings) if settings is not None else []
    guild_id = _string_value(fields, 1)
    if not guild_id:
        return None
    return GuildSearchSummary(
        guild_id=guild_id,
        name=_string_value(fields, 2),
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


def parse_join_guild_response(body: bytes) -> GuildActionResult:
    """Parse JoinGuildResponse.member_state (field 2)."""
    return _parse_guild_action_response(body, member_state_field=2)


def parse_attend_guild_response(body: bytes) -> GuildActionResult:
    """Parse attendance progression and the resulting member state."""
    return _parse_guild_action_response(
        body,
        progression_field=3,
        member_state_field=4,
    )


def parse_free_guild_lab_research_response(body: bytes) -> GuildActionResult:
    """Parse one free research result."""
    return _parse_guild_action_response(
        body,
        progression_field=2,
        lab_research_field=3,
        member_state_field=4,
        super_success_field=5,
    )


def parse_paid_guild_lab_research_response(body: bytes) -> GuildActionResult:
    """Parse one diamond-paid research/donation result."""
    return _parse_guild_action_response(
        body,
        progression_field=3,
        lab_research_field=4,
        member_state_field=6,
        super_success_field=5,
    )


def _parse_guild_action_response(
    body: bytes,
    *,
    member_state_field: int,
    progression_field: int | None = None,
    lab_research_field: int | None = None,
    super_success_field: int | None = None,
) -> GuildActionResult:
    fields = pb.decode_fields(body)

    member_state = None
    member_state_body = _message_value(fields, member_state_field)
    if member_state_body is not None:
        member_fields = pb.decode_fields(member_state_body)
        member_state = GuildMemberStateSnapshot(
            guild_level=_int_value(member_fields, 7),
            daily_free_research_count=_int_value(member_fields, 10),
            daily_paid_research_count=_int_value(member_fields, 12),
            role=_optional_int_value(member_fields, 4),
            guild_id=_string_value(member_fields, 1),
            guild_name=_string_value(member_fields, 2),
        )

    progression = None
    if progression_field is not None:
        progression_body = _message_value(fields, progression_field)
        if progression_body is not None:
            progression_fields = pb.decode_fields(progression_body)
            progression = GuildProgressionChanges(
                previous_experience=_int_value(progression_fields, 1),
                current_experience=_int_value(progression_fields, 2),
                previous_contribution=_int_value(progression_fields, 3),
                current_contribution=_int_value(progression_fields, 4),
            )

    lab_research = None
    if lab_research_field is not None:
        lab_research_body = _message_value(fields, lab_research_field)
        if lab_research_body is not None:
            lab_research_fields = pb.decode_fields(lab_research_body)
            lab_research = GuildLabResearchChanges(
                previous_research_point=_int_value(lab_research_fields, 1),
                current_research_point=_int_value(lab_research_fields, 2),
            )

    is_super_success = (
        _optional_bool_value(fields, super_success_field)
        if super_success_field is not None
        else None
    )
    return GuildActionResult(
        member_state=member_state,
        progression=progression,
        lab_research=lab_research,
        is_super_success=is_super_success,
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


def _optional_bool_value(fields, target: int) -> bool | None:
    for field_number, wire_type, value in fields:
        if field_number == target and wire_type == 0:
            return bool(value)
    return None


def _optional_int_value(fields, target: int) -> int | None:
    for field_number, wire_type, value in fields:
        if field_number == target and wire_type == 0:
            return int(value)
    return None


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

    def apply_guild(self, guild_id: str) -> GrpcResponse:
        """Apply to an approval-required guild as the authenticated user."""
        return self._guild_id_rpc(APPLY_GUILD_PATH, apply_guild_request, guild_id)

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

    def invite_user_to_guild(self, guild_id: str, invitee_id: str) -> GrpcResponse:
        """Invite ``invitee_id`` to the authenticated member's guild."""
        guild_id = self._validated_string(guild_id, "guild_id")
        invitee_id = self._validated_string(invitee_id, "invitee_id")
        return self._unary(
            INVITE_USER_TO_GUILD_PATH,
            invite_user_to_guild_request(guild_id, invitee_id),
        )

    def get_guild_invitations_for_user(self) -> GrpcResponse:
        """List pending guild invitations for the authenticated user."""
        return self._unary(
            GET_GUILD_INVITATIONS_FOR_USER_PATH,
            get_guild_invitations_for_user_request(),
        )

    def get_guild_applications_for_user(self) -> GrpcResponse:
        """List pending guild applications for the authenticated applicant."""
        return self._unary(
            GET_GUILD_APPLICATIONS_FOR_USER_PATH,
            get_guild_applications_for_user_request(),
        )

    def accept_guild_invitation(
        self,
        guild_id: str,
        invitation_id: str,
    ) -> GrpcResponse:
        """Accept a pending guild invitation by guild and invitation ids."""
        guild_id = self._validated_string(guild_id, "guild_id")
        invitation_id = self._validated_string(invitation_id, "invitation_id")
        return self._unary(
            ACCEPT_GUILD_INVITATION_PATH,
            accept_guild_invitation_request(guild_id, invitation_id),
        )

    def transfer_guild_master(
        self,
        guild_id: str,
        member_id: str,
    ) -> GrpcResponse:
        """Transfer the sole guild-master role to an existing member."""
        guild_id = self._validated_string(guild_id, "guild_id")
        member_id = self._validated_string(member_id, "member_id")
        return self._unary(
            TRANSFER_GUILD_MASTER_PATH,
            transfer_guild_master_request(guild_id, member_id),
        )

    def _guild_id_rpc(
        self,
        path: str,
        request_builder: GuildIdRequestBuilder,
        guild_id: str,
    ) -> GrpcResponse:
        guild_id = self._validated_string(guild_id, "guild_id")
        return self._unary(path, request_builder(guild_id))

    @staticmethod
    def _validated_string(value: str, name: str) -> str:
        if not isinstance(value, str):
            raise ValueError(f"{name} must be a string")
        value = value.strip()
        if not value:
            raise ValueError(f"{name} must not be empty")
        return value

    def _unary(self, path: str, body: bytes) -> GrpcResponse:
        response = self.client.unary(
            path,
            body,
            metadata=build_metadata(self.session),
        )
        if self.session.adopt_resource_key(response.headers):
            log.debug("resource_key <- %s", self.session.resource_key)
        return response
