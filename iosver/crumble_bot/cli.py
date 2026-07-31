from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

from .auth import guest_login, new_device_ids
from .constants import ENDPOINT, FALLBACK_RESOURCE_KEY, TO_STAGE
from .db import DEFAULT_DB, AccountDB
from .grpc_client import GrpcClient, GrpcError
from .invite import register_friend_inviter
from .inviter import parse_inviter
from .stage_runner import StageConfig, StageRunner


def _setup_log(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )


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
        print(f"inviter: {mid}  (from {raw[:80]}{suffix})", flush=True)
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
                f"secret={r.guest_secret[:8]}...\temail={r.email}"
            )
    return 0


def cmd_gen(args: argparse.Namespace) -> int:
    """Create guests, clear 1-30, store to sqlite. No invite."""
    n = args.n
    db_path = Path(args.db) if args.db else DEFAULT_DB
    n_label = "inf" if n is None else str(n)
    print(f"gen db={db_path} n={n_label} clear=1..{TO_STAGE} invite=no", flush=True)

    made = 0
    failures = 0
    with AccountDB(db_path) as db:
        while n is None or made < n:
            idx = made + 1
            try:
                device = new_device_ids()
                print(f"\n[{idx}] creating guest...", flush=True)
                state = guest_login(
                    guest_secret="",
                    device=device,
                    inviter_mid="",
                    resource_key=FALLBACK_RESOURCE_KEY,
                    endpoint=ENDPOINT,
                )
                state.inviter_mid = ""
                db.upsert_state(state, used=False, ready=False, note="created")
                print(f"[{idx}] mid={state.mid} secret={state.guest_secret}", flush=True)

                session = state.to_session()
                if not session.resource_key or session.resource_key == "dev-0000000000":
                    session.resource_key = FALLBACK_RESOURCE_KEY
                    state.resource_key = FALLBACK_RESOURCE_KEY

                scfg = _stage_cfg()
                with GrpcClient(ENDPOINT) as client:
                    runner = StageRunner(client, session, scfg)
                    print(f"[{idx}] SignUp...", flush=True)
                    body = runner.signup()
                    state.resource_key = session.resource_key
                    db.upsert_state(state, used=False, ready=False, note="signed_up")
                    print(
                        f"[{idx}] SignUp ok bytes={len(body)} resource_key={session.resource_key}",
                        flush=True,
                    )

                    def on_progress(r, _idx=idx, _state=state, _session=session, _db=db):
                        flag = "OK" if r.ok else "FAIL"
                        extra = f" current={r.current_after}" if r.current_after is not None else ""
                        print(
                            f"[{_idx}][{flag}] stage={r.stage} point={r.start_point}{extra} {r.error}",
                            flush=True,
                        )
                        if r.ok and r.current_after is not None:
                            _state.next_stage = r.current_after
                            _state.resource_key = _session.resource_key
                            _db.upsert_state(_state, used=False, ready=False, note="clearing")

                    print(f"[{idx}] clear 1..{TO_STAGE}...", flush=True)
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
                    print(
                        f"[{idx}] FAIL mid={state.mid} next={state.next_stage} stored=1",
                        flush=True,
                    )
                else:
                    prog = f"{made}/{n}" if n is not None else str(made)
                    print(
                        f"[{idx}] DONE mid={state.mid} next={state.next_stage} "
                        f"ready={int(ready)} progress={prog}",
                        flush=True,
                    )
            except KeyboardInterrupt:
                print("\ninterrupted", flush=True)
                print(json.dumps({"made": made, "failures": failures, **db.count()}, indent=2))
                return 130
            except Exception as e:
                failures += 1
                print(f"[{idx}] ERROR {type(e).__name__}: {e}", flush=True)
                if args.stop_on_error:
                    print(json.dumps({"made": made, "failures": failures, **db.count()}, indent=2))
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
                print(
                    f"[{idx}] warn: no ready unused, using mid={row.mid} next={row.next_stage}",
                    flush=True,
                )
        if row is None:
            return {"ok": False, "error": "no_unused_account", "idx": idx}

        print(
            f"[{idx}] claim mid={row.mid} ready={int(row.ready)} next={row.next_stage} inviter={inviter}",
            flush=True,
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
                    print(f"[{idx}] signup note: {e}", flush=True)
                state.resource_key = session.resource_key

                print(f"[{idx}] RegisterFriendInviter {inviter} as {state.mid}...", flush=True)
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
            else:
                fail_n += 1
                if item.get("error") == "no_unused_account":
                    print(f"[{i}] pool empty, stop", flush=True)
                    break

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


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="crumble_bot",
        description="Crumble bot: gen / inv / list",
    )
    p.add_argument("-v", "--verbose", action="store_true")
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
    _setup_log(args.verbose)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
