"""Resident-guild management workflows.

Unlike :mod:`guild_runner`, this module never leaves a managed account in the
normal path.  It reconciles the account pool with a persistent guild roster,
then executes the already-joined member actions once per server day.
"""
from __future__ import annotations

import logging
import json
import re
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import asdict
from datetime import datetime
from typing import Callable, Optional, Type

from .auth import AccountState
from .constants import ENDPOINT
from .crumble_dungeon import CrumbleDungeonRunner
from .db import (
    GUILD_COOLDOWN_SECONDS,
    AccountDB,
    AccountRow,
    GuildMembershipRow,
    ManagedGuildRow,
)
from .grpc_client import GrpcClient, GrpcError
from .guild_calendar import guild_day_key
from .guild import (
    Guild,
    GuildActionResult,
    GuildMemberStateSnapshot,
    parse_apply_guild_response,
    parse_attend_guild_response,
    parse_guild_detail_response,
    parse_guild_applications_for_user_response,
    parse_get_guild_members_response,
    parse_get_guild_support_requests_response,
    parse_join_guild_response,
    parse_provide_guild_supports_response,
    parse_guild_search_response,
)
from .guild_limits import (
    GUILD_DAILY_PAID_RESEARCH_MAX_COST,
    guild_max_member_count,
    parse_guild_daily_recruitment_limit,
)
from .guild_runner import GuildRunner
from .daily_runner import DailyRunner, DailyWorkflowResult
from .social import Social, parse_get_user_social_info_response

log = logging.getLogger(__name__)

ClientFactory = Type[GrpcClient]
LoginAccount = Callable[[AccountRow], AccountState]
SupportProgress = Callable[[dict], None]
RESIDENT_SUPPORT_WORKERS = 5


def resident_day_key(now: Optional[float] = None) -> str:
    return guild_day_key(now)


class ResidentGuildRunner:
    """Maintain one configured guild and its permanent account roster."""

    def __init__(
        self,
        db: AccountDB,
        login_account: LoginAccount,
        *,
        client_factory: ClientFactory = GrpcClient,
        sleep_seconds: float = 0.15,
    ) -> None:
        self.db = db
        self.login_account = login_account
        self.client_factory = client_factory
        self.sleep_seconds = max(0.0, float(sleep_seconds))

    def status(self, guild: ManagedGuildRow) -> dict:
        memberships = self.db.list_guild_memberships(guild.id)
        managed = [
            row
            for row in memberships
            if row.member_type == "managed" and row.status == "active"
        ]
        reserved = [
            row
            for row in memberships
            if row.member_type == "reserved" and row.status != "retired"
        ]
        today = resident_day_key()
        actions = []
        for row in managed:
            action = self.db.get_daily_guild_action(guild.id, today, row.mid)
            if action is None:
                actions.append({"mid": row.mid, "status": "pending"})
            else:
                action = dict(action)
                try:
                    action["details"] = json.loads(action.pop("details_json") or "{}")
                except (TypeError, ValueError):
                    action["details"] = {}
                actions.append(action)
        return self._status_payload(guild, memberships, managed, reserved, actions)

    def set_reserve_slots(
        self,
        guild: ManagedGuildRow,
        reserve_slots: int,
    ) -> ManagedGuildRow:
        """Persist the fill policy and recalculate its configured target."""
        reserve = int(reserve_slots)
        if reserve < 0:
            raise ValueError("--reserve-slots 不能小于 0")
        capacity = max(0, int(guild.capacity))
        if capacity > 0 and reserve > capacity:
            raise ValueError(
                f"--reserve-slots 不能大于当前公会容量 {capacity}"
            )
        return self.db.update_managed_guild(
            guild.id,
            reserve_slots=reserve,
            target_managed_count=max(0, capacity - reserve),
        )

    def sync(self, guild: ManagedGuildRow) -> dict:
        """Refresh member identities and mutable guild summary fields."""
        actors = self._actor_rows(guild)
        if not actors:
            return {
                "ok": False,
                "state": "no_member_actor",
                "stopped_reason": "no_member_actor",
                "next_action": {
                    "action": "fill",
                    "message": "当前没有可登录的常驻成员；先执行 fill。",
                },
            }

        errors = []
        for actor in actors:
            try:
                payload = self._sync_with_actor(guild, actor)
                payload["sync_actor_mid"] = actor.mid
                payload["sync_attempts"] = len(errors) + 1
                return payload
            except Exception as error:
                message = f"{type(error).__name__}: {error}"
                errors.append({"mid": actor.mid, "error": message})
                log.warning(
                    "resident guild sync failed with %s, trying next actor: %s",
                    actor.mid,
                    message,
                )

        message = errors[-1]["error"]
        self.db.update_managed_guild(
            guild.id,
            status="degraded",
            details={
                **guild.details,
                "last_sync_error": message,
                "last_sync_attempts": errors,
            },
        )
        return {
            **self.status(guild),
            "ok": False,
            "synced": False,
            "state": "sync_failed",
            "stopped_reason": "sync_failed",
            "error": message,
            "sync_attempts": len(errors),
            "sync_errors": errors,
        }

    def _sync_with_actor(
        self,
        guild: ManagedGuildRow,
        actor: AccountRow,
    ) -> dict:
        state = self.login_account(actor)
        with self.client_factory(state.endpoint or ENDPOINT) as client:
            api = Guild(client, state.to_session())
            summaries = parse_guild_search_response(
                api.search_guilds(guild.gname).message
            )
            summary = next(
                (item for item in summaries if item.guild_id == guild.guild_id),
                None,
            )
            member_response = api.get_guild_members(guild.guild_id)
            members = parse_get_guild_members_response(member_response.message)
            detail_response = api.get_guild(guild.guild_id)
            detail = parse_guild_detail_response(detail_response.message)
            state.resource_key = api.session.resource_key

        self._persist_logged_in(actor, state)
        self._reconcile_members(guild, members)
        live_level = (
            int(summary.guild_level)
            if summary is not None and summary.guild_level
            else int(guild.guild_level)
        )
        live_capacity = guild_max_member_count(live_level)
        capacity_source = "guild_level" if live_capacity is not None else ""
        changes = {
            "member_count": len(members.members),
            "last_sync_at": time.time(),
            "status": "active",
            "details": {
                **guild.details,
                "search_summary": (
                    asdict(summary)
                    if summary is not None
                    else guild.details.get("search_summary", {})
                ),
                "guild_detail": asdict(detail),
                "members": [asdict(member) for member in members.members],
            },
        }
        if live_capacity is not None:
            changes.update(
                {
                    "capacity": live_capacity,
                    "target_managed_count": max(
                        0,
                        live_capacity - int(guild.reserve_slots),
                    ),
                }
            )
        if summary is not None:
            changes.update(
                {
                    "gname": summary.name or guild.gname,
                    "gmname": summary.master_name or guild.gmname,
                    "join_method": summary.join_method,
                    "guild_level": summary.guild_level,
                }
            )
        if capacity_source:
            changes["details"]["capacity_source"] = capacity_source
            changes["details"]["capacity_level"] = live_level
        refreshed = self.db.update_managed_guild(guild.id, **changes)
        payload = self.status(refreshed)
        payload.update({"ok": True, "synced": True})
        return payload

    def fill(self, guild: ManagedGuildRow, *, max_accounts: int = 0) -> dict:
        """Fill missing managed slots without removing existing members."""
        # ``init --capacity`` is only a legacy bootstrap value.  Once the
        # current guild level is known, always replace it with the capacity
        # from the same GuildLevels table used by the game.  This also makes
        # direct runner calls safe when the caller did not run ``sync`` first.
        level_capacity = guild_max_member_count(guild.guild_level)
        if level_capacity is not None and (
            int(guild.capacity) != level_capacity
            or int(guild.target_managed_count)
            != max(0, level_capacity - int(guild.reserve_slots))
        ):
            guild = self.db.update_managed_guild(
                guild.id,
                capacity=level_capacity,
                target_managed_count=max(
                    0,
                    level_capacity - int(guild.reserve_slots),
                ),
                details={
                    **guild.details,
                    "capacity_source": "guild_level",
                    "capacity_level": int(guild.guild_level),
                },
            )
        memberships = self.db.list_guild_memberships(guild.id)
        active = [
            row
            for row in memberships
            if row.member_type == "managed" and row.status == "active"
        ]
        pending = [
            row
            for row in memberships
            if row.member_type == "managed"
            and row.status in {"planned", "applied", "invited", "accepted"}
        ]
        pending_validation: list[dict] = []
        stale_pending_mids: set[str] = set()
        if guild.join_method != 0:
            for membership in pending:
                if membership.status != "applied":
                    continue
                validation = self._validate_pending_application(
                    guild,
                    membership,
                )
                pending_validation.append(validation)
                if validation.get("invalidated"):
                    stale_pending_mids.add(membership.mid)
            if pending_validation:
                # Validation may have invalidated rows that the phone rejected
                # or cleared.  Reload before calculating vacancies so they no
                # longer reserve a slot in this same fill invocation.
                memberships = self.db.list_guild_memberships(guild.id)
                active = [
                    row
                    for row in memberships
                    if row.member_type == "managed" and row.status == "active"
                ]
                pending = [
                    row
                    for row in memberships
                    if row.member_type == "managed"
                    and row.status
                    in {"planned", "applied", "invited", "accepted"}
                ]
        # Existing non-managed members already consume server slots.  They
        # satisfy the configured reserve first; when reserve is zero, subtract
        # them from the managed target so a full guild is reported as at target
        # instead of permanently showing an impossible one-account vacancy.
        non_managed_active = sum(
            1
            for row in memberships
            if row.member_type != "managed" and row.status == "active"
        )
        effective_managed_target = min(
            int(guild.target_managed_count),
            max(0, int(guild.capacity) - non_managed_active),
        )
        target_vacancy = max(
            0,
            effective_managed_target - len(active) - len(pending),
        )
        if target_vacancy <= 0:
            pending_approval = [
                row for row in pending if row.status == "applied"
            ]
            pending_results = [
                {
                    "ok": True,
                    "joined": False,
                    "applied": row.status == "applied",
                    "awaiting_approval": row.status == "applied",
                    "mid": row.mid,
                    "name": str(row.details.get("name") or ""),
                    "slot_no": row.slot_no,
                    "status": row.status,
                    **(
                        {"application_id": row.details["application_id"]}
                        if row.details.get("application_id")
                        else {}
                    ),
                }
                for row in pending
            ]
            payload = {
                "ok": True,
                "state": (
                    "awaiting_approval"
                    if pending_approval
                    else "at_target"
                ),
                "requested": 0,
                "joined": 0,
                "applied": 0,
                "pending": len(pending),
                "pending_approval": len(pending_approval),
                "vacancy": 0,
                "results": pending_results,
            }
            if pending_validation:
                payload["pending_validation"] = self._pending_validation_summary(
                    pending_validation
                )
            if pending_approval:
                payload["next_action"] = {
                    "action": "approve_applications",
                    "message": (
                        "已有常驻账号申请待审批；请在手机同意后重跑 "
                        "guild --gname <name> fill。"
                    ),
                }
            return payload
        if guild.capacity <= 0 or guild.target_managed_count <= 0:
            return {
                "ok": False,
                "state": "capacity_unknown",
                "stopped_reason": "capacity_unknown",
                "requested": target_vacancy,
                "joined": 0,
                "vacancy": target_vacancy,
                "next_action": {
                    "action": "init_with_capacity",
                    "message": "无法确认公会容量，请在 init 时提供 --capacity。",
                },
                "results": [],
            }

        # ``member_count`` is refreshed by sync.  Cap the local target by the
        # actual free guild slots as well, otherwise external members could
        # make a nominal x-2 target overfill the server-side capacity.
        local_occupied = len(
            [row for row in memberships if row.status in {
                "planned", "applied", "invited", "accepted", "active", "reserved"
            }]
        )
        occupied = max(int(guild.member_count), local_occupied)
        capacity_remaining = max(0, int(guild.capacity) - occupied)
        vacancy = min(target_vacancy, capacity_remaining)
        if vacancy <= 0:
            return {
                "ok": False,
                "state": "capacity_full",
                "stopped_reason": "capacity_full",
                "requested": target_vacancy,
                "joined": 0,
                "vacancy": 0,
                "target_vacancy": target_vacancy,
                "capacity_remaining": capacity_remaining,
                "next_action": {
                    "action": "sync_or_remove_member",
                    "message": "公会当前没有可用成员位；先同步状态或在手机移除成员。",
                },
                "results": [],
            }

        recruited_today = self._recruitment_count(guild)
        recruit_remaining = max(0, guild.daily_recruit_limit - recruited_today)
        if recruit_remaining <= 0:
            return {
                "ok": False,
                "state": "daily_recruitment_limit",
                "stopped_reason": "daily_recruitment_limit",
                "requested": vacancy,
                "joined": 0,
                "vacancy": vacancy,
                "daily_recruit_remaining": 0,
                "next_action": {
                    "action": "retry_after_day_reset",
                    "message": "公会今日招募人数已达上限，明日重跑 maintain。",
                },
                "results": [],
            }

        limit = vacancy if not max_accounts else min(vacancy, max(0, int(max_accounts)))
        limit = min(limit, recruit_remaining)
        candidates = self.db.list_resident_candidates(guild.id)
        if stale_pending_mids:
            # A removed application should be retried with the same selected
            # account, not silently replaced by another random pool account.
            candidates.sort(
                key=lambda row: (row.mid not in stale_pending_mids, row.created_at, row.mid)
            )
        slots = {
            row.slot_no
            for row in memberships
            if row.member_type == "managed"
            and row.status in {
                "planned", "applied", "invited", "accepted", "active"
            }
            and row.slot_no > 0
        }
        results: list[dict] = []
        joined_so_far = 0
        filled_so_far = 0
        for candidate in candidates:
            if filled_so_far >= limit:
                break
            slot = self._next_slot(slots, effective_managed_target)
            if slot is None:
                break
            item = self._join_one(guild, candidate, None, slot)
            if candidate.mid in stale_pending_mids:
                item["reapplied"] = bool(item.get("applied"))
            results.append(item)
            if item.get("ok"):
                filled_so_far += 1
                self.db.mark_used(candidate.mid, True)
                slots.add(slot)
                recruited_today += 1
                if item.get("joined"):
                    joined_so_far += 1
                self.db.update_managed_guild(
                    guild.id,
                    daily_recruit_day=resident_day_key(),
                    daily_recruit_used=recruited_today,
                    member_count=max(
                        int(guild.member_count),
                        occupied + joined_so_far,
                    ),
                )
            else:
                # Recruitment is a server-side daily quota.  Stop immediately
                # when the API reports it instead of burning the remaining
                # candidates on identical permission failures.
                reported_limit = parse_guild_daily_recruitment_limit(
                    item.get("error", "")
                )
                if reported_limit is not None:
                    recruited_today = guild.daily_recruit_limit
                    self.db.update_managed_guild(
                        guild.id,
                        daily_recruit_day=resident_day_key(),
                        daily_recruit_used=recruited_today,
                    )
                    break

        joined = sum(1 for item in results if item.get("joined"))
        applied = sum(1 for item in results if item.get("applied"))
        filled = joined + applied
        failed = sum(1 for item in results if not item.get("ok"))
        recruitment_limit_hit = any(
            parse_guild_daily_recruitment_limit(item.get("error", "")) is not None
            for item in results
        )
        state = (
            "daily_recruitment_limit"
            if recruitment_limit_hit
            else (
                "awaiting_approval"
                if applied
                else ("filled" if joined else "fill_failed")
            )
        )
        payload = {
            "ok": filled >= limit and limit > 0,
            "state": state,
            "requested": limit,
            "attempted": len(results),
            "failed": failed,
            "joined": joined,
            "applied": applied,
            "filled": filled,
            "vacancy_before": target_vacancy,
            "vacancy_after": max(0, vacancy - filled),
            "capacity_remaining_before": capacity_remaining,
            "daily_recruit_remaining": max(
                0, guild.daily_recruit_limit - recruited_today
            ),
            "results": results,
        }
        if pending_validation:
            payload["pending_validation"] = self._pending_validation_summary(
                pending_validation
            )
        if applied:
            payload["next_action"] = {
                "action": "approve_applications",
                "message": (
                    "已为常驻账号提交入会申请；请在手机逐个同意申请，"
                    "完成后重跑 guild --gname <name> maintain。"
                ),
            }
        if recruitment_limit_hit:
            payload["stopped_reason"] = "daily_recruitment_limit"
            payload["next_action"] = {
                "action": "retry_after_day_reset",
                "message": "公会今日招募人数已达上限，明日重跑 maintain。",
            }
        return payload

    def _validate_pending_application(
        self,
        guild: ManagedGuildRow,
        membership: GuildMembershipRow,
    ) -> dict:
        """Verify one locally pending application against applicant state."""
        row = self.db.get(membership.mid)
        if row is None:
            message = "account_not_found"
            self.db.update_guild_membership(
                guild.id,
                membership.mid,
                slot_no=0,
                status="error",
                last_error=message,
            )
            return {
                "ok": False,
                "mid": membership.mid,
                "pending": False,
                "invalidated": True,
                "error": message,
            }
        try:
            state = self.login_account(row)
            self._persist_logged_in(
                row,
                state,
                note=f"resident:{guild.guild_id}",
            )
            with self.client_factory(state.endpoint or ENDPOINT) as client:
                api = Guild(client, state.to_session())
                applications = parse_guild_applications_for_user_response(
                    api.get_guild_applications_for_user().message
                )
                current = next(
                    (
                        item
                        for item in applications
                        if item.guild.guild_id == guild.guild_id
                    ),
                    None,
                )
                state.resource_key = api.session.resource_key
            self._persist_logged_in(row, state)
            checked_at = time.time()
            if current is not None:
                self.db.update_guild_membership(
                    guild.id,
                    membership.mid,
                    last_seen_at=checked_at,
                    last_error="",
                    details={
                        **membership.details,
                        "application_id": current.application_id,
                        "application_validated_at": checked_at,
                    },
                )
                return {
                    "ok": True,
                    "mid": membership.mid,
                    "pending": True,
                    "invalidated": False,
                    "application_id": current.application_id,
                }

            self.db.update_guild_membership(
                guild.id,
                membership.mid,
                status="stale",
                last_seen_at=checked_at,
                last_error="guild_application_not_found",
                details={
                    **membership.details,
                    "application_invalidated_at": checked_at,
                },
            )
            return {
                "ok": True,
                "mid": membership.mid,
                "pending": False,
                "invalidated": True,
            }
        except Exception as error:
            message = f"{type(error).__name__}: {error}"
            # A failed check must not trigger a duplicate application.  Keep
            # the row pending and expose the error for the next retry.
            self.db.update_guild_membership(
                guild.id,
                membership.mid,
                last_error=f"application_check_failed: {message}",
            )
            return {
                "ok": False,
                "mid": membership.mid,
                "pending": True,
                "invalidated": False,
                "error": message,
            }

    @staticmethod
    def _pending_validation_summary(results: list[dict]) -> dict:
        return {
            "checked": len(results),
            "confirmed": sum(
                1 for item in results if item.get("ok") and item.get("pending")
            ),
            "invalidated": sum(
                1 for item in results if item.get("invalidated")
            ),
            "failed": sum(1 for item in results if not item.get("ok")),
            "results": results,
        }

    def daily(self, guild: ManagedGuildRow) -> dict:
        """Run attendance, research/donation, and support for active accounts."""
        day_key = resident_day_key()
        memberships = self.db.list_guild_memberships(
            guild.id,
            member_type="managed",
            status="active",
        )
        results: list[dict] = []
        for membership in memberships:
            existing = self.db.get_daily_guild_action(guild.id, day_key, membership.mid)
            if (
                existing is not None
                and existing.get("status") == "done"
                and self._daily_action_has_account_workflows(existing)
            ):
                results.append(
                    {
                        "ok": True,
                        "mid": membership.mid,
                        "skipped": True,
                        "reason": "already_completed_today",
                        "daily_action": existing,
                    }
                )
                continue
            results.append(self._daily_one(guild, membership, day_key))
        completed = sum(1 for item in results if item.get("ok"))
        failed = len(results) - completed
        self.db.update_managed_guild(
            guild.id,
            last_daily_day=day_key,
            status="active" if failed == 0 else "degraded",
        )
        return {
            "ok": failed == 0,
            "day": day_key,
            "count": completed,
            "attempted": len(results),
            "failed": failed,
            "results": results,
        }

    def support(
        self,
        guild: ManagedGuildRow,
        *,
        on_progress: Optional[SupportProgress] = None,
    ) -> dict:
        """Support pending guild-center requests using active local members.

        This is deliberately narrower than :meth:`daily`: it does not run the
        account daily workflow, attendance, research, or the crumble dungeon.
        Each member's successful request ids are persisted in
        ``guild_support_actions`` so rerunning the command on the same server
        day does not support the same request twice.
        """
        day_key = resident_day_key()
        memberships = self.db.list_guild_memberships(
            guild.id,
            member_type="managed",
            status="active",
        )
        if not memberships:
            return {
                "ok": False,
                "mode": "resident_support",
                "day": day_key,
                "state": "no_active_members",
                "stopped_reason": "no_active_members",
                "count": 0,
                "attempted": 0,
                "accounts_attempted": 0,
                "failed": 0,
                "results": [],
                "next_action": {
                    "action": "fill",
                    "message": "当前没有可登录的常驻成员；先执行 guild --gname <name> fill。",
                },
            }

        total_accounts = len(memberships)
        self._notify_support_progress(
            on_progress,
            {
                "phase": "querying",
                "processed": 0,
                "total": total_accounts,
                "support_count": 0,
                "failed": 0,
            },
        )

        results: list[dict] = []
        support_count = 0
        support_attempted = 0
        failed = 0
        query_info: dict = {
            "ok": False,
            "attempted": False,
            "request_count": 0,
        }
        stopped_reason = ""

        runnable = []
        for membership in memberships:
            row = self.db.get(membership.mid)
            if row is None:
                message = "account_not_found"
                item = {
                    "ok": False,
                    "mid": membership.mid,
                    "name": str(membership.details.get("name") or ""),
                    "support": None,
                    "error": message,
                }
                self.db.update_guild_membership(
                    guild.id,
                    membership.mid,
                    status="active",
                    last_error=message,
                )
                results.append(item)
                failed += 1
                self._notify_support_progress(
                    on_progress,
                    {
                        "phase": "account",
                        "processed": len(results),
                        "total": total_accounts,
                        "support_count": support_count,
                        "failed": failed,
                        "mid": membership.mid,
                        "name": item["name"],
                        "status": "failed",
                        "error": message,
                    },
                )
                continue
            runnable.append((membership, row))

        if not runnable:
            stopped_reason = "no_active_accounts"
            self.db.update_managed_guild(guild.id, status="degraded")
            self._notify_support_progress(
                on_progress,
                {
                    "phase": "done",
                    "processed": len(results),
                    "total": total_accounts,
                    "support_count": support_count,
                    "failed": failed,
                    "stopped_reason": stopped_reason,
                },
            )
            return {
                "ok": False,
                "mode": "resident_support",
                "day": day_key,
                "state": stopped_reason,
                "count": 0,
                "attempted": 0,
                "accounts_attempted": len(results),
                "failed": failed,
                "query": query_info,
                "stopped_reason": stopped_reason,
                "workers": RESIDENT_SUPPORT_WORKERS,
                "results": results,
            }

        # A requester cannot see their own support request. Query with a second
        # distinct member when the first response is empty, then fan out only
        # after a concrete support request id has been discovered.
        query_candidates = sorted(
            runnable,
            key=lambda item: (
                item[0].slot_no <= 0,
                item[0].slot_no if item[0].slot_no > 0 else 10**9,
                item[0].mid,
            ),
        )
        query_attempts: list[dict] = []
        cached_requests = []
        successful_empty_queries = 0
        required_empty_queries = min(2, len(query_candidates))
        last_successful_query_mid = ""

        for query_membership, query_row in query_candidates[:2]:
            try:
                state = self.login_account(query_row)
                session = state.to_session()
                with self.client_factory(state.endpoint or ENDPOINT) as client:
                    api = Guild(client, session)
                    requests = parse_get_guild_support_requests_response(
                        api.get_guild_support_requests(guild.guild_id).message
                    )
                    state.resource_key = api.session.resource_key
                self._persist_logged_in(
                    query_row,
                    state,
                    note=f"resident:{guild.guild_id}",
                )
                last_successful_query_mid = query_membership.mid
                query_attempts.append(
                    {
                        "ok": True,
                        "mid": query_membership.mid,
                        "request_count": len(requests),
                    }
                )
                if requests:
                    cached_requests = requests
                    break
                successful_empty_queries += 1
            except Exception as error:
                query_attempts.append(
                    {
                        "ok": False,
                        "mid": query_membership.mid,
                        "request_count": 0,
                        "error": f"{type(error).__name__}: {error}",
                    }
                )

        empty_confirmed = successful_empty_queries >= required_empty_queries
        query_ok = bool(cached_requests) or empty_confirmed
        query_info = {
            "ok": query_ok,
            "attempted": True,
            "attempt_count": len(query_attempts),
            "queried_by_mid": last_successful_query_mid,
            "request_count": len(cached_requests),
            "empty_confirmations": successful_empty_queries,
            "attempts": query_attempts,
        }
        if not query_ok:
            stopped_reason = "query_failed"
            failed += 1
            last_attempt = query_attempts[-1]
            message = str(last_attempt.get("error") or "support_query_unconfirmed")
            item = {
                "ok": False,
                "mid": str(last_attempt.get("mid") or ""),
                "name": "",
                "support": None,
                "error": message,
            }
            results.append(item)

        self._notify_support_progress(
            on_progress,
            {
                "phase": "queried",
                "processed": len(results),
                "total": total_accounts,
                "support_count": support_count,
                "failed": failed,
                "request_count": len(cached_requests),
                "query_attempt_count": len(query_attempts),
            },
        )

        if not cached_requests:
            if not stopped_reason:
                stopped_reason = "no_pending_requests"
            self.db.update_managed_guild(
                guild.id,
                status="active" if failed == 0 else "degraded",
            )
            self._notify_support_progress(
                on_progress,
                {
                    "phase": "done",
                    "processed": len(results),
                    "total": total_accounts,
                    "support_count": support_count,
                    "failed": failed,
                    "stopped_reason": stopped_reason,
                },
            )
            return {
                "ok": failed == 0,
                "mode": "resident_support",
                "day": day_key,
                "state": stopped_reason,
                "count": support_count,
                "attempted": support_attempted,
                "accounts_attempted": len(results),
                "failed": failed,
                "query": query_info,
                "stopped_reason": stopped_reason,
                "workers": RESIDENT_SUPPORT_WORKERS,
                "results": results,
            }

        work_items = []
        for index, (membership, row) in enumerate(runnable):
            done_ids = self.db.list_guild_support_action_ids(
                guild.id,
                day_key,
                membership.mid,
            )
            requests = [
                request
                for request in cached_requests
                if request.support_request_id not in done_ids
                and request.requester_mid != membership.mid
            ]
            work_items.append((index, membership, row, requests))

        work_iter = iter(work_items)
        pending = {}
        with ThreadPoolExecutor(
            max_workers=RESIDENT_SUPPORT_WORKERS,
            thread_name_prefix="guild-support",
        ) as executor:
            for _ in range(RESIDENT_SUPPORT_WORKERS):
                work = next(work_iter, None)
                if work is None:
                    break
                index, membership, row, requests = work
                future = executor.submit(
                    self._support_network_one,
                    guild,
                    membership,
                    row,
                    requests,
                    len(cached_requests),
                )
                pending[future] = (index, membership, row)

            while pending:
                completed, _ = wait(pending, return_when=FIRST_COMPLETED)
                for future in completed:
                    index, membership, row = pending.pop(future)
                    item, state = future.result()
                    support_result = item.get("support")
                    if isinstance(support_result, dict):
                        support_result = self._persist_support_result(
                            guild,
                            day_key,
                            membership.mid,
                            support_result,
                        )
                        item["support"] = support_result
                    if state is not None:
                        self._persist_logged_in(row, state)

                    support_value = int(
                        (support_result or {}).get("count", 0) or 0
                    )
                    attempted_value = int(
                        (support_result or {}).get("attempted", 0) or 0
                    )
                    support_count += support_value
                    support_attempted += attempted_value
                    if not item.get("ok"):
                        failed += 1
                    member_stop_reason = str(
                        (support_result or {}).get("stopped_reason") or ""
                    )
                    if member_stop_reason == "support_limit":
                        stopped_reason = "support_limit"

                    now = time.time()
                    self.db.update_guild_membership(
                        guild.id,
                        membership.mid,
                        status="active",
                        last_seen_at=now,
                        last_support_at=(
                            now
                            if attempted_value > 0
                            else membership.last_support_at
                        ),
                        last_error=str(item.get("error") or ""),
                        details={
                            **membership.details,
                            "last_support_day": day_key,
                        },
                    )
                    item["_index"] = index
                    results.append(item)
                    self._notify_support_progress(
                        on_progress,
                        {
                            "phase": "account",
                            "processed": len(results),
                            "total": total_accounts,
                            "support_count": support_count,
                            "failed": failed,
                            "mid": membership.mid,
                            "name": item["name"],
                            "status": (
                                member_stop_reason
                                or ("ok" if item.get("ok") else "failed")
                            ),
                            "error": str(item.get("error") or ""),
                        },
                    )

                if not stopped_reason:
                    while len(pending) < RESIDENT_SUPPORT_WORKERS:
                        work = next(work_iter, None)
                        if work is None:
                            break
                        index, membership, row, requests = work
                        future = executor.submit(
                            self._support_network_one,
                            guild,
                            membership,
                            row,
                            requests,
                            len(cached_requests),
                        )
                        pending[future] = (index, membership, row)

        results.sort(key=lambda item: int(item.pop("_index", -1)))

        self.db.update_managed_guild(
            guild.id,
            status="active" if failed == 0 else "degraded",
        )
        self._notify_support_progress(
            on_progress,
            {
                "phase": "done",
                "processed": len(results),
                "total": total_accounts,
                "support_count": support_count,
                "failed": failed,
                "stopped_reason": stopped_reason,
            },
        )
        return {
            "ok": failed == 0,
            "mode": "resident_support",
            "day": day_key,
            "state": (
                stopped_reason
                if stopped_reason
                else ("completed" if failed == 0 else "partial_failure")
            ),
            "count": support_count,
            "attempted": support_attempted,
            "accounts_attempted": len(results),
            "failed": failed,
            "query": query_info,
            "workers": RESIDENT_SUPPORT_WORKERS,
            **({"stopped_reason": stopped_reason} if stopped_reason else {}),
            "results": results,
        }

    def _support_network_one(
        self,
        guild: ManagedGuildRow,
        membership: GuildMembershipRow,
        row: AccountRow,
        requests: list,
        available: int,
    ) -> tuple[dict, Optional[AccountState]]:
        item = {
            "ok": False,
            "mid": membership.mid,
            "name": str(membership.details.get("name") or ""),
            "support": None,
        }
        try:
            state = self.login_account(row)
            session = state.to_session()
            with self.client_factory(state.endpoint or ENDPOINT) as client:
                api = Guild(client, session)
                support_result = self._perform_support_requests(
                    guild,
                    membership.mid,
                    api,
                    requests,
                    available=available,
                )
                state.resource_key = api.session.resource_key
            item.update(
                {
                    "ok": bool(support_result.get("ok", False)),
                    "support": support_result,
                }
            )
            if not item["ok"]:
                item["error"] = str(
                    support_result.get("error") or "support_failed"
                )
            return item, state
        except Exception as error:
            item["error"] = f"{type(error).__name__}: {error}"
            return item, None

    @staticmethod
    def _notify_support_progress(
        callback: Optional[SupportProgress], payload: dict
    ) -> None:
        if callback is None:
            return
        try:
            callback(dict(payload))
        except Exception as error:  # noqa: BLE001 - display must not stop support
            log.debug("support progress callback failed: %s", error)

    @staticmethod
    def _daily_action_has_account_workflows(action: dict) -> bool:
        """Return whether a completed action includes the current daily SOP.

        Before resident ``guild daily`` also ran the account daily rewards and
        crumble dungeon, completed rows only contained ``workflow`` and
        ``support``.  Treat those legacy rows as pending once so upgrading a
        database does not silently skip the newly added actions.
        """
        try:
            details = json.loads(action.get("details_json") or "{}")
        except (TypeError, ValueError):
            return False
        return isinstance(details, dict) and {
            "account_daily",
            "crumble_dungeon",
        }.issubset(details)

    def maintain(self, guild: ManagedGuildRow) -> dict:
        """Reconcile, fill vacancies, reconcile again, then run daily actions."""
        before = self.status(guild)
        sync_before = self.sync(guild)
        current = self.db.get_managed_guild(guild.guild_id) or guild
        fill = self.fill(current)
        current = self.db.get_managed_guild(guild.guild_id) or current
        sync_after = self.sync(current)
        current = self.db.get_managed_guild(guild.guild_id) or current
        daily = self.daily(current)
        return {
            "ok": bool(daily.get("ok")) and bool(fill.get("ok", True)),
            "mode": "resident",
            "before": before,
            "sync_before": sync_before,
            "fill": fill,
            "sync_after": sync_after,
            "daily": daily,
            "status": self.status(current),
        }

    def _join_one(
        self,
        guild: ManagedGuildRow,
        row: AccountRow,
        controller: Optional[AccountRow],
        slot: int,
    ) -> dict:
        mid = row.mid
        candidate_name = ""
        try:
            state = self.login_account(row)
            self._persist_logged_in(row, state, note=f"resident:{guild.guild_id}")
            if guild.join_method == 0:
                with self.client_factory(state.endpoint or ENDPOINT) as client:
                    api = Guild(client, state.to_session())
                    candidate_name = self._lookup_user_name(
                        client, api.session, mid
                    )
                    response = api.join_guild(guild.guild_id)
                    action = parse_join_guild_response(response.message)
                    state.resource_key = api.session.resource_key
                    self._persist_logged_in(row, state)
                if action.member_state is None:
                    raise RuntimeError("join response missing member_state")
                joined_at = time.time()
                self.db.mark_guild_joined(mid, guild.guild_id, joined_at=joined_at)
                self.db.upsert_guild_membership(
                    guild.id,
                    mid,
                    slot_no=slot,
                    member_type="managed",
                    status="active",
                    role=int(action.member_state.role or 1),
                    joined_at=joined_at,
                    last_seen_at=joined_at,
                    details={
                        "member_state": asdict(action.member_state),
                        **({"name": candidate_name} if candidate_name else {}),
                    },
                )
                return {
                    "ok": True,
                    "joined": True,
                    "applied": False,
                    "mid": mid,
                    **({"name": candidate_name} if candidate_name else {}),
                    "slot_no": slot,
                    "joined_at": joined_at,
                    "member_state": asdict(action.member_state),
                }
            else:
                with self.client_factory(state.endpoint or ENDPOINT) as client:
                    api = Guild(client, state.to_session())
                    candidate_name = self._lookup_user_name(
                        client, api.session, mid
                    )
                    applications = parse_guild_applications_for_user_response(
                        api.get_guild_applications_for_user().message
                    )
                    application = next(
                        (
                            item
                            for item in applications
                            if item.guild.guild_id == guild.guild_id
                        ),
                        None,
                    )
                    application_id = (
                        application.application_id
                        if application is not None
                        else parse_apply_guild_response(
                            api.apply_guild(guild.guild_id).message
                        )
                    )
                    state.resource_key = api.session.resource_key
                    self._persist_logged_in(row, state)
                if not application_id:
                    raise RuntimeError(
                        "ApplyGuild response did not contain application_id"
                    )
                applied_at = time.time()
                self.db.upsert_guild_membership(
                    guild.id,
                    mid,
                    slot_no=slot,
                    member_type="managed",
                    status="applied",
                    role=1,
                    last_seen_at=applied_at,
                    details={
                        "application_id": application_id,
                        "applied_at": applied_at,
                        **({"name": candidate_name} if candidate_name else {}),
                    },
                )
                return {
                    "ok": True,
                    "joined": False,
                    "applied": True,
                    "awaiting_approval": True,
                    "mid": mid,
                    **({"name": candidate_name} if candidate_name else {}),
                    "slot_no": slot,
                    "application_id": application_id,
                    "applied_at": applied_at,
                }
        except Exception as error:
            message = f"{type(error).__name__}: {error}"
            cooldown_until = self._record_rejoin_cooldown(mid, message)
            self.db.upsert_guild_membership(
                guild.id,
                mid,
                # Failed candidates must not reserve a physical slot; the
                # next fill attempt should be able to retry that slot.
                slot_no=0,
                member_type="managed",
                status="error",
                last_error=message,
                details=(
                    {
                        **({"name": candidate_name} if candidate_name else {}),
                        **(
                            {"rejoin_cooldown_until": cooldown_until}
                            if cooldown_until is not None
                            else {}
                        ),
                    }
                ),
            )
            return {
                "ok": False,
                "mid": mid,
                **({"name": candidate_name} if candidate_name else {}),
                "slot_no": slot,
                "error": message,
                **(
                    {"rejoin_cooldown_until": cooldown_until}
                    if cooldown_until is not None
                    else {}
                ),
            }

    def _record_rejoin_cooldown(
        self,
        mid: str,
        message: str,
    ) -> Optional[float]:
        matched = re.search(
            r"rejoin cooldown ends at\s+([0-9]{4}-[0-9]{2}-[0-9]{2}T[^' ]+)",
            str(message or ""),
            flags=re.IGNORECASE,
        )
        if not matched:
            return None
        try:
            deadline = datetime.fromisoformat(
                matched.group(1).rstrip(".,;")
            ).timestamp()
        except ValueError:
            return None
        self.db.mark_guild_left(
            mid,
            left_at=deadline - GUILD_COOLDOWN_SECONDS,
        )
        return deadline

    def _daily_one(
        self,
        guild: ManagedGuildRow,
        membership: GuildMembershipRow,
        day_key: str,
    ) -> dict:
        row = self.db.get(membership.mid)
        if row is None:
            message = "account_not_found"
            self.db.update_guild_membership(
                guild.id, membership.mid, status="error", last_error=message
            )
            self.db.upsert_daily_guild_action(
                guild.id, day_key, membership.mid, status="failed", error=message
            )
            return {"ok": False, "mid": membership.mid, "error": message}

        try:
            state = self.login_account(row)
            self._persist_logged_in(row, state, note=f"resident:{guild.guild_id}")
            session = state.to_session()
            initial = self._initial_action(guild, membership, day_key)
            attendance_already_claimed = False
            account_daily: DailyWorkflowResult
            crumble_dungeon: dict
            cached_cookie_ids = tuple(
                int(value)
                for value in (membership.details.get("cookie_ids") or ())
                if str(value).strip().isdigit() and int(value) > 0
            )
            with self.client_factory(state.endpoint or ENDPOINT) as client:
                def persist_balance(balance: int) -> None:
                    state.diamond_balance = max(0, int(balance))
                    state.resource_key = session.resource_key
                    self._persist_logged_in(row, state)

                # Resident-guild daily is the account daily workflow plus the
                # guild member workflow.  Reuse the same session so the
                # SignUp response's live resource key and final diamond
                # balance flow into the subsequent guild requests.
                if row.daily and resident_day_key(row.daily) == day_key:
                    account_daily = DailyWorkflowResult(
                        login_completed=True,
                        diamond_balance_final=state.diamond_balance,
                        skipped=True,
                    )
                else:
                    account_daily = DailyRunner(
                        client,
                        session,
                        on_balance=persist_balance,
                    ).run()
                if account_daily.login_completed:
                    crumble_dungeon = CrumbleDungeonRunner(
                        client,
                        session,
                        cookie_ids=account_daily.cookie_ids or cached_cookie_ids,
                    ).run()
                else:
                    crumble_dungeon = {
                        "ok": False,
                        "started": False,
                        "finished": False,
                        "skipped": True,
                        "reason": "login_failed",
                        "error": account_daily.error or "daily_login_failed",
                    }

                api = Guild(client, session)
                try:
                    attendance = parse_attend_guild_response(
                        api.attend_guild(guild.guild_id).message
                    )
                    initial = attendance
                    attendance_already_claimed = True
                except GrpcError as error:
                    if not self._is_already_attended(error):
                        raise
                    attendance_already_claimed = True

                workflow = GuildRunner(
                    client,
                    api.session,
                    paid_research_limit=None,
                    paid_research_cost_limit=GUILD_DAILY_PAID_RESEARCH_MAX_COST,
                    initial_guild_level=guild.guild_level,
                    initial_diamond_balance=state.diamond_balance,
                    on_balance=persist_balance,
                    leave_after=False,
                    attendance_already_claimed=attendance_already_claimed,
                    sleep_seconds=self.sleep_seconds,
                ).run_joined(
                    guild.guild_id,
                    initial_action=initial,
                    joined_at=membership.joined_at or time.time(),
                )
                support = self._support_one(
                    guild, membership.mid, api, day_key
                )

            state.resource_key = api.session.resource_key
            if workflow.diamond_balance_final is not None:
                state.diamond_balance = workflow.diamond_balance_final
            self._persist_logged_in(row, state)
            if account_daily.ok and not account_daily.skipped:
                # Keep the standalone ``daily`` pool in sync.  Otherwise a
                # later top-level daily invocation would repeat the account
                # rewards that were already handled as part of this guild run.
                self.db.mark_daily_completed(row.mid)
            member_state = self._member_state_after(guild, initial, workflow)
            now = time.time()
            daily_ok = (
                bool(account_daily.ok)
                and bool(crumble_dungeon.get("ok", True))
                and bool(workflow.ok)
                and bool(support.get("ok", True))
            )
            daily_error = (
                account_daily.error
                or str(crumble_dungeon.get("error") or "")
                or workflow.error
                or str(support.get("error") or "")
            )
            if not daily_ok and not daily_error:
                if not account_daily.ok:
                    daily_error = "account_daily_failed"
                elif not crumble_dungeon.get("ok", True):
                    daily_error = "crumble_dungeon_failed"
                elif not support.get("ok", True):
                    daily_error = "support_failed"
                else:
                    daily_error = "workflow_failed"
            level_after = workflow.guild_progress.level_after
            if level_after is not None and int(level_after) > 0:
                self.db.update_managed_guild(
                    guild.id,
                    guild_level=max(int(guild.guild_level), int(level_after)),
                )
            self.db.update_guild_membership(
                guild.id,
                membership.mid,
                status="active",
                last_seen_at=now,
                last_attendance_at=now,
                last_donate_at=now,
                last_support_at=now if support["attempted"] else membership.last_support_at,
                last_error=daily_error,
                details={
                    **membership.details,
                    "cookie_ids": (
                        crumble_dungeon.get("cookie_ids")
                        or list(account_daily.cookie_ids)
                        or list(cached_cookie_ids)
                    ),
                    "member_state": member_state,
                    "member_state_day": day_key,
                },
            )
            run_id = self.db.record_guild_run(
                membership.mid,
                guild_id=guild.guild_id,
                joined_at=membership.joined_at or now,
                left_at=None,
                free_research_count=workflow.free_research_count,
                paid_research_count=workflow.paid_research_count,
                free_effective_count=workflow.free_effective_count,
                paid_effective_count=workflow.paid_effective_count,
                free_super_success_count=workflow.free_super_success_count,
                paid_super_success_count=workflow.paid_super_success_count,
                diamond_spent=workflow.diamond_spent,
                stop_reason=workflow.stop_reason,
                ok=daily_ok,
                error=daily_error,
            )
            action = self.db.upsert_daily_guild_action(
                guild.id,
                day_key,
                membership.mid,
                attendance_status="success",
                attendance_at=now,
                free_research_count=workflow.free_research_count,
                paid_research_count=workflow.paid_research_count,
                effective_research_count=workflow.effective_research_count,
                support_count=support["count"],
                diamond_spent=workflow.diamond_spent,
                status="done" if daily_ok else "failed",
                error=daily_error,
                details={
                    "account_daily": account_daily.to_dict(),
                    "crumble_dungeon": crumble_dungeon,
                    "workflow": workflow.to_dict(),
                    "support": support,
                    "guild_run_id": run_id,
                },
            )
            return {
                "ok": daily_ok,
                "mid": membership.mid,
                "account_daily": account_daily.to_dict(),
                "crumble_dungeon": crumble_dungeon,
                "workflow": workflow.to_dict(),
                "support": support,
                "daily_action": action,
            }
        except Exception as error:
            message = f"{type(error).__name__}: {error}"
            self.db.update_guild_membership(
                guild.id,
                membership.mid,
                status="error",
                last_error=message,
            )
            self.db.upsert_daily_guild_action(
                guild.id,
                day_key,
                membership.mid,
                status="failed",
                error=message,
            )
            return {"ok": False, "mid": membership.mid, "error": message}

    def _support_one(
        self,
        guild: ManagedGuildRow,
        supporter_mid: str,
        api: Guild,
        day_key: str,
        requests: Optional[list] = None,
    ) -> dict:
        if requests is None:
            try:
                requests = parse_get_guild_support_requests_response(
                    api.get_guild_support_requests(guild.guild_id).message
                )
            except Exception as error:
                return {
                    "ok": False,
                    "attempted": 0,
                    "count": 0,
                    "requests": [],
                    "error": f"{type(error).__name__}: {error}",
                }
        done_ids = self.db.list_guild_support_action_ids(
            guild.id, day_key, supporter_mid
        )
        candidates = [
            request
            for request in requests
            if request.support_request_id not in done_ids
            and request.requester_mid != supporter_mid
        ]
        result = self._perform_support_requests(
            guild,
            supporter_mid,
            api,
            candidates,
            available=len(requests),
        )
        return self._persist_support_result(
            guild,
            day_key,
            supporter_mid,
            result,
        )

    def _perform_support_requests(
        self,
        guild: ManagedGuildRow,
        supporter_mid: str,
        api: Guild,
        requests: list,
        *,
        available: int,
    ) -> dict:
        """Perform support RPCs without touching SQLite (worker-thread safe)."""
        results: list[dict] = []
        actions: list[dict] = []
        stopped_reason = ""
        for request in requests:
            try:
                response = api.provide_guild_supports(
                    guild.guild_id, [request.support_request_id]
                )
                parsed = parse_provide_guild_supports_response(response.message)
                actions.append(
                    {
                        "request_id": request.support_request_id,
                        "requester_mid": request.requester_mid,
                        "support_type": request.support_type,
                        "status": "success",
                        "reduced_time_millis": request.reduced_time_millis,
                        "response": {"action": asdict(parsed)},
                    }
                )
                results.append(
                    {"ok": True, "request_id": request.support_request_id}
                )
            except Exception as error:
                message = f"{type(error).__name__}: {error}"
                actions.append(
                    {
                        "request_id": request.support_request_id,
                        "requester_mid": request.requester_mid,
                        "support_type": request.support_type,
                        "status": "failed",
                        "reduced_time_millis": request.reduced_time_millis,
                        "error": message,
                    }
                )
                results.append(
                    {
                        "ok": False,
                        "request_id": request.support_request_id,
                        "error": message,
                    }
                )
                if self._is_support_limit_error(error):
                    stopped_reason = "support_limit"
                    break
        count = sum(1 for item in results if item.get("ok"))
        return {
            "ok": all(item.get("ok") for item in results),
            "attempted": len(results),
            "count": count,
            "available": available,
            "requests": results,
            "_actions": actions,
            **({"stopped_reason": stopped_reason} if stopped_reason else {}),
        }

    def _persist_support_result(
        self,
        guild: ManagedGuildRow,
        day_key: str,
        supporter_mid: str,
        result: dict,
    ) -> dict:
        """Persist one worker result from the SQLite-owning main thread."""
        actions = list(result.get("_actions") or [])
        for action in actions:
            self.db.upsert_guild_support_action(
                guild.id,
                day_key,
                supporter_mid,
                str(action.get("request_id") or ""),
                requester_mid=str(action.get("requester_mid") or ""),
                support_type=str(action.get("support_type") or ""),
                status=str(action.get("status") or "failed"),
                reduced_time_millis=int(
                    action.get("reduced_time_millis") or 0
                ),
                error=str(action.get("error") or ""),
                response=action.get("response") or {},
            )
        return {key: value for key, value in result.items() if key != "_actions"}

    def _reconcile_members(self, guild: ManagedGuildRow, snapshot) -> None:
        live_ids = {member.mid for member in snapshot.members}
        existing = {row.mid: row for row in self.db.list_guild_memberships(guild.id)}
        now = time.time()
        for member in snapshot.members:
            current = existing.get(member.mid)
            account = self.db.get(member.mid)
            locally_controlled = bool(
                account is not None
                and account.guest_secret
                and not account.invalid
            )
            if current is None:
                if account is not None:
                    self.db.mark_guild_joined(
                        member.mid,
                        guild.guild_id,
                        joined_at=now,
                    )
                self.db.upsert_guild_membership(
                    guild.id,
                    member.mid,
                    member_type=(
                        "managed" if locally_controlled else "external"
                    ),
                    status="active",
                    role=member.role,
                    last_seen_at=now,
                    details={"member": asdict(member)},
                )
            else:
                joined_at = current.joined_at or now
                if account is not None and account.guild_joined_at <= account.guild_left_at:
                    self.db.mark_guild_joined(
                        member.mid, guild.guild_id, joined_at=joined_at
                    )
                self.db.update_guild_membership(
                    guild.id,
                    member.mid,
                    member_type=(
                        "managed"
                        if current.member_type == "managed" or locally_controlled
                        else current.member_type
                    ),
                    status="active",
                    role=member.role,
                    joined_at=joined_at,
                    last_seen_at=now,
                    details={**current.details, "member": asdict(member)},
                )
        for member in existing.values():
            if (
                member.mid in live_ids
                or member.status in {"applied", "invited", "accepted"}
                or member.status != "active"
            ):
                continue
            account = self.db.get(member.mid)
            if account is not None:
                # A manual kick/leave is still subject to the same server
                # cooldown, including locally owned accounts that were
                # classified as external because they joined outside fill.
                self.db.mark_guild_left(member.mid, left_at=now)
            # External members must be reconciled too.  Otherwise a member
            # removed manually on the phone remains ``active`` forever and is
            # still printed as part of the current guild roster.
            self.db.update_guild_membership(
                guild.id,
                member.mid,
                status="missing",
                left_at=now,
                last_error="member_not_in_live_guild",
            )

    def _actor_rows(self, guild: ManagedGuildRow) -> list[AccountRow]:
        """Return login-capable live members in safest sync order."""
        memberships = self.db.list_guild_memberships(guild.id)
        mids = [
            row.mid
            for row in memberships
            if row.member_type == "managed" and row.status == "active"
        ]
        mids.extend(
            row.mid
            for row in memberships
            if row.member_type == "reserved"
            and row.status == "active"
            and row.mid not in mids
        )
        if guild.controller_mid and guild.controller_mid not in mids:
            mids.append(guild.controller_mid)
        mids.extend(
            row.mid
            for row in memberships
            if row.member_type == "external"
            and row.status == "active"
            and row.mid not in mids
        )
        actors = []
        for mid in mids:
            row = self.db.get(mid)
            if row is not None and row.guest_secret and not row.invalid:
                actors.append(row)
        return actors

    def _persist_logged_in(
        self,
        row: AccountRow,
        state: AccountState,
        *,
        note: Optional[str] = None,
    ) -> None:
        db_note = row.note if note is None else note
        self.db.upsert_state(
            state,
            used=row.used,
            ready=row.ready,
            invalid=row.invalid,
            note=db_note,
        )

    @staticmethod
    def _lookup_user_name(client: GrpcClient, session, user_id: str) -> str:
        """Resolve the candidate's display name for CLI/SQLite output."""
        target = str(user_id or "").strip().upper()
        if not target:
            return ""
        try:
            response = Social(client, session).get_user_social_info((target,))
            infos = parse_get_user_social_info_response(response.message)
            matched = next(
                (item for item in infos if item.user_id.strip().upper() == target),
                None,
            )
            return matched.name if matched is not None else ""
        except Exception as error:
            log.warning("unable to resolve resident member name for %s: %s", target, error)
            return ""

    def enrich_member_names(self, guild: ManagedGuildRow) -> dict:
        """Backfill names for locally tracked members/applications in one RPC."""
        memberships = [
            item
            for item in self.db.list_guild_memberships(guild.id)
            if item.status != "retired"
        ]
        missing = [
            item
            for item in memberships
            if not str(item.details.get("name") or "").strip()
        ]
        if not missing:
            return {"ok": True, "updated": 0, "missing": 0}

        actor = None
        for membership in memberships:
            row = self.db.get(membership.mid)
            if row is not None and row.guest_secret:
                actor = row
                break
        if actor is None:
            return {
                "ok": False,
                "updated": 0,
                "missing": len(missing),
                "error": "no_account_for_name_lookup",
            }

        try:
            state = self.login_account(actor)
            session = state.to_session()
            with self.client_factory(state.endpoint or ENDPOINT) as client:
                response = Social(client, session).get_user_social_info(
                    tuple(item.mid for item in missing)
                )
            state.resource_key = session.resource_key
            self._persist_logged_in(actor, state)
            names = {
                item.user_id.strip().upper(): item.name
                for item in parse_get_user_social_info_response(response.message)
                if item.name
            }
            updated = 0
            for membership in missing:
                name = names.get(membership.mid)
                if not name:
                    continue
                self.db.update_guild_membership(
                    guild.id,
                    membership.mid,
                    details={**membership.details, "name": name},
                )
                updated += 1
            return {
                "ok": True,
                "updated": updated,
                "missing": len(missing) - updated,
            }
        except Exception as error:
            return {
                "ok": False,
                "updated": 0,
                "missing": len(missing),
                "error": f"{type(error).__name__}: {error}",
            }

    def _recruitment_count(self, guild: ManagedGuildRow) -> int:
        today = resident_day_key()
        if guild.daily_recruit_day != today:
            self.db.update_managed_guild(
                guild.id, daily_recruit_day=today, daily_recruit_used=0
            )
            return 0
        return guild.daily_recruit_used

    @staticmethod
    def _next_slot(occupied: set[int], limit: int) -> Optional[int]:
        for slot in range(1, max(0, int(limit)) + 1):
            if slot not in occupied:
                return slot
        return None

    @staticmethod
    def _status_payload(
        guild: ManagedGuildRow,
        memberships: list[GuildMembershipRow],
        managed: list[GuildMembershipRow],
        reserved: list[GuildMembershipRow],
        actions: list[dict],
    ) -> dict:
        non_managed_active = sum(
            1
            for row in memberships
            if row.member_type != "managed" and row.status == "active"
        )
        effective_managed_target = min(
            int(guild.target_managed_count),
            max(0, int(guild.capacity) - non_managed_active),
        )
        return {
            "mode": "resident",
            "guild": {
                "id": guild.id,
                "guild_id": guild.guild_id,
                "name": guild.gname,
                "master_name": guild.gmname,
                "join_method": guild.join_method,
                "status": guild.status,
                "level": guild.guild_level,
                "capacity": guild.capacity,
                "reserve_slots": guild.reserve_slots,
                "target_managed_count": guild.target_managed_count,
                "member_count": guild.member_count,
                "capacity_source": str(guild.details.get("capacity_source") or ""),
                "capacity_level": guild.details.get("capacity_level"),
                "controller_mid": guild.controller_mid,
                "original_master_mid": guild.original_master_mid,
            },
            "roster": {
                "managed_active": len(managed),
                "managed_target": effective_managed_target,
                "configured_managed_target": guild.target_managed_count,
                "vacancy": max(0, effective_managed_target - len(managed)),
                "reserved": len(reserved),
                "non_managed_active": non_managed_active,
                "all_local_rows": len(memberships),
            },
            "recruitment": {
                "day": guild.daily_recruit_day,
                "used": guild.daily_recruit_used,
                "limit": guild.daily_recruit_limit,
                "remaining": max(
                    0, guild.daily_recruit_limit - guild.daily_recruit_used
                ),
            },
            "daily": actions,
            "members": [
                {
                    "mid": row.mid,
                    "slot_no": row.slot_no,
                    "member_type": row.member_type,
                    "controlled": row.member_type == "managed",
                    "status": row.status,
                    "role": row.role,
                    "joined_at": row.joined_at,
                    "last_seen_at": row.last_seen_at,
                    "last_error": row.last_error,
                    **(
                        {"name": row.details.get("name")}
                        if row.details.get("name")
                        else {}
                    ),
                    **{
                        key: member.get(key)
                        for key in (
                            "name",
                            "crumble_level",
                            "total_combat_power",
                            "contribution_point",
                            "joined_at_millis",
                            "last_accessed_at_millis",
                        )
                        if (member := row.details.get("member"))
                        and key in member
                    },
                    "details": row.details,
                }
                for row in memberships
                if row.status != "retired"
            ],
        }

    @staticmethod
    def _initial_action(
        guild: ManagedGuildRow,
        membership: GuildMembershipRow,
        day_key: str,
    ) -> GuildActionResult:
        saved = dict(membership.details.get("member_state") or {})
        previous_day = str(
            membership.details.get("member_state_day") or ""
        )
        if previous_day != day_key:
            saved["daily_free_research_count"] = 0
            saved["daily_paid_research_count"] = 0
            saved["daily_support_rewarded_count"] = 0
        state = GuildMemberStateSnapshot(
            guild_level=int(saved.get("guild_level") or guild.guild_level or 1),
            daily_free_research_count=int(
                saved.get("daily_free_research_count") or 0
            ),
            daily_paid_research_count=int(
                saved.get("daily_paid_research_count") or 0
            ),
            daily_support_rewarded_count=int(
                saved.get("daily_support_rewarded_count") or 0
            ),
            role=saved.get("role"),
            guild_id=saved.get("guild_id") or guild.guild_id,
            guild_name=saved.get("guild_name") or guild.gname,
            last_free_researched_at_millis=int(
                saved.get("last_free_researched_at_millis") or 0
            ),
            last_paid_researched_at_millis=int(
                saved.get("last_paid_researched_at_millis") or 0
            ),
            last_support_rewarded_at_millis=int(
                saved.get("last_support_rewarded_at_millis") or 0
            ),
        )
        return GuildActionResult(member_state=state)

    @staticmethod
    def _member_state_after(
        guild: ManagedGuildRow,
        initial: GuildActionResult,
        workflow,
    ) -> dict:
        state = asdict(initial.member_state) if initial.member_state else {}
        progress = workflow.guild_progress
        if progress.level_after is not None:
            state["guild_level"] = progress.level_after
        if progress.daily_free_research_count_after is not None:
            state["daily_free_research_count"] = (
                progress.daily_free_research_count_after
            )
        if progress.daily_donation_count_after is not None:
            state["daily_paid_research_count"] = progress.daily_donation_count_after
        state.setdefault("guild_id", guild.guild_id)
        state.setdefault("guild_name", guild.gname)
        state.setdefault("guild_level", guild.guild_level)
        return state

    @staticmethod
    def _is_already_attended(error: GrpcError) -> bool:
        text = f"{error.message}".lower()
        return any(
            token in text
            for token in ("already attended", "already claimed", "already received")
        )

    @staticmethod
    def _is_support_limit_error(error: Exception) -> bool:
        text = str(error).lower()
        return "support" in text and any(
            token in text
            for token in ("limit", "not enough", "daily", "maximum", "max")
        )
