from __future__ import annotations

import argparse
import json
import logging
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from .auth import AccountState, guest_login, new_device_ids
from .constants import ENDPOINT, FALLBACK_RESOURCE_KEY, TO_STAGE
from .db import DEFAULT_DB, AccountDB, AccountRow
from .grpc_client import GrpcClient, GrpcError
from .guild import Guild, GuildSearchSummary, parse_guild_search_response
from .guild_runner import GuildRunner
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


def _login_guild_account(row: AccountRow) -> AccountState:
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
        raise RuntimeError(f"re-login mid mismatch: expected {row.mid}, got {fresh.mid}")
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
    payload["member_ids_note"] = (
        "搜索接口只返回成员数；完整成员 ID 列表需账号加入后读取"
    )
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
                f"diamonds={r.diamond_balance}\tguild={_local_timestamp(r.guild)}\t"
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

                    def on_progress(r, _idx=idx, _state=state, _session=session, _db=db):
                        if r.ok:
                            # 默认只在 Boss(点位最后) 打一行；DEBUG 打每个点
                            if r.start_point == 1 or logging.getLogger().isEnabledFor(logging.DEBUG):
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
                            _db.upsert_state(_state, used=False, ready=False, note="clearing")

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
                print(json.dumps({"made": made, "failures": failures, **db.count()}, indent=2), flush=True)
                return 130
            except Exception as e:
                failures += 1
                log.error("[%s] %s: %s", idx, type(e).__name__, e)
                if args.stop_on_error:
                    print(json.dumps({"made": made, "failures": failures, **db.count()}, indent=2), flush=True)
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

        db.upsert_state(state, used=False, ready=row.ready, invalid=False, note="logged_in")
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
            return {"ok": False, "mid": state.mid, "error": note, "invalid": True, "idx": idx}

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
                log.error("[%s] FAIL mid=%s err=%s", i, item.get("mid"), item.get("error"))

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


def cmd_guild(args: argparse.Namespace) -> int:
    """Run the confirmed guild SOP across cooldown-eligible ready accounts."""
    gname = str(args.gname or "").strip()
    gmname = str(args.gmname or "").strip()
    requested = int(args.count)
    if not gname:
        raise SystemExit("--gname 不能为空")
    if not gmname:
        raise SystemExit("--gmname 不能为空")
    if requested < 1:
        raise SystemExit("--count 必须 >= 1")

    results: list[dict] = []
    completed = 0
    failures = 0
    attempted = 0
    target_source = "cache"
    prepared_states: dict[str, AccountState] = {}

    with AccountDB(args.db) as db:
        pool_before = _guild_pool_payload(db.guild_pool_status())
        eligible_rows = db.list_guild_eligible()
        if not eligible_rows:
            summary = {
                "ok": True,
                "requested": requested,
                "count": 0,
                "attempted": 0,
                "failed": 0,
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
                state = _login_guild_account(discovery_row)
                session = state.to_session()

                def persist_discovery_balance(balance: int) -> None:
                    state.diamond_balance = balance
                    state.resource_key = session.resource_key
                    db.upsert_state(
                        state,
                        used=discovery_row.used,
                        ready=discovery_row.ready,
                        invalid=discovery_row.invalid,
                        note=discovery_row.note,
                    )

                with GrpcClient(state.endpoint or ENDPOINT) as client:
                    runner = GuildRunner(
                        client,
                        session,
                        on_balance=persist_discovery_balance,
                    )
                    runner.sync_diamond_balance()
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
                    "requested": requested,
                    "count": 0,
                    "attempted": 0,
                    "failed": 1,
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
                    "requested": requested,
                    "count": 0,
                    "attempted": 0,
                    "failed": 0,
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
            confirmation = _guild_confirmation_payload(matched)
            if not _confirm_guild(confirmation):
                summary = {
                    "ok": True,
                    "requested": requested,
                    "count": 0,
                    "attempted": 0,
                    "failed": 0,
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
            log.info(
                "using cached guild target name=%s master=%s confirmed_at=%s",
                gname,
                gmname,
                _local_timestamp(target.confirmed_at),
            )

        for index, row in enumerate(eligible_rows, start=1):
            if completed >= requested:
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
                state = prepared_states.pop(row.mid, None) or _login_guild_account(row)
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
                        on_balance=persist_balance,
                    )
                    runner.sync_diamond_balance()
                    workflow = runner.run(target.guild_id)

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
                if workflow.left_guild:
                    left_at = db.mark_guild_left(row.mid)
                    item["guild_left_at"] = _local_timestamp(left_at)

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
                        guild_level=target.guild_level,
                        member_count=target.member_count,
                        master_user_id=target.master_user_id,
                        details=details,
                    )

                results.append(item)
                if workflow.ok:
                    completed += 1
                    log.info(
                        "[%s] guild SOP complete mid=%s count=%s/%s",
                        index,
                        row.mid,
                        completed,
                        requested,
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
            "count_reached"
            if completed >= requested
            else "all_eligible_accounts_attempted"
        )
        summary = {
            "ok": failures == 0,
            "requested": requested,
            "count": completed,
            "attempted": attempted,
            "failed": failures,
            "stopped_reason": stopped_reason,
            "guild": {
                "name": gname,
                "master_name": gmname,
                "source": target_source,
                "confirmed_at": _local_timestamp(target.confirmed_at),
            },
            "pool_before": pool_before,
            "pool_after": pool_after,
            "results": results,
        }
        print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)

    return 0 if failures == 0 else 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="crumble_bot",
        description="Crumble bot: gen / inv / guild / list",
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

    sp = sub.add_parser("guild", help="批量执行公会签到、研究、捐赠并退出")
    sp.add_argument("--gname", required=True, help="公会名称")
    sp.add_argument("--gmname", required=True, help="会长名称")
    sp.add_argument("--count", required=True, type=int, help="成功执行的账号数量")
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
    _setup_log(verbose=bool(getattr(args, "verbose", False)), quiet=bool(getattr(args, "quiet", False)))
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
