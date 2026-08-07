from __future__ import annotations

import argparse
import io
import json
import sqlite3
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from crumble_bot import cli, pbutil as pb
from crumble_bot.auth import AccountState
from crumble_bot.currency import DIAMOND_CURRENCY_DATA_ID
from crumble_bot.db import AccountDB
from crumble_bot.grpc_client import GrpcError, GrpcResponse
from crumble_bot.guild import (
    ACCEPT_GUILD_INVITATION_PATH,
    APPLY_GUILD_PATH,
    ATTEND_GUILD_PATH,
    BANISH_GUILD_MEMBER_PATH,
    CONDUCT_FREE_GUILD_LAB_RESEARCH_PATH,
    CONDUCT_PAID_GUILD_LAB_RESEARCH_PATH,
    GET_GUILD_APPLICATIONS_FOR_USER_PATH,
    GET_GUILD_INVITATIONS_FOR_USER_PATH,
    GET_GUILD_MEMBERS_PATH,
    GET_GUILD_PATH,
    INVITE_USER_TO_GUILD_PATH,
    JOIN_GUILD_PATH,
    LEAVE_GUILD_PATH,
    SEARCH_GUILDS_PATH,
    TRANSFER_GUILD_MASTER_PATH,
    Guild,
    parse_accept_guild_invitation_response,
    parse_apply_guild_response,
    parse_banish_guild_member_response,
    parse_guild_detail_response,
    parse_guild_applications_for_user_response,
    parse_guild_invitations_for_user_response,
    parse_get_guild_members_response,
    parse_guild_search_response,
    parse_invite_user_to_guild_response,
    parse_transfer_guild_master_response,
)
from crumble_bot.guild_limits import (
    GUILD_PAID_RESEARCH_PRICE_TIER_COUNT,
    guild_daily_free_research_limit,
    guild_paid_research_cost,
)
from crumble_bot.guild_runner import (
    GuildProgress,
    GuildRunner,
    GuildWorkflowResult,
)
from crumble_bot.mailbox import (
    MAIL_ADVERTISEMENT_DATA_ID,
    RECEIVE_MAIL_ADVERTISEMENT_REWARD_PATH,
    RECEIVE_MAIL_REWARDS_PATH,
    REFRESH_MAIL_BOX_PATH,
    MailReward,
    parse_receive_mail_advertisement_reward_response,
    parse_receive_mail_rewards_response,
    parse_refresh_mail_box_response,
    parse_signup_mail_advertisement_view_count,
)
from crumble_bot.messages import (
    accept_guild_invitation_request,
    apply_guild_request,
    banish_guild_member_request,
    get_guild_applications_for_user_request,
    get_guild_members_request,
    get_user_social_info_request,
    invite_user_to_guild_request,
    receive_mail_advertisement_reward_request,
    transfer_guild_master_request,
)
from crumble_bot.social import (
    GET_USER_SOCIAL_INFO_PATH,
    parse_get_user_social_info_response,
)
from crumble_bot.stage_runner import SIGNUP_PATH


def signup_response(diamonds: int, advertisement_count: int = 0) -> bytes:
    currency = b"".join(
        (
            pb.encode_int32_field(1, DIAMOND_CURRENCY_DATA_ID),
            pb.encode_int64_field(2, diamonds),
        )
    )
    inventory = pb.encode_message_field(1, currency)
    advertisement_counter = b"".join(
        (
            pb.encode_int32_field(1, MAIL_ADVERTISEMENT_DATA_ID),
            pb.encode_int64_field(2, advertisement_count),
        )
    )
    daily_counters = pb.encode_message_field(3, advertisement_counter)
    progress = pb.encode_message_field(2, daily_counters)
    crumble = b"".join(
        (
            pb.encode_message_field(3, inventory),
            pb.encode_message_field(5, progress),
        )
    )
    return pb.encode_message_field(3, crumble)


def guild_member_state(
    *,
    level: int,
    free_count: int,
    paid_count: int,
    role: int | None = None,
    guild_id: str = "",
    guild_name: str = "",
) -> bytes:
    parts = [
        pb.encode_int32_field(7, level),
        pb.encode_int32_field(10, free_count),
        pb.encode_int32_field(12, paid_count),
    ]
    if role is not None:
        parts.append(pb.encode_int32_field(4, role))
    if guild_id:
        parts.append(pb.encode_string_field(1, guild_id))
    if guild_name:
        parts.append(pb.encode_string_field(2, guild_name))
    return b"".join(parts)


def guild_progression(
    previous_experience: int,
    current_experience: int,
    previous_contribution: int,
    current_contribution: int,
) -> bytes:
    return b"".join(
        (
            pb.encode_int64_field(1, previous_experience),
            pb.encode_int64_field(2, current_experience),
            pb.encode_int64_field(3, previous_contribution),
            pb.encode_int64_field(4, current_contribution),
        )
    )


def guild_lab_research(previous_point: int, current_point: int) -> bytes:
    return b"".join(
        (
            pb.encode_int64_field(1, previous_point),
            pb.encode_int64_field(2, current_point),
        )
    )


def join_guild_response() -> bytes:
    return pb.encode_message_field(
        2,
        guild_member_state(level=1, free_count=0, paid_count=0),
    )


def attend_guild_response() -> bytes:
    return b"".join(
        (
            pb.encode_message_field(3, guild_progression(20, 21, 0, 1)),
            pb.encode_message_field(
                4,
                guild_member_state(level=1, free_count=0, paid_count=0),
            ),
        )
    )


def free_research_response(index: int) -> bytes:
    previous_experience = {1: 21, 2: 22, 3: 25}[index]
    previous_contribution = {1: 1, 2: 2, 3: 5}[index]
    previous_point = {1: 100, 2: 101, 3: 104}[index]
    gained = 3 if index == 2 else 1
    return b"".join(
        (
            pb.encode_message_field(
                2,
                guild_progression(
                    previous_experience,
                    previous_experience + gained,
                    previous_contribution,
                    previous_contribution + gained,
                ),
            ),
            pb.encode_message_field(
                3,
                guild_lab_research(previous_point, previous_point + gained),
            ),
            pb.encode_message_field(
                4,
                guild_member_state(level=1, free_count=index, paid_count=0),
            ),
            pb.encode_bool_field(5, index == 2),
        )
    )


def payment_response(amount: int) -> bytes:
    currency_payment = b"".join(
        (
            pb.encode_int32_field(1, DIAMOND_CURRENCY_DATA_ID),
            pb.encode_int64_field(2, amount),
        )
    )
    payment = pb.encode_message_field(1, currency_payment)
    return b"".join(
        (
            pb.encode_message_field(2, payment),
            pb.encode_message_field(3, guild_progression(26, 29, 6, 9)),
            pb.encode_message_field(4, guild_lab_research(105, 108)),
            pb.encode_bool_field(5, True),
            pb.encode_message_field(
                6,
                guild_member_state(level=2, free_count=3, paid_count=1),
            ),
        )
    )


def dynamic_free_research_response(*, level: int, free_count: int) -> bytes:
    return b"".join(
        (
            pb.encode_message_field(
                2,
                guild_progression(
                    free_count,
                    free_count + 1,
                    free_count,
                    free_count + 1,
                ),
            ),
            pb.encode_message_field(
                3,
                guild_lab_research(free_count, free_count + 1),
            ),
            pb.encode_message_field(
                4,
                guild_member_state(
                    level=level,
                    free_count=free_count,
                    paid_count=0 if level < 4 else 1,
                ),
            ),
            pb.encode_bool_field(5, False),
        )
    )


def dynamic_paid_research_response() -> bytes:
    currency_payment = b"".join(
        (
            pb.encode_int32_field(1, DIAMOND_CURRENCY_DATA_ID),
            pb.encode_int64_field(2, 10),
        )
    )
    payment = pb.encode_message_field(1, currency_payment)
    return b"".join(
        (
            pb.encode_message_field(2, payment),
            pb.encode_message_field(3, guild_progression(3, 4, 3, 4)),
            pb.encode_message_field(4, guild_lab_research(3, 4)),
            pb.encode_bool_field(5, False),
            pb.encode_message_field(
                6,
                guild_member_state(level=4, free_count=3, paid_count=1),
            ),
        )
    )


def guild_detail_response(
    total_experience: int = 33,
    *,
    member_ids: tuple[str, ...] = ("MASTER", "MEMBER"),
) -> bytes:
    settings = b"".join(
        (
            pb.encode_int32_field(1, 101),
            pb.encode_int32_field(2, 202),
            pb.encode_string_field(3, "description"),
        )
    )
    members = pb.encode_repeated_messages(
        1,
        tuple(pb.encode_string_field(1, mid) for mid in member_ids),
    )
    experiences = pb.encode_int64_field(1, total_experience)
    guild = b"".join(
        (
            pb.encode_string_field(1, "ahhhha"),
            pb.encode_message_field(2, settings),
            pb.encode_message_field(3, members),
            pb.encode_message_field(6, experiences),
        )
    )
    return b"".join(
        (
            pb.encode_message_field(2, guild),
            pb.encode_double_field(3, 12345),
            pb.encode_string_field(4, "absdbld"),
        )
    )


def guild_summary_message(
    *,
    level: int = 1,
    join_method: int = 0,
    master_mid: str = "MASTER",
    master_name: str = "absdbld",
) -> bytes:
    setting_parts = [
        pb.encode_int32_field(1, 101),
        pb.encode_int32_field(2, 202),
        pb.encode_string_field(3, "description"),
    ]
    if join_method:
        setting_parts.append(pb.encode_int32_field(4, join_method))
    settings = b"".join(setting_parts)
    master = b"".join(
        (
            pb.encode_string_field(1, master_mid),
            pb.encode_string_field(2, master_name),
            pb.encode_int32_field(7, 55),
        )
    )
    return b"".join(
        (
            pb.encode_string_field(1, "G-ID"),
            pb.encode_string_field(2, "ahhhha"),
            pb.encode_message_field(3, settings),
            pb.encode_int32_field(4, level),
            pb.encode_message_field(5, master),
            pb.encode_int32_field(6, 1),
            pb.encode_double_field(7, 12345),
        )
    )


def guild_search_response() -> bytes:
    return pb.encode_message_field(1, guild_summary_message())


def user_social_info_response(
    user_id: str = "CONTROLLER",
    name: str = "garlic-proxy",
    level: int = 2,
) -> bytes:
    info = b"".join(
        (
            pb.encode_string_field(1, user_id),
            pb.encode_int32_field(2, level),
            pb.encode_string_field(3, name),
        )
    )
    return pb.encode_message_field(1, info)


def guild_invitations_response() -> bytes:
    invited_at = pb.encode_int64_field(1, 1_786_000_000_000)
    invitation = b"".join(
        (
            pb.encode_string_field(1, "GI-ID"),
            pb.encode_message_field(2, invited_at),
            pb.encode_message_field(3, guild_summary_message(level=4)),
        )
    )
    return pb.encode_message_field(1, invitation)


def accept_guild_invitation_response() -> bytes:
    return pb.encode_message_field(
        2,
        guild_member_state(level=4, free_count=1, paid_count=2),
    )


def guild_applications_response() -> bytes:
    applied_at = pb.encode_int64_field(1, 1_786_000_000_000)
    application = b"".join(
        (
            pb.encode_string_field(1, "GA-ID"),
            pb.encode_message_field(2, applied_at),
            pb.encode_message_field(
                3,
                guild_summary_message(join_method=1),
            ),
        )
    )
    return pb.encode_message_field(1, application)


def mail_entry(
    mail_id: str,
    *,
    rewards: tuple[tuple[int, int], ...] = (),
    is_rewarded: bool = False,
) -> bytes:
    reward_messages = (
        b"".join(
            (
                pb.encode_int32_field(1, item_data_id),
                pb.encode_int64_field(2, amount),
            )
        )
        for item_data_id, amount in rewards
    )
    return b"".join(
        (
            pb.encode_string_field(1, mail_id),
            pb.encode_repeated_messages(3, reward_messages),
            pb.encode_bool_field(4, is_rewarded),
        )
    )


def refresh_mail_box_response() -> bytes:
    mail_box = pb.encode_repeated_messages(
        1,
        (
            mail_entry(
                "MAIL-1",
                rewards=((DIAMOND_CURRENCY_DATA_ID, 100),),
            ),
            mail_entry(
                "MAIL-2",
                rewards=((DIAMOND_CURRENCY_DATA_ID, 10),),
                is_rewarded=True,
            ),
            mail_entry("MAIL-3"),
        ),
    )
    return pb.encode_message_field(1, mail_box)


def receive_mail_rewards_response() -> bytes:
    generic_reward = pb.encode_int32_field(1, 1)
    updated_mail = mail_entry(
        "MAIL-1",
        rewards=((DIAMOND_CURRENCY_DATA_ID, 100),),
        is_rewarded=True,
    )
    return b"".join(
        (
            pb.encode_message_field(2, generic_reward),
            pb.encode_message_field(3, updated_mail),
        )
    )


def receive_mail_advertisement_reward_response() -> bytes:
    currency_reward = b"".join(
        (
            pb.encode_int32_field(1, DIAMOND_CURRENCY_DATA_ID),
            pb.encode_int64_field(2, 1000),
        )
    )
    reward_element = pb.encode_message_field(1, currency_reward)
    reward = pb.encode_message_field(1, reward_element)
    return pb.encode_message_field(2, reward)


class GuildParserTests(unittest.TestCase):
    def test_search_and_detail_parsers(self) -> None:
        settings = b"".join(
            (
                pb.encode_int32_field(1, 101),
                pb.encode_int32_field(2, 202),
                pb.encode_string_field(3, "description"),
                pb.encode_int32_field(4, 1),
            )
        )
        master = b"".join(
            (
                pb.encode_string_field(1, "MASTER"),
                pb.encode_string_field(2, "absdbld"),
                pb.encode_int32_field(7, 55),
            )
        )
        summary = b"".join(
            (
                pb.encode_string_field(1, "G00000000-0000-0000-0000-000000000000"),
                pb.encode_string_field(2, "ahhhha"),
                pb.encode_message_field(3, settings),
                pb.encode_int32_field(4, 3),
                pb.encode_message_field(5, master),
                pb.encode_int32_field(6, 2),
                pb.encode_double_field(7, 12345),
            )
        )
        parsed = parse_guild_search_response(pb.encode_message_field(1, summary))
        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0].name, "ahhhha")
        self.assertEqual(parsed[0].master_name, "absdbld")
        self.assertEqual(parsed[0].member_count, 2)
        self.assertEqual(parsed[0].join_method, 1)
        self.assertEqual(parsed[0].total_combat_power, 12345)

        detail = parse_guild_detail_response(guild_detail_response())
        self.assertEqual(detail.master_name, "absdbld")
        self.assertEqual(detail.member_ids, ("MASTER", "MEMBER"))
        self.assertEqual(detail.total_experience, 33)

    def test_guild_invitation_requests_and_responses(self) -> None:
        self.assertEqual(
            pb.decode_fields(invite_user_to_guild_request("G-ID", "INVITEE")),
            [(1, 2, b"G-ID"), (2, 2, b"INVITEE")],
        )
        self.assertEqual(
            pb.decode_fields(
                accept_guild_invitation_request("G-ID", "GI-ID")
            ),
            [(1, 2, b"G-ID"), (2, 2, b"GI-ID")],
        )
        self.assertEqual(
            parse_invite_user_to_guild_response(
                pb.encode_string_field(1, "GI-ID")
            ),
            "GI-ID",
        )

        invitations = parse_guild_invitations_for_user_response(
            guild_invitations_response()
        )
        self.assertEqual(len(invitations), 1)
        self.assertEqual(invitations[0].invitation_id, "GI-ID")
        self.assertEqual(invitations[0].guild.guild_id, "G-ID")
        self.assertEqual(invitations[0].guild.name, "ahhhha")
        self.assertEqual(invitations[0].guild.master_name, "absdbld")
        self.assertEqual(invitations[0].guild.guild_level, 4)
        self.assertEqual(invitations[0].invited_at_millis, 1_786_000_000_000)

        accepted = parse_accept_guild_invitation_response(
            accept_guild_invitation_response()
        )
        self.assertIsNotNone(accepted.member_state)
        self.assertEqual(accepted.member_state.guild_level, 4)
        self.assertEqual(accepted.member_state.daily_free_research_count, 1)
        self.assertEqual(accepted.member_state.daily_paid_research_count, 2)

    def test_guild_application_and_master_transfer_protocol(self) -> None:
        self.assertEqual(
            pb.decode_fields(apply_guild_request("G-ID")),
            [(1, 2, b"G-ID")],
        )
        self.assertEqual(get_guild_applications_for_user_request(), b"")
        self.assertEqual(
            pb.decode_fields(
                transfer_guild_master_request("G-ID", "NEW-MASTER")
            ),
            [(1, 2, b"G-ID"), (2, 2, b"NEW-MASTER")],
        )
        self.assertEqual(
            parse_apply_guild_response(pb.encode_string_field(1, "GA-ID")),
            "GA-ID",
        )

        applications = parse_guild_applications_for_user_response(
            guild_applications_response()
        )
        self.assertEqual(len(applications), 1)
        self.assertEqual(applications[0].application_id, "GA-ID")
        self.assertEqual(applications[0].guild.guild_id, "G-ID")
        self.assertEqual(applications[0].guild.join_method, 1)

        transferred = parse_transfer_guild_master_response(
            pb.encode_message_field(
                2,
                guild_member_state(
                    level=4,
                    free_count=1,
                    paid_count=2,
                    role=1,
                    guild_id="G-ID",
                    guild_name="ahhhha",
                ),
            )
        )
        self.assertIsNotNone(transferred.member_state)
        self.assertEqual(transferred.member_state.role, 1)
        self.assertEqual(transferred.member_state.guild_id, "G-ID")

    def test_guild_banish_member_protocol(self) -> None:
        self.assertEqual(
            pb.decode_fields(
                banish_guild_member_request("G-ID", "MEMBER-MID")
            ),
            [(1, 2, b"G-ID"), (2, 2, b"MEMBER-MID")],
        )
        last_banishment = pb.encode_int64_field(1, 1_786_000_000_000)
        banishments = b"".join(
            (
                pb.encode_message_field(1, last_banishment),
                pb.encode_int32_field(2, 2),
            )
        )
        parsed = parse_banish_guild_member_response(
            pb.encode_message_field(1, banishments)
        )
        self.assertEqual(parsed.last_banishment_at_millis, 1_786_000_000_000)
        self.assertEqual(parsed.daily_banishment_count, 2)

    def test_get_guild_members_protocol_exposes_member_information(self) -> None:
        self.assertEqual(
            pb.decode_fields(get_guild_members_request("G-ID")),
            [(1, 2, b"G-ID")],
        )

        def member_summary(
            mid: str,
            name: str,
            level: int,
            role: int,
            joined_at: int,
            last_accessed_at: int,
            combat_power: float,
            contribution: int,
        ) -> bytes:
            crumble = b"".join(
                (
                    pb.encode_string_field(1, mid),
                    pb.encode_string_field(2, name),
                    pb.encode_int32_field(3, 101),
                    pb.encode_int32_field(4, 202),
                    pb.encode_int32_field(5, 303),
                    pb.encode_int32_field(6, 4),
                    pb.encode_int32_field(7, level),
                )
            )
            return b"".join(
                (
                    pb.encode_message_field(1, crumble),
                    pb.encode_int32_field(2, role),
                    pb.encode_message_field(
                        3,
                        pb.encode_int64_field(1, joined_at),
                    ),
                    pb.encode_message_field(
                        4,
                        pb.encode_int64_field(1, last_accessed_at),
                    ),
                    pb.encode_double_field(5, combat_power),
                    pb.encode_int64_field(6, contribution),
                )
            )

        banishments = b"".join(
            (
                pb.encode_message_field(
                    1,
                    pb.encode_int64_field(1, 1_786_000_000_000),
                ),
                pb.encode_int32_field(2, 2),
            )
        )
        response = b"".join(
            (
                pb.encode_message_field(
                    1,
                    member_summary(
                        "OWNER",
                        "absdbld",
                        55,
                        0,
                        1_785_000_000_000,
                        1_786_000_000_000,
                        123_456.5,
                        789,
                    ),
                ),
                pb.encode_message_field(
                    1,
                    member_summary(
                        "MEMBER",
                        "donor-name",
                        31,
                        1,
                        1_785_100_000_000,
                        1_786_100_000_000,
                        50_000,
                        123,
                    ),
                ),
                pb.encode_message_field(2, banishments),
            )
        )

        parsed = parse_get_guild_members_response(response)
        self.assertEqual(len(parsed.members), 2)
        master, member = parsed.members
        self.assertEqual(master.mid, "OWNER")
        self.assertEqual(master.name, "absdbld")
        self.assertEqual(master.crumble_level, 55)
        self.assertEqual(master.role, 0)
        self.assertEqual(master.role_name, "master")
        self.assertEqual(master.joined_at_millis, 1_785_000_000_000)
        self.assertEqual(master.last_accessed_at_millis, 1_786_000_000_000)
        self.assertEqual(master.total_combat_power, 123_456.5)
        self.assertEqual(master.contribution_point, 789)
        self.assertEqual(master.profile_image_data_id, 101)
        self.assertEqual(master.profile_frame_data_id, 202)
        self.assertEqual(master.profile_title_data_id, 303)
        self.assertEqual(master.channel_id, 4)
        self.assertEqual(member.mid, "MEMBER")
        self.assertEqual(member.name, "donor-name")
        self.assertEqual(member.crumble_level, 31)
        self.assertEqual(member.role_name, "member")
        self.assertEqual(parsed.banishments.daily_banishment_count, 2)

    def test_guild_member_management_facades_are_not_commands(self) -> None:
        client = FakeWorkflowClient()
        guild = Guild(
            client,
            AccountState(mid="MID", game_access_token="token").to_session(),
        )

        guild.get_guild_members(" G-ID ")
        guild.banish_guild_member(" G-ID ", " MEMBER-MID ")

        self.assertEqual(client.calls[-1], BANISH_GUILD_MEMBER_PATH)
        self.assertEqual(client.calls[-2], GET_GUILD_MEMBERS_PATH)
        self.assertEqual(
            pb.decode_fields(client.requests[-2][1]),
            [(1, 2, b"G-ID")],
        )
        self.assertEqual(
            pb.decode_fields(client.requests[-1][1]),
            [(1, 2, b"G-ID"), (2, 2, b"MEMBER-MID")],
        )
        with self.assertRaisesRegex(ValueError, "guild_id must not be empty"):
            guild.banish_guild_member(" ", "MEMBER-MID")
        with self.assertRaisesRegex(ValueError, "member_id must be a string"):
            guild.banish_guild_member("G-ID", None)

    def test_user_social_info_protocol_exposes_game_name(self) -> None:
        request = get_user_social_info_request(("A", "B"))
        self.assertEqual(
            pb.decode_fields(request),
            [(1, 2, b"A"), (1, 2, b"B")],
        )
        parsed = parse_get_user_social_info_response(
            user_social_info_response("LSVNZ3678", "visible-name", 31)
        )
        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0].user_id, "LSVNZ3678")
        self.assertEqual(parsed[0].name, "visible-name")
        self.assertEqual(parsed[0].crumble_level, 31)

    def test_guild_application_and_transfer_facade_paths(self) -> None:
        client = FakeWorkflowClient()
        guild = Guild(
            client,
            AccountState(mid="MID", game_access_token="token").to_session(),
        )

        guild.apply_guild("G-ID")
        guild.get_guild_applications_for_user()
        guild.transfer_guild_master("G-ID", "NEW-MASTER")

        self.assertEqual(
            client.calls[-3:],
            [
                APPLY_GUILD_PATH,
                GET_GUILD_APPLICATIONS_FOR_USER_PATH,
                TRANSFER_GUILD_MASTER_PATH,
            ],
        )
        self.assertEqual(
            pb.decode_fields(client.requests[-1][1]),
            [(1, 2, b"G-ID"), (2, 2, b"NEW-MASTER")],
        )

    def test_10101_guild_limits(self) -> None:
        self.assertEqual(guild_daily_free_research_limit(1), 3)
        self.assertEqual(guild_daily_free_research_limit(4), 4)
        self.assertEqual(guild_daily_free_research_limit(7), 5)
        self.assertEqual(guild_daily_free_research_limit(14), 8)
        self.assertEqual(GUILD_PAID_RESEARCH_PRICE_TIER_COUNT, 27)
        self.assertEqual(guild_paid_research_cost(1), 10)
        self.assertEqual(guild_paid_research_cost(27), 10000)
        self.assertEqual(guild_paid_research_cost(28), 10000)

    def test_guild_invitation_facade_uses_10101_rpc_paths(self) -> None:
        client = FakeWorkflowClient()
        guild = Guild(
            client,
            AccountState(mid="MID", game_access_token="token").to_session(),
        )

        guild.invite_user_to_guild("G-ID", "PGLXK9073")
        guild.get_guild_invitations_for_user()
        guild.accept_guild_invitation("G-ID", "GI-ID")

        self.assertEqual(
            client.calls[-3:],
            [
                INVITE_USER_TO_GUILD_PATH,
                GET_GUILD_INVITATIONS_FOR_USER_PATH,
                ACCEPT_GUILD_INVITATION_PATH,
            ],
        )
        self.assertEqual(
            pb.decode_fields(client.requests[-3][1]),
            [(1, 2, b"G-ID"), (2, 2, b"PGLXK9073")],
        )
        self.assertEqual(client.requests[-2][1], b"")
        self.assertEqual(
            pb.decode_fields(client.requests[-1][1]),
            [(1, 2, b"G-ID"), (2, 2, b"GI-ID")],
        )

    def test_mailbox_parsers_find_only_unclaimed_attachment_mail(self) -> None:
        snapshot = parse_refresh_mail_box_response(refresh_mail_box_response())
        self.assertEqual(len(snapshot.mails), 3)
        self.assertEqual(
            [mail.mail_id for mail in snapshot.claimable_mails],
            ["MAIL-1"],
        )
        self.assertEqual(
            snapshot.claimable_mails[0].rewards,
            (MailReward(DIAMOND_CURRENCY_DATA_ID, 100),),
        )

        received = parse_receive_mail_rewards_response(receive_mail_rewards_response())
        self.assertEqual(received.reward_count, 1)
        self.assertEqual(len(received.updated_mails), 1)
        self.assertTrue(received.updated_mails[0].is_rewarded)

        self.assertEqual(
            parse_signup_mail_advertisement_view_count(signup_response(900, 1)),
            1,
        )
        advertisement = parse_receive_mail_advertisement_reward_response(
            receive_mail_advertisement_reward_response()
        )
        self.assertEqual(advertisement.reward_count, 1)
        self.assertEqual(
            advertisement.currency_rewards,
            (MailReward(DIAMOND_CURRENCY_DATA_ID, 1000),),
        )

    def test_mail_advertisement_request_exposes_optional_skip_count(self) -> None:
        request = receive_mail_advertisement_reward_request(MAIL_ADVERTISEMENT_DATA_ID)
        root_fields = pb.decode_fields(request)
        self.assertEqual(len(root_fields), 1)
        viewed_fields = pb.decode_fields(bytes(root_fields[0][2]))
        self.assertEqual(viewed_fields, [(1, 0, MAIL_ADVERTISEMENT_DATA_ID)])

        with_explicit_zero = receive_mail_advertisement_reward_request(
            MAIL_ADVERTISEMENT_DATA_ID,
            skip_count=0,
        )
        viewed_fields = pb.decode_fields(
            bytes(pb.decode_fields(with_explicit_zero)[0][2])
        )
        self.assertEqual(
            viewed_fields,
            [(1, 0, MAIL_ADVERTISEMENT_DATA_ID), (2, 0, 0)],
        )


class AccountDBGuildTests(unittest.TestCase):
    def test_legacy_guild_target_backfills_original_master_mid(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "accounts.db"
            with sqlite3.connect(db_path) as conn:
                conn.executescript(
                    """
                    CREATE TABLE guild_targets (
                        gname TEXT NOT NULL,
                        gmname TEXT NOT NULL,
                        guild_id TEXT NOT NULL,
                        guild_level INTEGER NOT NULL DEFAULT 0,
                        member_count INTEGER NOT NULL DEFAULT 0,
                        master_user_id TEXT NOT NULL DEFAULT '',
                        details_json TEXT NOT NULL DEFAULT '{}',
                        confirmed_at REAL NOT NULL,
                        updated_at REAL NOT NULL,
                        PRIMARY KEY (gname, gmname)
                    );
                    INSERT INTO guild_targets VALUES (
                        'ahhhha', 'absdbld', 'G-ID', 1, 2, 'OWNER',
                        '{}', 100, 100
                    );
                    """
                )

            with AccountDB(db_path) as db:
                target = db.get_guild_target("ahhhha", "absdbld")
                self.assertIsNotNone(target)
                self.assertEqual(target.original_master_mid, "OWNER")
                columns = {
                    row[1]
                    for row in db._conn.execute(
                        "PRAGMA table_info(guild_targets)"
                    ).fetchall()
                }
                self.assertIn("original_master_mid", columns)

    def test_cooldown_and_target_cache(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "accounts.db"
            with AccountDB(db_path) as db:
                for mid in ("RECENT", "OLD", "NEVER"):
                    db.upsert_state(
                        AccountState(mid=mid, next_stage=31),
                        used=True,
                        ready=True,
                        invalid=False,
                    )
                db.mark_guild_left("RECENT", left_at=100_000)
                db.mark_guild_left("OLD", left_at=1_000)

                eligible = db.list_guild_eligible(now=100_100)
                self.assertEqual([row.mid for row in eligible], ["NEVER", "OLD"])
                status = db.guild_pool_status(now=100_100)
                self.assertEqual(status["eligible"], 2)
                self.assertEqual(status["cooling"], 1)

                target = db.upsert_guild_target(
                    gname="ahhhha",
                    gmname="absdbld",
                    guild_id="G-ID",
                    guild_level=1,
                    member_count=2,
                    master_user_id="OWNER",
                    details={"confirmed": True},
                )
                self.assertEqual(target.original_master_mid, "OWNER")
                confirmed_at = target.confirmed_at
                updated = db.upsert_guild_target(
                    gname="ahhhha",
                    gmname="absdbld",
                    guild_id="G-ID",
                    guild_level=2,
                    member_count=3,
                    master_user_id="TEMP-MASTER",
                    details={"members": ["A", "B"]},
                )
                self.assertEqual(updated.confirmed_at, confirmed_at)
                self.assertEqual(updated.guild_level, 2)
                self.assertEqual(updated.details["members"], ["A", "B"])
                self.assertEqual(updated.master_user_id, "TEMP-MASTER")
                self.assertEqual(updated.original_master_mid, "OWNER")

                job = db.create_private_job(
                    guild_id="G-ID",
                    gname="ahhhha",
                    gmname="absdbld",
                    original_master_mid=updated.original_master_mid,
                    controller_mid="TEMP-MASTER",
                    paid_count_per_account=10,
                    total_count_limit=20,
                )
                self.assertEqual(job.status, "created")
                db.update_private_job(job.id, status="awaiting_master_transfer")
                account = db.update_private_account(
                    job.id,
                    "B",
                    state="accepted",
                    invitation_id="GI-ID",
                    member_state={
                        "guild_level": 1,
                        "daily_free_research_count": 0,
                        "daily_paid_research_count": 0,
                    },
                )
                self.assertEqual(account["state"], "accepted")
                self.assertEqual(account["member_state"]["guild_level"], 1)

    def test_guild_run_history_and_account_totals_are_recorded(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "accounts.db"
            with AccountDB(db_path) as db:
                db.upsert_state(
                    AccountState(mid="MID", next_stage=31),
                    used=True,
                    ready=True,
                    invalid=False,
                )
                run_id = db.record_guild_run(
                    "MID",
                    guild_id="G-ID",
                    joined_at=100,
                    left_at=200,
                    free_research_count=3,
                    paid_research_count=2,
                    free_effective_count=5,
                    paid_effective_count=4,
                    free_super_success_count=1,
                    paid_super_success_count=1,
                    diamond_spent=40,
                    stop_reason="total_count_reached",
                    ok=True,
                )

                account = db.get("MID")
                self.assertEqual(account.guild_last_id, "G-ID")
                self.assertEqual(account.guild_joined_at, 100)
                self.assertEqual(account.guild_left_at, 200)
                self.assertEqual(account.guild, 200)
                self.assertEqual(account.guild_free_research_total, 3)
                self.assertEqual(account.guild_paid_research_total, 2)
                self.assertEqual(account.guild_effective_research_total, 9)
                self.assertEqual(account.guild_super_success_total, 2)
                self.assertEqual(account.guild_diamond_spent_total, 40)

                runs = db.list_guild_runs("MID")
                self.assertEqual(len(runs), 1)
                self.assertEqual(runs[0]["id"], run_id)
                self.assertEqual(runs[0]["effective_research_count"], 9)
                self.assertEqual(runs[0]["super_success_count"], 2)


class FakeWorkflowClient:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.signup_count = 0
        self.get_guild_count = 0
        self.free_count = 0
        self.paid_count = 0
        self.requests: list[tuple[str, bytes]] = []
        self.signup_balances = (900, 860)

    def unary(self, path, message, metadata=None):
        self.calls.append(path)
        self.requests.append((path, message))
        if path == SIGNUP_PATH:
            self.signup_count += 1
            balance = self.signup_balances[self.signup_count - 1]
            return GrpcResponse(signup_response(balance), {}, {})
        if path == REFRESH_MAIL_BOX_PATH:
            return GrpcResponse(refresh_mail_box_response(), {}, {})
        if path == RECEIVE_MAIL_REWARDS_PATH:
            mail_ids = [
                bytes(value).decode("utf-8")
                for field_number, wire_type, value in pb.decode_fields(message)
                if field_number == 1 and wire_type == 2
            ]
            if mail_ids != ["MAIL-1"]:
                raise AssertionError(f"unexpected mail ids: {mail_ids}")
            return GrpcResponse(receive_mail_rewards_response(), {}, {})
        if path == RECEIVE_MAIL_ADVERTISEMENT_REWARD_PATH:
            root_fields = pb.decode_fields(message)
            if len(root_fields) != 1 or root_fields[0][:2] != (1, 2):
                raise AssertionError("unexpected advertisement request envelope")
            viewed_fields = pb.decode_fields(bytes(root_fields[0][2]))
            if viewed_fields != [(1, 0, MAIL_ADVERTISEMENT_DATA_ID)]:
                raise AssertionError(
                    f"unexpected viewed advertisement: {viewed_fields}"
                )
            return GrpcResponse(receive_mail_advertisement_reward_response(), {}, {})
        if path == JOIN_GUILD_PATH:
            return GrpcResponse(join_guild_response(), {}, {})
        if path == GET_GUILD_PATH:
            self.get_guild_count += 1
            experience = 20 if self.get_guild_count == 1 else 30
            return GrpcResponse(guild_detail_response(experience), {}, {})
        if path == ATTEND_GUILD_PATH:
            return GrpcResponse(attend_guild_response(), {}, {})
        if path == CONDUCT_FREE_GUILD_LAB_RESEARCH_PATH:
            self.free_count += 1
            return GrpcResponse(free_research_response(self.free_count), {}, {})
        if path == CONDUCT_PAID_GUILD_LAB_RESEARCH_PATH:
            self.paid_count += 1
            if self.paid_count == 1:
                return GrpcResponse(payment_response(40), {}, {})
            raise GrpcError(
                9,
                "Not enough resources for payment Some(1464007916). "
                "Owned amount: 860, using amount: 900.",
            )
        return GrpcResponse(b"", {}, {})


class DynamicGuildLevelClient:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.free_count = 0

    def unary(self, path, message, metadata=None):
        self.calls.append(path)
        if path == JOIN_GUILD_PATH:
            return GrpcResponse(
                pb.encode_message_field(
                    2,
                    guild_member_state(level=3, free_count=0, paid_count=0),
                ),
                {},
                {},
            )
        if path == ATTEND_GUILD_PATH:
            return GrpcResponse(
                pb.encode_message_field(
                    4,
                    guild_member_state(level=3, free_count=0, paid_count=0),
                ),
                {},
                {},
            )
        if path == CONDUCT_FREE_GUILD_LAB_RESEARCH_PATH:
            self.free_count += 1
            level = 3 if self.free_count <= 3 else 4
            return GrpcResponse(
                dynamic_free_research_response(
                    level=level,
                    free_count=self.free_count,
                ),
                {},
                {},
            )
        if path == CONDUCT_PAID_GUILD_LAB_RESEARCH_PATH:
            return GrpcResponse(dynamic_paid_research_response(), {}, {})
        if path == GET_GUILD_PATH:
            return GrpcResponse(guild_detail_response(), {}, {})
        return GrpcResponse(b"", {}, {})


class GuildRunnerTests(unittest.TestCase):
    def test_run_joined_reuses_sop_without_join_rpc(self) -> None:
        client = FakeWorkflowClient()
        runner = GuildRunner(
            client,
            AccountState(mid="MID", game_access_token="token").to_session(),
            paid_research_limit=0,
            sleep_seconds=0,
            initial_diamond_balance=900,
        )
        initial = parse_accept_guild_invitation_response(
            pb.encode_message_field(
                2,
                guild_member_state(level=1, free_count=0, paid_count=0),
            )
        )

        result = runner.run_joined(
            "G-ID",
            initial_action=initial,
            joined_at=100,
        )

        self.assertTrue(result.ok)
        self.assertEqual(result.joined_at, 100)
        self.assertNotIn(JOIN_GUILD_PATH, client.calls)
        self.assertIn(ATTEND_GUILD_PATH, client.calls)
        self.assertIn(LEAVE_GUILD_PATH, client.calls)

    def test_full_sop_until_insufficient_then_leave(self) -> None:
        client = FakeWorkflowClient()
        balances: list[int] = []
        runner = GuildRunner(
            client,
            AccountState(mid="MID", game_access_token="token").to_session(),
            sleep_seconds=0,
            on_balance=balances.append,
            initial_diamond_balance=900,
        )
        result = runner.run("G00000000-0000-0000-0000-000000000000")
        self.assertTrue(result.ok)
        self.assertEqual(result.free_research_count, 3)
        self.assertEqual(result.free_effective_count, 5)
        self.assertEqual(result.free_super_success_count, 1)
        self.assertEqual(result.paid_research_count, 1)
        self.assertEqual(result.paid_effective_count, 3)
        self.assertEqual(result.paid_super_success_count, 1)
        self.assertEqual(result.effective_research_count, 8)
        self.assertEqual(result.stop_reason, "insufficient_diamonds")
        self.assertEqual(result.diamond_spent, 40)
        self.assertEqual(result.diamond_balance_before_paid, 900)
        self.assertEqual(result.diamond_balance_final, 860)
        self.assertNotIn("mailbox", result.to_dict())
        self.assertEqual(result.to_dict()["donation_count"], 1)
        self.assertEqual(result.guild_progress.level_before, 1)
        self.assertEqual(result.guild_progress.level_after, 2)
        self.assertEqual(result.guild_progress.experience_before, 20)
        self.assertEqual(result.guild_progress.experience_after, 30)
        self.assertEqual(result.guild_progress.member_contribution_before, 0)
        self.assertEqual(result.guild_progress.member_contribution_after, 9)
        self.assertEqual(result.guild_progress.research_point_before, 100)
        self.assertEqual(result.guild_progress.research_point_after, 108)
        self.assertEqual(result.guild_progress.daily_free_research_count_after, 3)
        self.assertEqual(result.guild_progress.daily_donation_count_after, 1)
        self.assertEqual(result.guild_progress.super_success_count, 2)
        progress = result.to_dict()["guild_progress"]
        self.assertEqual(progress["level_change"], 1)
        self.assertEqual(progress["experience_gained"], 10)
        self.assertEqual(progress["member_contribution_gained"], 9)
        self.assertEqual(progress["research_point_gained"], 8)
        self.assertEqual(client.calls[0], JOIN_GUILD_PATH)
        self.assertNotIn(REFRESH_MAIL_BOX_PATH, client.calls)
        self.assertNotIn(RECEIVE_MAIL_REWARDS_PATH, client.calls)
        self.assertNotIn(RECEIVE_MAIL_ADVERTISEMENT_REWARD_PATH, client.calls)
        self.assertNotIn(SIGNUP_PATH, client.calls)
        self.assertIn(JOIN_GUILD_PATH, client.calls)
        self.assertIn(ATTEND_GUILD_PATH, client.calls)
        self.assertEqual(client.calls.count(CONDUCT_FREE_GUILD_LAB_RESEARCH_PATH), 3)
        self.assertEqual(client.calls.count(GET_GUILD_PATH), 2)
        self.assertIn(LEAVE_GUILD_PATH, client.calls)
        self.assertEqual(balances, [860, 860])

    def test_paid_count_limit_stops_without_insufficient_probe(self) -> None:
        client = FakeWorkflowClient()
        runner = GuildRunner(
            client,
            AccountState(mid="MID", game_access_token="token").to_session(),
            paid_research_limit=1,
            sleep_seconds=0,
            initial_diamond_balance=900,
        )

        result = runner.run("G-ID")

        self.assertTrue(result.ok)
        self.assertEqual(result.paid_research_count, 1)
        self.assertEqual(result.stop_reason, "paid_count_reached")
        self.assertEqual(result.diamond_balance_final, 860)
        self.assertEqual(
            client.calls.count(CONDUCT_PAID_GUILD_LAB_RESEARCH_PATH),
            1,
        )

    def test_paid_cost_limit_stops_before_expensive_tier(self) -> None:
        client = FakeWorkflowClient()
        runner = GuildRunner(
            client,
            AccountState(mid="MID", game_access_token="token").to_session(),
            paid_research_limit=None,
            paid_research_cost_limit=300,
            sleep_seconds=0,
            initial_diamond_balance=1000,
            attendance_already_claimed=True,
            leave_after=False,
        )
        initial = parse_accept_guild_invitation_response(
            pb.encode_message_field(
                2,
                guild_member_state(level=1, free_count=3, paid_count=17),
            )
        )

        result = runner.run_joined("G-ID", initial_action=initial)

        self.assertTrue(result.ok)
        self.assertEqual(result.paid_research_count, 0)
        self.assertEqual(result.stop_reason, "paid_cost_limit")
        self.assertIn("400", result.paid_stop_message)
        self.assertNotIn(CONDUCT_PAID_GUILD_LAB_RESEARCH_PATH, client.calls)

    def test_total_count_stops_during_free_research_and_counts_critical(self) -> None:
        client = FakeWorkflowClient()
        runner = GuildRunner(
            client,
            AccountState(mid="MID", game_access_token="token").to_session(),
            paid_research_limit=20,
            total_count_limit=4,
            sleep_seconds=0,
            initial_diamond_balance=900,
        )

        result = runner.run("G-ID")

        self.assertTrue(result.ok)
        self.assertEqual(result.free_research_count, 2)
        self.assertEqual(result.free_effective_count, 4)
        self.assertEqual(result.free_super_success_count, 1)
        self.assertEqual(result.paid_research_count, 0)
        self.assertEqual(result.effective_research_count, 4)
        self.assertEqual(result.stop_reason, "total_count_reached")
        self.assertNotIn(CONDUCT_PAID_GUILD_LAB_RESEARCH_PATH, client.calls)

    def test_account_count_uses_free_and_critical_paid_effective_count(
        self,
    ) -> None:
        client = FakeWorkflowClient()
        runner = GuildRunner(
            client,
            AccountState(mid="MID", game_access_token="token").to_session(),
            paid_research_limit=None,
            effective_research_limit=6,
            sleep_seconds=0,
            initial_diamond_balance=900,
        )

        result = runner.run("G-ID")

        self.assertTrue(result.ok)
        self.assertEqual(result.free_research_count, 3)
        self.assertEqual(result.free_effective_count, 5)
        self.assertEqual(result.paid_research_count, 1)
        self.assertEqual(result.paid_effective_count, 3)
        self.assertEqual(result.effective_research_count, 8)
        self.assertEqual(result.stop_reason, "account_count_reached")
        self.assertEqual(
            client.calls.count(CONDUCT_PAID_GUILD_LAB_RESEARCH_PATH),
            1,
        )

    def test_account_count_includes_normal_paid_and_new_free_research(self) -> None:
        client = DynamicGuildLevelClient()
        runner = GuildRunner(
            client,
            AccountState(mid="MID", game_access_token="token").to_session(),
            paid_research_limit=None,
            effective_research_limit=5,
            sleep_seconds=0,
            initial_diamond_balance=100,
        )

        result = runner.run("G-ID")

        self.assertTrue(result.ok)
        self.assertEqual(result.free_research_count, 4)
        self.assertEqual(result.free_effective_count, 4)
        self.assertEqual(result.paid_research_count, 1)
        self.assertEqual(result.paid_effective_count, 1)
        self.assertEqual(result.effective_research_count, 5)
        self.assertEqual(result.stop_reason, "account_count_reached")
        self.assertEqual(result.guild_progress.level_before, 3)
        self.assertEqual(result.guild_progress.level_after, 4)
        self.assertEqual(
            result.guild_progress.daily_free_research_limit_before,
            3,
        )
        self.assertEqual(
            result.guild_progress.daily_free_research_limit_after,
            4,
        )
        self.assertEqual(
            result.guild_progress.daily_free_research_remaining_after,
            0,
        )
        self.assertEqual(
            client.calls.count(CONDUCT_FREE_GUILD_LAB_RESEARCH_PATH),
            4,
        )
        self.assertEqual(
            client.calls.count(CONDUCT_PAID_GUILD_LAB_RESEARCH_PATH),
            1,
        )


class DummyClient:
    def __init__(self, *args, **kwargs) -> None:
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args) -> None:
        pass


class FakeRunner:
    def __init__(
        self,
        client,
        session,
        *,
        on_balance=None,
        paid_research_limit=100,
        effective_research_limit=None,
        total_count_limit=None,
        **kwargs,
    ) -> None:
        self.on_balance = on_balance
        self.paid_research_limit = paid_research_limit
        self.effective_research_limit = effective_research_limit
        self.total_count_limit = total_count_limit

    def sync_diamond_balance(self) -> int:
        if self.on_balance:
            self.on_balance(600)
        return 600

    def run(self, guild_id: str) -> GuildWorkflowResult:
        limits = [
            value
            for value in (
                self.effective_research_limit,
                self.total_count_limit,
            )
            if value is not None
        ]
        effective_limit = min(limits, default=1_000_000)
        free_count = min(3, effective_limit)
        paid_count = min(
            (
                1_000_000
                if self.paid_research_limit is None
                else self.paid_research_limit
            ),
            max(0, effective_limit - free_count),
        )
        effective_count = free_count + paid_count
        diamond_spent = paid_count * 100
        final_balance = max(0, 600 - diamond_spent)
        if self.on_balance:
            self.on_balance(final_balance)
        now = time.time()
        return GuildWorkflowResult(
            joined=True,
            joined_at=now - 1,
            attendance_claimed=True,
            free_research_count=free_count,
            free_effective_count=free_count,
            paid_research_count=paid_count,
            paid_effective_count=paid_count,
            diamond_balance_before_paid=600,
            diamond_spent=diamond_spent,
            diamond_balance_final=final_balance,
            left_guild=True,
            left_at=now,
            stop_reason=(
                "total_count_reached"
                if self.total_count_limit is not None
                and effective_count >= self.total_count_limit
                else (
                    "account_count_reached"
                    if self.effective_research_limit is not None
                    and effective_count >= self.effective_research_limit
                    else "paid_count_reached"
                )
            ),
            guild_progress=GuildProgress(
                level_before=1,
                level_after=2,
                experience_before=20,
                experience_after=20 + effective_count,
                member_contribution_before=0,
                member_contribution_after=effective_count,
                research_point_before=100,
                research_point_after=100 + effective_count,
                daily_free_research_count_before=0,
                daily_free_research_count_after=free_count,
                daily_donation_count_before=0,
                daily_donation_count_after=paid_count,
            ),
        )


class FakeRecruitmentLimitRunner(FakeRunner):
    def run(self, guild_id: str) -> GuildWorkflowResult:
        return GuildWorkflowResult(
            error=(
                "GrpcError: grpc-status=9 message='Guild has reached the "
                "daily recruitment limit of 51.'"
            ),
        )


class FakeSearchGuild:
    calls = 0

    def __init__(self, client, session) -> None:
        pass

    def search_guilds(self, query: str) -> GrpcResponse:
        type(self).calls += 1
        return GrpcResponse(guild_search_response(), {}, {})


class PrivateScenarioClient:
    calls: list[tuple[str, str]] = []
    master_mid = "CONTROLLER"
    free_counts: dict[str, int] = {}

    def __init__(self, endpoint: str) -> None:
        self.endpoint = endpoint

    def __enter__(self):
        return self

    def __exit__(self, *args) -> None:
        return None

    def unary(self, path, message, metadata=None):
        mid = str((metadata or {}).get("crumble-user-id") or "")
        type(self).calls.append((mid, path))
        if path == SEARCH_GUILDS_PATH:
            master_name = (
                "absdbld" if type(self).master_mid == "OWNER" else "controller"
            )
            summary = guild_summary_message(
                join_method=1,
                master_mid=type(self).master_mid,
                master_name=master_name,
            )
            return GrpcResponse(pb.encode_message_field(1, summary), {}, {})
        if path == GET_GUILD_PATH:
            return GrpcResponse(guild_detail_response(), {}, {})
        if path == INVITE_USER_TO_GUILD_PATH:
            return GrpcResponse(pb.encode_string_field(1, "GI-B"), {}, {})
        if path == GET_GUILD_INVITATIONS_FOR_USER_PATH:
            invited_at = pb.encode_int64_field(1, 1_786_000_000_000)
            invitation = b"".join(
                (
                    pb.encode_string_field(1, "GI-B"),
                    pb.encode_message_field(2, invited_at),
                    pb.encode_message_field(
                        3,
                        guild_summary_message(
                            join_method=1,
                            master_mid="CONTROLLER",
                            master_name="controller",
                        ),
                    ),
                )
            )
            return GrpcResponse(pb.encode_message_field(1, invitation), {}, {})
        if path == ACCEPT_GUILD_INVITATION_PATH:
            return GrpcResponse(
                pb.encode_message_field(
                    2,
                    guild_member_state(level=1, free_count=0, paid_count=0),
                ),
                {},
                {},
            )
        if path == ATTEND_GUILD_PATH:
            return GrpcResponse(attend_guild_response(), {}, {})
        if path == CONDUCT_FREE_GUILD_LAB_RESEARCH_PATH:
            count = type(self).free_counts.get(mid, 0) + 1
            type(self).free_counts[mid] = count
            return GrpcResponse(free_research_response(count), {}, {})
        if path == CONDUCT_PAID_GUILD_LAB_RESEARCH_PATH:
            return GrpcResponse(payment_response(10), {}, {})
        if path == LEAVE_GUILD_PATH:
            return GrpcResponse(b"", {}, {})
        raise AssertionError(f"unexpected private RPC: mid={mid} path={path}")


class PrivateRecruitmentLimitClient(PrivateScenarioClient):
    calls: list[tuple[str, str]] = []
    master_mid = "CONTROLLER"
    free_counts: dict[str, int] = {}

    def unary(self, path, message, metadata=None):
        if path == INVITE_USER_TO_GUILD_PATH:
            mid = str((metadata or {}).get("crumble-user-id") or "")
            type(self).calls.append((mid, path))
            raise GrpcError(
                9,
                "Guild has reached the daily recruitment limit of 51.",
            )
        return super().unary(path, message, metadata)


class PrivateWaitingClient:
    calls: list[str] = []

    def __init__(self, endpoint: str) -> None:
        self.endpoint = endpoint

    def __enter__(self):
        return self

    def __exit__(self, *args) -> None:
        return None

    def unary(self, path, message, metadata=None):
        type(self).calls.append(path)
        if path == GET_USER_SOCIAL_INFO_PATH:
            return GrpcResponse(user_social_info_response(), {}, {})
        if path == SEARCH_GUILDS_PATH:
            return GrpcResponse(
                pb.encode_message_field(
                    1,
                    guild_summary_message(
                        join_method=1,
                        master_mid="OWNER",
                        master_name="absdbld",
                    ),
                ),
                {},
                {},
            )
        if path == GET_GUILD_PATH:
            raise GrpcError(9, "user is not a guild member")
        if path == GET_GUILD_APPLICATIONS_FOR_USER_PATH:
            return GrpcResponse(b"", {}, {})
        if path == APPLY_GUILD_PATH:
            return GrpcResponse(pb.encode_string_field(1, "GA-ID"), {}, {})
        raise AssertionError(f"unexpected waiting RPC: {path}")


class PrivateReturnClient:
    calls: list[tuple[str, str]] = []
    master_mid = "CONTROLLER"
    member_ids: tuple[str, ...] = ("OWNER", "CONTROLLER")

    def __init__(self, endpoint: str) -> None:
        self.endpoint = endpoint

    def __enter__(self):
        return self

    def __exit__(self, *args) -> None:
        return None

    def unary(self, path, message, metadata=None):
        mid = str((metadata or {}).get("crumble-user-id") or "")
        type(self).calls.append((mid, path))
        if path == SEARCH_GUILDS_PATH:
            master_name = (
                "absdbld" if type(self).master_mid == "OWNER" else "controller"
            )
            return GrpcResponse(
                pb.encode_message_field(
                    1,
                    guild_summary_message(
                        join_method=1,
                        master_mid=type(self).master_mid,
                        master_name=master_name,
                    ),
                ),
                {},
                {},
            )
        if path == GET_GUILD_PATH:
            return GrpcResponse(
                guild_detail_response(member_ids=type(self).member_ids),
                {},
                {},
            )
        if path == TRANSFER_GUILD_MASTER_PATH:
            self.assert_transfer_request(message)
            type(self).master_mid = "OWNER"
            return GrpcResponse(b"", {}, {})
        raise AssertionError(f"unexpected private return RPC: {path}")

    @staticmethod
    def assert_transfer_request(message: bytes) -> None:
        if pb.decode_fields(message) != [
            (1, 2, b"G-ID"),
            (2, 2, b"OWNER"),
        ]:
            raise AssertionError("unexpected transfer request")


class GuildCommandTests(unittest.TestCase):
    def test_guild_parser_exposes_public_private_and_joblist(self) -> None:
        parser = cli.build_parser()
        args = parser.parse_args(
            [
                "guild",
                "public",
                "--gname",
                "ahhhha",
                "--gmname",
                "absdbld",
                "--count",
                "20",
                "--totalcount",
                "200",
            ]
        )
        self.assertEqual(args.count, 20)
        self.assertEqual(args.totalcount, 200)
        self.assertEqual(args.guild_action, "public")

        without_totalcount = parser.parse_args(
            [
                "guild",
                "public",
                "--gname",
                "ahhhha",
                "--gmname",
                "absdbld",
                "--count",
                "20",
            ]
        )
        self.assertIsNone(without_totalcount.totalcount)

        private_without_totalcount = parser.parse_args(
            [
                "guild",
                "private",
                "--gname",
                "ahhhha",
                "--gmname",
                "absdbld",
                "--count",
                "20",
            ]
        )
        self.assertIsNone(private_without_totalcount.totalcount)

        with redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                parser.parse_args(["guild"])
            for removed_action in (
                "run",
                "invite",
                "accept",
                "banish",
                "kick",
                "members",
                "getmembers",
            ):
                with self.subTest(removed_action=removed_action):
                    with self.assertRaises(SystemExit):
                        parser.parse_args(["guild", removed_action])

        public = parser.parse_args(
            [
                "guild",
                "public",
                "--gname",
                "ahhhha",
                "--gmname",
                "absdbld",
                "--count",
                "10",
                "--totalcount",
                "20",
            ]
        )
        self.assertEqual(public.guild_action, "public")

        private = parser.parse_args(
            [
                "guild",
                "private",
                "--gname",
                "ahhhha",
                "--gmname",
                "absdbld",
                "--master-mid",
                "CONTROLLER",
                "--count",
                "10",
                "--totalcount",
                "20",
            ]
        )
        self.assertEqual(private.guild_action, "private")
        self.assertEqual(private.master_mid, "CONTROLLER")
        self.assertFalse(private.confirm)

        private_auto = parser.parse_args(
            [
                "guild",
                "private",
                "--gname",
                "ahhhha",
                "--gmname",
                "absdbld",
                "--count",
                "10",
                "--totalcount",
                "20",
            ]
        )
        self.assertIsNone(private_auto.master_mid)

        private_confirm = parser.parse_args(
            [
                "guild",
                "private",
                "--gname",
                "ahhhha",
                "--gmname",
                "absdbld",
                "--count",
                "10",
                "--totalcount",
                "2000",
                "--confirm",
            ]
        )
        self.assertTrue(private_confirm.confirm)

        guild_joblist = parser.parse_args(["guild", "joblist"])
        self.assertEqual(guild_joblist.guild_action, "joblist")
        with redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                parser.parse_args(["guild", "list"])

        return_list = parser.parse_args(["guild", "private", "return"])
        self.assertEqual(return_list.private_action, "return")
        self.assertIsNone(return_list.private_job_id)

        return_one = parser.parse_args(["guild", "private", "return", "12"])
        self.assertEqual(return_one.private_action, "return")
        self.assertEqual(return_one.private_job_id, 12)

    def test_guild_joblist_shows_all_private_jobs(self) -> None:
        parser = cli.build_parser()
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "accounts.db"
            with AccountDB(db_path) as db:
                pending = db.create_private_job(
                    guild_id="G-ID",
                    gname="ahhhha",
                    gmname="absdbld",
                    original_master_mid="OWNER",
                    controller_mid="CONTROLLER-A",
                    paid_count_per_account=10,
                    total_count_limit=500,
                )
                db.update_private_job(
                    pending.id,
                    status="awaiting_master_return",
                    effective_count=500,
                )
                db.update_private_account(
                    pending.id,
                    "DONOR-A",
                    state="complete",
                )
                complete = db.create_private_job(
                    guild_id="G-ID",
                    gname="ahhhha",
                    gmname="absdbld",
                    original_master_mid="OWNER",
                    controller_mid="CONTROLLER-B",
                    paid_count_per_account=5,
                    total_count_limit=100,
                )
                db.update_private_job(
                    complete.id,
                    status="complete",
                    effective_count=100,
                )

            args = parser.parse_args(
                ["guild", "joblist", "--db", str(db_path)]
            )
            output = io.StringIO()
            with redirect_stdout(output):
                code = cli.cmd_guild(args)

            self.assertEqual(code, 0)
            payload = json.loads(output.getvalue())
            self.assertEqual(payload["mode"], "guild_joblist")
            self.assertEqual(payload["index_field"], "guild_private_jobs.id")
            self.assertEqual(payload["count"], 2)
            self.assertEqual(payload["pending_master_return_count"], 1)
            self.assertEqual(
                payload["status_counts"],
                {
                    "awaiting_master_return": 1,
                    "complete": 1,
                },
            )
            self.assertNotIn("guild_targets", payload)
            jobs = {job["id"]: job for job in payload["jobs"]}
            self.assertTrue(jobs[pending.id]["return_pending"])
            self.assertEqual(
                jobs[pending.id]["return_command"],
                f"python main.py guild private return {pending.id}",
            )
            self.assertEqual(jobs[pending.id]["account_states"], {"complete": 1})
            self.assertFalse(jobs[complete.id]["return_pending"])
            self.assertEqual(
                jobs[complete.id]["return_command"],
                f"python main.py guild private return {complete.id}",
            )

    def test_private_flow_invites_donor_and_waits_for_manual_master_return(
        self,
    ) -> None:
        PrivateScenarioClient.calls = []
        PrivateScenarioClient.free_counts = {}
        PrivateScenarioClient.master_mid = "CONTROLLER"
        parser = cli.build_parser()
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "accounts.db"
            with AccountDB(db_path) as db:
                for mid in ("CONTROLLER", "DONOR"):
                    db.upsert_state(
                        AccountState(
                            mid=mid,
                            guest_secret="secret",
                            game_access_token="token",
                            next_stage=31,
                            diamond_balance=900,
                        ),
                        used=True,
                        ready=True,
                        invalid=False,
                    )
                db.upsert_guild_target(
                    gname="ahhhha",
                    gmname="absdbld",
                    guild_id="G-ID",
                    guild_level=1,
                    member_count=2,
                    master_user_id="OWNER",
                    original_master_mid="OWNER",
                    details={"search_summary": {"join_method": 1}},
                )

            args = parser.parse_args(
                [
                    "guild",
                    "private",
                    "--gname",
                    "ahhhha",
                    "--gmname",
                    "absdbld",
                    "--master-mid",
                    "CONTROLLER",
                    "--count",
                    "6",
                    "--totalcount",
                    "6",
                    "--db",
                    str(db_path),
                ]
            )
            output = io.StringIO()
            with (
                patch.object(cli, "GrpcClient", PrivateScenarioClient),
                patch.object(
                    cli, "_login_account", side_effect=lambda row: row.to_state()
                ),
                redirect_stdout(output),
            ):
                code = cli.cmd_guild(args)

            self.assertEqual(code, 0)
            payload = json.loads(output.getvalue())
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["mode"], "private")
            self.assertEqual(payload["next_state"], "awaiting_master_return")
            self.assertEqual(
                payload["manual_master_return"]["to_original_master_mid"],
                "OWNER",
            )
            self.assertEqual(payload["accounts_attempted"], 1)
            self.assertEqual(payload["count"], 6)
            self.assertEqual(payload["totalcount"], 8)
            self.assertEqual(
                payload["results"][0]["effective_research_count"],
                8,
            )
            self.assertNotIn(
                TRANSFER_GUILD_MASTER_PATH,
                [path for _, path in PrivateScenarioClient.calls],
            )

            with AccountDB(db_path) as db:
                target = db.get_guild_target("ahhhha", "absdbld")
                self.assertEqual(target.original_master_mid, "OWNER")
                job = db.get_active_private_job("G-ID", "CONTROLLER")
                self.assertEqual(job.status, "awaiting_master_return")
                donor = db.get("DONOR")
                self.assertGreater(donor.guild_left_at, donor.guild_joined_at)
                self.assertEqual(donor.guild_paid_research_total, 1)

            PrivateScenarioClient.master_mid = "OWNER"
            resumed_output = io.StringIO()
            with (
                patch.object(cli, "GrpcClient", PrivateScenarioClient),
                patch.object(
                    cli, "_login_account", side_effect=lambda row: row.to_state()
                ),
                redirect_stdout(resumed_output),
            ):
                resumed_code = cli.cmd_guild(args)

            self.assertEqual(resumed_code, 0)
            resumed = json.loads(resumed_output.getvalue())
            self.assertTrue(resumed["complete"])
            self.assertEqual(resumed["stopped_reason"], "complete")
            with AccountDB(db_path) as db:
                jobs = db._conn.execute(
                    "SELECT status FROM guild_private_jobs ORDER BY id"
                ).fetchall()
                self.assertEqual([row[0] for row in jobs], ["complete"])

            repeated_output = io.StringIO()
            with redirect_stdout(repeated_output):
                repeated_code = cli.cmd_guild(args)
            self.assertEqual(repeated_code, 0)
            repeated = json.loads(repeated_output.getvalue())
            self.assertTrue(repeated["complete"])
            self.assertEqual(
                repeated["stopped_reason"],
                "target_already_complete",
            )
            self.assertEqual(repeated["next_action"]["action"], "none")
            with AccountDB(db_path) as db:
                self.assertEqual(len(db.list_private_jobs()), 1)

    def test_private_flow_rejects_total_above_daily_invitation_capacity(
        self,
    ) -> None:
        parser = cli.build_parser()
        args = parser.parse_args(
            [
                "guild",
                "private",
                "--gname",
                "ahhhha",
                "--gmname",
                "absdbld",
                "--count",
                "10",
                "--totalcount",
                "501",
            ]
        )

        with self.assertRaisesRegex(
            SystemExit,
            r"--count=10.*50.*= 500.*--totalcount.*<= 500.*--count.*>= 11",
        ):
            cli.cmd_guild(args)

    def test_private_flow_waits_for_more_accounts_and_resumes_same_target(
        self,
    ) -> None:
        PrivateScenarioClient.calls = []
        PrivateScenarioClient.free_counts = {}
        PrivateScenarioClient.master_mid = "CONTROLLER"
        parser = cli.build_parser()
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "accounts.db"
            with AccountDB(db_path) as db:
                for mid in ("CONTROLLER", "DONOR-A"):
                    db.upsert_state(
                        AccountState(
                            mid=mid,
                            guest_secret="secret",
                            game_access_token="token",
                            next_stage=31,
                            diamond_balance=900,
                        ),
                        used=True,
                        ready=True,
                        invalid=False,
                    )
                db.upsert_guild_target(
                    gname="ahhhha",
                    gmname="absdbld",
                    guild_id="G-ID",
                    guild_level=1,
                    member_count=2,
                    master_user_id="OWNER",
                    original_master_mid="OWNER",
                    details={"search_summary": {"join_method": 1}},
                )

            args = parser.parse_args(
                [
                    "guild",
                    "private",
                    "--gname",
                    "ahhhha",
                    "--gmname",
                    "absdbld",
                    "--master-mid",
                    "CONTROLLER",
                    "--count",
                    "8",
                    "--totalcount",
                    "9",
                    "--db",
                    str(db_path),
                ]
            )

            first_output = io.StringIO()
            with (
                patch.object(cli, "GrpcClient", PrivateScenarioClient),
                patch.object(
                    cli, "_login_account", side_effect=lambda row: row.to_state()
                ),
                redirect_stdout(first_output),
            ):
                first_code = cli.cmd_guild(args)

            self.assertEqual(first_code, 0)
            first = json.loads(first_output.getvalue())
            self.assertEqual(first["state"], "awaiting_donors")
            self.assertEqual(first["stopped_reason"], "target_not_reached")
            self.assertEqual(first["totalcount"], 8)
            self.assertEqual(first["remaining_totalcount"], 1)
            self.assertEqual(
                first["next_action"]["action"],
                "prepare_eligible_donors_and_rerun",
            )
            with AccountDB(db_path) as db:
                active = db.get_active_private_job("G-ID", "CONTROLLER")
                self.assertEqual(active.status, "awaiting_donors")
                db.upsert_state(
                    AccountState(
                        mid="DONOR-B",
                        guest_secret="secret",
                        game_access_token="token",
                        next_stage=31,
                        diamond_balance=900,
                    ),
                    used=True,
                    ready=True,
                    invalid=False,
                )

            second_output = io.StringIO()
            with (
                patch.object(cli, "GrpcClient", PrivateScenarioClient),
                patch.object(
                    cli, "_login_account", side_effect=lambda row: row.to_state()
                ),
                redirect_stdout(second_output),
            ):
                second_code = cli.cmd_guild(args)

            self.assertEqual(second_code, 0)
            second = json.loads(second_output.getvalue())
            self.assertEqual(second["state"], "awaiting_master_return")
            self.assertTrue(second["totalcount_reached"])
            self.assertEqual(second["totalcount"], 9)
            self.assertEqual(second["totalcount_added"], 1)
            self.assertEqual(second["next_action"]["action"], "return_master")
            with AccountDB(db_path) as db:
                active = db.get_active_private_job("G-ID", "CONTROLLER")
                self.assertEqual(active.status, "awaiting_master_return")
                self.assertEqual(len(db.list_private_jobs()), 1)

    def test_private_flow_stops_at_daily_recruitment_limit_and_keeps_donor(
        self,
    ) -> None:
        PrivateRecruitmentLimitClient.calls = []
        PrivateRecruitmentLimitClient.master_mid = "CONTROLLER"
        parser = cli.build_parser()
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "accounts.db"
            with AccountDB(db_path) as db:
                for mid in ("CONTROLLER", "DONOR-A", "DONOR-B"):
                    db.upsert_state(
                        AccountState(
                            mid=mid,
                            guest_secret="secret",
                            game_access_token="token",
                            next_stage=31,
                            diamond_balance=900,
                        ),
                        used=True,
                        ready=True,
                        invalid=False,
                    )
                db.upsert_guild_target(
                    gname="ahhhha",
                    gmname="absdbld",
                    guild_id="G-ID",
                    guild_level=1,
                    member_count=2,
                    master_user_id="OWNER",
                    original_master_mid="OWNER",
                    details={"search_summary": {"join_method": 1}},
                )
                job = db.create_private_job(
                    guild_id="G-ID",
                    gname="ahhhha",
                    gmname="absdbld",
                    original_master_mid="OWNER",
                    controller_mid="CONTROLLER",
                    paid_count_per_account=1,
                    total_count_limit=50,
                )
                db.update_private_job(job.id, status="awaiting_donors")
                db.update_private_account(
                    job.id,
                    "DONOR-A",
                    state="failed",
                    error=(
                        "GrpcError: grpc-status=9 message='Guild has reached "
                        "the daily recruitment limit of 51.'"
                    ),
                )

            args = parser.parse_args(
                [
                    "guild",
                    "private",
                    "--gname",
                    "ahhhha",
                    "--gmname",
                    "absdbld",
                    "--master-mid",
                    "CONTROLLER",
                    "--count",
                    "1",
                    "--totalcount",
                    "50",
                    "--db",
                    str(db_path),
                ]
            )
            output = io.StringIO()
            with (
                patch.object(cli, "GrpcClient", PrivateRecruitmentLimitClient),
                patch.object(
                    cli, "_login_account", side_effect=lambda row: row.to_state()
                ),
                redirect_stdout(output),
            ):
                code = cli.cmd_guild(args)

            self.assertEqual(code, 0)
            payload = json.loads(output.getvalue())
            self.assertTrue(payload["ok"])
            self.assertFalse(payload["complete"])
            self.assertEqual(payload["state"], "awaiting_recruitment_reset")
            self.assertEqual(
                payload["stopped_reason"],
                "daily_recruitment_limit_reached",
            )
            self.assertEqual(payload["daily_recruitment_limit"], 51)
            self.assertEqual(payload["retryable_mid"], "DONOR-A")
            self.assertEqual(payload["accounts_attempted"], 1)
            self.assertEqual(payload["accounts_failed"], 0)
            self.assertEqual(
                payload["next_action"]["action"],
                "wait_for_daily_recruitment_reset_and_rerun",
            )
            invite_calls = [
                path
                for _, path in PrivateRecruitmentLimitClient.calls
                if path == INVITE_USER_TO_GUILD_PATH
            ]
            self.assertEqual(len(invite_calls), 1)

            with AccountDB(db_path) as db:
                saved_job = db.get_private_job(job.id)
                self.assertEqual(saved_job.status, "awaiting_recruitment_reset")
                donor_a = db.get_private_account(job.id, "DONOR-A")
                self.assertEqual(donor_a["state"], "selected")
                self.assertIn("daily recruitment limit", donor_a["error"])
                self.assertIsNone(db.get_private_account(job.id, "DONOR-B"))
                account = db.get("DONOR-A")
                self.assertEqual(account.guild_joined_at, 0)
                self.assertEqual(account.guild_left_at, 0)

    def test_private_without_totalcount_finishes_when_recruitment_is_blocked(
        self,
    ) -> None:
        PrivateRecruitmentLimitClient.calls = []
        PrivateRecruitmentLimitClient.master_mid = "CONTROLLER"
        parser = cli.build_parser()
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "accounts.db"
            with AccountDB(db_path) as db:
                for mid in ("CONTROLLER", "DONOR"):
                    db.upsert_state(
                        AccountState(
                            mid=mid,
                            guest_secret="secret",
                            game_access_token="token",
                            next_stage=31,
                            diamond_balance=900,
                        ),
                        used=True,
                        ready=True,
                        invalid=False,
                    )
                db.upsert_guild_target(
                    gname="ahhhha",
                    gmname="absdbld",
                    guild_id="G-ID",
                    guild_level=1,
                    member_count=2,
                    master_user_id="OWNER",
                    original_master_mid="OWNER",
                    details={"search_summary": {"join_method": 1}},
                )

            args = parser.parse_args(
                [
                    "guild",
                    "private",
                    "--gname",
                    "ahhhha",
                    "--gmname",
                    "absdbld",
                    "--master-mid",
                    "CONTROLLER",
                    "--count",
                    "10",
                    "--db",
                    str(db_path),
                ]
            )
            output = io.StringIO()
            with (
                patch.object(cli, "GrpcClient", PrivateRecruitmentLimitClient),
                patch.object(
                    cli, "_login_account", side_effect=lambda row: row.to_state()
                ),
                redirect_stdout(output),
            ):
                code = cli.cmd_guild(args)

            self.assertEqual(code, 0)
            payload = json.loads(output.getvalue())
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["state"], "awaiting_master_return")
            self.assertIsNone(payload["requested_totalcount"])
            self.assertIsNone(payload["remaining_totalcount"])
            self.assertFalse(payload["totalcount_reached"])
            self.assertEqual(payload["account_limit"], 50)
            self.assertEqual(payload["account_count"], 0)
            self.assertFalse(payload["account_limit_reached"])
            self.assertEqual(
                payload["stopped_reason"],
                "daily_recruitment_limit_reached",
            )
            self.assertEqual(payload["next_action"]["action"], "return_master")
            with AccountDB(db_path) as db:
                job = db.get_active_private_job("G-ID", "CONTROLLER")
                self.assertEqual(job.status, "awaiting_master_return")
                self.assertEqual(job.total_count_limit, 0)

    def test_private_without_totalcount_finishes_after_fifty_accounts(self) -> None:
        PrivateScenarioClient.calls = []
        PrivateScenarioClient.master_mid = "CONTROLLER"
        parser = cli.build_parser()
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "accounts.db"
            with AccountDB(db_path) as db:
                db.upsert_state(
                    AccountState(
                        mid="CONTROLLER",
                        guest_secret="secret",
                        game_access_token="token",
                        next_stage=31,
                    ),
                    used=True,
                    ready=True,
                    invalid=False,
                )
                db.upsert_guild_target(
                    gname="ahhhha",
                    gmname="absdbld",
                    guild_id="G-ID",
                    guild_level=1,
                    member_count=2,
                    master_user_id="OWNER",
                    original_master_mid="OWNER",
                    details={"search_summary": {"join_method": 1}},
                )
                job = db.create_private_job(
                    guild_id="G-ID",
                    gname="ahhhha",
                    gmname="absdbld",
                    original_master_mid="OWNER",
                    controller_mid="CONTROLLER",
                    paid_count_per_account=10,
                    total_count_limit=0,
                )
                db.update_private_job(job.id, status="running")
                for index in range(50):
                    db.update_private_account(
                        job.id,
                        f"DONOR-{index:02d}",
                        state="complete",
                    )

            args = parser.parse_args(
                [
                    "guild",
                    "private",
                    "--gname",
                    "ahhhha",
                    "--gmname",
                    "absdbld",
                    "--master-mid",
                    "CONTROLLER",
                    "--count",
                    "10",
                    "--db",
                    str(db_path),
                ]
            )
            output = io.StringIO()
            with (
                patch.object(cli, "GrpcClient", PrivateScenarioClient),
                patch.object(
                    cli, "_login_account", side_effect=lambda row: row.to_state()
                ),
                redirect_stdout(output),
            ):
                code = cli.cmd_guild(args)

            self.assertEqual(code, 0)
            payload = json.loads(output.getvalue())
            self.assertEqual(payload["state"], "awaiting_master_return")
            self.assertIsNone(payload["progress"]["target"])
            self.assertEqual(payload["progress"]["account_count"], 50)
            self.assertEqual(payload["progress"]["account_limit"], 50)
            self.assertTrue(payload["progress"]["account_limit_reached"])
            self.assertEqual(payload["next_action"]["action"], "return_master")
            with AccountDB(db_path) as db:
                saved = db.get_private_job(job.id)
                self.assertEqual(saved.status, "awaiting_master_return")

    def test_private_return_lists_pending_jobs_by_database_id(self) -> None:
        parser = cli.build_parser()
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "accounts.db"
            with AccountDB(db_path) as db:
                pending = db.create_private_job(
                    guild_id="G-ID",
                    gname="ahhhha",
                    gmname="absdbld",
                    original_master_mid="OWNER",
                    controller_mid="CONTROLLER",
                    paid_count_per_account=10,
                    total_count_limit=20,
                )
                db.update_private_job(
                    pending.id,
                    status="awaiting_master_return",
                    effective_count=20,
                )
                complete = db.create_private_job(
                    guild_id="OTHER-GUILD",
                    gname="other",
                    gmname="other-owner",
                    original_master_mid="OTHER-OWNER",
                    controller_mid="OTHER-CONTROLLER",
                    paid_count_per_account=1,
                    total_count_limit=3,
                )
                db.update_private_job(complete.id, status="complete")

            args = parser.parse_args(
                [
                    "guild",
                    "private",
                    "return",
                    "--db",
                    str(db_path),
                ]
            )
            output = io.StringIO()
            with redirect_stdout(output):
                code = cli.cmd_guild(args)

            self.assertEqual(code, 0)
            payload = json.loads(output.getvalue())
            self.assertEqual(payload["index_field"], "guild_private_jobs.id")
            self.assertEqual(payload["count"], 1)
            self.assertEqual(payload["jobs"][0]["id"], pending.id)
            self.assertEqual(
                payload["jobs"][0]["return_command"],
                f"python main.py guild private return {pending.id}",
            )

    def test_private_return_transfers_to_original_master_and_completes_job(
        self,
    ) -> None:
        PrivateReturnClient.calls = []
        PrivateReturnClient.master_mid = "CONTROLLER"
        PrivateReturnClient.member_ids = ("OWNER", "CONTROLLER")
        parser = cli.build_parser()
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "accounts.db"
            with AccountDB(db_path) as db:
                db.upsert_state(
                    AccountState(
                        mid="CONTROLLER",
                        guest_secret="secret",
                        game_access_token="token",
                        next_stage=31,
                    ),
                    used=True,
                    ready=True,
                    invalid=False,
                )
                job = db.create_private_job(
                    guild_id="G-ID",
                    gname="ahhhha",
                    gmname="absdbld",
                    original_master_mid="OWNER",
                    controller_mid="CONTROLLER",
                    paid_count_per_account=10,
                    total_count_limit=20,
                )
                db.update_private_job(
                    job.id,
                    status="awaiting_master_return",
                    effective_count=20,
                )

            args = parser.parse_args(
                [
                    "guild",
                    "private",
                    "return",
                    str(job.id),
                    "--db",
                    str(db_path),
                ]
            )
            output = io.StringIO()
            with (
                patch.object(cli, "GrpcClient", PrivateReturnClient),
                patch.object(
                    cli, "_login_account", side_effect=lambda row: row.to_state()
                ),
                redirect_stdout(output),
            ):
                code = cli.cmd_guild(args)

            self.assertEqual(code, 0)
            payload = json.loads(output.getvalue())
            self.assertTrue(payload["ok"])
            self.assertTrue(payload["complete"])
            self.assertTrue(payload["transferred"])
            self.assertEqual(payload["to_original_master_mid"], "OWNER")
            self.assertIn(
                ("CONTROLLER", TRANSFER_GUILD_MASTER_PATH),
                PrivateReturnClient.calls,
            )
            with AccountDB(db_path) as db:
                saved = db.get_private_job(job.id)
                self.assertEqual(saved.status, "complete")
                self.assertGreater(saved.completed_at, 0)

    def test_private_return_allows_running_job_when_explicitly_requested(
        self,
    ) -> None:
        PrivateReturnClient.calls = []
        PrivateReturnClient.master_mid = "CONTROLLER"
        PrivateReturnClient.member_ids = ("OWNER", "CONTROLLER")
        parser = cli.build_parser()
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "accounts.db"
            with AccountDB(db_path) as db:
                db.upsert_state(
                    AccountState(
                        mid="CONTROLLER",
                        guest_secret="secret",
                        game_access_token="token",
                        next_stage=31,
                    ),
                    used=True,
                    ready=True,
                    invalid=False,
                )
                job = db.create_private_job(
                    guild_id="G-ID",
                    gname="ahhhha",
                    gmname="absdbld",
                    original_master_mid="OWNER",
                    controller_mid="CONTROLLER",
                    paid_count_per_account=10,
                    total_count_limit=2000,
                )
                db.update_private_job(
                    job.id,
                    status="running",
                    effective_count=672,
                )

            args = parser.parse_args(
                [
                    "guild",
                    "private",
                    "return",
                    str(job.id),
                    "--db",
                    str(db_path),
                ]
            )
            output = io.StringIO()
            with (
                patch.object(cli, "GrpcClient", PrivateReturnClient),
                patch.object(
                    cli, "_login_account", side_effect=lambda row: row.to_state()
                ),
                redirect_stdout(output),
            ):
                code = cli.cmd_guild(args)

            self.assertEqual(code, 0)
            payload = json.loads(output.getvalue())
            self.assertTrue(payload["ok"])
            self.assertTrue(payload["complete"])
            self.assertTrue(payload["transferred"])
            self.assertEqual(payload["job"]["totalcount"], 672)
            with AccountDB(db_path) as db:
                saved = db.get_private_job(job.id)
                self.assertEqual(saved.status, "complete")
                self.assertEqual(saved.effective_count, 672)

    def test_private_flow_applies_and_persists_waiting_state(self) -> None:
        PrivateWaitingClient.calls = []
        parser = cli.build_parser()
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "accounts.db"
            with AccountDB(db_path) as db:
                db.upsert_state(
                    AccountState(
                        mid="CONTROLLER",
                        guest_secret="secret",
                        game_access_token="token",
                        next_stage=31,
                    ),
                    used=True,
                    ready=True,
                    invalid=False,
                )
                db.upsert_guild_target(
                    gname="ahhhha",
                    gmname="absdbld",
                    guild_id="G-ID",
                    master_user_id="OWNER",
                    original_master_mid="OWNER",
                    details={"search_summary": {"join_method": 1}},
                )
            args = parser.parse_args(
                [
                    "guild",
                    "private",
                    "--gname",
                    "ahhhha",
                    "--gmname",
                    "absdbld",
                    "--master-mid",
                    "CONTROLLER",
                    "--count",
                    "1",
                    "--totalcount",
                    "6",
                    "--db",
                    str(db_path),
                ]
            )
            output = io.StringIO()
            with (
                patch.object(cli, "GrpcClient", PrivateWaitingClient),
                patch.object(
                    cli, "_login_account", side_effect=lambda row: row.to_state()
                ),
                redirect_stdout(output),
            ):
                code = cli.cmd_guild(args)

            self.assertEqual(code, 0)
            payload = json.loads(output.getvalue())
            self.assertEqual(
                payload["stopped_reason"],
                "awaiting_application_approval",
            )
            self.assertEqual(payload["job"]["application_id"], "GA-ID")
            self.assertEqual(payload["job"]["original_master_mid"], "OWNER")
            self.assertEqual(payload["controller"]["name"], "garlic-proxy")
            self.assertEqual(
                payload["manual_action"]["controller_name"],
                "garlic-proxy",
            )
            self.assertIn("garlic-proxy", payload["next_action"]["message"])
            self.assertIn("CONTROLLER", payload["next_action"]["message"])
            self.assertIn(APPLY_GUILD_PATH, PrivateWaitingClient.calls)

    def test_private_confirm_updates_active_job_parameters(self) -> None:
        parser = cli.build_parser()
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "accounts.db"
            with AccountDB(db_path) as db:
                db.upsert_state(
                    AccountState(
                        mid="CONTROLLER",
                        guest_secret="secret",
                        game_access_token="token",
                        next_stage=31,
                    ),
                    used=True,
                    ready=True,
                    invalid=False,
                )
                db.upsert_guild_target(
                    gname="ahhhha",
                    gmname="absdbld",
                    guild_id="G-ID",
                    master_user_id="OWNER",
                    original_master_mid="OWNER",
                    details={"search_summary": {"join_method": 1}},
                )
                job = db.create_private_job(
                    guild_id="G-ID",
                    gname="ahhhha",
                    gmname="absdbld",
                    original_master_mid="OWNER",
                    controller_mid="CONTROLLER",
                    paid_count_per_account=10,
                    total_count_limit=200,
                )

            command = [
                "guild",
                "private",
                "--gname",
                "ahhhha",
                "--gmname",
                "absdbld",
                "--master-mid",
                "CONTROLLER",
                "--count",
                "40",
                "--totalcount",
                "2000",
                "--db",
                str(db_path),
            ]
            without_confirm = parser.parse_args(command)
            with self.assertRaisesRegex(SystemExit, "--confirm"):
                cli.cmd_guild(without_confirm)

            with_confirm = parser.parse_args([*command, "--confirm"])
            output = io.StringIO()
            with (
                patch.object(
                    cli.PrivateGuildRunner,
                    "run",
                    return_value={
                        "ok": True,
                        "complete": False,
                        "mode": "private",
                        "state": "awaiting_donors",
                        "stopped_reason": "target_not_reached",
                    },
                ) as run,
                redirect_stdout(output),
            ):
                code = cli.cmd_guild(with_confirm)

            self.assertEqual(code, 0)
            updated_job = run.call_args.args[0]
            self.assertEqual(updated_job.id, job.id)
            self.assertEqual(updated_job.paid_count_per_account, 40)
            self.assertEqual(updated_job.total_count_limit, 2000)
            payload = json.loads(output.getvalue())
            self.assertEqual(
                payload["job_parameters_updated"],
                {
                    "confirmed": True,
                    "previous": {"count": 10, "totalcount": 200},
                    "current": {"count": 40, "totalcount": 2000},
                },
            )
            with AccountDB(db_path) as db:
                saved = db.get_private_job(job.id)
                self.assertEqual(saved.paid_count_per_account, 40)
                self.assertEqual(saved.total_count_limit, 2000)

    def test_private_without_totalcount_starts_new_batch_after_completion(
        self,
    ) -> None:
        parser = cli.build_parser()
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "accounts.db"
            with AccountDB(db_path) as db:
                db.upsert_state(
                    AccountState(
                        mid="CONTROLLER",
                        guest_secret="secret",
                        game_access_token="token",
                        next_stage=31,
                    ),
                    used=True,
                    ready=True,
                    invalid=False,
                )
                db.upsert_guild_target(
                    gname="ahhhha",
                    gmname="absdbld",
                    guild_id="G-ID",
                    master_user_id="OWNER",
                    original_master_mid="OWNER",
                    details={"search_summary": {"join_method": 1}},
                )
                previous = db.create_private_job(
                    guild_id="G-ID",
                    gname="ahhhha",
                    gmname="absdbld",
                    original_master_mid="OWNER",
                    controller_mid="CONTROLLER",
                    paid_count_per_account=10,
                    total_count_limit=0,
                )
                db.update_private_job(previous.id, status="complete")

            args = parser.parse_args(
                [
                    "guild",
                    "private",
                    "--gname",
                    "ahhhha",
                    "--gmname",
                    "absdbld",
                    "--count",
                    "10",
                    "--db",
                    str(db_path),
                ]
            )
            output = io.StringIO()
            with (
                patch.object(
                    cli.PrivateGuildRunner,
                    "run",
                    return_value={
                        "ok": True,
                        "complete": False,
                        "mode": "private",
                        "state": "awaiting_donors",
                        "stopped_reason": "awaiting_eligible_accounts",
                    },
                ) as run,
                redirect_stdout(output),
            ):
                code = cli.cmd_guild(args)

            self.assertEqual(code, 0)
            current = run.call_args.args[0]
            self.assertNotEqual(current.id, previous.id)
            self.assertEqual(current.total_count_limit, 0)
            payload = json.loads(output.getvalue())
            self.assertEqual(payload["controller"]["mid"], "CONTROLLER")
            self.assertEqual(payload["controller"]["source"], "latest_job")
            with AccountDB(db_path) as db:
                self.assertEqual(len(db.list_private_jobs()), 2)

    def test_private_flow_refreshes_stale_public_cache(self) -> None:
        PrivateWaitingClient.calls = []
        parser = cli.build_parser()
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "accounts.db"
            with AccountDB(db_path) as db:
                db.upsert_state(
                    AccountState(
                        mid="CONTROLLER",
                        guest_secret="secret",
                        game_access_token="token",
                        next_stage=31,
                    ),
                    used=True,
                    ready=True,
                    invalid=False,
                )
                db.upsert_guild_target(
                    gname="ahhhha",
                    gmname="absdbld",
                    guild_id="G-ID",
                    master_user_id="OWNER",
                    original_master_mid="OWNER",
                    details={"search_summary": {"join_method": 0}},
                )

            args = parser.parse_args(
                [
                    "guild",
                    "private",
                    "--gname",
                    "ahhhha",
                    "--gmname",
                    "absdbld",
                    "--master-mid",
                    "CONTROLLER",
                    "--count",
                    "1",
                    "--totalcount",
                    "6",
                    "--db",
                    str(db_path),
                ]
            )
            output = io.StringIO()
            with (
                patch.object(cli, "GrpcClient", PrivateWaitingClient),
                patch.object(
                    cli, "_login_account", side_effect=lambda row: row.to_state()
                ),
                redirect_stdout(output),
            ):
                code = cli.cmd_guild(args)

            self.assertEqual(code, 0)
            payload = json.loads(output.getvalue())
            self.assertEqual(payload["state"], "awaiting_application_approval")
            self.assertIn(SEARCH_GUILDS_PATH, PrivateWaitingClient.calls)
            self.assertIn(APPLY_GUILD_PATH, PrivateWaitingClient.calls)
            with AccountDB(db_path) as db:
                target = db.get_guild_target("ahhhha", "absdbld")
                self.assertEqual(
                    target.details["search_summary"]["join_method"],
                    1,
                )
                self.assertEqual(target.original_master_mid, "OWNER")

    def test_private_flow_auto_selects_and_reuses_low_diamond_controller(
        self,
    ) -> None:
        PrivateWaitingClient.calls = []
        parser = cli.build_parser()
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "accounts.db"
            with AccountDB(db_path) as db:
                for mid, diamonds in (
                    ("OWNER", 0),
                    ("PROXY-LOW", 10),
                    ("PROXY-HIGH", 900),
                ):
                    db.upsert_state(
                        AccountState(
                            mid=mid,
                            guest_secret="secret",
                            game_access_token="token",
                            next_stage=31,
                            diamond_balance=diamonds,
                        ),
                        used=True,
                        ready=True,
                        invalid=False,
                    )
                db.upsert_guild_target(
                    gname="ahhhha",
                    gmname="absdbld",
                    guild_id="G-ID",
                    master_user_id="OWNER",
                    original_master_mid="OWNER",
                    details={"search_summary": {"join_method": 1}},
                )

            args = parser.parse_args(
                [
                    "guild",
                    "private",
                    "--gname",
                    "ahhhha",
                    "--gmname",
                    "absdbld",
                    "--count",
                    "1",
                    "--totalcount",
                    "6",
                    "--db",
                    str(db_path),
                ]
            )
            first_output = io.StringIO()
            with (
                patch.object(cli, "GrpcClient", PrivateWaitingClient),
                patch.object(
                    cli, "_login_account", side_effect=lambda row: row.to_state()
                ),
                redirect_stdout(first_output),
            ):
                first_code = cli.cmd_guild(args)

            self.assertEqual(first_code, 0)
            first = json.loads(first_output.getvalue())
            self.assertEqual(first["controller"]["mid"], "PROXY-LOW")
            self.assertEqual(first["controller"]["source"], "auto")
            self.assertEqual(first["state"], "awaiting_application_approval")
            with AccountDB(db_path) as db:
                jobs = db.list_active_private_jobs_for_guild("G-ID")
                self.assertEqual(len(jobs), 1)
                self.assertEqual(jobs[0].controller_mid, "PROXY-LOW")

            second_output = io.StringIO()
            with (
                patch.object(cli, "GrpcClient", PrivateWaitingClient),
                patch.object(
                    cli, "_login_account", side_effect=lambda row: row.to_state()
                ),
                redirect_stdout(second_output),
            ):
                second_code = cli.cmd_guild(args)

            self.assertEqual(second_code, 0)
            second = json.loads(second_output.getvalue())
            self.assertEqual(second["controller"]["mid"], "PROXY-LOW")
            self.assertEqual(second["controller"]["source"], "active_job")
            with AccountDB(db_path) as db:
                self.assertEqual(len(db.list_private_jobs()), 1)

    def test_legacy_database_is_migrated_before_guild_pool_query(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "accounts.db"
            with sqlite3.connect(db_path) as conn:
                conn.executescript(
                    """
                    CREATE TABLE accounts (
                        mid TEXT PRIMARY KEY,
                        guest_secret TEXT NOT NULL DEFAULT '',
                        refresh_token TEXT NOT NULL DEFAULT '',
                        game_access_token TEXT NOT NULL DEFAULT '',
                        oven_access_token TEXT NOT NULL DEFAULT '',
                        resource_key TEXT NOT NULL DEFAULT '',
                        endpoint TEXT NOT NULL DEFAULT '',
                        email TEXT NOT NULL DEFAULT '',
                        device_json TEXT NOT NULL DEFAULT '{}',
                        inviter_mid TEXT NOT NULL DEFAULT '',
                        next_stage INTEGER NOT NULL DEFAULT 1,
                        used INTEGER NOT NULL DEFAULT 0,
                        ready INTEGER NOT NULL DEFAULT 0,
                        note TEXT NOT NULL DEFAULT '',
                        created_at REAL NOT NULL,
                        updated_at REAL NOT NULL
                    );
                    INSERT INTO accounts (
                        mid, guest_secret, next_stage, used, ready, note,
                        created_at, updated_at
                    ) VALUES (
                        'LEGACY', 'secret', 1, 1, 1, 'keep-me', 100, 200
                    );
                    """
                )

            args = argparse.Namespace(
                guild_action="public",
                gname="ahhhha",
                gmname="absdbld",
                count=1,
                totalcount=1,
                db=str(db_path),
            )
            output = io.StringIO()
            with redirect_stdout(output):
                code = cli.cmd_guild(args)

            self.assertEqual(code, 0)
            summary = json.loads(output.getvalue())
            self.assertEqual(summary["stopped_reason"], "all_accounts_cooling")

            with sqlite3.connect(db_path) as conn:
                columns = {
                    row[1] for row in conn.execute("PRAGMA table_info(accounts)")
                }
                self.assertTrue(
                    {
                        "invalid",
                        "diamond_balance",
                        "guild",
                        "daily",
                        "guild_last_id",
                        "guild_joined_at",
                        "guild_left_at",
                        "guild_free_research_total",
                        "guild_paid_research_total",
                        "guild_effective_research_total",
                        "guild_super_success_total",
                        "guild_diamond_spent_total",
                    }.issubset(columns)
                )
                self.assertIsNotNone(
                    conn.execute(
                        "SELECT 1 FROM sqlite_master "
                        "WHERE type='table' AND name='guild_targets'"
                    ).fetchone()
                )
                self.assertIsNotNone(
                    conn.execute(
                        "SELECT 1 FROM sqlite_master "
                        "WHERE type='table' AND name='guild_runs'"
                    ).fetchone()
                )
                legacy = conn.execute(
                    "SELECT note, invalid, diamond_balance, guild, daily, "
                    "guild_last_id, guild_joined_at, guild_left_at, "
                    "guild_paid_research_total, guild_super_success_total "
                    "FROM accounts WHERE mid='LEGACY'"
                ).fetchone()
                self.assertEqual(
                    legacy,
                    ("keep-me", 0, 0, 0.0, 0.0, "", 0.0, 0.0, 0, 0),
                )

    def test_cached_target_reuses_id_and_marks_two_accounts_cooling(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "accounts.db"
            with AccountDB(db_path) as db:
                for mid in ("A", "B", "COOLING"):
                    db.upsert_state(
                        AccountState(
                            mid=mid,
                            guest_secret="secret",
                            game_access_token="token",
                            next_stage=31,
                        ),
                        used=True,
                        ready=True,
                        invalid=False,
                    )
                db.mark_guild_left("COOLING")
                db.upsert_guild_target(
                    gname="ahhhha",
                    gmname="absdbld",
                    guild_id="G-ID",
                    guild_level=1,
                    member_count=1,
                )

            args = argparse.Namespace(
                guild_action="public",
                gname="ahhhha",
                gmname="absdbld",
                count=4,
                totalcount=8,
                db=str(db_path),
            )
            output = io.StringIO()
            with (
                patch.object(cli, "GrpcClient", DummyClient),
                patch.object(cli, "GuildRunner", FakeRunner),
                patch.object(
                    cli, "_login_account", side_effect=lambda row: row.to_state()
                ),
                patch.object(
                    cli, "_confirm_guild", side_effect=AssertionError("must use cache")
                ),
                redirect_stdout(output),
            ):
                code = cli.cmd_guild(args)

            self.assertEqual(code, 0)
            summary = json.loads(output.getvalue())
            self.assertEqual(summary["count"], 4)
            self.assertEqual(summary["requested_totalcount"], 8)
            self.assertEqual(summary["totalcount"], 8)
            self.assertTrue(summary["totalcount_reached"])
            self.assertEqual(summary["account_count"], 2)
            self.assertEqual(summary["accounts_attempted"], 2)
            self.assertEqual(summary["guild"]["source"], "cache")
            self.assertEqual(summary["guild"]["level_before"], 1)
            self.assertEqual(summary["guild"]["level_after"], 2)
            self.assertEqual(summary["guild"]["level_change"], 1)
            self.assertEqual(summary["totals"]["free_research_count"], 6)
            self.assertEqual(summary["totals"]["donation_count"], 2)
            self.assertEqual(summary["totals"]["effective_research_count"], 8)
            self.assertNotIn("mailbox_checked_count", summary["totals"])
            self.assertNotIn("mailbox", summary["results"][0])
            self.assertEqual(summary["totals"]["diamond_spent"], 200)
            self.assertEqual(summary["totals"]["guild_experience_gained"], 8)
            self.assertEqual(summary["totals"]["research_point_gained"], 8)
            self.assertEqual(summary["results"][0]["donation_count"], 1)
            self.assertEqual(
                summary["results"][0]["effective_research_count"],
                4,
            )
            self.assertEqual(
                summary["results"][0]["stop_reason"],
                "account_count_reached",
            )
            self.assertEqual(
                summary["results"][0]["guild_progress"]["experience_gained"],
                4,
            )
            with AccountDB(db_path) as db:
                account_a = db.get("A")
                account_b = db.get("B")
                self.assertGreater(account_a.guild, 0)
                self.assertGreater(account_b.guild, 0)
                self.assertGreater(account_a.guild_joined_at, 0)
                self.assertGreater(account_a.guild_left_at, 0)
                self.assertEqual(account_a.guild_paid_research_total, 1)
                self.assertEqual(account_a.guild_effective_research_total, 4)
                self.assertEqual(account_b.guild_paid_research_total, 1)
                self.assertEqual(account_b.guild_effective_research_total, 4)
                self.assertEqual(len(db.list_guild_runs("A")), 1)
                self.assertEqual(len(db.list_guild_runs("B")), 1)
                self.assertEqual(db.guild_pool_status()["cooling"], 3)

    def test_public_without_totalcount_stops_after_fifty_joined_accounts(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "accounts.db"
            with AccountDB(db_path) as db:
                for index in range(51):
                    db.upsert_state(
                        AccountState(
                            mid=f"ACCOUNT-{index:02d}",
                            guest_secret="secret",
                            game_access_token="token",
                            next_stage=31,
                        ),
                        used=True,
                        ready=True,
                        invalid=False,
                    )
                db.upsert_guild_target(
                    gname="ahhhha",
                    gmname="absdbld",
                    guild_id="G-ID",
                    guild_level=1,
                    member_count=1,
                )

            args = argparse.Namespace(
                guild_action="public",
                gname="ahhhha",
                gmname="absdbld",
                count=1,
                totalcount=None,
                db=str(db_path),
            )
            output = io.StringIO()
            with (
                patch.object(cli, "GrpcClient", DummyClient),
                patch.object(cli, "GuildRunner", FakeRunner),
                patch.object(
                    cli, "_login_account", side_effect=lambda row: row.to_state()
                ),
                patch.object(
                    cli, "_confirm_guild", side_effect=AssertionError("must use cache")
                ),
                redirect_stdout(output),
            ):
                code = cli.cmd_guild(args)

            self.assertEqual(code, 0)
            summary = json.loads(output.getvalue())
            self.assertIsNone(summary["requested_totalcount"])
            self.assertFalse(summary["totalcount_reached"])
            self.assertEqual(summary["account_limit"], 50)
            self.assertEqual(summary["joined_account_count"], 50)
            self.assertTrue(summary["account_limit_reached"])
            self.assertEqual(summary["accounts_attempted"], 50)
            self.assertEqual(summary["account_count"], 50)
            self.assertEqual(summary["totalcount"], 50)
            self.assertEqual(summary["stopped_reason"], "account_limit_reached")

    def test_public_without_totalcount_stops_when_guild_rejects_join(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "accounts.db"
            with AccountDB(db_path) as db:
                for mid in ("A", "B"):
                    db.upsert_state(
                        AccountState(
                            mid=mid,
                            guest_secret="secret",
                            game_access_token="token",
                            next_stage=31,
                        ),
                        used=True,
                        ready=True,
                        invalid=False,
                    )
                db.upsert_guild_target(
                    gname="ahhhha",
                    gmname="absdbld",
                    guild_id="G-ID",
                    guild_level=1,
                    member_count=1,
                )

            args = argparse.Namespace(
                guild_action="public",
                gname="ahhhha",
                gmname="absdbld",
                count=10,
                totalcount=None,
                db=str(db_path),
            )
            output = io.StringIO()
            with (
                patch.object(cli, "GrpcClient", DummyClient),
                patch.object(cli, "GuildRunner", FakeRecruitmentLimitRunner),
                patch.object(
                    cli, "_login_account", side_effect=lambda row: row.to_state()
                ),
                redirect_stdout(output),
            ):
                code = cli.cmd_guild(args)

            self.assertEqual(code, 0)
            summary = json.loads(output.getvalue())
            self.assertTrue(summary["ok"])
            self.assertEqual(summary["accounts_attempted"], 1)
            self.assertEqual(summary["accounts_failed"], 0)
            self.assertEqual(summary["joined_account_count"], 0)
            self.assertEqual(
                summary["stopped_reason"],
                "daily_recruitment_limit_reached",
            )
            self.assertEqual(summary["daily_recruitment_limit"], 51)

    def test_public_flow_refreshes_stale_private_cache(self) -> None:
        FakeSearchGuild.calls = 0
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "accounts.db"
            with AccountDB(db_path) as db:
                db.upsert_state(
                    AccountState(
                        mid="A",
                        guest_secret="secret",
                        game_access_token="token",
                        next_stage=31,
                    ),
                    used=True,
                    ready=True,
                    invalid=False,
                )
                db.upsert_guild_target(
                    gname="ahhhha",
                    gmname="absdbld",
                    guild_id="G-ID",
                    guild_level=1,
                    member_count=1,
                    master_user_id="OWNER",
                    original_master_mid="OWNER",
                    details={"search_summary": {"join_method": 1}},
                )

            args = argparse.Namespace(
                guild_action="public",
                gname="ahhhha",
                gmname="absdbld",
                count=1,
                totalcount=4,
                db=str(db_path),
            )
            output = io.StringIO()
            with (
                patch.object(cli, "GrpcClient", DummyClient),
                patch.object(cli, "GuildRunner", FakeRunner),
                patch.object(cli, "Guild", FakeSearchGuild),
                patch.object(
                    cli, "_login_account", side_effect=lambda row: row.to_state()
                ),
                patch.object(
                    cli,
                    "_confirm_guild",
                    side_effect=AssertionError("known guild must not reconfirm"),
                ),
                redirect_stdout(output),
            ):
                code = cli.cmd_guild(args)

            self.assertEqual(code, 0)
            summary = json.loads(output.getvalue())
            self.assertEqual(summary["guild"]["source"], "refresh")
            self.assertEqual(FakeSearchGuild.calls, 1)
            with AccountDB(db_path) as db:
                target = db.get_guild_target("ahhhha", "absdbld")
                self.assertEqual(
                    target.details["search_summary"]["join_method"],
                    0,
                )
                self.assertEqual(target.original_master_mid, "OWNER")

    def test_uncached_target_searches_and_confirms_once(self) -> None:
        FakeSearchGuild.calls = 0
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "accounts.db"
            with AccountDB(db_path) as db:
                db.upsert_state(
                    AccountState(
                        mid="A",
                        guest_secret="secret",
                        game_access_token="token",
                        next_stage=31,
                    ),
                    used=True,
                    ready=True,
                    invalid=False,
                )

            args = argparse.Namespace(
                guild_action="public",
                gname="ahhhha",
                gmname="absdbld",
                count=1,
                totalcount=4,
                db=str(db_path),
            )
            output = io.StringIO()
            with (
                patch.object(cli, "GrpcClient", DummyClient),
                patch.object(cli, "GuildRunner", FakeRunner),
                patch.object(cli, "Guild", FakeSearchGuild),
                patch.object(
                    cli, "_login_account", side_effect=lambda row: row.to_state()
                ),
                patch.object(cli, "_confirm_guild", return_value=True) as confirm,
                redirect_stdout(output),
            ):
                code = cli.cmd_guild(args)

            self.assertEqual(code, 0)
            self.assertEqual(FakeSearchGuild.calls, 1)
            self.assertEqual(confirm.call_count, 1)
            confirmation = confirm.call_args.args[0]
            self.assertEqual(confirmation["name"], "ahhhha")
            self.assertEqual(confirmation["master_name"], "absdbld")
            self.assertEqual(confirmation["guild_level"], 1)
            with AccountDB(db_path) as db:
                target = db.get_guild_target("ahhhha", "absdbld")
                self.assertIsNotNone(target)
                self.assertEqual(target.guild_id, "G-ID")


if __name__ == "__main__":
    unittest.main()
