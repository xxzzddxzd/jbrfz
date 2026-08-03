"""One-account guild automation workflow."""
from __future__ import annotations

import logging
import time
from dataclasses import asdict, dataclass
from typing import Callable, Optional

from .currency import (
    DIAMOND_CURRENCY_DATA_ID,
    parse_currency_payments,
    parse_signup_currency_balance,
)
from .grpc_client import GrpcClient, GrpcError
from .guild import Guild, GuildDetail, parse_guild_detail_response
from .headers import Session, build_metadata
from .stage_runner import SIGNUP_PATH

log = logging.getLogger(__name__)


@dataclass
class GuildWorkflowResult:
    joined: bool = False
    attendance_claimed: bool = False
    free_research_count: int = 0
    paid_research_count: int = 0
    diamond_balance_before_paid: Optional[int] = None
    diamond_spent: int = 0
    diamond_balance_final: Optional[int] = None
    left_guild: bool = False
    paid_stop_status: Optional[int] = None
    paid_stop_message: str = ""
    error: str = ""
    guild_detail: Optional[GuildDetail] = None

    @property
    def ok(self) -> bool:
        return bool(
            self.joined
            and self.attendance_claimed
            and self.free_research_count == 3
            and self.left_guild
            and self.diamond_balance_final is not None
            and self.paid_stop_status == 9
            and not self.error
        )

    def to_dict(self) -> dict:
        return {"ok": self.ok, **asdict(self)}


class GuildRunner:
    """Execute join -> attendance -> research -> leave for one account."""

    def __init__(
        self,
        client: GrpcClient,
        session: Session,
        *,
        free_research_count: int = 3,
        paid_guard: int = 100,
        sleep_seconds: float = 0.15,
        on_balance: Optional[Callable[[int], None]] = None,
    ) -> None:
        self.client = client
        self.session = session
        self.free_research_count = max(0, int(free_research_count))
        self.paid_guard = max(1, int(paid_guard))
        self.sleep_seconds = max(0.0, float(sleep_seconds))
        self.on_balance = on_balance

    def sync_diamond_balance(self) -> int:
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
        return balance

    def run(self, guild_id: str) -> GuildWorkflowResult:
        result = GuildWorkflowResult()
        guild = Guild(self.client, self.session)
        joined = False

        try:
            guild.join_guild(guild_id)
            joined = True
            result.joined = True
            log.info("joined guild")

            try:
                detail_response = guild.get_guild(guild_id)
                result.guild_detail = parse_guild_detail_response(detail_response.message)
            except Exception as error:
                log.warning("guild detail unavailable after join: %s", error)

            guild.attend_guild(guild_id)
            result.attendance_claimed = True
            log.info("attendance reward claimed")

            for index in range(1, self.free_research_count + 1):
                guild.conduct_free_guild_lab_research(guild_id)
                result.free_research_count = index
                log.info("free guild research %s/%s", index, self.free_research_count)
                self._sleep()

            balance = self.sync_diamond_balance()
            result.diamond_balance_before_paid = balance
            log.info("diamond balance before paid research=%s", balance)

            for index in range(1, self.paid_guard + 1):
                try:
                    response = guild.conduct_paid_guild_lab_research(guild_id)
                except GrpcError as error:
                    if self._is_insufficient_diamonds(error):
                        result.paid_stop_status = error.status
                        result.paid_stop_message = error.message
                        log.info("paid research exhausted usable diamonds: %s", error.message)
                        break
                    raise

                charged = sum(
                    payment.amount
                    for payment in parse_currency_payments(response.message)
                    if payment.data_id == DIAMOND_CURRENCY_DATA_ID
                )
                if charged <= 0:
                    raise RuntimeError(
                        f"paid guild research {index} returned no diamond payment"
                    )
                result.paid_research_count = index
                result.diamond_spent += charged
                balance = max(0, balance - charged)
                self._notify_balance(balance)
                log.info(
                    "paid guild research %s charged=%s estimated_balance=%s",
                    index,
                    charged,
                    balance,
                )
                self._sleep()
            else:
                raise RuntimeError(f"paid research safety guard reached ({self.paid_guard})")

        except Exception as error:
            result.error = f"{type(error).__name__}: {error}"
            log.error("guild workflow failed: %s", result.error)
        finally:
            if joined:
                try:
                    guild.leave_guild(guild_id)
                    result.left_guild = True
                    log.info("left guild")
                except Exception as error:
                    self._append_error(
                        result,
                        f"leave failed: {type(error).__name__}: {error}",
                    )

            try:
                result.diamond_balance_final = self.sync_diamond_balance()
                log.info("final diamond balance=%s", result.diamond_balance_final)
            except Exception as error:
                self._append_error(
                    result,
                    f"final balance sync failed: {type(error).__name__}: {error}",
                )

        return result

    def _notify_balance(self, balance: int) -> None:
        if self.on_balance is not None:
            self.on_balance(max(0, int(balance)))

    def _sleep(self) -> None:
        if self.sleep_seconds > 0:
            time.sleep(self.sleep_seconds)

    @staticmethod
    def _is_insufficient_diamonds(error: GrpcError) -> bool:
        message = error.message.lower()
        return bool(
            error.status == 9
            and "not enough resources" in message
            and str(DIAMOND_CURRENCY_DATA_ID) in message
        )

    @staticmethod
    def _append_error(result: GuildWorkflowResult, message: str) -> None:
        result.error = f"{result.error}; {message}" if result.error else message
