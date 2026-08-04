"""One-account daily login and mailbox workflow."""
from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from typing import Callable, Optional

from .currency import DIAMOND_CURRENCY_DATA_ID, parse_signup_currency_balance
from .grpc_client import GrpcClient
from .headers import Session, build_metadata
from .mailbox import (
    MAIL_ADVERTISEMENT_DAILY_LIMIT,
    MAIL_ADVERTISEMENT_DATA_ID,
    MAIL_ADVERTISEMENT_DIAMOND_AMOUNT,
    MailReward,
    Mailbox,
    ReceiveAllMailRewardsResult,
    ReceiveMailAdvertisementRewardResult,
    parse_receive_mail_advertisement_reward_response,
    parse_signup_mail_advertisement_view_count,
)
from .stage_runner import SIGNUP_PATH

log = logging.getLogger(__name__)


def _numeric_change(before: Optional[int], after: Optional[int]) -> Optional[int]:
    if before is None or after is None:
        return None
    return int(after) - int(before)


@dataclass
class MailAdvertisementProgress:
    checked: bool = False
    advertisement_data_id: int = MAIL_ADVERTISEMENT_DATA_ID
    daily_limit: int = MAIL_ADVERTISEMENT_DAILY_LIMIT
    configured_diamond_amount: int = MAIL_ADVERTISEMENT_DIAMOND_AMOUNT
    daily_view_count_before: Optional[int] = None
    daily_view_count_after: Optional[int] = None
    claimable_count: int = 0
    claim_requested_count: int = 0
    claimed_count: int = 0
    reward_count: int = 0
    currency_rewards: list[MailReward] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(
            self.checked
            and self.claimable_count == self.claim_requested_count == self.claimed_count
        )

    @property
    def remaining_claimable_count(self) -> int:
        return max(0, self.claimable_count - self.claimed_count)

    @property
    def diamond_reward_amount(self) -> int:
        return sum(
            reward.amount
            for reward in self.currency_rewards
            if reward.item_data_id == DIAMOND_CURRENCY_DATA_ID
        )

    def observe_daily_count(self, count: Optional[int]) -> None:
        self.checked = True
        self.daily_view_count_before = max(0, int(count)) if count is not None else None
        self.daily_view_count_after = self.daily_view_count_before
        effective_count = self.daily_view_count_before or 0
        self.claimable_count = max(0, self.daily_limit - effective_count)

    def begin_claim(self) -> None:
        self.claim_requested_count += 1

    def observe_claim(self, received: ReceiveMailAdvertisementRewardResult) -> None:
        self.reward_count += received.reward_count
        if received.reward_count <= 0:
            return

        self.claimed_count += 1
        effective_before = self.daily_view_count_before or 0
        self.daily_view_count_after = effective_before + self.claimed_count
        totals = {
            reward.item_data_id: reward.amount for reward in self.currency_rewards
        }
        for reward in received.currency_rewards:
            totals[reward.item_data_id] = (
                totals.get(reward.item_data_id, 0) + reward.amount
            )
        self.currency_rewards = [
            MailReward(item_data_id=item_data_id, amount=amount)
            for item_data_id, amount in sorted(totals.items())
        ]

    def to_dict(self) -> dict:
        return {
            "checked": self.checked,
            "advertisement_data_id": self.advertisement_data_id,
            "daily_limit": self.daily_limit,
            "configured_diamond_amount": self.configured_diamond_amount,
            "daily_view_count_before": self.daily_view_count_before,
            "daily_view_count_after": self.daily_view_count_after,
            "claimable_count": self.claimable_count,
            "claim_requested_count": self.claim_requested_count,
            "claimed_count": self.claimed_count,
            "remaining_claimable_count": self.remaining_claimable_count,
            "reward_count": self.reward_count,
            "currency_rewards": [asdict(reward) for reward in self.currency_rewards],
            "diamond_reward_amount": self.diamond_reward_amount,
        }


@dataclass
class MailboxProgress:
    checked: bool = False
    mail_count: int = 0
    already_rewarded_count: int = 0
    claimable_count: int = 0
    claim_requested_count: int = 0
    claimed_count: int = 0
    reward_count: int = 0
    updated_mail_count: int = 0
    claimed_rewards: list[MailReward] = field(default_factory=list)
    diamond_balance_before: Optional[int] = None
    diamond_balance_after: Optional[int] = None
    advertisement: MailAdvertisementProgress = field(
        default_factory=MailAdvertisementProgress
    )

    @property
    def ok(self) -> bool:
        return bool(
            self.checked
            and self.claimable_count == self.claim_requested_count == self.claimed_count
            and self.advertisement.ok
        )

    @property
    def remaining_claimable_count(self) -> int:
        return max(0, self.claimable_count - self.claimed_count)

    def observe(self, received: ReceiveAllMailRewardsResult) -> None:
        self.checked = True
        self.mail_count = len(received.snapshot.mails)
        self.already_rewarded_count = sum(
            1 for mail in received.snapshot.mails if mail.is_rewarded
        )
        self.claimable_count = len(received.snapshot.claimable_mails)
        self.claim_requested_count = len(received.requested_mail_ids)
        self.claimed_count = len(set(received.claimed_mail_ids))
        self.reward_count = received.reward_count
        self.updated_mail_count = len(received.updated_mails)

        claimed_ids = set(received.claimed_mail_ids)
        totals: dict[int, int] = {}
        for mail in received.snapshot.mails:
            if mail.mail_id not in claimed_ids:
                continue
            for reward in mail.rewards:
                totals[reward.item_data_id] = (
                    totals.get(reward.item_data_id, 0) + reward.amount
                )
        self.claimed_rewards = [
            MailReward(item_data_id=item_data_id, amount=amount)
            for item_data_id, amount in sorted(totals.items())
        ]

    def to_dict(self) -> dict:
        return {
            "checked": self.checked,
            "mail_count": self.mail_count,
            "already_rewarded_count": self.already_rewarded_count,
            "claimable_count": self.claimable_count,
            "claim_requested_count": self.claim_requested_count,
            "claimed_count": self.claimed_count,
            "remaining_claimable_count": self.remaining_claimable_count,
            "reward_count": self.reward_count,
            "updated_mail_count": self.updated_mail_count,
            "claimed_rewards": [asdict(reward) for reward in self.claimed_rewards],
            "diamond_balance_before": self.diamond_balance_before,
            "diamond_balance_after": self.diamond_balance_after,
            "diamond_gained": _numeric_change(
                self.diamond_balance_before,
                self.diamond_balance_after,
            ),
            "advertisement": self.advertisement.to_dict(),
        }


@dataclass
class DailyWorkflowResult:
    login_completed: bool = False
    diamond_balance_final: Optional[int] = None
    error: str = ""
    mailbox: MailboxProgress = field(default_factory=MailboxProgress)

    @property
    def ok(self) -> bool:
        return bool(
            self.login_completed
            and self.mailbox.ok
            and self.diamond_balance_final is not None
            and not self.error
        )

    def to_dict(self) -> dict:
        payload = asdict(self)
        payload["mailbox"] = self.mailbox.to_dict()
        return {"ok": self.ok, **payload}


class DailyRunner:
    """Execute login sync, attachment claim-all, and mailbox advertisement."""

    def __init__(
        self,
        client: GrpcClient,
        session: Session,
        *,
        on_balance: Optional[Callable[[int], None]] = None,
    ) -> None:
        self.client = client
        self.session = session
        self.on_balance = on_balance

    def run(self) -> DailyWorkflowResult:
        result = DailyWorkflowResult()
        mailbox = Mailbox(self.client, self.session)
        balance: Optional[int] = None

        try:
            balance, signup_body = self._sync_account_state()
            result.login_completed = True
            result.mailbox.diamond_balance_before = balance
            advertisement_count = parse_signup_mail_advertisement_view_count(
                signup_body,
                result.mailbox.advertisement.advertisement_data_id,
            )
            if advertisement_count is None:
                log.warning(
                    "mail advertisement daily counter missing; "
                    "treating it as zero for this claim attempt"
                )
            result.mailbox.advertisement.observe_daily_count(advertisement_count)

            mail_result = mailbox.receive_all_rewards()
            result.mailbox.observe(mail_result)
            for _ in range(result.mailbox.advertisement.claimable_count):
                result.mailbox.advertisement.begin_claim()
                response = mailbox.receive_mail_advertisement_reward(
                    result.mailbox.advertisement.advertisement_data_id
                )
                result.mailbox.advertisement.observe_claim(
                    parse_receive_mail_advertisement_reward_response(response.message)
                )

            if (
                result.mailbox.claim_requested_count
                or result.mailbox.advertisement.claim_requested_count
            ):
                balance, _ = self._sync_account_state()
            result.mailbox.diamond_balance_after = balance
            result.diamond_balance_final = balance
            log.info(
                "daily mailbox total=%s claimable=%s claimed=%s "
                "advertisement_claimed=%s diamond_change=%s",
                result.mailbox.mail_count,
                result.mailbox.claimable_count,
                result.mailbox.claimed_count,
                result.mailbox.advertisement.claimed_count,
                _numeric_change(
                    result.mailbox.diamond_balance_before,
                    result.mailbox.diamond_balance_after,
                ),
            )
            if not result.mailbox.ok:
                raise RuntimeError(
                    "mail claim-all incomplete: "
                    f"requested={result.mailbox.claim_requested_count}, "
                    f"claimed={result.mailbox.claimed_count}; "
                    "advertisement_requested="
                    f"{result.mailbox.advertisement.claim_requested_count}, "
                    "advertisement_claimed="
                    f"{result.mailbox.advertisement.claimed_count}"
                )
        except Exception as error:
            result.error = f"{type(error).__name__}: {error}"
            log.error("daily workflow failed: %s", result.error)
            if balance is not None:
                result.mailbox.diamond_balance_after = balance
                result.diamond_balance_final = balance

        return result

    def _sync_account_state(self) -> tuple[int, bytes]:
        response = self.client.unary(
            SIGNUP_PATH,
            b"",
            metadata=build_metadata(self.session),
        )
        self.session.adopt_resource_key(response.headers)
        balance = parse_signup_currency_balance(response.message)
        if balance is None:
            raise RuntimeError("diamond currency missing from SignUpResponse")
        self._notify_balance(balance)
        return balance, response.message

    def _notify_balance(self, balance: int) -> None:
        if self.on_balance is not None:
            self.on_balance(max(0, int(balance)))
