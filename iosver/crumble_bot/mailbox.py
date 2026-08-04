"""Mailbox RPCs and protobuf response parsing."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Sequence

from . import pbutil as pb
from .grpc_client import GrpcClient, GrpcResponse
from .headers import Session, build_metadata
from .messages import (
    receive_mail_advertisement_reward_request,
    receive_mail_rewards_request,
    refresh_mail_box_request,
)

log = logging.getLogger(__name__)

REFRESH_MAIL_BOX_PATH = "/cc.public.game.MailService/RefreshMailBox"
RECEIVE_MAIL_REWARDS_PATH = "/cc.public.game.MailService/ReceiveMailRewards"
RECEIVE_MAIL_ADVERTISEMENT_REWARD_PATH = (
    "/cc.public.game.CrumbleService/ReceiveMailAdvertisementReward"
)

# 10101 Advertisements/AdvertisementRewards table values for AdvertisementType.MailReward.
MAIL_ADVERTISEMENT_DATA_ID = 1673636113
MAIL_ADVERTISEMENT_DAILY_LIMIT = 1
MAIL_ADVERTISEMENT_DIAMOND_AMOUNT = 1000


@dataclass(frozen=True)
class MailReward:
    item_data_id: int
    amount: int


@dataclass(frozen=True)
class MailEntry:
    mail_id: str
    rewards: tuple[MailReward, ...]
    is_rewarded: bool
    delivered_at_millis: int = 0
    expires_at_millis: int = 0

    @property
    def is_claimable(self) -> bool:
        return bool(self.mail_id and self.rewards and not self.is_rewarded)


@dataclass(frozen=True)
class MailBoxSnapshot:
    mails: tuple[MailEntry, ...]

    @property
    def claimable_mails(self) -> tuple[MailEntry, ...]:
        return tuple(mail for mail in self.mails if mail.is_claimable)


@dataclass(frozen=True)
class ReceiveMailRewardsResult:
    reward_count: int
    updated_mails: tuple[MailEntry, ...]


@dataclass(frozen=True)
class ReceiveAllMailRewardsResult:
    snapshot: MailBoxSnapshot
    requested_mail_ids: tuple[str, ...]
    claimed_mail_ids: tuple[str, ...]
    reward_count: int
    updated_mails: tuple[MailEntry, ...]


@dataclass(frozen=True)
class ReceiveMailAdvertisementRewardResult:
    reward_count: int
    currency_rewards: tuple[MailReward, ...]


def parse_refresh_mail_box_response(body: bytes) -> MailBoxSnapshot:
    """Parse ``RefreshMailBoxResponse.mail_box.mails``."""
    fields = pb.decode_fields(body)
    mail_box = _message_value(fields, 1)
    if mail_box is None:
        return MailBoxSnapshot(mails=())
    return MailBoxSnapshot(mails=_parse_repeated_mails(mail_box, field_number=1))


def parse_receive_mail_rewards_response(body: bytes) -> ReceiveMailRewardsResult:
    """Parse reward count and updated mails from a batch receive response."""
    fields = pb.decode_fields(body)
    reward_count = sum(
        1
        for field_number, wire_type, _ in fields
        if field_number == 2 and wire_type == 2
    )
    updated_mails = tuple(
        _parse_mail(bytes(value))
        for field_number, wire_type, value in fields
        if field_number == 3 and wire_type == 2
    )
    return ReceiveMailRewardsResult(
        reward_count=reward_count,
        updated_mails=updated_mails,
    )


def parse_receive_mail_advertisement_reward_response(
    body: bytes,
) -> ReceiveMailAdvertisementRewardResult:
    """Parse rewards returned by ``ReceiveMailAdvertisementReward``."""
    reward_messages = tuple(
        bytes(value)
        for field_number, wire_type, value in pb.decode_fields(body)
        if field_number == 2 and wire_type == 2
    )
    totals: dict[int, int] = {}
    for reward in reward_messages:
        for field_number, wire_type, value in pb.decode_fields(reward):
            if field_number != 1 or wire_type != 2:
                continue
            reward_element = pb.decode_fields(bytes(value))
            currency_reward = _message_value(reward_element, 1)
            if currency_reward is None:
                continue
            currency_fields = pb.decode_fields(currency_reward)
            item_data_id = _int_value(currency_fields, 1)
            amount = _int_value(currency_fields, 2)
            if item_data_id:
                totals[item_data_id] = totals.get(item_data_id, 0) + amount

    return ReceiveMailAdvertisementRewardResult(
        reward_count=len(reward_messages),
        currency_rewards=tuple(
            MailReward(item_data_id=item_data_id, amount=amount)
            for item_data_id, amount in sorted(totals.items())
        ),
    )


def parse_signup_mail_advertisement_view_count(
    body: bytes,
    advertisement_data_id: int = MAIL_ADVERTISEMENT_DATA_ID,
) -> int | None:
    """Read today's ad view count from SignUpResponse.crumble.progress."""
    root_fields = pb.decode_fields(body)
    crumble = _message_value(root_fields, 3)
    if crumble is None:
        return None
    progress = _message_value(pb.decode_fields(crumble), 5)
    if progress is None:
        return None
    daily_counters = _message_value(pb.decode_fields(progress), 2)
    if daily_counters is None:
        return None

    for field_number, wire_type, value in pb.decode_fields(daily_counters):
        if field_number != 3 or wire_type != 2:
            continue
        entry_fields = pb.decode_fields(bytes(value))
        if _int_value(entry_fields, 1) == advertisement_data_id:
            return _int_value(entry_fields, 2)
    return 0


def _parse_repeated_mails(body: bytes, *, field_number: int) -> tuple[MailEntry, ...]:
    return tuple(
        _parse_mail(bytes(value))
        for current_field, wire_type, value in pb.decode_fields(body)
        if current_field == field_number and wire_type == 2
    )


def _parse_mail(body: bytes) -> MailEntry:
    fields = pb.decode_fields(body)
    rewards = tuple(
        _parse_mail_reward(bytes(value))
        for field_number, wire_type, value in fields
        if field_number == 3 and wire_type == 2
    )
    return MailEntry(
        mail_id=_string_value(fields, 1),
        rewards=rewards,
        is_rewarded=bool(_int_value(fields, 4)),
        delivered_at_millis=_time_value(fields, 5),
        expires_at_millis=_time_value(fields, 6),
    )


def _parse_mail_reward(body: bytes) -> MailReward:
    fields = pb.decode_fields(body)
    return MailReward(
        item_data_id=_int_value(fields, 1),
        amount=_int_value(fields, 2),
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


def _time_value(fields, target: int) -> int:
    time_body = _message_value(fields, target)
    if time_body is None:
        return 0
    return _int_value(pb.decode_fields(time_body), 1)


class Mailbox:
    """Mailbox RPC facade bound to one authenticated account session."""

    def __init__(self, client: GrpcClient, session: Session) -> None:
        self.client = client
        self.session = session

    def refresh_mail_box(self) -> GrpcResponse:
        """Fetch all currently visible server mails."""
        return self._unary(REFRESH_MAIL_BOX_PATH, refresh_mail_box_request())

    def receive_mail_rewards(self, mail_ids: Sequence[str]) -> GrpcResponse:
        """Receive attachments for the supplied mail ids in one request."""
        normalized = self._normalize_mail_ids(mail_ids)
        if not normalized:
            raise ValueError("mail_ids must not be empty")
        return self._unary(
            RECEIVE_MAIL_REWARDS_PATH,
            receive_mail_rewards_request(normalized),
        )

    def receive_mail_advertisement_reward(
        self,
        advertisement_data_id: int = MAIL_ADVERTISEMENT_DATA_ID,
        *,
        skip_count: int | None = None,
    ) -> GrpcResponse:
        """Receive one mailbox advertisement reward.

        ``advertisement_data_id`` and the optional ``skip_count`` are exposed
        exactly as request parameters.  The normal 10101 client flow omits
        ``skip_count``; callers must opt in explicitly if it is ever needed.
        """
        if isinstance(advertisement_data_id, bool) or not isinstance(
            advertisement_data_id, int
        ):
            raise ValueError("advertisement_data_id must be an integer")
        if advertisement_data_id <= 0:
            raise ValueError("advertisement_data_id must be positive")
        if skip_count is not None:
            if isinstance(skip_count, bool) or not isinstance(skip_count, int):
                raise ValueError("skip_count must be an integer or None")
            if skip_count < 0:
                raise ValueError("skip_count must not be negative")
        return self._unary(
            RECEIVE_MAIL_ADVERTISEMENT_REWARD_PATH,
            receive_mail_advertisement_reward_request(
                advertisement_data_id,
                skip_count=skip_count,
            ),
        )

    def receive_all_rewards(self) -> ReceiveAllMailRewardsResult:
        """Refresh the mailbox and receive every unclaimed attachment mail."""
        refresh_response = self.refresh_mail_box()
        snapshot = parse_refresh_mail_box_response(refresh_response.message)
        requested_mail_ids = tuple(mail.mail_id for mail in snapshot.claimable_mails)
        if not requested_mail_ids:
            return ReceiveAllMailRewardsResult(
                snapshot=snapshot,
                requested_mail_ids=(),
                claimed_mail_ids=(),
                reward_count=0,
                updated_mails=(),
            )

        response = self.receive_mail_rewards(requested_mail_ids)
        received = parse_receive_mail_rewards_response(response.message)
        requested = set(requested_mail_ids)
        claimed_mail_ids = tuple(
            mail.mail_id
            for mail in received.updated_mails
            if mail.mail_id in requested and mail.is_rewarded
        )
        return ReceiveAllMailRewardsResult(
            snapshot=snapshot,
            requested_mail_ids=requested_mail_ids,
            claimed_mail_ids=claimed_mail_ids,
            reward_count=received.reward_count,
            updated_mails=received.updated_mails,
        )

    @staticmethod
    def _normalize_mail_ids(mail_ids: Sequence[str]) -> tuple[str, ...]:
        if isinstance(mail_ids, (str, bytes)):
            raise ValueError("mail_ids must be a sequence of strings")
        normalized: list[str] = []
        seen: set[str] = set()
        for mail_id in mail_ids:
            if not isinstance(mail_id, str):
                raise ValueError("each mail_id must be a string")
            mail_id = mail_id.strip()
            if not mail_id:
                raise ValueError("mail_id must not be empty")
            if mail_id not in seen:
                seen.add(mail_id)
                normalized.append(mail_id)
        return tuple(normalized)

    def _unary(self, path: str, body: bytes) -> GrpcResponse:
        response = self.client.unary(
            path,
            body,
            metadata=build_metadata(self.session),
        )
        if self.session.adopt_resource_key(response.headers):
            log.debug("resource_key <- %s", self.session.resource_key)
        return response
