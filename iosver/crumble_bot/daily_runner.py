"""One-account daily login, reward, mailbox, and optional dungeon workflow."""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from typing import Callable, Optional

from .currency import DIAMOND_CURRENCY_DATA_ID, parse_signup_currency_balance
from .crumble_dungeon import CrumbleDungeonRunner, parse_signup_cookie_ids
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
from .stage_rewards import (
    STAGE_BONUS_ADVERTISEMENT_DATA_ID,
    STAGE_BONUS_DAILY_ADVERTISEMENT_LIMIT,
    STAGE_BONUS_DAILY_FREE_LIMIT,
    StageAutoProductionRewardResult,
    StageBonusAutoProductionRewardResult,
    StageReward,
    StageRewards,
    parse_receive_stage_auto_production_rewards_response,
    parse_receive_stage_bonus_auto_production_rewards_response,
    parse_signup_stage_reward_counters,
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


def _merge_stage_rewards(
    current: list[StageReward],
    incoming: tuple[StageReward, ...],
) -> list[StageReward]:
    totals = {
        (reward.reward_type, reward.item_data_id): reward.amount for reward in current
    }
    for reward in incoming:
        key = (reward.reward_type, reward.item_data_id)
        totals[key] = totals.get(key, 0) + reward.amount
    return [
        StageReward(
            reward_type=reward_type,
            item_data_id=item_data_id,
            amount=amount,
        )
        for (reward_type, item_data_id), amount in sorted(totals.items())
    ]


@dataclass
class OfflineStageRewardProgress:
    checked: bool = False
    refresh_requested_count: int = 0
    stacked_duration_millis: int = 0
    stacked_rewards: list[StageReward] = field(default_factory=list)
    claim_requested_count: int = 0
    claimed_count: int = 0
    reward_count: int = 0
    claimed_rewards: list[StageReward] = field(default_factory=list)

    @property
    def claimable(self) -> bool:
        return any(reward.amount > 0 for reward in self.stacked_rewards)

    @property
    def ok(self) -> bool:
        return bool(
            self.checked
            and self.refresh_requested_count == 1
            and self.claim_requested_count == self.claimed_count
        )

    def begin_refresh(self) -> None:
        self.refresh_requested_count += 1

    def observe_stack(self, received: StageAutoProductionRewardResult) -> None:
        self.checked = True
        self.stacked_duration_millis = received.stacked_duration_millis
        self.stacked_rewards = list(received.stacked_rewards)

    def begin_claim(self) -> None:
        self.claim_requested_count += 1

    def observe_claim(self, received: StageAutoProductionRewardResult) -> None:
        self.reward_count += received.reward_count
        self.claimed_rewards = _merge_stage_rewards(
            self.claimed_rewards,
            received.rewards,
        )
        if received.reward_count > 0:
            self.claimed_count += 1

    def to_dict(self) -> dict:
        return {
            "checked": self.checked,
            "refresh_requested_count": self.refresh_requested_count,
            "stacked_duration_millis": self.stacked_duration_millis,
            "stacked_reward_count": len(self.stacked_rewards),
            "stacked_rewards": [asdict(reward) for reward in self.stacked_rewards],
            "claimable": self.claimable,
            "claim_requested_count": self.claim_requested_count,
            "claimed_count": self.claimed_count,
            "reward_count": self.reward_count,
            "claimed_rewards": [asdict(reward) for reward in self.claimed_rewards],
        }


@dataclass
class StageBonusRewardProgress:
    checked: bool = False
    advertisement_data_id: int = STAGE_BONUS_ADVERTISEMENT_DATA_ID
    daily_free_limit: int = STAGE_BONUS_DAILY_FREE_LIMIT
    daily_advertisement_limit: int = STAGE_BONUS_DAILY_ADVERTISEMENT_LIMIT
    daily_free_count_before: Optional[int] = None
    daily_free_count_after: Optional[int] = None
    daily_advertisement_count_before: Optional[int] = None
    daily_advertisement_count_after: Optional[int] = None
    free_claimable_count: int = 0
    free_claim_requested_count: int = 0
    free_claimed_count: int = 0
    advertisement_claimable_count: int = 0
    advertisement_claim_requested_count: int = 0
    advertisement_claimed_count: int = 0
    reward_count: int = 0
    rewards: list[StageReward] = field(default_factory=list)
    bonus_types: list[int] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(
            self.checked
            and self.free_claimable_count
            == self.free_claim_requested_count
            == self.free_claimed_count
            and self.advertisement_claimable_count
            == self.advertisement_claim_requested_count
            == self.advertisement_claimed_count
        )

    @property
    def free_remaining_claimable_count(self) -> int:
        return max(0, self.free_claimable_count - self.free_claimed_count)

    @property
    def advertisement_remaining_claimable_count(self) -> int:
        return max(
            0,
            self.advertisement_claimable_count - self.advertisement_claimed_count,
        )

    def observe_daily_counts(
        self,
        free_count: Optional[int],
        advertisement_count: Optional[int],
    ) -> None:
        self.checked = True
        self.daily_free_count_before = (
            max(0, int(free_count)) if free_count is not None else None
        )
        self.daily_free_count_after = self.daily_free_count_before
        self.daily_advertisement_count_before = (
            max(0, int(advertisement_count))
            if advertisement_count is not None
            else None
        )
        self.daily_advertisement_count_after = self.daily_advertisement_count_before
        self.free_claimable_count = max(
            0,
            self.daily_free_limit - (self.daily_free_count_before or 0),
        )
        self.advertisement_claimable_count = max(
            0,
            self.daily_advertisement_limit
            - (self.daily_advertisement_count_before or 0),
        )

    def begin_free_claim(self) -> None:
        self.free_claim_requested_count += 1

    def observe_free_claim(
        self,
        received: StageBonusAutoProductionRewardResult,
    ) -> None:
        self._observe_claim(received)
        if received.reward_count <= 0:
            return
        self.free_claimed_count += 1
        self.daily_free_count_after = (
            received.daily_free_count
            if received.daily_free_count is not None
            else (self.daily_free_count_before or 0) + self.free_claimed_count
        )

    def begin_advertisement_claim(self) -> None:
        self.advertisement_claim_requested_count += 1

    def observe_advertisement_claim(
        self,
        received: StageBonusAutoProductionRewardResult,
    ) -> None:
        self._observe_claim(received)
        if received.reward_count <= 0:
            return
        self.advertisement_claimed_count += 1
        self.daily_advertisement_count_after = (
            received.daily_advertisement_count
            if received.daily_advertisement_count is not None
            else (self.daily_advertisement_count_before or 0)
            + self.advertisement_claimed_count
        )

    def _observe_claim(
        self,
        received: StageBonusAutoProductionRewardResult,
    ) -> None:
        self.reward_count += received.reward_count
        self.rewards = _merge_stage_rewards(self.rewards, received.rewards)
        self.bonus_types.append(received.bonus_type)
        if received.daily_free_count is not None:
            self.daily_free_count_after = received.daily_free_count
        if received.daily_advertisement_count is not None:
            self.daily_advertisement_count_after = received.daily_advertisement_count

    def to_dict(self) -> dict:
        return {
            "checked": self.checked,
            "advertisement_data_id": self.advertisement_data_id,
            "daily_free_limit": self.daily_free_limit,
            "daily_advertisement_limit": self.daily_advertisement_limit,
            "daily_free_count_before": self.daily_free_count_before,
            "daily_free_count_after": self.daily_free_count_after,
            "daily_advertisement_count_before": (self.daily_advertisement_count_before),
            "daily_advertisement_count_after": (self.daily_advertisement_count_after),
            "free_claimable_count": self.free_claimable_count,
            "free_claim_requested_count": self.free_claim_requested_count,
            "free_claimed_count": self.free_claimed_count,
            "free_remaining_claimable_count": (self.free_remaining_claimable_count),
            "advertisement_claimable_count": (self.advertisement_claimable_count),
            "advertisement_claim_requested_count": (
                self.advertisement_claim_requested_count
            ),
            "advertisement_claimed_count": self.advertisement_claimed_count,
            "advertisement_remaining_claimable_count": (
                self.advertisement_remaining_claimable_count
            ),
            "reward_count": self.reward_count,
            "rewards": [asdict(reward) for reward in self.rewards],
            "bonus_types": list(self.bonus_types),
        }


@dataclass
class DailyStageRewardProgress:
    offline: OfflineStageRewardProgress = field(
        default_factory=OfflineStageRewardProgress
    )
    bonus: StageBonusRewardProgress = field(default_factory=StageBonusRewardProgress)

    @property
    def ok(self) -> bool:
        return bool(self.offline.ok and self.bonus.ok)

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "offline": self.offline.to_dict(),
            "bonus": self.bonus.to_dict(),
        }


@dataclass
class DailyWorkflowResult:
    login_completed: bool = False
    diamond_balance_final: Optional[int] = None
    error: str = ""
    skipped: bool = False
    cookie_ids: tuple[int, ...] = field(default_factory=tuple)
    # Empty means the caller did not request the optional dungeon step.  The
    # top-level account-pool daily enables it; resident-guild daily keeps its
    # existing explicit dungeon call for compatibility with older runners.
    crumble_dungeon: dict = field(default_factory=dict)
    stage_rewards: DailyStageRewardProgress = field(
        default_factory=DailyStageRewardProgress
    )
    mailbox: MailboxProgress = field(default_factory=MailboxProgress)

    @property
    def ok(self) -> bool:
        if self.skipped:
            return True
        return bool(
            self.login_completed
            and self.stage_rewards.ok
            and self.mailbox.ok
            and (
                not self.crumble_dungeon
                or bool(self.crumble_dungeon.get("ok", True))
            )
            and self.diamond_balance_final is not None
            and not self.error
        )

    def to_dict(self) -> dict:
        payload = asdict(self)
        payload["cookie_ids"] = list(self.cookie_ids)
        payload["stage_rewards"] = self.stage_rewards.to_dict()
        payload["mailbox"] = self.mailbox.to_dict()
        return {"ok": self.ok, **payload}


class DailyRunner:
    """Execute login, stage rewards, mailbox claim-all, and advertisements."""

    def __init__(
        self,
        client: GrpcClient,
        session: Session,
        *,
        on_balance: Optional[Callable[[int], None]] = None,
        include_crumble_dungeon: bool = False,
    ) -> None:
        self.client = client
        self.session = session
        self.on_balance = on_balance
        self.include_crumble_dungeon = bool(include_crumble_dungeon)

    def run(self) -> DailyWorkflowResult:
        result = DailyWorkflowResult()
        mailbox = Mailbox(self.client, self.session)
        stage_rewards = StageRewards(self.client, self.session)
        balance: Optional[int] = None

        try:
            balance, signup_body = self._sync_account_state()
            result.login_completed = True
            result.cookie_ids = parse_signup_cookie_ids(signup_body)
            result.mailbox.diamond_balance_before = balance
            stage_counters = parse_signup_stage_reward_counters(
                signup_body,
                result.stage_rewards.bonus.advertisement_data_id,
            )
            if (
                stage_counters.daily_free_count is None
                or stage_counters.daily_advertisement_count is None
            ):
                log.warning(
                    "stage bonus daily counters missing; "
                    "treating missing values as zero for this claim attempt"
                )
            result.stage_rewards.bonus.observe_daily_counts(
                stage_counters.daily_free_count,
                stage_counters.daily_advertisement_count,
            )
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

            result.stage_rewards.offline.begin_refresh()
            offline_stack = parse_receive_stage_auto_production_rewards_response(
                stage_rewards.refresh_offline_stack().message
            )
            result.stage_rewards.offline.observe_stack(offline_stack)
            if result.stage_rewards.offline.claimable:
                result.stage_rewards.offline.begin_claim()
                offline_claim = parse_receive_stage_auto_production_rewards_response(
                    stage_rewards.receive_offline_rewards().message
                )
                result.stage_rewards.offline.observe_claim(offline_claim)

            for _ in range(result.stage_rewards.bonus.free_claimable_count):
                result.stage_rewards.bonus.begin_free_claim()
                free_claim = parse_receive_stage_bonus_auto_production_rewards_response(
                    stage_rewards.receive_bonus_reward().message,
                    result.stage_rewards.bonus.advertisement_data_id,
                )
                result.stage_rewards.bonus.observe_free_claim(free_claim)

            for _ in range(result.stage_rewards.bonus.advertisement_claimable_count):
                result.stage_rewards.bonus.begin_advertisement_claim()
                advertisement_claim = (
                    parse_receive_stage_bonus_auto_production_rewards_response(
                        stage_rewards.receive_bonus_advertisement_reward(
                            result.stage_rewards.bonus.advertisement_data_id
                        ).message,
                        result.stage_rewards.bonus.advertisement_data_id,
                    )
                )
                result.stage_rewards.bonus.observe_advertisement_claim(
                    advertisement_claim
                )

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
                result.stage_rewards.offline.claim_requested_count
                or result.stage_rewards.bonus.free_claim_requested_count
                or result.stage_rewards.bonus.advertisement_claim_requested_count
                or result.mailbox.claim_requested_count
                or result.mailbox.advertisement.claim_requested_count
            ):
                balance, _ = self._sync_account_state()
            result.mailbox.diamond_balance_after = balance
            result.diamond_balance_final = balance

            if self.include_crumble_dungeon:
                result.crumble_dungeon = CrumbleDungeonRunner(
                    self.client,
                    self.session,
                    cookie_ids=result.cookie_ids,
                ).run()

            log.info(
                "daily offline_claimed=%s bonus_free=%s bonus_advertisement=%s "
                "mailbox_total=%s mailbox_claimable=%s mailbox_claimed=%s "
                "mail_advertisement_claimed=%s diamond_change=%s",
                result.stage_rewards.offline.claimed_count,
                result.stage_rewards.bonus.free_claimed_count,
                result.stage_rewards.bonus.advertisement_claimed_count,
                result.mailbox.mail_count,
                result.mailbox.claimable_count,
                result.mailbox.claimed_count,
                result.mailbox.advertisement.claimed_count,
                _numeric_change(
                    result.mailbox.diamond_balance_before,
                    result.mailbox.diamond_balance_after,
                ),
            )
            if not result.stage_rewards.ok:
                raise RuntimeError(
                    "stage reward claim incomplete: "
                    "offline_requested="
                    f"{result.stage_rewards.offline.claim_requested_count}, "
                    "offline_claimed="
                    f"{result.stage_rewards.offline.claimed_count}; "
                    "free_requested="
                    f"{result.stage_rewards.bonus.free_claim_requested_count}, "
                    "free_claimed="
                    f"{result.stage_rewards.bonus.free_claimed_count}; "
                    "advertisement_requested="
                    f"{result.stage_rewards.bonus.advertisement_claim_requested_count}, "
                    "advertisement_claimed="
                    f"{result.stage_rewards.bonus.advertisement_claimed_count}"
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
            if result.crumble_dungeon and not result.crumble_dungeon.get("ok"):
                raise RuntimeError(
                    "crumble dungeon failed: "
                    f"{result.crumble_dungeon.get('error') or 'unknown error'}"
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
