from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from dataclasses import asdict
from datetime import datetime
import time
from pathlib import Path

from .auth import AccountState, guest_login, new_device_ids
from .constants import ENDPOINT, FALLBACK_RESOURCE_KEY, TO_STAGE
from .daily_runner import (
    DAILY_ACTION_VERSIONS,
    DAILY_BASE_ACTION_VERSIONS,
    DailyRunner,
    DailyWorkflowResult,
    daily_action_is_complete,
)
from .db import (
    DEFAULT_DB,
    DAILY_TIMEZONE,
    GUILD_COOLDOWN_SECONDS,
    AccountDB,
    AccountRow,
    GuildTargetRow,
    ManagedGuildRow,
)
from .grpc_client import GrpcClient, GrpcError
from .guild import (
    Guild,
    GuildSearchSummary,
    parse_guild_search_response,
)
from .guild_limits import (
    GUILD_DAILY_RECRUITMENT_ACCOUNT_LIMIT,
    GUILD_PRIVATE_DAILY_INVITATION_LIMIT,
    guild_max_member_count,
    parse_guild_daily_recruitment_limit,
)
from .guild_runner import GuildRunner, GuildWorkflowResult
from .guild_private_runner import PrivateGuildRunner
from .guild_resident_runner import ResidentGuildRunner
from .invite import register_friend_inviter
from .inviter import parse_inviter
from .patch_data import fetch_patch_data
from .red_dot import RedDotRunner
from .social import Social, parse_get_user_social_info_response
from .stage_runner import StageConfig, StageRunner

log = logging.getLogger(__name__)


def _setup_log(verbose: bool = False, quiet: bool = False) -> None:
    """Log levels:
    - default: INFO  关键进度（建号/通关汇总/邀请结果）
    - -v:      DEBUG 含每关点位、httpx、可选 StartStage 失败
    - -q:      WARNING 仅异常 + 最终摘要仍 print
    """
    if quiet:
        level = logging.WARNING
    elif verbose:
        level = logging.DEBUG
    else:
        level = logging.INFO

    root = logging.getLogger()
    root.handlers.clear()
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    # 默认关掉 HTTP 库刷屏；-v 时打开
    noisy = ("httpx", "httpcore", "hpack", "h2")
    for name in noisy:
        logging.getLogger(name).setLevel(logging.DEBUG if verbose else logging.WARNING)

    # 业务 logger
    logging.getLogger("crumble_bot").setLevel(level)


def _resolve_inviter(value: str | None, fallback: str = "") -> str:
    raw = (value or fallback or "").strip()
    if not raw:
        raise SystemExit("需要邀请目标 mid 或完整邀请链接")
    try:
        mid = parse_inviter(raw)
    except ValueError as e:
        raise SystemExit(str(e)) from e
    if raw != mid:
        suffix = "..." if len(raw) > 80 else ""
        log.info("inviter=%s (from %s%s)", mid, raw[:80], suffix)
    return mid


def _persist_snapshot(state, cfg_path: Path, state_path: Path) -> None:
    try:
        state.save(state_path)
        state.save_yaml(cfg_path)
    except Exception as e:
        print(f"snapshot warn: {e}", flush=True)


def _samples_dir() -> str:
    return str((Path(__file__).resolve().parent.parent / "data" / "samples"))


def _stage_cfg(samples: str | None = None) -> StageConfig:
    return StageConfig(
        team_index=1,
        clear_points=[0, 1],
        sleep_ms=200,
        samples_dir=samples or _samples_dir(),
        try_start=True,
    )


def _local_timestamp(timestamp: float) -> str:
    if not timestamp:
        return "-"
    return datetime.fromtimestamp(timestamp).astimezone().isoformat(timespec="seconds")


def _guild_pool_payload(status: dict) -> dict:
    payload = dict(status)
    next_available = status.get("next_available_at")
    payload["next_available_local"] = (
        _local_timestamp(float(next_available)) if next_available else None
    )
    return payload


def _guild_human_output(payload: object) -> str:
    """Render the small, actionable summary used by the interactive CLI.

    The command handlers still build the complete structured payload so callers
    can request it with ``guild --json``.  The default terminal view only shows
    the state, progress, failure reason, and the next manual action.
    """
    if not isinstance(payload, dict):
        return str(payload)

    status = payload.get("status")
    status = status if isinstance(status, dict) else {}
    operation_name = ""
    if isinstance(payload.get("fill"), dict) and isinstance(
        payload.get("daily"), dict
    ):
        operation_name = "maintain"
    elif isinstance(payload.get("fill"), dict):
        operation_name = "fill"
    elif isinstance(payload.get("daily"), dict):
        operation_name = "daily"
    elif isinstance(payload.get("support"), dict):
        operation_name = "support"
    elif isinstance(payload.get("before"), dict):
        operation_name = "maintain"
    operation = (
        payload
        if operation_name == "maintain"
        else payload.get(operation_name)
    )
    operation = operation if isinstance(operation, dict) else {}

    raw_mode = str(payload.get("mode") or "guild")
    if raw_mode == "resident" and operation_name:
        mode = f"resident {operation_name}"
    elif raw_mode == "resident_support":
        mode = "resident support"
    elif raw_mode == "guild" and payload.get("day") is not None:
        mode = "resident daily"
    else:
        mode = raw_mode

    ok_value = operation.get("ok") if operation else payload.get("ok", True)
    ok = bool(ok_value)
    complete = bool(payload.get("complete"))
    state = str(
        payload.get("state")
        or payload.get("stopped_reason")
        or operation.get("state")
        or operation.get("stopped_reason")
        or status.get("state")
        or status.get("stopped_reason")
        or ""
    )
    if complete or state in {"complete", "target_already_complete"}:
        result = "完成"
    elif ok:
        result = "已处理"
    else:
        result = "失败"

    lines = [f"公会 {mode}：{result}"]
    guild = payload.get("guild")
    if not isinstance(guild, dict):
        guild = status.get("guild")
    if not isinstance(guild, dict):
        after = payload.get("after")
        guild = after.get("guild") if isinstance(after, dict) else None
    if not isinstance(guild, dict):
        before = payload.get("before")
        guild = before.get("guild") if isinstance(before, dict) else None
    if isinstance(guild, dict):
        name = str(guild.get("name") or guild.get("gname") or "")
        master = str(
            guild.get("master_name")
            or guild.get("original_master_name")
            or ""
        )
        if name or master:
            lines.append(
                f"公会：{name or '-'}"
                + (f"（会长：{master}）" if master else "")
            )
        member_count = guild.get("member_count")
        capacity = guild.get("capacity")
        if member_count is not None or capacity is not None:
            lines.append(
                f"成员：{member_count if member_count is not None else 0}/"
                f"{capacity if capacity is not None else '?'}"
            )
        capacity_source = str(guild.get("capacity_source") or "")
        capacity_level = guild.get("capacity_level")
        if capacity_source == "guild_level":
            lines.append(f"容量来源：公会等级 {capacity_level or '-'}")
        roster = status.get("roster")
        target = (
            roster.get("managed_target")
            if isinstance(roster, dict)
            else guild.get("target_managed_count")
        )
        if target is not None:
            active = (
                roster.get("managed_active")
                if isinstance(roster, dict)
                else None
            )
            if active is not None:
                pending_count = 0
                members = status.get("members")
                if isinstance(members, list):
                    pending_count = sum(
                        1
                        for item in members
                        if isinstance(item, dict)
                        and item.get("member_type") == "managed"
                        and item.get("status")
                        in {"planned", "applied", "invited", "accepted"}
                    )
                remaining = max(0, int(target) - int(active) - pending_count)
                pending_text = (
                    f"，待审批/处理中 {pending_count}，剩余缺口 {remaining}"
                    if pending_count
                    else f"，缺口 {remaining}"
                )
                lines.append(
                    f"常驻：{active}/{target}{pending_text}"
                )
        if guild.get("reserve_slots") is not None:
            lines.append(f"预留位置：{guild.get('reserve_slots', 0)}")

    roster = status.get("roster")
    if isinstance(roster, dict) and operation_name == "fill":
        active = roster.get("managed_active")
        target = roster.get("managed_target")
        if active is not None and target is not None and not isinstance(guild, dict):
            lines.append(f"常驻：{active}/{target}，缺口 {roster.get('vacancy', 0)}")

    fill = payload.get("fill")
    if isinstance(fill, dict):
        requested = fill.get("requested", 0)
        joined = fill.get("joined", 0)
        applied = fill.get("applied", 0)
        filled = fill.get("filled", joined + applied)
        waiting = fill.get("pending_approval", applied)
        attempted = fill.get("attempted", requested)
        failed_fill = fill.get("failed", 0)
        lines.append(
            f"补位：目标 {requested}，尝试 {attempted}，已入会 {joined}，"
            f"待审批 {waiting}，失败 {failed_fill}，完成 {filled}"
        )
        if fill.get("daily_recruit_remaining") is not None:
            lines.append(f"今日招募剩余：{fill['daily_recruit_remaining']}")
        if fill.get("pending_approval"):
            lines.append(f"当前待审批：{fill['pending_approval']} 个")
        pending_validation = fill.get("pending_validation")
        if isinstance(pending_validation, dict):
            lines.append(
                f"待审批校验：有效 {pending_validation.get('confirmed', 0)}，"
                f"失效 {pending_validation.get('invalidated', 0)}，"
                f"失败 {pending_validation.get('failed', 0)}"
            )
        member_names = {}
        members = status.get("members")
        if not isinstance(members, list):
            members = []
        else:
            member_names = {
                str(item.get("mid") or "").upper(): str(item.get("name") or "")
                for item in members
                if isinstance(item, dict) and item.get("mid")
            }
        results = fill.get("results")
        if isinstance(results, list) and results:
            lines.append("本次账号：")
            for item in results:
                if not isinstance(item, dict):
                    continue
                mid = str(item.get("mid") or "-").strip()
                name = str(
                    item.get("name")
                    or member_names.get(mid.upper())
                    or ""
                ).strip()
                label = f"{name}（{mid}）" if name else mid
                if item.get("joined"):
                    state_label = "已入会"
                elif item.get("applied"):
                    state_label = (
                        "已重新申请，待审批"
                        if item.get("reapplied")
                        else "待审批"
                    )
                elif item.get("status") in {
                    "planned",
                    "invited",
                    "accepted",
                }:
                    state_label = "处理中"
                else:
                    cooldown_until = item.get("rejoin_cooldown_until")
                    error = str(item.get("error") or "").strip()
                    if cooldown_until:
                        state_label = (
                            "失败（重新入会冷却至 "
                            f"{_local_timestamp(float(cooldown_until))}）"
                        )
                    elif error:
                        state_label = f"失败（{error}）"
                    else:
                        state_label = "失败"
                lines.append(f"  - {label}：{state_label}")

        live_members = [
            item
            for item in members
            if isinstance(item, dict) and item.get("status") == "active"
        ]
        if live_members:
            controlled_count = sum(
                1 for item in live_members if bool(item.get("controlled"))
            )
            lines.append(
                f"成员控制状态：受控 {controlled_count}，"
                f"非受控 {len(live_members) - controlled_count}"
            )
            for item in live_members:
                mid = str(item.get("mid") or "-").strip()
                name = str(item.get("name") or "").strip()
                label = f"{name}（{mid}）" if name else mid
                control_label = "受控" if item.get("controlled") else "非受控"
                lines.append(f"  - [{control_label}] {label}")

    daily = payload.get("daily")
    if (
        not isinstance(daily, dict)
        and payload.get("day") is not None
        and raw_mode != "resident_support"
    ):
        daily = payload
    if isinstance(daily, dict):
        lines.append(
            f"每日：完成 {daily.get('count', 0)}/{daily.get('attempted', 0)}，"
            f"失败 {daily.get('failed', 0)}"
        )

    support = payload.get("support")
    if not isinstance(support, dict) and raw_mode == "resident_support":
        support = payload
    if isinstance(support, dict):
        lines.append(
            f"支援：完成 {support.get('count', 0)}，"
            f"尝试 {support.get('attempted', 0)}，失败 {support.get('failed', 0)}"
        )

    sync = payload.get("sync")
    if isinstance(sync, dict) and not sync.get("ok", True):
        sync_error = str(sync.get("error") or sync.get("stopped_reason") or "同步失败")
        lines.append(f"同步：失败（{sync_error}）")

    if payload.get("mode") in {"guild_joblist", "private_return_list"}:
        jobs = payload.get("jobs")
        if isinstance(jobs, list):
            lines.append(f"任务数：{len(jobs)}")
            for item in jobs:
                if not isinstance(item, dict):
                    continue
                job_id = item.get("id", "?")
                name = item.get("gname") or item.get("name") or "-"
                job_status = item.get("status") or "-"
                effective = item.get("totalcount")
                target = item.get("requested_totalcount")
                progress = (
                    f"，有效 {effective}/{target}"
                    if effective is not None and target
                    else ""
                )
                lines.append(f"#{job_id} {name}：{job_status}{progress}")

    job = payload.get("job")
    if isinstance(job, dict):
        job_id = job.get("id")
        if job_id is not None:
            lines.append(f"任务：#{job_id}，状态 {job.get('status') or state or '-'}")

    controller = payload.get("controller")
    if isinstance(controller, dict):
        controller_name = str(controller.get("name") or "")
        controller_mid = str(controller.get("mid") or "")
        label = controller_name or controller_mid or "-"
        if controller_name and controller_mid:
            label = f"{controller_name}（{controller_mid}）"
        lines.append(f"代理会长：{label}")

    progress = payload.get("progress")
    if isinstance(progress, dict):
        current = progress.get("current")
        target = progress.get("target")
        remaining = progress.get("remaining")
    else:
        current = payload.get("totalcount")
        target = payload.get("requested_totalcount")
        remaining = payload.get("remaining_totalcount")
    if current is not None or target is not None:
        target_text = str(target) if target not in (None, 0) else "不限"
        line = f"有效次数：{current if current is not None else 0}/{target_text}"
        if remaining is not None:
            line += f"，剩余 {remaining}"
        lines.append(line)

    attempted = payload.get("accounts_attempted")
    failed = payload.get("accounts_failed")
    added = payload.get("account_count_added")
    if attempted is not None or failed is not None:
        line = f"账号：执行 {attempted or 0} 个，失败 {failed or 0} 个"
        if added is not None:
            line += f"，本次完成 {added} 个"
        lines.append(line)

    totals = payload.get("totals")
    if isinstance(totals, dict):
        effective = totals.get("effective_research_count")
        spent = totals.get("diamond_spent")
        if effective is not None or spent is not None:
            bits = []
            if effective is not None:
                bits.append(f"有效 {effective} 次")
            if spent is not None:
                bits.append(f"消耗 {spent} 钻")
            lines.append("本次：" + "，".join(bits))

    if state:
        lines.append(f"状态：{state}")

    error = str(payload.get("error") or operation.get("error") or "")
    if error:
        lines.append(f"原因：{error}")

    next_action = payload.get("next_action")
    if not next_action and operation_name:
        next_action = operation.get("next_action")
    if isinstance(next_action, dict):
        message = str(next_action.get("message") or "")
        command = str(next_action.get("command") or "")
        if message:
            lines.append(f"下一步：{message}")
        if command and command != message:
            lines.append(f"命令：{command}")
    elif isinstance(payload.get("next_action"), str):
        lines.append(f"下一步：{payload['next_action']}")

    manual = payload.get("manual_master_return")
    if isinstance(manual, dict) and manual.get("command"):
        command = str(manual["command"])
        if not any(command in line for line in lines):
            lines.append(f"命令：{command}")

    return "\n".join(lines)


def _print_guild_payload(payload: object, args: argparse.Namespace) -> None:
    """Print JSON for library/tests, or a concise summary for the CLI."""
    if bool(getattr(args, "_human_output", False)):
        print(_guild_human_output(payload), flush=True)
    else:
        print(json.dumps(payload, ensure_ascii=False, indent=2), flush=True)


def _numeric_change(before: int | None, after: int | None) -> int | None:
    if before is None or after is None:
        return None
    return int(after) - int(before)


def _daily_run_totals(workflows: list[DailyWorkflowResult]) -> dict:
    dungeons = [
        workflow.crumble_dungeon
        for workflow in workflows
        if isinstance(workflow.crumble_dungeon, dict)
        and workflow.crumble_dungeon
    ]
    return {
        "login_completed_count": sum(
            1 for workflow in workflows if workflow.login_completed
        ),
        "stage_offline_checked_count": sum(
            1 for workflow in workflows if workflow.stage_rewards.offline.checked
        ),
        "stage_offline_claim_requested_count": sum(
            workflow.stage_rewards.offline.claim_requested_count
            for workflow in workflows
        ),
        "stage_offline_claimed_count": sum(
            workflow.stage_rewards.offline.claimed_count for workflow in workflows
        ),
        "stage_offline_reward_count": sum(
            workflow.stage_rewards.offline.reward_count for workflow in workflows
        ),
        "stage_bonus_checked_count": sum(
            1 for workflow in workflows if workflow.stage_rewards.bonus.checked
        ),
        "stage_bonus_free_claim_requested_count": sum(
            workflow.stage_rewards.bonus.free_claim_requested_count
            for workflow in workflows
        ),
        "stage_bonus_free_claimed_count": sum(
            workflow.stage_rewards.bonus.free_claimed_count
            for workflow in workflows
        ),
        "stage_bonus_advertisement_claim_requested_count": sum(
            workflow.stage_rewards.bonus.advertisement_claim_requested_count
            for workflow in workflows
        ),
        "stage_bonus_advertisement_claimed_count": sum(
            workflow.stage_rewards.bonus.advertisement_claimed_count
            for workflow in workflows
        ),
        "stage_bonus_reward_count": sum(
            workflow.stage_rewards.bonus.reward_count for workflow in workflows
        ),
        "mailbox_checked_count": sum(
            1 for workflow in workflows if workflow.mailbox.checked
        ),
        "mail_count": sum(workflow.mailbox.mail_count for workflow in workflows),
        "mail_claimable_count": sum(
            workflow.mailbox.claimable_count for workflow in workflows
        ),
        "mail_claim_requested_count": sum(
            workflow.mailbox.claim_requested_count for workflow in workflows
        ),
        "mail_claimed_count": sum(
            workflow.mailbox.claimed_count for workflow in workflows
        ),
        "mail_remaining_claimable_count": sum(
            workflow.mailbox.remaining_claimable_count for workflow in workflows
        ),
        "mail_reward_count": sum(
            workflow.mailbox.reward_count for workflow in workflows
        ),
        "mail_advertisement_checked_count": sum(
            1 for workflow in workflows if workflow.mailbox.advertisement.checked
        ),
        "mail_advertisement_claimable_count": sum(
            workflow.mailbox.advertisement.claimable_count for workflow in workflows
        ),
        "mail_advertisement_claim_requested_count": sum(
            workflow.mailbox.advertisement.claim_requested_count
            for workflow in workflows
        ),
        "mail_advertisement_claimed_count": sum(
            workflow.mailbox.advertisement.claimed_count for workflow in workflows
        ),
        "mail_advertisement_remaining_claimable_count": sum(
            workflow.mailbox.advertisement.remaining_claimable_count
            for workflow in workflows
        ),
        "mail_advertisement_reward_count": sum(
            workflow.mailbox.advertisement.reward_count for workflow in workflows
        ),
        "mail_advertisement_diamond_reward_amount": sum(
            workflow.mailbox.advertisement.diamond_reward_amount
            for workflow in workflows
        ),
        "mail_diamond_gained": sum(
            _numeric_change(
                workflow.mailbox.diamond_balance_before,
                workflow.mailbox.diamond_balance_after,
            )
            or 0
            for workflow in workflows
        ),
        "crumble_dungeon_checked_count": len(dungeons),
        "crumble_dungeon_started_count": sum(
            1 for dungeon in dungeons if dungeon.get("started")
        ),
        "crumble_dungeon_finished_count": sum(
            1 for dungeon in dungeons if dungeon.get("finished")
        ),
        "crumble_dungeon_completed_count": sum(
            1
            for dungeon in dungeons
            if dungeon.get("ok") and not dungeon.get("skipped")
        ),
        "crumble_dungeon_skipped_count": sum(
            1 for dungeon in dungeons if dungeon.get("skipped")
        ),
        "crumble_dungeon_failed_count": sum(
            1
            for dungeon in dungeons
            if not dungeon.get("ok", True) and not dungeon.get("skipped")
        ),
        "crumble_dungeon_reward_count": sum(
            int((dungeon.get("result") or {}).get("reward_count") or 0)
            for dungeon in dungeons
        ),
    }


def _guild_run_totals(workflows: list[GuildWorkflowResult]) -> dict:
    def summed_change(before_attr: str, after_attr: str) -> int:
        total = 0
        for workflow in workflows:
            progress = workflow.guild_progress
            change = _numeric_change(
                getattr(progress, before_attr),
                getattr(progress, after_attr),
            )
            if change is not None:
                total += change
        return total

    return {
        "attendance_reward_count": sum(
            1 for workflow in workflows if workflow.attendance_claimed
        ),
        "free_research_count": sum(
            workflow.free_research_count for workflow in workflows
        ),
        "free_effective_count": sum(
            workflow.free_effective_count for workflow in workflows
        ),
        "donation_count": sum(workflow.paid_research_count for workflow in workflows),
        "paid_effective_count": sum(
            workflow.paid_effective_count for workflow in workflows
        ),
        "effective_research_count": sum(
            workflow.effective_research_count for workflow in workflows
        ),
        "diamond_spent": sum(workflow.diamond_spent for workflow in workflows),
        "guild_experience_gained": summed_change(
            "experience_before",
            "experience_after",
        ),
        "member_contribution_gained": summed_change(
            "member_contribution_before",
            "member_contribution_after",
        ),
        "research_point_gained": summed_change(
            "research_point_before",
            "research_point_after",
        ),
        "super_success_count": sum(
            workflow.super_success_count for workflow in workflows
        ),
        "free_super_success_count": sum(
            workflow.free_super_success_count for workflow in workflows
        ),
        "paid_super_success_count": sum(
            workflow.paid_super_success_count for workflow in workflows
        ),
    }


def _guild_overall_progress(workflows: list[GuildWorkflowResult]) -> dict:
    def first_value(attribute: str) -> int | None:
        for workflow in workflows:
            value = getattr(workflow.guild_progress, attribute)
            if value is not None:
                return int(value)
        return None

    def last_value(attribute: str) -> int | None:
        for workflow in reversed(workflows):
            value = getattr(workflow.guild_progress, attribute)
            if value is not None:
                return int(value)
        return None

    level_before = first_value("level_before")
    level_after = last_value("level_after")
    experience_before = first_value("experience_before")
    experience_after = last_value("experience_after")
    research_point_before = first_value("research_point_before")
    research_point_after = last_value("research_point_after")
    return {
        "level_before": level_before,
        "level_after": level_after,
        "level_change": _numeric_change(level_before, level_after),
        "experience_before": experience_before,
        "experience_after": experience_after,
        "experience_gained": _numeric_change(
            experience_before,
            experience_after,
        ),
        "research_point_before": research_point_before,
        "research_point_after": research_point_after,
        "research_point_gained": _numeric_change(
            research_point_before,
            research_point_after,
        ),
    }


def _login_account(row: AccountRow) -> AccountState:
    state = row.to_state()
    if not state.guest_secret:
        raise RuntimeError("missing guest_secret")
    fresh = guest_login(
        guest_secret=state.guest_secret,
        device=state.device,
        inviter_mid=state.inviter_mid,
        resource_key=state.resource_key or FALLBACK_RESOURCE_KEY,
        endpoint=state.endpoint or ENDPOINT,
    )
    if fresh.mid != row.mid:
        raise RuntimeError(
            f"re-login mid mismatch: expected {row.mid}, got {fresh.mid}"
        )
    fresh.next_stage = state.next_stage
    fresh.inviter_mid = state.inviter_mid
    fresh.diamond_balance = state.diamond_balance
    if not fresh.device:
        fresh.device = state.device
    return fresh


def _guild_confirmation_payload(summary: GuildSearchSummary) -> dict:
    payload = asdict(summary)
    payload["join_method_name"] = {
        0: "immediate",
        1: "approval",
    }.get(summary.join_method, f"unknown:{summary.join_method}")
    payload["member_ids"] = None
    payload["member_ids_note"] = "搜索接口只返回成员数；完整成员 ID 列表需账号加入后读取"
    return payload


def _confirm_guild(payload: dict) -> bool:
    name = str(payload.get("name") or "-")
    master = str(payload.get("master_name") or "-")
    level = payload.get("guild_level")
    members = payload.get("member_count")
    join_method = payload.get("join_method_name") or payload.get("join_method")
    details = [f"公会：{name}", f"会长：{master}"]
    if level is not None:
        details.append(f"等级：{level}")
    if members is not None:
        details.append(f"成员：{members}")
    if join_method is not None:
        details.append(f"入会：{join_method}")
    print("目标公会确认：" + "，".join(details), flush=True)
    try:
        answer = input("确认使用这个公会？[y/N]: ").strip().lower()
    except EOFError:
        return False
    return answer in {"y", "yes", "是", "确认"}


def _guild_actor_mid(value: str | None, argument: str = "--mid") -> str:
    mid = str(value or "").strip().upper()
    if not mid:
        raise SystemExit(f"{argument} 不能为空")
    return mid


def _select_private_controller(
    db: AccountDB,
    *,
    exclude_mids: set[str] | None = None,
) -> AccountRow | None:
    """Choose a low-value eligible account for the non-donating controller role."""
    excluded = {mid.upper() for mid in (exclude_mids or set())}
    excluded.update(db.active_private_account_mids())
    excluded.update(db.active_private_controller_mids())
    candidates = [
        row
        for row in db.list_guild_eligible()
        if row.mid not in excluded and bool(row.guest_secret)
    ]
    if not candidates:
        return None
    return min(
        candidates,
        key=lambda row: (
            row.diamond_balance,
            row.created_at,
            row.mid,
        ),
    )


def _private_controller_availability(
    row: AccountRow | None,
    guild_id: str,
    *,
    now: float | None = None,
) -> dict:
    """Check whether a controller can enter or already belongs to a guild.

    Automatic controller selection already uses ``list_guild_eligible``.  A
    controller reused from an older private job used to bypass that query,
    which could select an account still inside the 24-hour rejoin cooldown.
    Keep the check explicit for both automatic reuse and ``--master-mid``.
    """
    if row is None:
        return {"ok": False, "reason": "controller_not_found"}
    if not row.guest_secret:
        return {"ok": False, "reason": "missing_guest_secret"}
    if not row.ready or row.invalid or row.next_stage <= 30:
        return {
            "ok": False,
            "reason": "controller_not_eligible",
            "ready": bool(row.ready),
            "invalid": bool(row.invalid),
            "next_stage": row.next_stage,
        }

    current = time.time() if now is None else float(now)
    if row.guild_joined_at > row.guild_left_at:
        if row.guild_last_id == guild_id:
            return {"ok": True, "reason": "already_in_target_guild"}
        return {
            "ok": False,
            "reason": "currently_in_other_guild",
            "guild_id": row.guild_last_id,
        }

    last_left_at = max(float(row.guild or 0), float(row.guild_left_at or 0))
    cooldown_until = last_left_at + GUILD_COOLDOWN_SECONDS if last_left_at else 0
    if cooldown_until > current:
        return {
            "ok": False,
            "reason": "controller_cooldown",
            "cooldown_until": cooldown_until,
            "cooldown_until_local": _local_timestamp(cooldown_until),
        }
    return {"ok": True, "reason": "cooldown_complete"}


def _private_controller_unavailable_payload(
    mid: str,
    availability: dict,
) -> dict:
    reason = str(availability.get("reason") or "controller_unavailable")
    message = {
        "controller_cooldown": (
            "代理会长仍在入会冷却中，请换一个已结束冷却的账号，或等待到 "
            f"{availability.get('cooldown_until_local', '-')} 后重试。"
        ),
        "currently_in_other_guild": "代理账号当前在其他公会中，无法作为本批次会长。",
        "missing_guest_secret": "代理账号缺少登录凭据。",
        "controller_not_eligible": "代理账号未满足 ready/invalid/next_stage 条件。",
        "controller_not_found": "SQLite 中找不到代理账号。",
    }.get(reason, "代理账号当前不可用。")
    return {
        "ok": False,
        "complete": False,
        "mode": "private",
        "state": reason,
        "stopped_reason": reason,
        "controller": {
            "mid": mid,
            **{
                key: value
                for key, value in availability.items()
                if key not in {"ok", "reason"}
            },
        },
        "next_action": {
            "action": "choose_another_controller",
            "message": message,
            "command": "增加 --master-mid 指定可用代理账号，或等待冷却结束后重跑。",
        },
    }


def _record_private_controller_cooldown(
    db: AccountDB,
    mid: str,
    message: str,
) -> None:
    """Persist a server-reported rejoin deadline for future selection."""
    matched = re.search(
        r"rejoin cooldown ends at\s+([0-9]{4}-[0-9]{2}-[0-9]{2}T[^' ]+)",
        str(message or ""),
        flags=re.IGNORECASE,
    )
    if not matched:
        return
    try:
        deadline = datetime.fromisoformat(
            matched.group(1).rstrip(".,;")
        ).timestamp()
    except ValueError:
        return
    db.mark_guild_left(
        mid,
        left_at=deadline - GUILD_COOLDOWN_SECONDS,
    )


def _private_controller_candidate_error() -> dict:
    return {
        "ok": False,
        "complete": False,
        "mode": "private",
        "state": "controller_candidate_not_found",
        "stopped_reason": "controller_candidate_not_found",
        "error": "没有可作为代理会长的账号",
        "next_action": {
            "action": "prepare_controller_account",
            "message": (
                "请补充一个 ready=1、invalid=0、next_stage>30、当前不在公会且"
                "已结束24小时冷却的账号，或显式传入 --master-mid。"
            ),
        },
    }


def _persist_logged_in_actor(
    db: AccountDB,
    row: AccountRow,
    state: AccountState,
    session,
) -> None:
    state.resource_key = session.resource_key
    db.upsert_state(
        state,
        used=row.used,
        ready=row.ready,
        invalid=row.invalid,
        note=row.note,
    )


def _resident_target(db: AccountDB, args: argparse.Namespace) -> ManagedGuildRow:
    gname = str(getattr(args, "gname", "") or "").strip()
    gmname = str(getattr(args, "gmname", "") or "").strip()
    if not gname:
        raise SystemExit("常驻公会命令必须提供 --gname")
    matches = db.find_managed_guilds(gname, gmname=gmname or None)
    if not matches:
        raise SystemExit(
            f"公会 {gname!r} 尚未初始化，请先执行 "
            "guild --gname <name> init --gmname <master>"
        )
    if len(matches) > 1:
        payload = {
            "ok": False,
            "state": "ambiguous_guild_name",
            "gname": gname,
            "candidates": [
                {
                    "id": item.id,
                    "guild_id": item.guild_id,
                    "gname": item.gname,
                    "gmname": item.gmname,
                }
                for item in matches
            ],
            "next_action": "补充 --gmname 或使用更长的 --gname 前缀",
        }
        raise SystemExit(json.dumps(payload, ensure_ascii=False, indent=2))
    return matches[0]


def _resident_discovery_row(
    db: AccountDB,
    target: ManagedGuildRow | None,
) -> AccountRow | None:
    if target is not None:
        if target.controller_mid:
            row = db.get(target.controller_mid)
            if row is not None and row.guest_secret:
                return row
        for membership in db.list_guild_memberships(target.id, status="active"):
            row = db.get(membership.mid)
            if row is not None and row.guest_secret:
                return row
        candidates = db.list_resident_candidates(target.id)
        if candidates:
            return candidates[0]
    candidates = db.list_guild_eligible()
    return next((row for row in candidates if row.guest_secret), None)


def _resident_search_summary(
    row: AccountRow,
    *,
    gname: str,
    gmname: str,
) -> tuple[AccountState, GuildSearchSummary]:
    state = _login_account(row)
    session = state.to_session()
    with GrpcClient(state.endpoint or ENDPOINT) as client:
        summaries = parse_guild_search_response(
            Guild(client, session).search_guilds(gname).message
        )
    matches = [
        item
        for item in summaries
        if item.name == gname or item.name.startswith(gname)
    ]
    if gmname:
        matches = [item for item in matches if item.master_name == gmname]
    if len(matches) != 1:
        raise RuntimeError(
            f"公会搜索结果不唯一或不存在: gname={gname!r}, gmname={gmname!r}, "
            f"matches={len(matches)}"
        )
    # The login/session layer may refresh the resource key while building the
    # authenticated headers.  Carry that value back to the persisted account
    # state so resident commands do not fall back to an obsolete DB value.
    state.resource_key = session.resource_key
    return state, matches[0]


def _resident_lookup_user_name(state: AccountState, user_id: str) -> str:
    """Resolve a controller's display name using the authenticated actor."""
    target = str(user_id or "").strip().upper()
    if not target:
        return ""
    try:
        session = state.to_session()
        with GrpcClient(state.endpoint or ENDPOINT) as client:
            response = Social(client, session).get_user_social_info((target,))
        state.resource_key = session.resource_key
        infos = parse_get_user_social_info_response(response.message)
        matched = next(
            (item for item in infos if item.user_id.strip().upper() == target),
            None,
        )
        return matched.name if matched is not None else ""
    except Exception as error:
        log.warning("unable to resolve resident controller name for %s: %s", target, error)
        return ""


def _cmd_guild_resident_init(args: argparse.Namespace) -> int:
    gname = str(getattr(args, "gname", "") or "").strip()
    gmname = str(getattr(args, "gmname", "") or "").strip()
    if not gname:
        raise SystemExit("guild init 必须提供 --gname")
    if not gmname:
        raise SystemExit("guild init 必须提供 --gmname")
    capacity_arg = getattr(args, "capacity", None)
    if capacity_arg is not None and int(capacity_arg) < 3:
        raise SystemExit("--capacity 必须 >= 3，至少保留 2 个位置")

    with AccountDB(args.db) as db:
        existing_matches = db.find_managed_guilds(gname, gmname=gmname)
        existing = existing_matches[0] if len(existing_matches) == 1 else None
        discovery = _resident_discovery_row(db, existing)
        if discovery is None:
            payload = {
                "ok": False,
                "mode": "resident",
                "state": "no_discovery_account",
                "stopped_reason": "no_discovery_account",
                "next_action": {
                    "action": "prepare_account",
                    "message": "需要一个 ready=1、invalid=0、next_stage>30 且有登录凭据的账号。",
                },
            }
            _print_guild_payload(payload, args)
            return 1
        try:
            state, summary = _resident_search_summary(
                discovery,
                gname=gname,
                gmname=gmname,
            )
        except Exception as error:
            payload = {
                "ok": False,
                "mode": "resident",
                "state": "guild_search_failed",
                "stopped_reason": "guild_search_failed",
                "error": f"{type(error).__name__}: {error}",
            }
            _print_guild_payload(payload, args)
            return 1

        level_capacity = guild_max_member_count(summary.guild_level)
        capacity = int(
            level_capacity
            if level_capacity is not None
            else (
                capacity_arg
                if capacity_arg is not None
                else (existing.capacity if existing is not None else 0)
            )
        )
        details = {
            **(existing.details if existing is not None else {}),
            "search_summary": _guild_confirmation_payload(summary),
            "capacity_source": (
                "guild_level"
                if level_capacity is not None
                else (
                    "argument"
                    if capacity_arg is not None
                    else (
                        "cached"
                        if existing is not None and existing.capacity
                        else "unknown"
                    )
                )
            ),
            **(
                {"capacity_level": int(summary.guild_level)}
                if level_capacity is not None
                else {}
            ),
        }
        current_master_mid = str(summary.master_user_id or "").strip().upper()
        explicit_controller_mid = str(
            getattr(args, "controller_mid", "")
            or getattr(args, "master_mid", "")
            or ""
        ).strip().upper()

        # Resident private-guild filling uses ApplyGuild from each candidate;
        # the owner approves those applications on the phone.  No proxy/master
        # account is needed for the resident flow.  Keep an explicitly passed
        # controller only as legacy metadata; fill does not use it.
        controller_mid = explicit_controller_mid
        controller_source = "argument" if controller_mid else "none"

        controller_name = ""
        if controller_mid:
            if controller_mid == current_master_mid:
                controller_name = summary.master_name or gmname
            else:
                controller_name = _resident_lookup_user_name(state, controller_mid)
        details["controller_source"] = controller_source
        details["controller_name"] = controller_name
        _persist_logged_in_actor(db, discovery, state, state.to_session())
        target = db.upsert_managed_guild(
            guild_id=summary.guild_id,
            gname=summary.name or gname,
            gmname=summary.master_name or gmname,
            original_master_mid=(
                existing.original_master_mid
                if existing is not None and existing.original_master_mid
                else summary.master_user_id
            ),
            controller_mid=controller_mid,
            join_method=summary.join_method,
            status="initialized",
            guild_level=summary.guild_level,
            capacity=capacity,
            reserve_slots=2,
            member_count=summary.member_count,
            details=details,
        )
        # ``upsert_managed_guild`` intentionally preserves an existing
        # controller when the incoming value is empty for compatibility with
        # older callers.  Resident init is authoritative, so clear any stale
        # controller from the previous proxy-based implementation.
        if not controller_mid and target.controller_mid:
            target = db.update_managed_guild(target.id, controller_mid="")
        for old_membership in db.list_guild_memberships(
            target.id, member_type="reserved"
        ):
            if old_membership.mid != controller_mid:
                db.update_guild_membership(
                    target.id,
                    old_membership.mid,
                    status="retired",
                    last_error="resident flow does not require a controller",
                )
        if controller_mid:
            controller_is_master = bool(
                summary.master_user_id
                and controller_mid == summary.master_user_id.strip().upper()
            )
            db.upsert_guild_membership(
                target.id,
                controller_mid,
                member_type="reserved",
                status="active" if controller_is_master else "planned",
                role=0 if controller_is_master else 1,
                details={
                    "source": "controller",
                    **({"name": controller_name} if controller_name else {}),
                },
            )
        payload = ResidentGuildRunner(db, _login_account).status(target)
        init_ok = bool(capacity)
        payload.update(
            {
                "ok": init_ok,
                "state": "initialized" if capacity else "capacity_unknown",
                "stopped_reason": "" if capacity else "capacity_unknown",
                "init": {
                    "discovery_mid": discovery.mid,
                    "guild_id": summary.guild_id,
                    "capacity": capacity or None,
                    "target_managed_count": target.target_managed_count,
                },
            }
        )
        if not capacity:
            payload["next_action"] = {
                "action": "rerun_init_with_capacity",
                "message": (
                    "暂时无法从公会等级确定容量，请补充 --capacity x 后重跑 init。"
                ),
            }
        else:
            payload["next_action"] = {
                "action": "fill",
                "message": (
                    "init 只初始化配置，不发送邀请；执行 guild --gname <name> fill "
                    "开始补充常驻成员。"
                ),
            }
        _print_guild_payload(payload, args)
    return 0 if init_ok else 1


def _cmd_guild_resident_status(args: argparse.Namespace) -> int:
    with AccountDB(args.db) as db:
        target = _resident_target(db, args)
        runner = ResidentGuildRunner(db, _login_account)
        payload = runner.sync(target)
        current = db.get_managed_guild(target.guild_id) or target
        name_refresh = runner.enrich_member_names(current)
        if not payload.get("ok"):
            payload = {**runner.status(current), **payload}
        payload["name_refresh"] = name_refresh
        _print_guild_payload(payload, args)
    return 0


def _cmd_guild_resident_fill(args: argparse.Namespace) -> int:
    with AccountDB(args.db) as db:
        target = _resident_target(db, args)
        runner = ResidentGuildRunner(db, _login_account)
        # Reconcile first so external/manual members are reflected before we
        # calculate the vacancies.  After submitting applications (or direct
        # joins for a public guild), reconcile again and persist who is truly
        # in the guild; an application is not treated as membership.
        before = runner.sync(target)
        current = db.get_managed_guild(target.guild_id) or target
        reserve_slots = getattr(args, "reserve_slots", None)
        if reserve_slots is not None:
            try:
                current = runner.set_reserve_slots(current, reserve_slots)
            except ValueError as error:
                raise SystemExit(str(error)) from error
        fill = runner.fill(current)
        current = db.get_managed_guild(target.guild_id) or current
        after = runner.sync(current)
        current = db.get_managed_guild(target.guild_id) or current
        name_refresh = runner.enrich_member_names(current)
        payload = {
            "ok": bool(fill.get("ok")),
            "mode": "resident",
            "before": before,
            "fill": fill,
            "after": after,
            "name_refresh": name_refresh,
        }
        current = db.get_managed_guild(target.guild_id) or target
        payload["status"] = runner.status(current)
        _print_guild_payload(payload, args)
    return 0 if payload.get("ok") else 1


def _cmd_guild_resident_daily(args: argparse.Namespace) -> int:
    with AccountDB(args.db) as db:
        target = _resident_target(db, args)
        runner = ResidentGuildRunner(db, _login_account)
        # The phone may have approved applications since the last fill.  Sync
        # first so those rows become active in SQLite before daily selects its
        # member roster; otherwise daily would keep showing stale ``applied``
        # rows and skip the newly accepted accounts.
        sync = runner.sync(target)
        current = db.get_managed_guild(target.guild_id) or target
        payload = runner.daily(current)
        payload["sync"] = sync
        current = db.get_managed_guild(target.guild_id) or current
        payload["status"] = runner.status(current)
        _print_guild_payload(payload, args)
    return 0 if payload.get("ok") else 1


def _cmd_guild_resident_support(args: argparse.Namespace) -> int:
    with AccountDB(args.db) as db:
        target = _resident_target(db, args)
        runner = ResidentGuildRunner(db, _login_account)
        # A member may have been manually approved, joined, or removed since
        # the last resident command.  Refresh first so support uses every
        # login-capable local member and does not include stale rows.
        sync = runner.sync(target)
        target = db.get_managed_guild(target.guild_id) or target
        progress_display = _GuildSupportProgressDisplay(
            sys.stderr,
            enabled=not bool(getattr(args, "quiet", False)),
        )
        try:
            payload = runner.support(target, on_progress=progress_display.update)
        finally:
            progress_display.close()
        payload["sync"] = sync
        current = db.get_managed_guild(target.guild_id) or target
        payload["status"] = runner.status(current)
        _print_guild_payload(payload, args)
    return 0 if payload.get("ok") else 1


class _GuildSupportProgressDisplay:
    """Render live support progress without adding one line per account."""

    def __init__(self, stream, *, enabled: bool = True) -> None:
        self.stream = stream
        self.enabled = enabled
        isatty = getattr(stream, "isatty", None)
        self.interactive = bool(isatty and isatty())
        self.line_open = False

    def update(self, progress: dict) -> None:
        if not self.enabled:
            return
        phase = str(progress.get("phase") or "")
        line = _guild_support_progress_line(progress)
        if self.interactive:
            # Clear and rewrite the current terminal row.  The final event is
            # the only one that advances to a new line.
            final = phase == "done"
            self.stream.write(f"\r\x1b[2K{line}{'\n' if final else ''}")
            self.stream.flush()
            self.line_open = not final
            return

        # Pipes and captured logs cannot redraw a line.  Emit milestones only
        # so long-running jobs remain observable without producing one row per
        # account.
        processed = max(0, int(progress.get("processed") or 0))
        total = max(0, int(progress.get("total") or 0))
        should_emit = phase in {"querying", "queried", "done"} or (
            phase == "account"
            and (processed == 1 or processed == total or processed % 5 == 0)
        )
        if should_emit:
            print(line, file=self.stream, flush=True)

    def close(self) -> None:
        if self.enabled and self.interactive and self.line_open:
            self.stream.write("\n")
            self.stream.flush()
            self.line_open = False


def _guild_support_progress_line(progress: dict) -> str:
    total = max(0, int(progress.get("total") or 0))
    processed = max(0, min(total, int(progress.get("processed") or 0)))
    width = 16
    filled = (
        width
        if total == 0
        else min(width, (width * processed + total - 1) // total)
    )
    bar = "█" * filled + "░" * (width - filled)
    phase = str(progress.get("phase") or "")
    suffix = ""
    if phase == "querying":
        suffix = "查询支援列表…"
    elif phase == "queried":
        suffix = f"待支援 {int(progress.get('request_count') or 0)} 条"
    elif phase == "account":
        identity = str(progress.get("name") or progress.get("mid") or "-")
        status_map = {
            "ok": "成功",
            "failed": "失败",
            "support_limit": "支援已达上限",
            "no_pending_requests": "没有待支援请求",
            "query_failed": "查询失败",
        }
        status = status_map.get(
            str(progress.get("status") or ""),
            str(progress.get("status") or "已处理"),
        )
        suffix = f"{identity}：{status}"
    elif phase == "done":
        suffix = "完成"
        stopped_reason = str(progress.get("stopped_reason") or "")
        if stopped_reason:
            reason_map = {
                "support_limit": "支援已达上限",
                "no_pending_requests": "没有待支援请求",
                "query_failed": "查询失败",
            }
            suffix += f"（{reason_map.get(stopped_reason, stopped_reason)}）"
    return (
        f"支援 [{bar}] {processed}/{total}｜"
        f"成功 {int(progress.get('support_count') or 0)}｜"
        f"失败 {int(progress.get('failed') or 0)}｜{suffix}"
    ).rstrip()


def _cmd_guild_resident_maintain(args: argparse.Namespace) -> int:
    with AccountDB(args.db) as db:
        target = _resident_target(db, args)
        runner = ResidentGuildRunner(db, _login_account)
        payload = runner.maintain(target)
        current = db.get_managed_guild(target.guild_id) or target
        payload["name_refresh"] = runner.enrich_member_names(current)
        payload["status"] = runner.status(current)
        _print_guild_payload(payload, args)
    return 0 if payload.get("ok") else 1


def cmd_list(args: argparse.Namespace) -> int:
    with AccountDB(args.db) as db:
        print(json.dumps(db.count(), indent=2))
        if args.all:
            rows = list(db.iter_all())
        elif args.unused:
            rows = db.list_unused(ready_only=args.ready, limit=args.limit or 0)
        elif args.ready:
            rows = db.list_unused(ready_only=True, limit=args.limit or 0)
        else:
            rows = db.list_unused(ready_only=False, limit=args.limit or 50)
        for r in rows:
            inv = r.inviter_mid or "-"
            print(
                f"{r.mid}\tused={int(r.used)}\tready={int(r.ready)}\tinvalid={int(r.invalid)}\t"
                f"next={r.next_stage}\tinviter={inv}\t"
                f"diamonds={r.diamond_balance}\tdaily={_local_timestamp(r.daily)}\t"
                f"guild_joined={_local_timestamp(r.guild_joined_at)}\t"
                f"guild_left={_local_timestamp(r.guild_left_at or r.guild)}\t"
                f"guild_paid={r.guild_paid_research_total}\t"
                f"guild_effective={r.guild_effective_research_total}\t"
                f"guild_crit={r.guild_super_success_total}\t"
                f"secret={r.guest_secret[:8]}...\temail={r.email}"
            )
    return 0


def cmd_gen(args: argparse.Namespace) -> int:
    """Create guests, clear 1-30, store to sqlite. No invite."""
    n = args.n
    db_path = Path(args.db) if args.db else DEFAULT_DB
    n_label = "inf" if n is None else str(n)
    log.info("gen start db=%s n=%s stages=1..%s", db_path, n_label, TO_STAGE)

    made = 0
    failures = 0
    with AccountDB(db_path) as db:
        while n is None or made < n:
            idx = made + 1
            try:
                device = new_device_ids()
                log.info("[%s] creating guest...", idx)
                state = guest_login(
                    guest_secret="",
                    device=device,
                    inviter_mid="",
                    resource_key=FALLBACK_RESOURCE_KEY,
                    endpoint=ENDPOINT,
                )
                state.inviter_mid = ""
                db.upsert_state(state, used=False, ready=False, note="created")
                log.info("[%s] mid=%s", idx, state.mid)
                log.debug("[%s] guest_secret=%s", idx, state.guest_secret)

                session = state.to_session()
                if not session.resource_key or session.resource_key == "dev-0000000000":
                    session.resource_key = FALLBACK_RESOURCE_KEY
                    state.resource_key = FALLBACK_RESOURCE_KEY

                scfg = _stage_cfg()
                with GrpcClient(ENDPOINT) as client:
                    runner = StageRunner(client, session, scfg)
                    log.debug("[%s] SignUp...", idx)
                    body = runner.signup()
                    state.resource_key = session.resource_key
                    db.upsert_state(state, used=False, ready=False, note="signed_up")
                    log.info(
                        "[%s] SignUp ok resource_key=%s",
                        idx,
                        session.resource_key,
                    )
                    log.debug("[%s] SignUp bytes=%s", idx, len(body))

                    def on_progress(
                        r, _idx=idx, _state=state, _session=session, _db=db
                    ):
                        if r.ok:
                            # 默认只在 Boss(点位最后) 打一行；DEBUG 打每个点
                            if r.start_point == 1 or logging.getLogger().isEnabledFor(
                                logging.DEBUG
                            ):
                                log.info(
                                    "[%s] stage=%s sp=%s -> %s",
                                    _idx,
                                    r.stage,
                                    r.start_point,
                                    r.current_after,
                                )
                            else:
                                log.debug(
                                    "[%s] stage=%s sp=%s -> %s",
                                    _idx,
                                    r.stage,
                                    r.start_point,
                                    r.current_after,
                                )
                        else:
                            log.warning(
                                "[%s] FAIL stage=%s sp=%s %s",
                                _idx,
                                r.stage,
                                r.start_point,
                                r.error,
                            )
                        if r.ok and r.current_after is not None:
                            _state.next_stage = r.current_after
                            _state.resource_key = _session.resource_key
                            _db.upsert_state(
                                _state, used=False, ready=False, note="clearing"
                            )

                    log.info("[%s] clear 1..%s ...", idx, TO_STAGE)
                    results = StageRunner(
                        client, session, scfg, on_progress=on_progress
                    ).clear_range(None, TO_STAGE)

                state.resource_key = session.resource_key
                ok = bool(results) and all(r.ok for r in results)
                ready = (state.next_stage or 0) > TO_STAGE
                db.upsert_state(
                    state,
                    used=False,
                    ready=ready,
                    note="ready" if ready else "clear_incomplete",
                )
                _persist_snapshot(
                    state,
                    Path(f"configs/{state.mid}.yaml"),
                    Path(f"data/accounts/{state.mid}.json"),
                )

                made += 1
                if not ok and not ready:
                    failures += 1
                    log.warning(
                        "[%s] FAIL mid=%s next=%s (stored)",
                        idx,
                        state.mid,
                        state.next_stage,
                    )
                else:
                    prog = f"{made}/{n}" if n is not None else str(made)
                    log.info(
                        "[%s] DONE mid=%s next=%s ready=%s progress=%s",
                        idx,
                        state.mid,
                        state.next_stage,
                        int(ready),
                        prog,
                    )
            except KeyboardInterrupt:
                log.warning("interrupted")
                print(
                    json.dumps(
                        {"made": made, "failures": failures, **db.count()}, indent=2
                    ),
                    flush=True,
                )
                return 130
            except Exception as e:
                failures += 1
                log.error("[%s] %s: %s", idx, type(e).__name__, e)
                if args.stop_on_error:
                    print(
                        json.dumps(
                            {"made": made, "failures": failures, **db.count()}, indent=2
                        ),
                        flush=True,
                    )
                    return 1
                continue

        summary = {"made": made, "failures": failures, **db.count(), "db": str(db_path)}
        print(json.dumps(summary, indent=2, ensure_ascii=False), flush=True)
    return 0 if failures == 0 else 1


def cmd_inv(args: argparse.Namespace) -> int:
    """From sqlite pool: take unused account(s) -> login -> invite -> write back."""
    inviter = _resolve_inviter(getattr(args, "target", None))
    db_path = Path(args.db) if getattr(args, "db", None) else DEFAULT_DB
    require_ready = not getattr(args, "any", False)
    count = int(getattr(args, "count", 1) or 1)
    if count < 1:
        raise SystemExit("count 必须 >= 1")

    results = []
    ok_n = fail_n = 0

    def _one(db: AccountDB, idx: int) -> dict:
        row = db.claim_unused(require_ready=require_ready)
        if row is None and require_ready:
            row = db.claim_unused(require_ready=False)
            if row is not None:
                log.warning(
                    "[%s] no ready unused, using mid=%s next=%s",
                    idx,
                    row.mid,
                    row.next_stage,
                )
        if row is None:
            return {"ok": False, "error": "no_unused_account", "idx": idx}

        log.info(
            "[%s] claim mid=%s ready=%s next=%s inviter=%s",
            idx,
            row.mid,
            int(row.ready),
            row.next_stage,
            inviter,
        )
        state = row.to_state()
        state.inviter_mid = inviter

        try:
            if not state.guest_secret:
                raise RuntimeError("missing guest_secret")
            fresh = guest_login(
                guest_secret=state.guest_secret,
                device=state.device,
                inviter_mid=inviter,
                resource_key=state.resource_key or FALLBACK_RESOURCE_KEY,
                endpoint=ENDPOINT,
            )
            fresh.next_stage = state.next_stage
            fresh.inviter_mid = inviter
            if not fresh.device:
                fresh.device = state.device
            state = fresh
        except Exception as e:
            db.mark_invalid(row.mid, note=f"login_fail:{e}")
            return {
                "ok": False,
                "mid": row.mid,
                "error": f"login_fail:{e}",
                "invalid": True,
                "idx": idx,
            }

        db.upsert_state(
            state, used=False, ready=row.ready, invalid=False, note="logged_in"
        )
        session = state.to_session()
        if not session.resource_key or session.resource_key == "dev-0000000000":
            session.resource_key = FALLBACK_RESOURCE_KEY
            state.resource_key = FALLBACK_RESOURCE_KEY

        try:
            with GrpcClient(ENDPOINT) as client:
                try:
                    StageRunner(client, session, _stage_cfg()).signup()
                except GrpcError as e:
                    log.debug("[%s] signup note: %s", idx, e)
                state.resource_key = session.resource_key

                log.info("[%s] invite %s <- %s ...", idx, inviter, state.mid)
                resp = register_friend_inviter(client, session, inviter)
                state.resource_key = session.resource_key
                state.inviter_mid = inviter
                db.mark_invited(state.mid, inviter, state=state)
                _persist_snapshot(
                    state,
                    Path(f"configs/{state.mid}.yaml"),
                    Path(f"data/accounts/{state.mid}.json"),
                )
                return {
                    "ok": True,
                    "mid": state.mid,
                    "inviter_mid": inviter,
                    "invite_bytes": len(resp.message),
                    "resource_key": state.resource_key,
                    "used": True,
                    "invalid": False,
                    "idx": idx,
                }
        except GrpcError as e:
            note = f"invite_fail:{e}"
            db.mark_invalid(state.mid, note=note)
            state.inviter_mid = inviter
            db.upsert_state(state, used=True, invalid=True, note=note)
            return {
                "ok": False,
                "mid": state.mid,
                "inviter_mid": inviter,
                "error": note,
                "used": True,
                "invalid": True,
                "idx": idx,
            }
        except Exception as e:
            note = f"invite_error:{type(e).__name__}:{e}"
            db.mark_invalid(state.mid, note=note)
            return {
                "ok": False,
                "mid": state.mid,
                "error": note,
                "invalid": True,
                "idx": idx,
            }

    with AccountDB(db_path) as db:
        for i in range(1, count + 1):
            item = _one(db, i)
            results.append(item)
            if item.get("ok"):
                ok_n += 1
                log.info("[%s] OK mid=%s inviter=%s", i, item.get("mid"), inviter)
            else:
                fail_n += 1
                if item.get("error") == "no_unused_account":
                    log.warning("[%s] pool empty, stop", i)
                    break
                log.error(
                    "[%s] FAIL mid=%s err=%s", i, item.get("mid"), item.get("error")
                )

        summary = {
            "ok": fail_n == 0 and ok_n > 0,
            "requested": count,
            "ok_n": ok_n,
            "fail_n": fail_n,
            "inviter_mid": inviter,
            "results": results,
            "db": str(db_path),
            **db.count(),
        }
        print(json.dumps(summary, indent=2, ensure_ascii=False), flush=True)

    if ok_n == 0 and any(r.get("error") == "no_unused_account" for r in results):
        return 2
    return 0 if fail_n == 0 and ok_n > 0 else 1


def cmd_daily(args: argparse.Namespace) -> int:
    """Run daily actions for every ready account not completed today."""
    results: list[dict] = []
    workflows: list[DailyWorkflowResult] = []
    completed = 0
    failures = 0
    attempted = 0

    with AccountDB(args.db) as db:
        daily_pool_options = {
            "required_action_versions": DAILY_ACTION_VERSIONS,
            "legacy_action_versions": DAILY_BASE_ACTION_VERSIONS,
        }
        pool = db.daily_pool_status(**daily_pool_options)
        eligible_rows = db.list_daily_accounts(**daily_pool_options)
        if not eligible_rows:
            stopped_reason = (
                "all_accounts_completed_today"
                if pool["total"] > 0 and pool["completed_today"] == pool["total"]
                else "no_eligible_accounts"
            )
            summary = {
                "ok": True,
                "count": 0,
                "attempted": 0,
                "failed": 0,
                "skipped_today": pool["completed_today"],
                "stopped_reason": stopped_reason,
                "pool": pool,
                "pool_after": pool,
                "totals": _daily_run_totals([]),
                "results": [],
            }
            print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
            return 0

        for index, row in enumerate(eligible_rows, start=1):
            attempted += 1
            log.info(
                "[%s] daily SOP mid=%s ready=%s next=%s",
                index,
                row.mid,
                int(row.ready),
                row.next_stage,
            )

            try:
                daily_state = db.prepare_daily_state(
                    row,
                    legacy_action_versions=DAILY_BASE_ACTION_VERSIONS,
                )
                # Persist the normalized day document before the first RPC so
                # legacy same-day completion flags and partial progress survive
                # a process interruption.
                db.save_daily_state(row.mid, daily_state)
                state = _login_account(row)
                session = state.to_session()

                def persist_balance(
                    balance: int,
                    *,
                    _state=state,
                    _session=session,
                    _row=row,
                ) -> None:
                    _state.diamond_balance = balance
                    _state.resource_key = _session.resource_key
                    db.upsert_state(
                        _state,
                        used=_row.used,
                        ready=_row.ready,
                        invalid=_row.invalid,
                        note=_row.note,
                    )

                def persist_action(
                    action_key: str,
                    action_state: dict,
                    *,
                    _mid=row.mid,
                    _day=pool["day"],
                ) -> None:
                    db.update_daily_action_state(
                        _mid,
                        day_key=_day,
                        action_key=action_key,
                        action_state=action_state,
                    )

                db.upsert_state(
                    state,
                    used=row.used,
                    ready=row.ready,
                    invalid=row.invalid,
                    note=row.note,
                )
                with GrpcClient(state.endpoint or ENDPOINT) as client:
                    daily_runner = DailyRunner(
                        client,
                        session,
                        on_balance=persist_balance,
                    )
                    # Keep the switch as an attribute assignment so older
                    # test/integration runners with the original constructor
                    # signature remain compatible.
                    daily_runner.include_crumble_dungeon = True
                    daily_runner.daily_action_state = daily_state
                    daily_runner.on_action_completed = persist_action
                    workflow = daily_runner.run()
                    workflows.append(workflow)

                state.resource_key = session.resource_key
                if workflow.diamond_balance_final is not None:
                    state.diamond_balance = workflow.diamond_balance_final
                db.upsert_state(
                    state,
                    used=row.used,
                    ready=row.ready,
                    invalid=row.invalid,
                    note=row.note,
                )

                item = {"mid": row.mid, **workflow.to_dict()}
                if workflow.ok:
                    # Compatibility runners may return the legacy aggregate
                    # result without per-action states.  A successful aggregate
                    # run still satisfies every action in the current SOP.
                    persisted = db.prepare_daily_state(
                        db.get(row.mid) or row,
                        legacy_action_versions=DAILY_BASE_ACTION_VERSIONS,
                    )
                    persisted_actions = persisted.setdefault("actions", {})
                    for action_key, version in DAILY_ACTION_VERSIONS.items():
                        if daily_action_is_complete(
                            persisted_actions, action_key, version
                        ):
                            continue
                        timestamp = time.time()
                        persisted_actions[action_key] = {
                            "version": int(version),
                            "status": "done",
                            "completed_at": timestamp,
                            "updated_at": timestamp,
                            "source": "aggregate_workflow_compat",
                        }
                    db.save_daily_state(row.mid, persisted)
                    completed_at = db.mark_daily_completed(row.mid)
                    item["daily_completed_at"] = _local_timestamp(completed_at)
                    completed += 1
                    log.info(
                        "[%s] daily SOP complete mid=%s count=%s",
                        index,
                        row.mid,
                        completed,
                    )
                else:
                    failures += 1
                    log.error(
                        "[%s] daily SOP failed mid=%s error=%s",
                        index,
                        row.mid,
                        workflow.error,
                    )
                results.append(item)
            except Exception as error:
                failures += 1
                item = {
                    "ok": False,
                    "mid": row.mid,
                    "error": f"{type(error).__name__}: {error}",
                }
                results.append(item)
                log.error(
                    "[%s] daily account failed mid=%s error=%s",
                    index,
                    row.mid,
                    item["error"],
                )

        pool_after = db.daily_pool_status(**daily_pool_options)
        summary = {
            "ok": failures == 0,
            "count": completed,
            "attempted": attempted,
            "failed": failures,
            "skipped_today": pool["completed_today"],
            "stopped_reason": "all_eligible_accounts_attempted",
            "pool": pool,
            "pool_after": pool_after,
            "totals": _daily_run_totals(workflows),
            "results": results,
        }
        print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)

    return 0 if failures == 0 else 1


def cmd_reddot(args: argparse.Namespace) -> int:
    """Clear zero-cost, server-actionable red dots for one account."""
    with AccountDB(args.db) as db:
        row = db.get(args.mid) if args.mid else None
        if args.mid and row is None:
            raise SystemExit(f"sqlite 中没有账号 {args.mid}")
        if row is None:
            row = next(
                (
                    candidate
                    for candidate in db.iter_all()
                    if not candidate.invalid and candidate.guest_secret
                ),
                None,
            )
        if row is None:
            raise SystemExit("sqlite 中没有可登录账号")
        if row.invalid:
            raise SystemExit(f"账号 {row.mid} 已标记 invalid")

        log.info("reddot login mid=%s", row.mid)
        state = _login_account(row)
        session = state.to_session()
        log.info("reddot loading live patch data")
        patch_data = fetch_patch_data()
        if patch_data.resource_key != session.resource_key:
            session.resource_key = patch_data.resource_key
            state.resource_key = patch_data.resource_key

        with GrpcClient(state.endpoint or ENDPOINT) as client:
            payload = RedDotRunner(client, session, patch_data).run()

        state.resource_key = session.resource_key
        for asset in payload.get("assets_after", []):
            if asset.get("tag") == "ITEMHARDCODINGTAG_CRYSTAL":
                state.diamond_balance = int(asset.get("amount") or 0)
                break
        db.upsert_state(
            state,
            used=row.used,
            ready=row.ready,
            invalid=row.invalid,
            note=row.note,
        )

    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2), flush=True)
    else:
        status = "完成" if payload.get("ok") else "部分失败"
        print(f"红点清理：{status}")
        print(f"账号：{payload.get('name') or '-'}（{payload['mid']}）")
        print(
            "动作：发现 {detected}，执行 {attempted}，失败 {failed}，"
            "剩余安全红点 {remaining}".format(
                detected=payload.get("actions_detected", 0),
                attempted=payload.get("actions_attempted", 0),
                failed=payload.get("actions_failed", 0),
                remaining=payload.get("remaining_safe_count", 0),
            )
        )
        gains = payload.get("gains", [])
        print("收益：")
        if gains:
            for item in gains:
                name = item.get("name") or item.get("tag") or item.get("data_id")
                amount = int(item.get("amount") or 0)
                print(f"  - {name}（{item['data_id']}）：{amount:+d}")
        else:
            print("  - 无资产变化")
        print("执行项：")
        for action in payload.get("actions", []):
            if not action.get("attempted"):
                continue
            marker = "成功" if action.get("ok") else "失败"
            count = int(action.get("detected_count") or 0)
            suffix = f"，发现 {count}" if count else ""
            if action.get("error"):
                suffix += f"，{action['error']}"
            print(f"  - {action.get('key')}：{marker}{suffix}")
    return 0 if payload.get("ok") else 1


def cmd_guild(args: argparse.Namespace) -> int:
    """Dispatch transient public/private SOPs or resident guild management."""
    action = str(getattr(args, "guild_action", "") or "")
    private_action = str(getattr(args, "private_action", "") or "")
    if not action and str(getattr(args, "gname", "") or "").strip():
        action = "status"
    resident_actions = {"init", "status", "fill", "daily", "support", "maintain"}
    if action in resident_actions:
        if private_action or getattr(args, "private_job_id", None) is not None:
            raise SystemExit("常驻公会命令不接受 private return 参数")
        if getattr(args, "count", None) is not None:
            raise SystemExit("常驻公会命令不接受 --count")
        if getattr(args, "totalcount", None) is not None:
            raise SystemExit("常驻公会命令不接受 --totalcount")
        if bool(getattr(args, "confirm", False)):
            raise SystemExit("常驻公会命令不接受 --confirm")
        if action != "fill" and getattr(args, "reserve_slots", None) is not None:
            raise SystemExit("--reserve-slots 只能用于 guild fill")
        if action == "init":
            return _cmd_guild_resident_init(args)
        if action == "status":
            return _cmd_guild_resident_status(args)
        if action == "fill":
            return _cmd_guild_resident_fill(args)
        if action == "daily":
            return _cmd_guild_resident_daily(args)
        if action == "support":
            return _cmd_guild_resident_support(args)
        return _cmd_guild_resident_maintain(args)
    if action == "joblist":
        if private_action or getattr(args, "private_job_id", None) is not None:
            raise SystemExit("guild joblist 不接受 private return 参数")
        unsupported = [
            name
            for name in ("gname", "gmname", "count", "totalcount", "master_mid")
            if getattr(args, name, None) not in (None, "")
        ]
        if unsupported:
            formatted = ", ".join(f"--{name.replace('_', '-')}" for name in unsupported)
            raise SystemExit(f"guild joblist 不接受参数: {formatted}")
        if bool(getattr(args, "confirm", False)):
            raise SystemExit("--confirm 只能用于 guild private")
        if getattr(args, "reserve_slots", None) is not None:
            raise SystemExit("--reserve-slots 只能用于 guild fill")
        return _cmd_guild_joblist(args)
    if getattr(args, "reserve_slots", None) is not None:
        raise SystemExit("--reserve-slots 只能用于 guild fill")
    if action == "private":
        if private_action == "return":
            if bool(getattr(args, "confirm", False)):
                raise SystemExit("--confirm 只能用于 guild private 主流程")
            return _cmd_guild_private_return(args)
        return _cmd_guild_private(args)
    if action == "public":
        if private_action:
            raise SystemExit("guild private return 只能用于 private 流程")
        if bool(getattr(args, "confirm", False)):
            raise SystemExit("--confirm 只能用于 guild private")
        return _cmd_guild_run(args)
    raise SystemExit(f"未知 guild 动作: {action}")


def _guild_target_join_method(details: dict) -> int | None:
    candidates = [
        details.get("search_summary"),
        (details.get("accepted_invitation") or {}).get("guild"),
    ]
    for candidate in candidates:
        if isinstance(candidate, dict) and "join_method" in candidate:
            try:
                return int(candidate["join_method"])
            except (TypeError, ValueError):
                pass
    return None


def _refresh_cached_guild_target(
    db: AccountDB,
    row: AccountRow,
    target: GuildTargetRow,
) -> tuple[GuildTargetRow, GuildSearchSummary, AccountState]:
    """Refresh mutable guild settings while preserving the confirmed guild ID."""
    state = _login_account(row)
    session = state.to_session()
    with GrpcClient(state.endpoint or ENDPOINT) as client:
        summaries = parse_guild_search_response(
            Guild(client, session).search_guilds(target.gname).message
        )
    _persist_logged_in_actor(db, row, state, session)

    matches = [item for item in summaries if item.guild_id == target.guild_id]
    if len(matches) != 1:
        raise RuntimeError(
            "在线搜索无法按缓存 guild_id 唯一定位目标公会: "
            f"guild_id={target.guild_id}, matches={len(matches)}"
        )

    matched = matches[0]
    details = dict(target.details)
    details["search_summary"] = _guild_confirmation_payload(matched)
    refreshed = db.upsert_guild_target(
        gname=target.gname,
        gmname=target.gmname,
        guild_id=matched.guild_id,
        guild_level=matched.guild_level,
        member_count=matched.member_count,
        master_user_id=matched.master_user_id,
        original_master_mid=(
            target.original_master_mid or target.master_user_id
        ),
        details=details,
    )
    return refreshed, matched, state


def _private_cli_job_payload(job) -> dict:
    return {
        "id": job.id,
        "status": job.status,
        "guild_id": job.guild_id,
        "gname": job.gname,
        "original_master_name": job.gmname,
        "original_master_mid": job.original_master_mid,
        "controller_mid": job.controller_mid,
        "count": job.effective_count_per_account,
        "requested_totalcount": job.requested_totalcount,
        "totalcount": job.effective_count,
        "account_limit": (
            None
            if job.has_total_count_limit
            else GUILD_DAILY_RECRUITMENT_ACCOUNT_LIMIT
        ),
    }


def _private_job_day_key(job) -> str:
    """Return the Asia/Shanghai day on which a private batch completed.

    Completed jobs are retained as history.  A totalcount target is a daily
    batch, so a completed job from a previous local day must not suppress the
    next day's invocation.
    """
    timestamp = float(job.completed_at or job.updated_at or job.created_at or 0)
    if not timestamp:
        return ""
    return datetime.fromtimestamp(timestamp, DAILY_TIMEZONE).date().isoformat()


def _private_current_day_key() -> str:
    return datetime.now(DAILY_TIMEZONE).date().isoformat()


def _private_return_job_payload(job) -> dict:
    return {
        **_private_cli_job_payload(job),
        "master_acquired_at": job.master_acquired_at,
        "master_acquired_at_local": _local_timestamp(job.master_acquired_at),
        "updated_at": job.updated_at,
        "updated_at_local": _local_timestamp(job.updated_at),
        "error": job.error,
        "return_command": f"python main.py guild private return {job.id}",
    }


def _guild_joblist_item_payload(db: AccountDB, job) -> dict:
    accounts = db.list_private_accounts(job.id)
    account_states: dict[str, int] = {}
    for account in accounts:
        state = str(account["state"])
        account_states[state] = account_states.get(state, 0) + 1
    completed_account_count = account_states.get("complete", 0)
    remaining = (
        max(0, job.total_count_limit - job.effective_count)
        if job.has_total_count_limit
        else None
    )
    return {
        **_private_cli_job_payload(job),
        "application_id": job.application_id,
        "remaining_totalcount": remaining,
        "totalcount_reached": bool(
            job.has_total_count_limit
            and job.effective_count >= job.total_count_limit
        ),
        "master_acquired_at": job.master_acquired_at,
        "master_acquired_at_local": _local_timestamp(job.master_acquired_at),
        "completed_at": job.completed_at,
        "completed_at_local": _local_timestamp(job.completed_at),
        "created_at": job.created_at,
        "created_at_local": _local_timestamp(job.created_at),
        "updated_at": job.updated_at,
        "updated_at_local": _local_timestamp(job.updated_at),
        "account_count": len(accounts),
        "completed_account_count": completed_account_count,
        "account_limit_reached": bool(
            not job.has_total_count_limit
            and completed_account_count
            >= GUILD_DAILY_RECRUITMENT_ACCOUNT_LIMIT
        ),
        "account_states": account_states,
        "return_pending": job.status == "awaiting_master_return",
        "return_command": f"python main.py guild private return {job.id}",
        "error": job.error,
    }


def _cmd_guild_joblist(args: argparse.Namespace) -> int:
    """List every private guild job from SQLite."""
    with AccountDB(args.db) as db:
        jobs = db.list_private_jobs()
        status_counts: dict[str, int] = {}
        for job in jobs:
            status_counts[job.status] = status_counts.get(job.status, 0) + 1
        payload = {
            "ok": True,
            "mode": "guild_joblist",
            "db": str(Path(args.db).expanduser().resolve()),
            "index_field": "guild_private_jobs.id",
            "count": len(jobs),
            "pending_master_return_count": sum(
                1 for job in jobs if job.status == "awaiting_master_return"
            ),
            "status_counts": status_counts,
            "jobs": [_guild_joblist_item_payload(db, job) for job in jobs],
        }
    _print_guild_payload(payload, args)
    return 0


def _cmd_guild_private_return(args: argparse.Namespace) -> int:
    """List pending master returns or execute one by private-job row id."""
    job_id = getattr(args, "private_job_id", None)
    if job_id is not None and int(job_id) < 1:
        raise SystemExit("guild_private_jobs.id 必须 >= 1")

    with AccountDB(args.db) as db:
        if job_id is None:
            jobs = db.list_private_jobs(status="awaiting_master_return")
            payload = {
                "ok": True,
                "mode": "private_return_list",
                "index_field": "guild_private_jobs.id",
                "count": len(jobs),
                "jobs": [_private_return_job_payload(job) for job in jobs],
            }
            _print_guild_payload(payload, args)
            return 0

        job = db.get_private_job(int(job_id))
        if job is None:
            payload = {
                "ok": False,
                "mode": "private_return",
                "stopped_reason": "private_job_not_found",
                "job_id": int(job_id),
                "error": f"guild_private_jobs 中没有 id={int(job_id)} 的记录",
            }
            _print_guild_payload(payload, args)
            return 1

        runner = PrivateGuildRunner(
            db,
            _login_account,
            client_factory=GrpcClient,
        )
        try:
            payload = runner.return_master(job)
        except Exception as error:
            message = f"{type(error).__name__}: {error}"
            db.update_private_job(job.id, error=message)
            payload = {
                "ok": False,
                "complete": False,
                "mode": "private_return",
                "stopped_reason": "master_return_failed",
                "job_id": job.id,
                "error": message,
            }

    _print_guild_payload(payload, args)
    return 0 if payload.get("ok") else 1


def _cmd_guild_private(args: argparse.Namespace) -> int:
    """Run or resume an approval-guild job using a temporary controlled master."""
    gname = str(getattr(args, "gname", "") or "").strip()
    gmname = str(getattr(args, "gmname", "") or "").strip()
    controller_value = str(getattr(args, "master_mid", "") or "").strip()
    controller_mid = (
        _guild_actor_mid(controller_value, "--master-mid")
        if controller_value
        else ""
    )
    controller_source = "argument" if controller_mid else ""
    confirm_update = bool(getattr(args, "confirm", False))
    parameter_update: dict | None = None
    if getattr(args, "count", None) is None:
        raise SystemExit("guild private 必须提供 --count")
    count = int(args.count)
    totalcount_value = getattr(args, "totalcount", None)
    totalcount = int(totalcount_value) if totalcount_value is not None else 0
    if not gname:
        raise SystemExit("--gname 不能为空")
    if not gmname:
        raise SystemExit("--gmname 不能为空")
    if count < 1:
        raise SystemExit("--count 必须 >= 1")
    if totalcount_value is not None and totalcount < 1:
        raise SystemExit("--totalcount 必须 >= 1")
    daily_capacity = count * GUILD_PRIVATE_DAILY_INVITATION_LIMIT
    if totalcount and daily_capacity < totalcount:
        minimum_count = (
            totalcount + GUILD_PRIVATE_DAILY_INVITATION_LIMIT - 1
        ) // GUILD_PRIVATE_DAILY_INVITATION_LIMIT
        raise SystemExit(
            f"--totalcount={totalcount} 超出 guild private 单日有效上限："
            f"--count={count} × 每日最多邀请 "
            f"{GUILD_PRIVATE_DAILY_INVITATION_LIMIT} 个账号 = {daily_capacity}；"
            f"请将 --totalcount 调整为 <= {daily_capacity}，"
            f"或将 --count 调整为 >= {minimum_count}"
        )

    with AccountDB(args.db) as db:
        target = db.get_guild_target(gname, gmname)
        if not controller_mid and target is not None:
            active_jobs = db.list_active_private_jobs_for_guild(target.guild_id)
            if len(active_jobs) > 1:
                payload = {
                    "ok": False,
                    "complete": False,
                    "mode": "private",
                    "state": "controller_ambiguous",
                    "stopped_reason": "controller_ambiguous",
                    "controllers": [job.controller_mid for job in active_jobs],
                    "next_action": {
                        "action": "specify_controller",
                        "message": "该公会存在多个未完成任务，请使用 --master-mid 指定代理会长。",
                    },
                }
                _print_guild_payload(payload, args)
                return 1
            if active_jobs:
                controller_mid = active_jobs[0].controller_mid
                controller_source = "active_job"
            else:
                latest = db.get_latest_private_job_for_guild(target.guild_id)
                if (
                    latest is not None
                    and latest.status == "complete"
                    and latest.effective_count_per_account == count
                    and latest.total_count_limit == totalcount
                ):
                    controller_mid = latest.controller_mid
                    controller_source = "latest_job"

        if not controller_mid:
            excluded = set()
            if target is not None:
                excluded.add(target.original_master_mid or target.master_user_id)
            selected = _select_private_controller(db, exclude_mids=excluded)
            if selected is None:
                _print_guild_payload(_private_controller_candidate_error(), args)
                return 1
            controller_mid = selected.mid
            controller_source = "auto"

        controller_row = db.get(controller_mid)
        if controller_row is None:
            payload = {
                "ok": False,
                "complete": False,
                "mode": "private",
                "state": "controller_not_found",
                "stopped_reason": "controller_not_found",
                "error": f"sqlite 中没有自控会长账号: {controller_mid}",
                "next_action": {
                    "action": "restore_controller_account",
                    "message": f"请先把临时会长账号 {controller_mid} 补回 SQLite。",
                },
            }
            _print_guild_payload(payload, args)
            return 1

        if target is None:
            state = _login_account(controller_row)
            session = state.to_session()
            with GrpcClient(state.endpoint or ENDPOINT) as client:
                summaries = parse_guild_search_response(
                    Guild(client, session).search_guilds(gname).message
                )
            _persist_logged_in_actor(
                db,
                controller_row,
                state,
                session,
            )
            matches = [
                item
                for item in summaries
                if item.name == gname and item.master_name == gmname
            ]
            if len(matches) != 1:
                _print_guild_payload(
                    {
                        "ok": False,
                        "mode": "private",
                        "state": "guild_target_not_unique",
                        "stopped_reason": "guild_target_not_unique",
                        "match_count": len(matches),
                        "search_result_count": len(summaries),
                        "next_action": {
                            "action": "verify_guild_identity",
                            "message": "请核对 --gname 和 --gmname，确保只匹配一个公会。",
                        },
                    },
                    args,
                )
                return 1
            matched = matches[0]
            confirmation = _guild_confirmation_payload(matched)
            if not _confirm_guild(confirmation):
                _print_guild_payload(
                    {
                        "ok": True,
                        "mode": "private",
                        "state": "not_confirmed",
                        "stopped_reason": "not_confirmed",
                        "next_action": {
                            "action": "confirm_guild",
                            "message": "确认目标信息后，重跑原命令并输入 y。",
                        },
                    },
                    args,
                )
                return 0
            if matched.join_method != 1:
                _print_guild_payload(
                    {
                        "ok": False,
                        "mode": "private",
                        "state": "guild_is_public",
                        "stopped_reason": "guild_is_public",
                        "join_method": matched.join_method,
                        "next_action": {
                            "action": "use_public_flow",
                            "message": "该公会可直接加入，请改用 guild public。",
                        },
                    },
                    args,
                )
                return 1
            target = db.upsert_guild_target(
                gname=gname,
                gmname=gmname,
                guild_id=matched.guild_id,
                guild_level=matched.guild_level,
                member_count=matched.member_count,
                master_user_id=matched.master_user_id,
                original_master_mid=matched.master_user_id,
                details={"search_summary": confirmation},
            )
        else:
            join_method = _guild_target_join_method(target.details)
            if join_method == 0:
                try:
                    target, refreshed_summary, _ = _refresh_cached_guild_target(
                        db,
                        controller_row,
                        target,
                    )
                    join_method = refreshed_summary.join_method
                    log.info(
                        "refreshed cached guild mode name=%s guild_id=%s "
                        "join_method=%s",
                        gname,
                        target.guild_id,
                        join_method,
                    )
                except Exception as error:
                    _print_guild_payload(
                        {
                            "ok": False,
                            "mode": "private",
                            "state": "guild_mode_refresh_failed",
                            "stopped_reason": "guild_mode_refresh_failed",
                            "cached_join_method": 0,
                            "error": f"{type(error).__name__}: {error}",
                            "next_action": {
                                "action": "retry_guild_search",
                                "message": "在线刷新公会入会方式失败，请检查网络后重跑原命令。",
                            },
                        },
                        args,
                    )
                    return 1
            if join_method == 0:
                _print_guild_payload(
                    {
                        "ok": False,
                        "mode": "private",
                        "state": "guild_is_public",
                        "stopped_reason": "guild_is_public",
                        "join_method": join_method,
                        "source": "refresh",
                        "next_action": {
                            "action": "use_public_flow",
                            "message": "该公会可直接加入，请改用 guild public。",
                        },
                    },
                    args,
                )
                return 1

        original_master_mid = (
            target.original_master_mid or target.master_user_id
        )
        if not original_master_mid:
            raise SystemExit("目标公会缺少原会长 MID，无法建立 private 任务")

        active_controller_job = db.get_active_private_job(
            target.guild_id,
            controller_mid,
        )
        if active_controller_job is not None and active_controller_job.error:
            # A job created by an older binary may already contain the server
            # deadline even though that binary did not persist it to accounts.
            _record_private_controller_cooldown(
                db,
                controller_mid,
                active_controller_job.error,
            )
            controller_row = db.get(controller_mid) or controller_row
        controller_availability = _private_controller_availability(
            controller_row,
            target.guild_id,
        )
        if not controller_availability.get("ok"):
            can_replace_controller = bool(
                not controller_value
                and controller_source in {"latest_job", "active_job"}
                and (
                    controller_source == "latest_job"
                    or (
                        active_controller_job is not None
                        and active_controller_job.status == "created"
                        and not db.list_private_accounts(active_controller_job.id)
                    )
                )
            )
            if can_replace_controller:
                selected = _select_private_controller(
                    db,
                    exclude_mids={original_master_mid, controller_mid},
                )
                if selected is not None:
                    if active_controller_job is not None:
                        db.update_private_job(
                            active_controller_job.id,
                            controller_mid=selected.mid,
                            application_id="",
                            status="created",
                            master_acquired_at=0,
                            error="",
                        )
                    log.info(
                        "private controller %s unavailable (%s); using %s",
                        controller_mid,
                        controller_availability.get("reason"),
                        selected.mid,
                    )
                    controller_mid = selected.mid
                    controller_row = selected
                    controller_source = "auto_replaced"
                    controller_availability = _private_controller_availability(
                        controller_row,
                        target.guild_id,
                    )
            if not controller_availability.get("ok"):
                payload = _private_controller_unavailable_payload(
                    controller_mid,
                    controller_availability,
                )
                _print_guild_payload(payload, args)
                return 1

        if controller_mid == original_master_mid:
            if controller_source == "auto":
                selected = _select_private_controller(
                    db,
                    exclude_mids={original_master_mid},
                )
                if selected is None:
                    _print_guild_payload(
                        _private_controller_candidate_error(), args
                    )
                    return 1
                controller_mid = selected.mid
                controller_row = selected
            else:
                payload = {
                    "ok": False,
                    "complete": False,
                    "mode": "private",
                    "state": "controller_is_original_master",
                    "stopped_reason": "controller_is_original_master",
                    "controller_mid": controller_mid,
                    "next_action": {
                        "action": "choose_another_controller",
                        "message": "代理会长不能是原会长，请改用其他 --master-mid。",
                    },
                }
                _print_guild_payload(payload, args)
                return 1
        job = db.get_active_private_job(target.guild_id, controller_mid)
        if job is not None and (
            job.effective_count_per_account != count
            or job.total_count_limit != totalcount
        ):
            if not confirm_update:
                raise SystemExit(
                    "已有未完成 private 任务，--count/--totalcount 与原任务不一致；"
                    "确认要更新原任务目标时请增加 --confirm"
                )
            previous = {
                "count": job.effective_count_per_account,
                "totalcount": job.requested_totalcount,
            }
            job = db.update_private_job(
                job.id,
                paid_count_per_account=count,
                total_count_limit=totalcount,
            )
            parameter_update = {
                "confirmed": True,
                "previous": previous,
                "current": {
                    "count": job.effective_count_per_account,
                    "totalcount": job.requested_totalcount,
                },
            }
        if job is None:
            latest = db.get_latest_private_job(target.guild_id, controller_mid)
            same_target = bool(
                latest is not None
                and latest.effective_count_per_account == count
                and latest.total_count_limit == totalcount
            )
            if (
                latest is not None
                and latest.status == "complete"
                and same_target
                and totalcount > 0
            ):
                if latest.effective_count >= latest.total_count_limit:
                    latest_day = _private_job_day_key(latest)
                    current_day = _private_current_day_key()
                    if latest_day == current_day:
                        payload = {
                            "ok": True,
                            "complete": True,
                            "mode": "private",
                            "state": "complete",
                            "stopped_reason": "target_already_complete",
                            "run_day": latest_day,
                            "job": _private_cli_job_payload(latest),
                            "controller": {
                                "mid": controller_mid,
                                "source": controller_source,
                            },
                            "progress": {
                                "current": latest.effective_count,
                                "target": latest.total_count_limit,
                                "remaining": 0,
                                "reached": True,
                            },
                            "next_action": {
                                "action": "none",
                                "message": (
                                    "本日目标已达到且会长已交还；下个自然日"
                                    "重跑同一命令开始新的批次。"
                                ),
                            },
                        }
                        _print_guild_payload(payload, args)
                        return 0
                    log.info(
                        "private totalcount batch completed on %s; starting a new "
                        "batch for %s",
                        latest_day or "unknown day",
                        current_day,
                    )
                else:
                    job = db.update_private_job(
                        latest.id,
                        status="awaiting_donors",
                        completed_at=0,
                    )
            else:
                job = None
            if job is None:
                job = db.create_private_job(
                    guild_id=target.guild_id,
                    gname=gname,
                    gmname=gmname,
                    original_master_mid=original_master_mid,
                    controller_mid=controller_mid,
                    paid_count_per_account=count,
                    total_count_limit=totalcount,
                )

        runner = PrivateGuildRunner(
            db,
            _login_account,
            client_factory=GrpcClient,
        )
        try:
            payload = runner.run(job)
            if isinstance(payload.get("pool"), dict):
                payload["pool"] = _guild_pool_payload(payload["pool"])
        except Exception as error:
            message = f"{type(error).__name__}: {error}"
            _record_private_controller_cooldown(db, controller_mid, message)
            db.update_private_job(job.id, error=message)
            payload = {
                "ok": False,
                "complete": False,
                "mode": "private",
                "state": "private_job_failed",
                "stopped_reason": "private_job_failed",
                "job_id": job.id,
                "error": message,
                "next_action": {
                    "action": "inspect_error_and_rerun",
                    "message": "检查 error；修复对应条件后重跑同一命令继续。",
                },
            }
        controller_name = str(payload.pop("controller_name", "") or "")
        payload["controller"] = {
            "mid": controller_mid,
            "source": controller_source,
        }
        if controller_name:
            payload["controller"]["name"] = controller_name
        if parameter_update is not None:
            payload["job_parameters_updated"] = parameter_update

        _print_guild_payload(payload, args)
    return 0 if payload.get("ok") else 1


def _cmd_guild_run(args: argparse.Namespace) -> int:
    """Run the direct-join public-guild workflow."""
    gname = str(args.gname or "").strip()
    gmname = str(args.gmname or "").strip()
    if getattr(args, "count", None) is None:
        raise SystemExit("guild public 必须提供 --count")
    effective_count_per_account = int(args.count)
    totalcount_value = getattr(args, "totalcount", None)
    requested_total_count = (
        int(totalcount_value) if totalcount_value is not None else None
    )
    if not gname:
        raise SystemExit("--gname 不能为空")
    if not gmname:
        raise SystemExit("--gmname 不能为空")
    if effective_count_per_account < 1:
        raise SystemExit("--count 必须 >= 1")
    if requested_total_count is not None and requested_total_count < 1:
        raise SystemExit("--totalcount 必须 >= 1")

    results: list[dict] = []
    completed = 0
    failures = 0
    attempted = 0
    joined_accounts = 0
    effective_total = 0
    recruitment_limit: int | None = None
    join_unavailable = False
    target_source = "cache"
    prepared_states: dict[str, AccountState] = {}
    workflows: list[GuildWorkflowResult] = []

    with AccountDB(args.db) as db:
        pool_before = _guild_pool_payload(db.guild_pool_status())
        eligible_rows = db.list_guild_eligible()
        if not eligible_rows:
            summary = {
                "ok": True,
                "mode": "public",
                "count": effective_count_per_account,
                "requested_totalcount": requested_total_count,
                "totalcount": 0,
                "totalcount_reached": False,
                "account_count": 0,
                "accounts_attempted": 0,
                "accounts_failed": 0,
                "stopped_reason": "all_accounts_cooling",
                "guild": {"name": gname, "master_name": gmname},
                "pool": pool_before,
                "results": [],
            }
            _print_guild_payload(summary, args)
            return 0

        target = db.get_guild_target(gname, gmname)
        if target is None:
            target_source = "search"
            discovery_row = eligible_rows[0]
            try:
                state = _login_account(discovery_row)
                session = state.to_session()

                with GrpcClient(state.endpoint or ENDPOINT) as client:
                    response = Guild(client, session).search_guilds(gname)
                    summaries = parse_guild_search_response(response.message)

                state.resource_key = session.resource_key
                db.upsert_state(
                    state,
                    used=discovery_row.used,
                    ready=discovery_row.ready,
                    invalid=discovery_row.invalid,
                    note=discovery_row.note,
                )
                prepared_states[discovery_row.mid] = state
            except Exception as error:
                summary = {
                    "ok": False,
                    "count": effective_count_per_account,
                    "requested_totalcount": requested_total_count,
                    "totalcount": 0,
                    "totalcount_reached": False,
                    "account_count": 0,
                    "accounts_attempted": 0,
                    "accounts_failed": 1,
                    "stopped_reason": "guild_search_failed",
                    "error": f"{type(error).__name__}: {error}",
                    "guild": {"name": gname, "master_name": gmname},
                    "pool": pool_before,
                    "results": [],
                }
                _print_guild_payload(summary, args)
                return 1

            matches = [
                item
                for item in summaries
                if item.name == gname and item.master_name == gmname
            ]
            if len(matches) != 1:
                summary = {
                    "ok": False,
                    "count": effective_count_per_account,
                    "requested_totalcount": requested_total_count,
                    "totalcount": 0,
                    "totalcount_reached": False,
                    "account_count": 0,
                    "accounts_attempted": 0,
                    "accounts_failed": 0,
                    "stopped_reason": "guild_target_not_unique",
                    "match_count": len(matches),
                    "search_result_count": len(summaries),
                    "guild": {"name": gname, "master_name": gmname},
                    "pool": pool_before,
                    "results": [],
                }
                _print_guild_payload(summary, args)
                return 1

            matched = matches[0]
            if matched.join_method != 0:
                summary = {
                    "ok": False,
                    "mode": "public",
                    "count": effective_count_per_account,
                    "requested_totalcount": requested_total_count,
                    "totalcount": 0,
                    "totalcount_reached": False,
                    "account_count": 0,
                    "accounts_attempted": 0,
                    "accounts_failed": 0,
                    "stopped_reason": "guild_requires_approval",
                    "join_method": matched.join_method,
                    "guild": {"name": gname, "master_name": gmname},
                    "pool": pool_before,
                    "results": [],
                }
                _print_guild_payload(summary, args)
                return 1
            confirmation = _guild_confirmation_payload(matched)
            if not _confirm_guild(confirmation):
                summary = {
                    "ok": True,
                    "count": effective_count_per_account,
                    "requested_totalcount": requested_total_count,
                    "totalcount": 0,
                    "totalcount_reached": False,
                    "account_count": 0,
                    "accounts_attempted": 0,
                    "accounts_failed": 0,
                    "stopped_reason": "not_confirmed",
                    "guild": {"name": gname, "master_name": gmname},
                    "pool": pool_before,
                    "results": [],
                }
                _print_guild_payload(summary, args)
                return 0

            target = db.upsert_guild_target(
                gname=gname,
                gmname=gmname,
                guild_id=matched.guild_id,
                guild_level=matched.guild_level,
                member_count=matched.member_count,
                master_user_id=matched.master_user_id,
                details={"search_summary": confirmation},
            )
            log.info("guild target confirmed and cached")
        else:
            join_method = _guild_target_join_method(target.details)
            if join_method == 1:
                refresh_row = eligible_rows[0]
                try:
                    target, refreshed_summary, refreshed_state = (
                        _refresh_cached_guild_target(
                            db,
                            refresh_row,
                            target,
                        )
                    )
                    prepared_states[refresh_row.mid] = refreshed_state
                    join_method = refreshed_summary.join_method
                    target_source = "refresh"
                    log.info(
                        "refreshed cached guild mode name=%s guild_id=%s "
                        "join_method=%s",
                        gname,
                        target.guild_id,
                        join_method,
                    )
                except Exception as error:
                    summary = {
                        "ok": False,
                        "mode": "public",
                        "count": effective_count_per_account,
                        "requested_totalcount": requested_total_count,
                        "totalcount": 0,
                        "totalcount_reached": False,
                        "account_count": 0,
                        "accounts_attempted": 0,
                        "accounts_failed": 1,
                        "stopped_reason": "guild_mode_refresh_failed",
                        "cached_join_method": 1,
                        "error": f"{type(error).__name__}: {error}",
                        "guild": {"name": gname, "master_name": gmname},
                        "pool": pool_before,
                        "results": [],
                    }
                    _print_guild_payload(summary, args)
                    return 1
            if join_method == 1:
                summary = {
                    "ok": False,
                    "mode": "public",
                    "count": effective_count_per_account,
                    "requested_totalcount": requested_total_count,
                    "totalcount": 0,
                    "totalcount_reached": False,
                    "account_count": 0,
                    "accounts_attempted": 0,
                    "accounts_failed": 0,
                    "stopped_reason": "guild_requires_approval",
                    "join_method": join_method,
                    "source": "refresh",
                    "guild": {"name": gname, "master_name": gmname},
                    "pool": pool_before,
                    "results": [],
                }
                _print_guild_payload(summary, args)
                return 1
            log.info(
                "using cached guild target name=%s master=%s confirmed_at=%s",
                gname,
                gmname,
                _local_timestamp(target.confirmed_at),
            )

        for index, row in enumerate(eligible_rows, start=1):
            if (
                requested_total_count is not None
                and effective_total >= requested_total_count
            ):
                break
            if (
                requested_total_count is None
                and joined_accounts
                >= GUILD_DAILY_RECRUITMENT_ACCOUNT_LIMIT
            ):
                break
            attempted += 1
            log.info(
                "[%s] guild SOP mid=%s ready=%s next=%s",
                index,
                row.mid,
                int(row.ready),
                row.next_stage,
            )

            try:
                state = prepared_states.pop(row.mid, None) or _login_account(row)
                session = state.to_session()

                def persist_balance(
                    balance: int,
                    *,
                    _state=state,
                    _session=session,
                    _row=row,
                ) -> None:
                    _state.diamond_balance = balance
                    _state.resource_key = _session.resource_key
                    db.upsert_state(
                        _state,
                        used=_row.used,
                        ready=_row.ready,
                        invalid=_row.invalid,
                        note=_row.note,
                    )

                db.upsert_state(
                    state,
                    used=row.used,
                    ready=row.ready,
                    invalid=row.invalid,
                    note=row.note,
                )
                with GrpcClient(state.endpoint or ENDPOINT) as client:
                    runner = GuildRunner(
                        client,
                        session,
                        paid_research_limit=None,
                        effective_research_limit=(
                            effective_count_per_account
                        ),
                        total_count_limit=(
                            requested_total_count - effective_total
                            if requested_total_count is not None
                            else None
                        ),
                        on_balance=persist_balance,
                        initial_guild_level=target.guild_level,
                        initial_diamond_balance=state.diamond_balance,
                    )
                    workflow = runner.run(target.guild_id)
                    workflows.append(workflow)
                    effective_total += workflow.effective_research_count

                if workflow.joined:
                    joined_accounts += 1
                if requested_total_count is None and not workflow.joined:
                    join_unavailable = True
                    recruitment_limit = parse_guild_daily_recruitment_limit(
                        workflow.error
                    )
                    workflow.stop_reason = (
                        "daily_recruitment_limit_reached"
                        if recruitment_limit is not None
                        else "guild_join_failed"
                    )

                state.resource_key = session.resource_key
                if workflow.diamond_balance_final is not None:
                    state.diamond_balance = workflow.diamond_balance_final
                db.upsert_state(
                    state,
                    used=row.used,
                    ready=row.ready,
                    invalid=row.invalid,
                    note=row.note,
                )

                run_id = db.record_guild_run(
                    row.mid,
                    guild_id=target.guild_id,
                    joined_at=workflow.joined_at,
                    left_at=workflow.left_at,
                    free_research_count=workflow.free_research_count,
                    paid_research_count=workflow.paid_research_count,
                    free_effective_count=workflow.free_effective_count,
                    paid_effective_count=workflow.paid_effective_count,
                    free_super_success_count=(
                        workflow.free_super_success_count
                    ),
                    paid_super_success_count=(
                        workflow.paid_super_success_count
                    ),
                    diamond_spent=workflow.diamond_spent,
                    stop_reason=workflow.stop_reason,
                    ok=workflow.ok,
                    error=workflow.error,
                )
                item = {
                    "mid": row.mid,
                    "guild_run_id": run_id,
                    **workflow.to_dict(),
                    "joined_at_local": (
                        _local_timestamp(workflow.joined_at)
                        if workflow.joined_at
                        else None
                    ),
                    "left_at_local": (
                        _local_timestamp(workflow.left_at)
                        if workflow.left_at
                        else None
                    ),
                }

                if workflow.guild_detail is not None:
                    details = dict(target.details)
                    details["member_detail"] = {
                        "captured_while_runner_was_member": True,
                        **asdict(workflow.guild_detail),
                    }
                    target = db.upsert_guild_target(
                        gname=gname,
                        gmname=gmname,
                        guild_id=target.guild_id,
                        guild_level=(
                            workflow.guild_progress.level_after
                            if workflow.guild_progress.level_after is not None
                            else target.guild_level
                        ),
                        member_count=target.member_count,
                        master_user_id=target.master_user_id,
                        details=details,
                    )

                results.append(item)
                if workflow.ok:
                    completed += 1
                    log.info(
                        "[%s] guild SOP complete mid=%s accounts=%s "
                        "totalcount=%s/%s",
                        index,
                        row.mid,
                        completed,
                        effective_total,
                        requested_total_count,
                    )
                elif join_unavailable:
                    if recruitment_limit is None:
                        failures += 1
                    log.warning(
                        "[%s] guild join unavailable mid=%s error=%s",
                        index,
                        row.mid,
                        workflow.error,
                    )
                else:
                    failures += 1
                    log.error(
                        "[%s] guild SOP failed mid=%s error=%s",
                        index,
                        row.mid,
                        workflow.error or workflow.paid_stop_message,
                    )
                if join_unavailable:
                    break
            except Exception as error:
                failures += 1
                item = {
                    "ok": False,
                    "mid": row.mid,
                    "error": f"{type(error).__name__}: {error}",
                }
                results.append(item)
                log.error(
                    "[%s] guild account failed mid=%s error=%s",
                    index,
                    row.mid,
                    item["error"],
                )

        pool_after = _guild_pool_payload(db.guild_pool_status())
        totalcount_reached = bool(
            requested_total_count is not None
            and effective_total >= requested_total_count
        )
        account_limit_reached = bool(
            requested_total_count is None
            and joined_accounts >= GUILD_DAILY_RECRUITMENT_ACCOUNT_LIMIT
        )
        if totalcount_reached:
            stopped_reason = "totalcount_reached"
        elif account_limit_reached:
            stopped_reason = "account_limit_reached"
        elif join_unavailable:
            stopped_reason = (
                "daily_recruitment_limit_reached"
                if recruitment_limit is not None
                else "guild_join_failed"
            )
        else:
            stopped_reason = "all_eligible_accounts_attempted"
        summary = {
            "ok": failures == 0,
            "mode": "public",
            "count": effective_count_per_account,
            "requested_totalcount": requested_total_count,
            "totalcount": effective_total,
            "totalcount_reached": totalcount_reached,
            "account_limit": (
                None
                if requested_total_count is not None
                else GUILD_DAILY_RECRUITMENT_ACCOUNT_LIMIT
            ),
            "joined_account_count": joined_accounts,
            "account_limit_reached": account_limit_reached,
            "account_count": completed,
            "accounts_attempted": attempted,
            "accounts_failed": failures,
            "stopped_reason": stopped_reason,
            "guild": {
                "name": gname,
                "master_name": gmname,
                "source": target_source,
                "confirmed_at": _local_timestamp(target.confirmed_at),
                **_guild_overall_progress(workflows),
            },
            "totals": _guild_run_totals(workflows),
            "pool_before": pool_before,
            "pool_after": pool_after,
            "results": results,
        }
        if recruitment_limit is not None:
            summary["daily_recruitment_limit"] = recruitment_limit or None
        _print_guild_payload(summary, args)

    return 0 if failures == 0 else 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="crumble_bot",
        description="Crumble bot: gen / inv / daily / reddot / guild / list",
    )
    p.add_argument("-v", "--verbose", action="store_true", help="DEBUG：每关点位 + HTTP")
    p.add_argument("-q", "--quiet", action="store_true", help="仅警告/错误 + 最终 JSON")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("inv", help="从 sqlite 取未使用号 -> 登录 -> 邀请")
    sp.add_argument("target", metavar="mid|url", help="邀请目标 mid，或完整邀请链接")
    sp.add_argument("-c", "--count", type=int, default=1, help="取号数量，默认 1")
    sp.add_argument("--db", default=str(DEFAULT_DB), help="sqlite 路径")
    sp.add_argument("--any", action="store_true", help="允许使用未 ready 的号")
    sp.set_defaults(func=cmd_inv)

    sp = sub.add_parser("gen", help="批量建号+推1-30入库(sqlite)，不邀请")
    sp.add_argument("n", nargs="?", type=int, default=None, help="生成数量；省略则一直生成")
    sp.add_argument("--db", default=str(DEFAULT_DB), help="sqlite 路径")
    sp.add_argument("--stop-on-error", action="store_true")
    sp.set_defaults(func=cmd_gen)

    sp = sub.add_parser("daily", help="批量执行每日登录、邮箱领取和邮箱广告")
    sp.add_argument("--db", default=str(DEFAULT_DB), help="sqlite 路径")
    sp.set_defaults(func=cmd_daily)

    sp = sub.add_parser("reddot", help="清理一个账号可安全领取的红点")
    sp.add_argument("--mid", help="指定账号 MID；省略时选择一个有效账号")
    sp.add_argument("--json", action="store_true", help="输出完整 JSON")
    sp.add_argument("--db", default=str(DEFAULT_DB), help="sqlite 路径")
    sp.set_defaults(func=cmd_reddot)

    sp = sub.add_parser("guild", help="公开、审批公会 SOP 或查看公会数据")
    sp.add_argument(
        "guild_action",
        choices=(
            "public",
            "private",
            "joblist",
            "init",
            "status",
            "fill",
            "daily",
            "support",
            "maintain",
        ),
        help=(
            "公会流程或常驻管理动作：init/status/fill/daily/support/maintain；"
            "joblist 查看旧 private 任务"
        ),
    )
    sp.add_argument(
        "private_action",
        nargs="?",
        choices=("return",),
        help="private 可选动作：return 列出或交还会长",
    )
    sp.add_argument(
        "private_job_id",
        nargs="?",
        type=int,
        help="return：guild_private_jobs.id；省略时列出待交还任务",
    )
    sp.add_argument("--gname", help="公会名称")
    sp.add_argument("--gmname", help="会长名称")
    sp.add_argument(
        "--count",
        type=int,
        help=(
            "public/private：每个账号的免费+钻石有效研究次数上限；"
            "暴击按倍率计数"
        ),
    )
    sp.add_argument(
        "--totalcount",
        type=int,
        help=(
            "public/private：可选；跨账号免费+钻石研究有效总次数。"
            "省略时最多处理 50 个账号，或在公会无法继续入会时停止"
        ),
    )
    sp.add_argument(
        "--master-mid",
        help=(
            "private：可选；显式指定临时会长账号 MID。省略时优先复用已有任务，"
            "否则自动选择钻石最少的可用账号；resident init 不需要此参数"
        ),
    )
    sp.add_argument(
        "--controller-mid",
        help="兼容旧配置：指定常驻公会控制账号；当前 resident fill 不需要代理会长",
    )
    sp.add_argument(
        "--capacity",
        type=int,
        help=(
            "resident init：旧版本或未知公会等级时的容量兜底；"
            "当前版本会按公会等级自动读取"
        ),
    )
    sp.add_argument(
        "--reserve-slots",
        type=int,
        help=(
            "resident fill：保留的非受控成员席位数；设为 0 时用受控账号填满"
            "所有剩余成员位，并持久化到 sqlite"
        ),
    )
    sp.add_argument(
        "--confirm",
        action="store_true",
        help=(
            "private：当 count/totalcount 与未完成任务不一致时，"
            "确认更新原任务参数并继续"
        ),
    )
    sp.add_argument(
        "--json",
        action="store_true",
        help="guild：输出完整 JSON；默认输出简明状态摘要",
    )
    sp.add_argument("--db", default=str(DEFAULT_DB), help="sqlite 路径")
    sp.set_defaults(func=cmd_guild)

    sp = sub.add_parser("list", help="列出 sqlite 账号")
    sp.add_argument("--db", default=str(DEFAULT_DB))
    sp.add_argument("--all", action="store_true")
    sp.add_argument("--unused", action="store_true")
    sp.add_argument("--ready", action="store_true")
    sp.add_argument("--limit", type=int, default=50)
    sp.set_defaults(func=cmd_list)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    # Keep direct ``cmd_guild`` calls/API consumers on the structured output,
    # while the executable defaults to the readable terminal summary.
    args._human_output = bool(
        getattr(args, "cmd", "") == "guild"
        and not bool(getattr(args, "json", False))
    )
    _setup_log(
        verbose=bool(getattr(args, "verbose", False)),
        quiet=bool(getattr(args, "quiet", False)),
    )
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print(
            json.dumps(
                {
                    "ok": False,
                    "stopped_reason": "interrupted",
                    "message": "已中断；任务进度保存在 SQLite，重跑原命令可继续。",
                },
                ensure_ascii=False,
                indent=2,
            ),
            flush=True,
        )
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
