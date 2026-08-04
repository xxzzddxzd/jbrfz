"""Resumable workflow for approval-required guilds."""
from __future__ import annotations

import logging
import time
from dataclasses import asdict
from typing import Callable, Optional

from .auth import AccountState
from .constants import ENDPOINT
from .db import AccountDB, AccountRow, GuildPrivateJobRow
from .grpc_client import GrpcClient, GrpcError
from .guild import (
    Guild,
    GuildActionResult,
    GuildMemberStateSnapshot,
    GuildSearchSummary,
    parse_accept_guild_invitation_response,
    parse_apply_guild_response,
    parse_guild_applications_for_user_response,
    parse_guild_invitations_for_user_response,
    parse_guild_search_response,
    parse_invite_user_to_guild_response,
)
from .guild_runner import GuildRunner, GuildWorkflowResult

log = logging.getLogger(__name__)

LoginAccount = Callable[[AccountRow], AccountState]
ClientFactory = Callable[[str], GrpcClient]


class PrivateGuildRunner:
    """Coordinate one temporary guild-master account and sequential donors."""

    def __init__(
        self,
        db: AccountDB,
        login_account: LoginAccount,
        *,
        client_factory: ClientFactory = GrpcClient,
        wait_timeout: float = 300,
        poll_interval: float = 5,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.db = db
        self.login_account = login_account
        self.client_factory = client_factory
        self.wait_timeout = max(0.0, float(wait_timeout))
        self.poll_interval = max(0.1, float(poll_interval))
        self.sleep = sleep

    def run(self, job: GuildPrivateJobRow) -> dict:
        controller_row = self.db.get(job.controller_mid)
        if controller_row is None:
            return self._waiting_payload(
                job,
                "controller_not_found",
                ok=False,
                error=f"sqlite 中没有自控会长账号: {job.controller_mid}",
            )

        controller_state = self.login_account(controller_row)
        controller_session = controller_state.to_session()
        try:
            with self.client_factory(
                controller_state.endpoint or ENDPOINT
            ) as controller_client:
                controller = Guild(controller_client, controller_session)

                if job.status == "awaiting_master_return":
                    current = self._find_guild(controller, job)
                    if (
                        current is not None
                        and current.master_user_id == job.original_master_mid
                    ):
                        job = self.db.update_private_job(
                            job.id,
                            status="complete",
                            completed_at=time.time(),
                            error="",
                        )
                        return self._waiting_payload(
                            job,
                            "complete",
                            complete=True,
                            current_master=current,
                        )
                    return self._waiting_payload(
                        job,
                        "awaiting_master_return",
                        current_master=current,
                    )

                member = self._controller_is_member(controller, job.guild_id)
                if not member:
                    job = self._ensure_application(controller, job)
                    member = self._wait_for_membership(controller, job.guild_id)
                    if not member:
                        job = self.db.update_private_job(
                            job.id,
                            status="awaiting_application_approval",
                        )
                        return self._waiting_payload(
                            job,
                            "awaiting_application_approval",
                        )

                current = self._wait_for_controller_master(controller, job)
                if current is None or current.master_user_id != job.controller_mid:
                    job = self.db.update_private_job(
                        job.id,
                        status="awaiting_master_transfer",
                    )
                    return self._waiting_payload(
                        job,
                        "awaiting_master_transfer",
                        current_master=current,
                    )

                if not job.master_acquired_at:
                    job = self.db.update_private_job(
                        job.id,
                        status="running",
                        master_acquired_at=time.time(),
                        error="",
                    )
                else:
                    job = self.db.update_private_job(job.id, status="running")

                result = self._run_donors(job, controller)
                job = self.db.get_private_job(job.id) or job
                result["job"] = self._job_payload(job)
                return result
        finally:
            self._persist_account(
                controller_row,
                controller_state,
                controller_session.resource_key,
            )

    def _ensure_application(
        self,
        guild: Guild,
        job: GuildPrivateJobRow,
    ) -> GuildPrivateJobRow:
        applications = parse_guild_applications_for_user_response(
            guild.get_guild_applications_for_user().message
        )
        existing = next(
            (
                item
                for item in applications
                if item.guild.guild_id == job.guild_id
            ),
            None,
        )
        application_id = existing.application_id if existing is not None else ""
        if not application_id:
            application_id = parse_apply_guild_response(
                guild.apply_guild(job.guild_id).message
            )
        if not application_id:
            raise RuntimeError("ApplyGuild response did not contain application_id")
        return self.db.update_private_job(
            job.id,
            application_id=application_id,
            status="awaiting_application_approval",
            error="",
        )

    def _wait_for_membership(self, guild: Guild, guild_id: str) -> bool:
        deadline = time.monotonic() + self.wait_timeout
        while True:
            if self._controller_is_member(guild, guild_id):
                return True
            if time.monotonic() >= deadline:
                return False
            self.sleep(min(self.poll_interval, max(0.0, deadline - time.monotonic())))

    @staticmethod
    def _controller_is_member(guild: Guild, guild_id: str) -> bool:
        try:
            guild.get_guild(guild_id)
            return True
        except GrpcError:
            return False

    def _wait_for_controller_master(
        self,
        guild: Guild,
        job: GuildPrivateJobRow,
    ) -> Optional[GuildSearchSummary]:
        deadline = time.monotonic() + self.wait_timeout
        while True:
            current = self._find_guild(guild, job)
            if current is not None and current.master_user_id == job.controller_mid:
                return current
            if time.monotonic() >= deadline:
                return current
            self.sleep(min(self.poll_interval, max(0.0, deadline - time.monotonic())))

    @staticmethod
    def _find_guild(
        guild: Guild,
        job: GuildPrivateJobRow,
    ) -> Optional[GuildSearchSummary]:
        summaries = parse_guild_search_response(guild.search_guilds(job.gname).message)
        return next(
            (item for item in summaries if item.guild_id == job.guild_id),
            None,
        )

    def _run_donors(self, job: GuildPrivateJobRow, controller: Guild) -> dict:
        results: list[dict] = []
        workflows: list[GuildWorkflowResult] = []
        failures = 0
        attempted = 0
        effective_total = int(job.effective_count)

        pending = [
            item
            for item in self.db.list_private_accounts(job.id)
            if item["state"] in {"selected", "invited", "accepted"}
        ]
        rows: list[tuple[AccountRow, Optional[dict]]] = []
        seen: set[str] = set()
        for item in pending:
            row = self.db.get(str(item["mid"]))
            if row is not None and row.mid != job.controller_mid:
                rows.append((row, item))
                seen.add(row.mid)

        active_mids = self.db.active_private_account_mids()
        for row in self.db.list_guild_eligible():
            if row.mid == job.controller_mid or row.mid in seen:
                continue
            if row.mid in active_mids:
                continue
            rows.append((row, None))
            seen.add(row.mid)

        for row, record in rows:
            if effective_total >= job.total_count_limit:
                break
            attempted += 1
            if record is None:
                record = self.db.reserve_private_account(job.id, row.mid)
            try:
                workflow, item = self._run_one_donor(
                    job,
                    controller,
                    row,
                    record,
                    remaining_total=job.total_count_limit - effective_total,
                )
                workflows.append(workflow)
                effective_total += workflow.effective_research_count
                job = self.db.update_private_job(
                    job.id,
                    effective_count=effective_total,
                    error="",
                )
                results.append(item)
                if not workflow.ok:
                    failures += 1
            except Exception as error:
                failures += 1
                message = f"{type(error).__name__}: {error}"
                self.db.update_private_account(
                    job.id,
                    row.mid,
                    state="failed",
                    error=message,
                )
                results.append({"mid": row.mid, "ok": False, "error": message})
                log.error("private guild donor %s failed: %s", row.mid, message)

        stopped_reason = (
            "totalcount_reached"
            if effective_total >= job.total_count_limit
            else "all_eligible_accounts_attempted"
        )
        job = self.db.update_private_job(
            job.id,
            status="awaiting_master_return",
            effective_count=effective_total,
            error="" if failures == 0 else f"{failures} donor account(s) failed",
        )
        return {
            "ok": failures == 0,
            "complete": False,
            "mode": "private",
            "count": job.paid_count_per_account,
            "requested_totalcount": job.total_count_limit,
            "totalcount": effective_total,
            "totalcount_reached": effective_total >= job.total_count_limit,
            "accounts_attempted": attempted,
            "accounts_failed": failures,
            "stopped_reason": stopped_reason,
            "next_state": "awaiting_master_return",
            "manual_master_return": {
                "required": True,
                "from_mid": job.controller_mid,
                "to_original_master_mid": job.original_master_mid,
            },
            "totals": self._totals(workflows),
            "results": results,
        }

    def _run_one_donor(
        self,
        job: GuildPrivateJobRow,
        controller: Guild,
        row: AccountRow,
        record: dict,
        *,
        remaining_total: int,
    ) -> tuple[GuildWorkflowResult, dict]:
        state_name = str(record["state"])
        invitation_id = str(record.get("invitation_id") or "")
        if state_name == "selected":
            invitation_id = parse_invite_user_to_guild_response(
                controller.invite_user_to_guild(job.guild_id, row.mid).message
            )
            if not invitation_id:
                raise RuntimeError("InviteUserToGuild response missing invitation_id")
            record = self.db.update_private_account(
                job.id,
                row.mid,
                state="invited",
                invitation_id=invitation_id,
                invited_at=time.time(),
                error="",
            )
            state_name = "invited"

        donor_state = self.login_account(row)
        donor_session = donor_state.to_session()
        joined = state_name == "accepted"
        joined_at = float(record.get("accepted_at") or 0)
        workflow: Optional[GuildWorkflowResult] = None
        try:
            with self.client_factory(donor_state.endpoint or ENDPOINT) as donor_client:
                donor = Guild(donor_client, donor_session)
                if state_name == "invited":
                    invitations = parse_guild_invitations_for_user_response(
                        donor.get_guild_invitations_for_user().message
                    )
                    selected = next(
                        (
                            item
                            for item in invitations
                            if item.invitation_id == invitation_id
                            and item.guild.guild_id == job.guild_id
                        ),
                        None,
                    )
                    if selected is None:
                        raise RuntimeError("expected pending guild invitation not found")
                    accepted = parse_accept_guild_invitation_response(
                        donor.accept_guild_invitation(
                            job.guild_id,
                            invitation_id,
                        ).message
                    )
                    if accepted.member_state is None:
                        raise RuntimeError("AcceptGuildInvitation missing member_state")
                    joined = True
                    joined_at = time.time()
                    self.db.mark_guild_joined(row.mid, job.guild_id, joined_at=joined_at)
                    record = self.db.update_private_account(
                        job.id,
                        row.mid,
                        state="accepted",
                        accepted_at=joined_at,
                        member_state=asdict(accepted.member_state),
                        error="",
                    )
                    initial_action = accepted
                else:
                    initial_action = self._saved_initial_action(record)

                def persist_balance(balance: int) -> None:
                    donor_state.diamond_balance = balance
                    self._persist_account(
                        row,
                        donor_state,
                        donor_session.resource_key,
                    )

                runner = GuildRunner(
                    donor_client,
                    donor_session,
                    paid_research_limit=job.paid_count_per_account,
                    total_count_limit=remaining_total,
                    on_balance=persist_balance,
                    initial_guild_level=(
                        initial_action.member_state.guild_level
                        if initial_action.member_state is not None
                        else None
                    ),
                    initial_diamond_balance=donor_state.diamond_balance,
                )
                workflow = runner.run_joined(
                    job.guild_id,
                    initial_action=initial_action,
                    joined_at=joined_at,
                )
        except Exception:
            if joined and workflow is None:
                try:
                    with self.client_factory(donor_state.endpoint or ENDPOINT) as client:
                        Guild(client, donor_session).leave_guild(job.guild_id)
                    left_at = time.time()
                    self.db.mark_guild_left(row.mid, left_at=left_at)
                    self.db.update_private_account(
                        job.id,
                        row.mid,
                        state="failed",
                        left_at=left_at,
                    )
                except Exception as leave_error:
                    log.error("private donor emergency leave failed: %s", leave_error)
            raise
        finally:
            self._persist_account(
                row,
                donor_state,
                donor_session.resource_key,
            )

        if workflow is None:
            raise RuntimeError("private donor workflow did not run")
        if workflow.diamond_balance_final is not None:
            donor_state.diamond_balance = workflow.diamond_balance_final
            self._persist_account(
                row,
                donor_state,
                donor_session.resource_key,
            )
        run_id = self.db.record_guild_run(
            row.mid,
            guild_id=job.guild_id,
            joined_at=workflow.joined_at,
            left_at=workflow.left_at,
            free_research_count=workflow.free_research_count,
            paid_research_count=workflow.paid_research_count,
            free_effective_count=workflow.free_effective_count,
            paid_effective_count=workflow.paid_effective_count,
            free_super_success_count=workflow.free_super_success_count,
            paid_super_success_count=workflow.paid_super_success_count,
            diamond_spent=workflow.diamond_spent,
            stop_reason=workflow.stop_reason,
            ok=workflow.ok,
            error=workflow.error,
        )
        self.db.update_private_account(
            job.id,
            row.mid,
            state="complete" if workflow.ok else "failed",
            left_at=workflow.left_at or 0,
            guild_run_id=run_id,
            error=workflow.error,
        )
        return workflow, {
            "mid": row.mid,
            "guild_run_id": run_id,
            "invitation_id": invitation_id,
            **workflow.to_dict(),
        }

    @staticmethod
    def _saved_initial_action(record: dict) -> GuildActionResult:
        saved = dict(record.get("member_state") or {})
        if not saved:
            raise RuntimeError("accepted private account is missing saved member_state")
        allowed = {
            "guild_level",
            "daily_free_research_count",
            "daily_paid_research_count",
            "role",
            "guild_id",
            "guild_name",
        }
        state = GuildMemberStateSnapshot(
            **{key: value for key, value in saved.items() if key in allowed}
        )
        return GuildActionResult(member_state=state)

    def _persist_account(
        self,
        row: AccountRow,
        state: AccountState,
        resource_key: str,
    ) -> None:
        state.resource_key = resource_key
        self.db.upsert_state(
            state,
            used=row.used,
            ready=row.ready,
            invalid=row.invalid,
            note=row.note,
        )

    def _waiting_payload(
        self,
        job: GuildPrivateJobRow,
        reason: str,
        *,
        ok: bool = True,
        complete: bool = False,
        error: str = "",
        current_master: Optional[GuildSearchSummary] = None,
    ) -> dict:
        payload = {
            "ok": ok,
            "complete": complete,
            "mode": "private",
            "stopped_reason": reason,
            "job": self._job_payload(job),
            "manual_action": None,
        }
        if reason == "awaiting_application_approval":
            payload["manual_action"] = {
                "action": "approve_application_and_transfer_master",
                "application_id": job.application_id,
                "controller_mid": job.controller_mid,
            }
        elif reason == "awaiting_master_transfer":
            payload["manual_action"] = {
                "action": "transfer_master_to_controller",
                "controller_mid": job.controller_mid,
            }
        elif reason == "awaiting_master_return":
            payload["manual_action"] = {
                "action": "return_master_manually",
                "from_mid": job.controller_mid,
                "to_original_master_mid": job.original_master_mid,
            }
        if current_master is not None:
            payload["current_master"] = {
                "mid": current_master.master_user_id,
                "name": current_master.master_name,
            }
        if error:
            payload["error"] = error
        return payload

    @staticmethod
    def _job_payload(job: GuildPrivateJobRow) -> dict:
        return {
            "id": job.id,
            "status": job.status,
            "guild_id": job.guild_id,
            "name": job.gname,
            "original_master_name": job.gmname,
            "original_master_mid": job.original_master_mid,
            "controller_mid": job.controller_mid,
            "application_id": job.application_id,
            "count": job.paid_count_per_account,
            "requested_totalcount": job.total_count_limit,
            "totalcount": job.effective_count,
        }

    @staticmethod
    def _totals(workflows: list[GuildWorkflowResult]) -> dict:
        return {
            "free_research_count": sum(item.free_research_count for item in workflows),
            "donation_count": sum(item.paid_research_count for item in workflows),
            "effective_research_count": sum(
                item.effective_research_count for item in workflows
            ),
            "super_success_count": sum(item.super_success_count for item in workflows),
            "diamond_spent": sum(item.diamond_spent for item in workflows),
        }
