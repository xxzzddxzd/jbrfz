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
    parse_guild_detail_response,
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
    ) -> None:
        self.db = db
        self.login_account = login_account
        self.client_factory = client_factory

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

                if job.status == "complete":
                    return self._waiting_payload(
                        job,
                        "complete",
                        complete=True,
                    )

                target_reached = job.effective_count >= job.total_count_limit
                if target_reached:
                    if job.status != "awaiting_master_return":
                        job = self.db.update_private_job(
                            job.id,
                            status="awaiting_master_return",
                        )
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

                if job.status == "awaiting_master_return":
                    # Older runs used this state even when the account pool was
                    # exhausted before reaching the requested target. Resume
                    # those jobs instead of returning the master prematurely.
                    job = self.db.update_private_job(
                        job.id,
                        status="awaiting_donors",
                    )

                member = self._controller_is_member(controller, job.guild_id)
                if not member:
                    job = self._ensure_application(controller, job)
                    member = self._controller_is_member(controller, job.guild_id)
                    if not member:
                        job = self.db.update_private_job(
                            job.id,
                            status="awaiting_application_approval",
                        )
                        return self._waiting_payload(
                            job,
                            "awaiting_application_approval",
                        )

                current = self._find_guild(controller, job)
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

    def return_master(self, job: GuildPrivateJobRow) -> dict:
        """Return a completed private batch to its recorded original master."""
        if job.status != "awaiting_master_return":
            return self._return_payload(
                job,
                ok=False,
                stopped_reason="job_not_awaiting_master_return",
                error=f"private job status is {job.status}",
            )
        if not job.original_master_mid:
            return self._return_payload(
                job,
                ok=False,
                stopped_reason="original_master_missing",
                error="private job does not contain original_master_mid",
            )
        if job.original_master_mid == job.controller_mid:
            return self._return_payload(
                job,
                ok=False,
                stopped_reason="original_master_is_controller",
                error="original master and controller are the same account",
            )

        controller_row = self.db.get(job.controller_mid)
        if controller_row is None:
            return self._return_payload(
                job,
                ok=False,
                stopped_reason="controller_not_found",
                error=f"sqlite 中没有自控会长账号: {job.controller_mid}",
            )

        controller_state = self.login_account(controller_row)
        controller_session = controller_state.to_session()
        try:
            with self.client_factory(
                controller_state.endpoint or ENDPOINT
            ) as controller_client:
                guild = Guild(controller_client, controller_session)
                current = self._find_guild(guild, job)
                if current is None:
                    return self._return_payload(
                        job,
                        ok=False,
                        stopped_reason="guild_not_found",
                        error="target guild was not found by its saved id",
                    )

                if current.master_user_id == job.original_master_mid:
                    job = self.db.update_private_job(
                        job.id,
                        status="complete",
                        completed_at=time.time(),
                        error="",
                    )
                    return self._return_payload(
                        job,
                        stopped_reason="complete",
                        transferred=False,
                        already_returned=True,
                        current_master=current,
                    )

                if current.master_user_id != job.controller_mid:
                    return self._return_payload(
                        job,
                        ok=False,
                        stopped_reason="controller_is_not_current_master",
                        error=(
                            "saved controller is not the current guild master; "
                            "refusing to transfer"
                        ),
                        current_master=current,
                    )

                detail = parse_guild_detail_response(
                    guild.get_guild(job.guild_id).message
                )
                if job.original_master_mid not in detail.member_ids:
                    return self._return_payload(
                        job,
                        ok=False,
                        stopped_reason="original_master_not_member",
                        error="recorded original master is not a current guild member",
                        current_master=current,
                    )

                guild.transfer_guild_master(
                    job.guild_id,
                    job.original_master_mid,
                )
                verified = self._find_guild(guild, job)
                if (
                    verified is None
                    or verified.master_user_id != job.original_master_mid
                ):
                    job = self.db.update_private_job(
                        job.id,
                        error="master transfer sent but verification is pending",
                    )
                    return self._return_payload(
                        job,
                        ok=False,
                        stopped_reason="master_return_verification_pending",
                        error=job.error,
                        transferred=True,
                        current_master=verified,
                    )

                job = self.db.update_private_job(
                    job.id,
                    status="complete",
                    completed_at=time.time(),
                    error="",
                )
                return self._return_payload(
                    job,
                    stopped_reason="complete",
                    transferred=True,
                    current_master=verified,
                )
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

    @staticmethod
    def _controller_is_member(guild: Guild, guild_id: str) -> bool:
        try:
            guild.get_guild(guild_id)
            return True
        except GrpcError:
            return False

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
        effective_before = int(job.effective_count)
        effective_total = int(job.effective_count)

        records = self.db.list_private_accounts(job.id)
        pending = [
            item
            for item in records
            if item["state"] in {"selected", "invited", "accepted"}
        ]
        rows: list[tuple[AccountRow, Optional[dict]]] = []
        seen: set[str] = {str(item["mid"]) for item in records}
        for item in pending:
            row = self.db.get(str(item["mid"]))
            if row is not None and row.mid != job.controller_mid:
                rows.append((row, item))

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

        target_reached = effective_total >= job.total_count_limit
        if target_reached:
            stopped_reason = "totalcount_reached"
            next_state = "awaiting_master_return"
        elif attempted:
            stopped_reason = "target_not_reached"
            next_state = "awaiting_donors"
        else:
            stopped_reason = "awaiting_eligible_accounts"
            next_state = "awaiting_donors"

        job = self.db.update_private_job(
            job.id,
            status=next_state,
            effective_count=effective_total,
            error="" if failures == 0 else f"{failures} donor account(s) failed",
        )
        pool = self.db.guild_pool_status()
        saved_records = self.db.list_private_accounts(job.id)
        record_counts: dict[str, int] = {}
        for item in saved_records:
            state = str(item["state"])
            record_counts[state] = record_counts.get(state, 0) + 1

        payload = {
            "ok": failures == 0,
            "complete": False,
            "mode": "private",
            "state": next_state,
            "count": job.paid_count_per_account,
            "requested_totalcount": job.total_count_limit,
            "totalcount": effective_total,
            "totalcount_added": effective_total - effective_before,
            "remaining_totalcount": max(
                0,
                job.total_count_limit - effective_total,
            ),
            "totalcount_reached": target_reached,
            "accounts_attempted": attempted,
            "accounts_failed": failures,
            "stopped_reason": stopped_reason,
            "next_state": next_state,
            "account_records": record_counts,
            "pool": {
                **pool,
                "candidates_this_run": len(rows),
            },
            "totals": self._totals(workflows),
            "results": results,
        }
        if target_reached:
            payload["manual_master_return"] = {
                "required": True,
                "from_mid": job.controller_mid,
                "to_original_master_mid": job.original_master_mid,
                "command": f"python main.py guild private return {job.id}",
            }
            payload["next_action"] = {
                "action": "return_master",
                "command": f"python main.py guild private return {job.id}",
                "message": "目标已达到，请将会长交还给原会长。",
            }
        else:
            payload["next_action"] = {
                "action": "prepare_eligible_donors_and_rerun",
                "message": (
                    "目标尚未达到；补充符合条件的 B 账号，或等待账号退出公会满 "
                    "24 小时后，重新执行同一条 guild private 命令。"
                ),
                "requirements": [
                    "ready=1",
                    "invalid=0",
                    "next_stage>30",
                    "当前不在公会",
                    "最近退出公会已满24小时",
                    "未在本任务中使用过",
                ],
            }
        return payload

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
            "complete": complete or job.status == "complete",
            "mode": "private",
            "state": reason,
            "stopped_reason": reason,
            "job": self._job_payload(job),
            "progress": {
                "current": job.effective_count,
                "target": job.total_count_limit,
                "remaining": max(
                    0,
                    job.total_count_limit - job.effective_count,
                ),
                "reached": job.effective_count >= job.total_count_limit,
            },
            "manual_action": None,
            "next_action": None,
        }
        if reason == "awaiting_application_approval":
            payload["manual_action"] = {
                "action": "approve_application_and_transfer_master",
                "application_id": job.application_id,
                "controller_mid": job.controller_mid,
            }
            payload["next_action"] = {
                "action": "approve_and_grant_master",
                "message": (
                    f"手机使用原会长账号批准 {job.controller_mid} 入会，"
                    "然后将会长委任给该账号；完成后重跑同一命令。"
                ),
            }
        elif reason == "awaiting_master_transfer":
            payload["manual_action"] = {
                "action": "transfer_master_to_controller",
                "controller_mid": job.controller_mid,
            }
            payload["next_action"] = {
                "action": "grant_master",
                "message": (
                    f"临时账号 {job.controller_mid} 已入会；手机使用当前会长账号"
                    "将会长委任给它，然后重跑同一命令。"
                ),
            }
        elif reason == "awaiting_master_return":
            payload["manual_action"] = {
                "action": "return_master",
                "from_mid": job.controller_mid,
                "to_original_master_mid": job.original_master_mid,
                "command": f"python main.py guild private return {job.id}",
            }
            payload["next_action"] = {
                "action": "return_master",
                "command": f"python main.py guild private return {job.id}",
                "message": "目标已达到，请执行 return 命令交还会长。",
            }
        elif reason == "controller_not_found":
            payload["next_action"] = {
                "action": "restore_controller_account",
                "message": f"请先把临时会长账号 {job.controller_mid} 补回 SQLite。",
            }
        elif reason == "complete":
            payload["next_action"] = {
                "action": "none",
                "message": "目标已达到且会长已交还，无需继续操作。",
            }
        if current_master is not None:
            payload["current_master"] = {
                "mid": current_master.master_user_id,
                "name": current_master.master_name,
            }
        if error:
            payload["error"] = error
        return payload

    def _return_payload(
        self,
        job: GuildPrivateJobRow,
        *,
        stopped_reason: str,
        ok: bool = True,
        transferred: bool = False,
        already_returned: bool = False,
        error: str = "",
        current_master: Optional[GuildSearchSummary] = None,
    ) -> dict:
        payload = {
            "ok": ok,
            "complete": job.status == "complete",
            "mode": "private_return",
            "stopped_reason": stopped_reason,
            "transferred": transferred,
            "already_returned": already_returned,
            "from_mid": job.controller_mid,
            "to_original_master_mid": job.original_master_mid,
            "job": self._job_payload(job),
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
