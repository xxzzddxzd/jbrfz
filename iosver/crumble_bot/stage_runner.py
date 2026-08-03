"""Stage clear state machine.

Live observation (prod.devslime):
- CompleteStage is authoritative and returns currentStageIndex (field 5).
- StartStage often returns generic Application error from offline bot; optional.
- Probe current stage via CompleteStage mismatch error: "current N, requested M".
"""
from __future__ import annotations

import logging
import random
import re
import time
from dataclasses import dataclass
from typing import Callable, List, Optional, Sequence

from . import messages as msg
from . import pbutil as pb
from .grpc_client import GrpcClient, GrpcError
from .headers import Session, build_metadata
from .samples import SampleBank

log = logging.getLogger(__name__)

START_PATH = "/cc.public.game.AdventureService/StartStage"
COMPLETE_PATH = "/cc.public.game.AdventureService/CompleteStage"
SIGNUP_PATH = "/cc.public.game.CrumbleService/SignUp"


@dataclass
class StageConfig:
    team_index: int = 1
    clear_points: Sequence[int] = (0, 1)
    sleep_ms: int = 200
    samples_dir: str = "data/samples"
    try_start: bool = True


@dataclass
class StageResult:
    stage: int
    start_point: int
    ok: bool
    error: str = ""
    current_after: Optional[int] = None
    response: bytes = b""


def parse_current_stage_from_error(message: str) -> Optional[int]:
    m = re.search(r"current\s+(\d+)", message)
    return int(m.group(1)) if m else None


def parse_current_stage_from_complete_response(body: bytes) -> Optional[int]:
    try:
        for fn, wt, val in pb.decode_fields(body):
            if fn == 5 and wt == 0:
                return int(val)
    except Exception:
        return None
    return None


def parse_max_completed_from_signup(body: bytes) -> Optional[int]:
    """SignUpResponse.crumble.adventures.stages.maxCompletedStageIndex."""
    try:
        top = dict((fn, val) for fn, wt, val in pb.decode_fields(body) if wt == 2)
        crumble = top.get(3)
        if not crumble:
            return None
        adv = None
        for fn, wt, val in pb.decode_fields(crumble):
            if fn == 6 and wt == 2:
                adv = val
                break
        if not adv:
            return None
        stages = None
        for fn, wt, val in pb.decode_fields(adv):
            if fn == 1 and wt == 2:
                stages = val
                break
        if not stages:
            return None
        for fn, wt, val in pb.decode_fields(stages):
            if fn == 1 and wt == 0:
                return int(val)
    except Exception:
        return None
    return None


class StageRunner:
    def __init__(
        self,
        client: GrpcClient,
        session: Session,
        cfg: StageConfig,
        *,
        on_progress: Optional[Callable[[StageResult], None]] = None,
    ) -> None:
        self.client = client
        self.session = session
        self.cfg = cfg
        self.bank = SampleBank(cfg.samples_dir)
        self.on_progress = on_progress

    def _meta(self):
        return build_metadata(self.session)

    def _unary(self, path: str, body: bytes):
        resp = self.client.unary(path, body, metadata=self._meta())
        if self.session.adopt_resource_key(resp.headers):
            log.debug("resource_key <- %s", self.session.resource_key)
        return resp.message

    def signup(self) -> bytes:
        return self._unary(SIGNUP_PATH, b"")

    def probe_current_stage(self) -> int:
        # mismatch probe
        try:
            self.complete_stage(1, 0)
            return 1
        except GrpcError as e:
            cur = parse_current_stage_from_error(str(e))
            if cur is not None:
                return cur
            raise

    def start_stage(self, stage: int, start_point: int, trigger_case: int = 0) -> bytes:
        req = msg.start_stage_request(
            stage,
            team_index=self.cfg.team_index,
            start_point=start_point,
            start_trigger_case=trigger_case,
        )
        return self._unary(START_PATH, req)

    def complete_stage(self, stage: int, start_point: int) -> bytes:
        clear_tpl, client_tpl = self.bank.pick_pair()
        seed = random.randint(1, (1 << 31) - 1)
        clear = msg.stage_clear_report(
            random_seed=seed,
            battle_time_ms=random.randint(12000, 32000),
            raw_template=clear_tpl,
        )
        if client_tpl is None:
            client_tpl = msg.client_battle_report_minimal(self.session.mid)
        req = msg.complete_stage_request(
            stage,
            start_point=start_point,
            clear_report=clear,
            client_report=client_tpl,
        )
        return self._unary(COMPLETE_PATH, req)

    def clear_stage(self, stage: int) -> List[StageResult]:
        results: List[StageResult] = []
        for sp in self.cfg.clear_points:
            if self.cfg.try_start:
                try:
                    self.start_stage(stage, sp)
                except GrpcError as e:
                    log.debug("start optional fail stage=%s sp=%s: %s", stage, sp, e)
            try:
                body = self.complete_stage(stage, sp)
                cur = parse_current_stage_from_complete_response(body)
                r = StageResult(stage=stage, start_point=sp, ok=True, current_after=cur, response=body)
            except GrpcError as e:
                r = StageResult(stage=stage, start_point=sp, ok=False, error=str(e))
            except Exception as e:  # noqa: BLE001
                r = StageResult(stage=stage, start_point=sp, ok=False, error=repr(e))
            results.append(r)
            if self.on_progress:
                self.on_progress(r)
            if not r.ok:
                break
            time.sleep(self.cfg.sleep_ms / 1000.0)
        return results

    def clear_range(self, from_stage: Optional[int], to_stage: int) -> List[StageResult]:
        all_results: List[StageResult] = []
        stage = from_stage if from_stage and from_stage > 0 else self.probe_current_stage()
        log.debug("clear_range from=%s to=%s", stage, to_stage)
        guard = 0
        while stage <= to_stage and guard < 200:
            guard += 1
            log.debug("clearing stage %s", stage)
            rs = self.clear_stage(stage)
            all_results.extend(rs)
            if not rs or not rs[-1].ok:
                # resync on mismatch
                err = rs[-1].error if rs else ""
                cur = parse_current_stage_from_error(err)
                if cur is not None and cur != stage:
                    stage = cur
                    continue
                break
            # advance using response on last point
            last = rs[-1]
            if last.current_after is not None and last.start_point == self.cfg.clear_points[-1]:
                if last.current_after <= stage:
                    stage += 1
                else:
                    stage = last.current_after
            else:
                stage += 1
        return all_results
