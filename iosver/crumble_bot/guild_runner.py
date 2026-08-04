"""One-account guild automation workflow."""
from __future__ import annotations

import logging
import re
import time
from dataclasses import asdict, dataclass, field
from typing import Callable, Optional

from .currency import (
    DIAMOND_CURRENCY_DATA_ID,
    parse_currency_payments,
)
from .grpc_client import GrpcClient, GrpcError
from .guild import (
    Guild,
    GuildActionResult,
    GuildDetail,
    parse_attend_guild_response,
    parse_free_guild_lab_research_response,
    parse_guild_detail_response,
    parse_join_guild_response,
    parse_paid_guild_lab_research_response,
)
from .headers import Session

log = logging.getLogger(__name__)


@dataclass
class GuildProgress:
    level_before: Optional[int] = None
    level_after: Optional[int] = None
    experience_before: Optional[int] = None
    experience_after: Optional[int] = None
    member_contribution_before: Optional[int] = None
    member_contribution_after: Optional[int] = None
    research_point_before: Optional[int] = None
    research_point_after: Optional[int] = None
    daily_free_research_count_before: Optional[int] = None
    daily_free_research_count_after: Optional[int] = None
    daily_donation_count_before: Optional[int] = None
    daily_donation_count_after: Optional[int] = None
    super_success_count: int = 0

    def observe_action(
        self, action: GuildActionResult, *, initial: bool = False
    ) -> None:
        member_state = action.member_state
        if member_state is not None:
            if initial:
                self.level_before = member_state.guild_level
                self.daily_free_research_count_before = (
                    member_state.daily_free_research_count
                )
                self.daily_donation_count_before = (
                    member_state.daily_paid_research_count
                )
            self.level_after = member_state.guild_level
            self.daily_free_research_count_after = (
                member_state.daily_free_research_count
            )
            self.daily_donation_count_after = member_state.daily_paid_research_count

        progression = action.progression
        if progression is not None:
            if self.experience_before is None:
                self.experience_before = progression.previous_experience
            self.experience_after = progression.current_experience
            if self.member_contribution_before is None:
                self.member_contribution_before = progression.previous_contribution
            self.member_contribution_after = progression.current_contribution

        lab_research = action.lab_research
        if lab_research is not None:
            if self.research_point_before is None:
                self.research_point_before = lab_research.previous_research_point
            self.research_point_after = lab_research.current_research_point

        if action.is_super_success:
            self.super_success_count += 1

    def observe_detail(self, detail: GuildDetail, *, initial: bool = False) -> None:
        if initial and self.experience_before is None:
            self.experience_before = detail.total_experience
        self.experience_after = detail.total_experience

    def to_dict(self) -> dict:
        return {
            "level_before": self.level_before,
            "level_after": self.level_after,
            "level_change": self._change(self.level_before, self.level_after),
            "experience_before": self.experience_before,
            "experience_after": self.experience_after,
            "experience_gained": self._change(
                self.experience_before,
                self.experience_after,
            ),
            "member_contribution_before": self.member_contribution_before,
            "member_contribution_after": self.member_contribution_after,
            "member_contribution_gained": self._change(
                self.member_contribution_before,
                self.member_contribution_after,
            ),
            "research_point_before": self.research_point_before,
            "research_point_after": self.research_point_after,
            "research_point_gained": self._change(
                self.research_point_before,
                self.research_point_after,
            ),
            "daily_free_research_count_before": (self.daily_free_research_count_before),
            "daily_free_research_count_after": self.daily_free_research_count_after,
            "daily_donation_count_before": self.daily_donation_count_before,
            "daily_donation_count_after": self.daily_donation_count_after,
            "super_success_count": self.super_success_count,
        }

    @staticmethod
    def _change(before: Optional[int], after: Optional[int]) -> Optional[int]:
        if before is None or after is None:
            return None
        return int(after) - int(before)


@dataclass
class GuildWorkflowResult:
    joined: bool = False
    joined_at: Optional[float] = None
    attendance_claimed: bool = False
    free_research_count: int = 0
    free_effective_count: int = 0
    free_super_success_count: int = 0
    paid_research_count: int = 0
    paid_effective_count: int = 0
    paid_super_success_count: int = 0
    diamond_balance_before_paid: Optional[int] = None
    diamond_spent: int = 0
    diamond_balance_final: Optional[int] = None
    left_guild: bool = False
    left_at: Optional[float] = None
    stop_reason: str = ""
    paid_stop_status: Optional[int] = None
    paid_stop_message: str = ""
    error: str = ""
    guild_detail: Optional[GuildDetail] = None
    guild_progress: GuildProgress = field(default_factory=GuildProgress)

    @property
    def ok(self) -> bool:
        return bool(
            self.joined
            and self.attendance_claimed
            and self.left_guild
            and self.diamond_balance_final is not None
            and not self.error
        )

    @property
    def effective_research_count(self) -> int:
        return self.free_effective_count + self.paid_effective_count

    @property
    def super_success_count(self) -> int:
        return self.free_super_success_count + self.paid_super_success_count

    def to_dict(self) -> dict:
        payload = asdict(self)
        payload["donation_count"] = self.paid_research_count
        payload["effective_research_count"] = self.effective_research_count
        payload["super_success_count"] = self.super_success_count
        payload["guild_progress"] = self.guild_progress.to_dict()
        return {"ok": self.ok, **payload}


class GuildRunner:
    """Execute join -> attendance -> research -> leave for one account."""

    def __init__(
        self,
        client: GrpcClient,
        session: Session,
        *,
        free_research_count: int = 3,
        paid_research_limit: int = 100,
        total_count_limit: Optional[int] = None,
        sleep_seconds: float = 0.15,
        on_balance: Optional[Callable[[int], None]] = None,
        initial_guild_level: Optional[int] = None,
        initial_diamond_balance: Optional[int] = None,
    ) -> None:
        self.client = client
        self.session = session
        self.free_research_count = max(0, int(free_research_count))
        self.paid_research_limit = max(0, int(paid_research_limit))
        self.total_count_limit = (
            None
            if total_count_limit is None
            else max(0, int(total_count_limit))
        )
        self.sleep_seconds = max(0.0, float(sleep_seconds))
        self.on_balance = on_balance
        self.initial_guild_level = (
            max(0, int(initial_guild_level))
            if initial_guild_level is not None
            else None
        )
        self.initial_diamond_balance = (
            max(0, int(initial_diamond_balance))
            if initial_diamond_balance is not None
            else None
        )

    def run(self, guild_id: str) -> GuildWorkflowResult:
        result = GuildWorkflowResult()
        if self.initial_guild_level is not None:
            result.guild_progress.level_before = self.initial_guild_level
            result.guild_progress.level_after = self.initial_guild_level
        guild = Guild(self.client, self.session)
        joined = False
        balance = self.initial_diamond_balance

        try:
            join_response = guild.join_guild(guild_id)
            joined = True
            result.joined = True
            result.joined_at = time.time()
            result.guild_progress.observe_action(
                parse_join_guild_response(join_response.message),
                initial=True,
            )
            log.info("joined guild")

            try:
                detail_response = guild.get_guild(guild_id)
                result.guild_detail = parse_guild_detail_response(
                    detail_response.message
                )
                result.guild_progress.observe_detail(
                    result.guild_detail,
                    initial=True,
                )
            except Exception as error:
                log.warning("guild detail unavailable after join: %s", error)

            attendance_response = guild.attend_guild(guild_id)
            result.guild_progress.observe_action(
                parse_attend_guild_response(attendance_response.message)
            )
            result.attendance_claimed = True
            log.info("attendance reward claimed")

            for index in range(1, self.free_research_count + 1):
                if self._total_count_reached(result):
                    result.stop_reason = "total_count_reached"
                    break
                response = guild.conduct_free_guild_lab_research(guild_id)
                action = parse_free_guild_lab_research_response(response.message)
                result.guild_progress.observe_action(action)
                effective_count = self._record_research_result(
                    result,
                    action,
                    paid=False,
                )
                result.free_research_count = index
                log.info(
                    "free guild research %s/%s effective=%s total_effective=%s",
                    index,
                    self.free_research_count,
                    effective_count,
                    result.effective_research_count,
                )
                self._sleep()

            result.diamond_balance_before_paid = balance
            log.info("persisted diamond balance before paid research=%s", balance)

            if not self._total_count_reached(result):
                for index in range(1, self.paid_research_limit + 1):
                    if self._total_count_reached(result):
                        result.stop_reason = "total_count_reached"
                        break
                    try:
                        response = guild.conduct_paid_guild_lab_research(guild_id)
                    except GrpcError as error:
                        if self._is_insufficient_diamonds(error):
                            result.stop_reason = "insufficient_diamonds"
                            result.paid_stop_status = error.status
                            result.paid_stop_message = error.message
                            owned_amount = self._owned_amount(error)
                            if owned_amount is None:
                                raise RuntimeError(
                                    "insufficient-diamond response missing owned amount"
                                ) from error
                            balance = owned_amount
                            result.diamond_balance_before_paid = (
                                owned_amount + result.diamond_spent
                            )
                            result.diamond_balance_final = owned_amount
                            self._notify_balance(owned_amount)
                            log.info(
                                "paid research exhausted usable diamonds: %s",
                                error.message,
                            )
                            break
                        raise

                    action = parse_paid_guild_lab_research_response(response.message)
                    charged = sum(
                        payment.amount
                        for payment in parse_currency_payments(response.message)
                        if payment.data_id == DIAMOND_CURRENCY_DATA_ID
                    )
                    if charged <= 0:
                        raise RuntimeError(
                            f"paid guild research {index} returned no diamond payment"
                        )
                    result.guild_progress.observe_action(action)
                    effective_count = self._record_research_result(
                        result,
                        action,
                        paid=True,
                    )
                    result.paid_research_count = index
                    result.diamond_spent += charged
                    if balance is not None:
                        balance = max(0, balance - charged)
                        self._notify_balance(balance)
                    log.info(
                        "paid guild research %s/%s charged=%s effective=%s "
                        "total_effective=%s estimated_balance=%s",
                        index,
                        self.paid_research_limit,
                        charged,
                        effective_count,
                        result.effective_research_count,
                        balance,
                    )
                    self._sleep()
                else:
                    result.stop_reason = (
                        "total_count_reached"
                        if self._total_count_reached(result)
                        else "paid_count_reached"
                    )
            else:
                result.stop_reason = "total_count_reached"

            if result.diamond_balance_final is None:
                result.diamond_balance_final = balance

        except Exception as error:
            result.error = f"{type(error).__name__}: {error}"
            log.error("guild workflow failed: %s", result.error)
        finally:
            if joined:
                try:
                    detail_response = guild.get_guild(guild_id)
                    result.guild_detail = parse_guild_detail_response(
                        detail_response.message
                    )
                    result.guild_progress.observe_detail(result.guild_detail)
                except Exception as error:
                    log.warning(
                        "final guild detail unavailable before leave: %s", error
                    )

                try:
                    guild.leave_guild(guild_id)
                    result.left_guild = True
                    result.left_at = time.time()
                    log.info("left guild")
                except Exception as error:
                    self._append_error(
                        result,
                        f"leave failed: {type(error).__name__}: {error}",
                    )

        return result

    def _notify_balance(self, balance: int) -> None:
        if self.on_balance is not None:
            self.on_balance(max(0, int(balance)))

    def _sleep(self) -> None:
        if self.sleep_seconds > 0:
            time.sleep(self.sleep_seconds)

    def _total_count_reached(self, result: GuildWorkflowResult) -> bool:
        return bool(
            self.total_count_limit is not None
            and result.effective_research_count >= self.total_count_limit
        )

    @staticmethod
    def _record_research_result(
        result: GuildWorkflowResult,
        action: GuildActionResult,
        *,
        paid: bool,
    ) -> int:
        effective_count = GuildRunner._effective_research_count(action)
        if paid:
            result.paid_effective_count += effective_count
            if action.is_super_success:
                result.paid_super_success_count += 1
        else:
            result.free_effective_count += effective_count
            if action.is_super_success:
                result.free_super_success_count += 1
        return effective_count

    @staticmethod
    def _effective_research_count(action: GuildActionResult) -> int:
        progression = action.progression
        if progression is not None:
            contribution_gained = (
                progression.current_contribution
                - progression.previous_contribution
            )
            if contribution_gained > 0:
                return int(contribution_gained)
        return 3 if action.is_super_success else 1

    @staticmethod
    def _is_insufficient_diamonds(error: GrpcError) -> bool:
        message = error.message.lower()
        return bool(
            error.status == 9
            and "not enough resources" in message
            and str(DIAMOND_CURRENCY_DATA_ID) in message
        )

    @staticmethod
    def _owned_amount(error: GrpcError) -> Optional[int]:
        matched = re.search(r"Owned amount:\s*(\d+)", error.message, re.IGNORECASE)
        return int(matched.group(1)) if matched else None

    @staticmethod
    def _append_error(result: GuildWorkflowResult, message: str) -> None:
        result.error = f"{result.error}; {message}" if result.error else message
