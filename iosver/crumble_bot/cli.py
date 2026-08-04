from __future__ import annotations

import argparse
import json
import logging
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from .auth import AccountState, guest_login, new_device_ids
from .constants import ENDPOINT, FALLBACK_RESOURCE_KEY, TO_STAGE
from .daily_runner import DailyRunner, DailyWorkflowResult
from .db import DEFAULT_DB, AccountDB, AccountRow, GuildTargetRow
from .grpc_client import GrpcClient, GrpcError
from .guild import (
    Guild,
    GuildSearchSummary,
    parse_guild_search_response,
)
from .guild_runner import GuildRunner, GuildWorkflowResult
from .guild_private_runner import PrivateGuildRunner
from .invite import register_friend_inviter
from .inviter import parse_inviter
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


def _numeric_change(before: int | None, after: int | None) -> int | None:
    if before is None or after is None:
        return None
    return int(after) - int(before)


def _daily_run_totals(workflows: list[DailyWorkflowResult]) -> dict:
    return {
        "login_completed_count": sum(
            1 for workflow in workflows if workflow.login_completed
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
    print(
        json.dumps({"guild_confirmation": payload}, ensure_ascii=False, indent=2),
        flush=True,
    )
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
        pool = db.daily_pool_status()
        eligible_rows = db.list_daily_accounts()
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

                db.upsert_state(
                    state,
                    used=row.used,
                    ready=row.ready,
                    invalid=row.invalid,
                    note=row.note,
                )
                with GrpcClient(state.endpoint or ENDPOINT) as client:
                    workflow = DailyRunner(
                        client,
                        session,
                        on_balance=persist_balance,
                    ).run()
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

        pool_after = db.daily_pool_status()
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


def cmd_guild(args: argparse.Namespace) -> int:
    """Dispatch the public or private guild SOP."""
    action = str(getattr(args, "guild_action", "") or "")
    private_action = str(getattr(args, "private_action", "") or "")
    if action == "private":
        if private_action == "return":
            return _cmd_guild_private_return(args)
        return _cmd_guild_private(args)
    if action == "public":
        if private_action:
            raise SystemExit("guild private return 只能用于 private 流程")
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
        "count": job.paid_count_per_account,
        "requested_totalcount": job.total_count_limit,
        "totalcount": job.effective_count,
    }


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
            print(json.dumps(payload, ensure_ascii=False, indent=2), flush=True)
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
            print(json.dumps(payload, ensure_ascii=False, indent=2), flush=True)
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

    print(json.dumps(payload, ensure_ascii=False, indent=2), flush=True)
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
    if getattr(args, "count", None) is None:
        raise SystemExit("guild private 必须提供 --count")
    if getattr(args, "totalcount", None) is None:
        raise SystemExit("guild private 必须提供 --totalcount")
    count = int(args.count)
    totalcount = int(args.totalcount)
    if not gname:
        raise SystemExit("--gname 不能为空")
    if not gmname:
        raise SystemExit("--gmname 不能为空")
    if count < 1:
        raise SystemExit("--count 必须 >= 1")
    if totalcount < 1:
        raise SystemExit("--totalcount 必须 >= 1")

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
                print(json.dumps(payload, ensure_ascii=False, indent=2), flush=True)
                return 1
            if active_jobs:
                controller_mid = active_jobs[0].controller_mid
                controller_source = "active_job"
            else:
                latest = db.get_latest_private_job_for_guild(target.guild_id)
                if (
                    latest is not None
                    and latest.status == "complete"
                    and latest.paid_count_per_account == count
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
                print(
                    json.dumps(
                        _private_controller_candidate_error(),
                        ensure_ascii=False,
                        indent=2,
                    ),
                    flush=True,
                )
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
            print(json.dumps(payload, ensure_ascii=False, indent=2), flush=True)
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
                print(
                    json.dumps(
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
                        ensure_ascii=False,
                        indent=2,
                    ),
                    flush=True,
                )
                return 1
            matched = matches[0]
            confirmation = _guild_confirmation_payload(matched)
            if not _confirm_guild(confirmation):
                print(
                    json.dumps(
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
                        ensure_ascii=False,
                        indent=2,
                    ),
                    flush=True,
                )
                return 0
            if matched.join_method != 1:
                print(
                    json.dumps(
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
                        ensure_ascii=False,
                        indent=2,
                    ),
                    flush=True,
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
                    print(
                        json.dumps(
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
                            ensure_ascii=False,
                            indent=2,
                        ),
                        flush=True,
                    )
                    return 1
            if join_method == 0:
                print(
                    json.dumps(
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
                        ensure_ascii=False,
                        indent=2,
                    ),
                    flush=True,
                )
                return 1

        original_master_mid = (
            target.original_master_mid or target.master_user_id
        )
        if not original_master_mid:
            raise SystemExit("目标公会缺少原会长 MID，无法建立 private 任务")
        if controller_mid == original_master_mid:
            if controller_source == "auto":
                selected = _select_private_controller(
                    db,
                    exclude_mids={original_master_mid},
                )
                if selected is None:
                    print(
                        json.dumps(
                            _private_controller_candidate_error(),
                            ensure_ascii=False,
                            indent=2,
                        ),
                        flush=True,
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
                print(json.dumps(payload, ensure_ascii=False, indent=2), flush=True)
                return 1
        job = db.get_active_private_job(target.guild_id, controller_mid)
        if job is not None and (
            job.paid_count_per_account != count
            or job.total_count_limit != totalcount
        ):
            raise SystemExit(
                "已有未完成 private 任务，--count/--totalcount 必须与原任务一致"
            )
        if job is None:
            latest = db.get_latest_private_job(target.guild_id, controller_mid)
            same_target = bool(
                latest is not None
                and latest.paid_count_per_account == count
                and latest.total_count_limit == totalcount
            )
            if latest is not None and latest.status == "complete" and same_target:
                if latest.effective_count >= latest.total_count_limit:
                    payload = {
                        "ok": True,
                        "complete": True,
                        "mode": "private",
                        "state": "complete",
                        "stopped_reason": "target_already_complete",
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
                            "message": "目标已达到且会长已交还，无需继续操作。",
                        },
                    }
                    print(
                        json.dumps(payload, ensure_ascii=False, indent=2),
                        flush=True,
                    )
                    return 0
                job = db.update_private_job(
                    latest.id,
                    status="awaiting_donors",
                    completed_at=0,
                )
            else:
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
        payload["controller"] = {
            "mid": controller_mid,
            "source": controller_source,
        }

    print(json.dumps(payload, ensure_ascii=False, indent=2), flush=True)
    return 0 if payload.get("ok") else 1


def _cmd_guild_run(args: argparse.Namespace) -> int:
    """Run the direct-join public-guild workflow."""
    gname = str(args.gname or "").strip()
    gmname = str(args.gmname or "").strip()
    if getattr(args, "count", None) is None:
        raise SystemExit("guild public 必须提供 --count")
    if getattr(args, "totalcount", None) is None:
        raise SystemExit("guild public 必须提供 --totalcount")
    paid_count_per_account = int(args.count)
    requested_total_count = int(args.totalcount)
    if not gname:
        raise SystemExit("--gname 不能为空")
    if not gmname:
        raise SystemExit("--gmname 不能为空")
    if paid_count_per_account < 1:
        raise SystemExit("--count 必须 >= 1")
    if requested_total_count < 1:
        raise SystemExit("--totalcount 必须 >= 1")

    results: list[dict] = []
    completed = 0
    failures = 0
    attempted = 0
    effective_total = 0
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
                "count": paid_count_per_account,
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
            print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
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
                    "count": paid_count_per_account,
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
                print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
                return 1

            matches = [
                item
                for item in summaries
                if item.name == gname and item.master_name == gmname
            ]
            if len(matches) != 1:
                summary = {
                    "ok": False,
                    "count": paid_count_per_account,
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
                print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
                return 1

            matched = matches[0]
            if matched.join_method != 0:
                summary = {
                    "ok": False,
                    "mode": "public",
                    "count": paid_count_per_account,
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
                print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
                return 1
            confirmation = _guild_confirmation_payload(matched)
            if not _confirm_guild(confirmation):
                summary = {
                    "ok": True,
                    "count": paid_count_per_account,
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
                print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
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
                        "count": paid_count_per_account,
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
                    print(
                        json.dumps(summary, ensure_ascii=False, indent=2),
                        flush=True,
                    )
                    return 1
            if join_method == 1:
                summary = {
                    "ok": False,
                    "mode": "public",
                    "count": paid_count_per_account,
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
                print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
                return 1
            log.info(
                "using cached guild target name=%s master=%s confirmed_at=%s",
                gname,
                gmname,
                _local_timestamp(target.confirmed_at),
            )

        for index, row in enumerate(eligible_rows, start=1):
            if effective_total >= requested_total_count:
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
                        paid_research_limit=paid_count_per_account,
                        total_count_limit=(
                            requested_total_count - effective_total
                        ),
                        on_balance=persist_balance,
                        initial_guild_level=target.guild_level,
                        initial_diamond_balance=state.diamond_balance,
                    )
                    workflow = runner.run(target.guild_id)
                    workflows.append(workflow)
                    effective_total += workflow.effective_research_count

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
                else:
                    failures += 1
                    log.error(
                        "[%s] guild SOP failed mid=%s error=%s",
                        index,
                        row.mid,
                        workflow.error or workflow.paid_stop_message,
                    )
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
        stopped_reason = (
            "totalcount_reached"
            if effective_total >= requested_total_count
            else "all_eligible_accounts_attempted"
        )
        summary = {
            "ok": failures == 0,
            "mode": "public",
            "count": paid_count_per_account,
            "requested_totalcount": requested_total_count,
            "totalcount": effective_total,
            "totalcount_reached": effective_total >= requested_total_count,
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
        print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)

    return 0 if failures == 0 else 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="crumble_bot",
        description="Crumble bot: gen / inv / daily / guild / list",
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

    sp = sub.add_parser("guild", help="公开或审批公会 SOP")
    sp.add_argument(
        "guild_action",
        choices=("public", "private"),
        help="公会流程类型",
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
        help="public/private：每个账号最多执行的钻石捐赠次数",
    )
    sp.add_argument(
        "--totalcount",
        type=int,
        help="public/private：跨账号免费+钻石研究有效总次数；暴击按倍率计数",
    )
    sp.add_argument(
        "--master-mid",
        help=(
            "private：可选；显式指定临时会长账号 MID。省略时优先复用已有任务，"
            "否则自动选择钻石最少的可用账号"
        ),
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
