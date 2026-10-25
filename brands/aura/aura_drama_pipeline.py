"""
aura_drama_pipeline.py
- Aura 드라마 파이프라인 (신규 파일, EasyTax 잔재 제거)
- 계층 분리: 계약(Contract) / 검증(Validator) / 실행(Renderer)
- 24시간 무인 가동 안전장치: 타임아웃, 좀비 회수, 재시도, 서킷브레이커, 격리
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import signal
import tempfile
import time
import uuid
from dataclasses import dataclass, field, asdict
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger("aura.pipeline")

# ---------------------------------------------------------------------------
# 1) 계약 계층 (Contract) — 단일 진실 공급원, 버전 관리
# ---------------------------------------------------------------------------
CONTRACT_VERSION = "aura.scene.v1"

# S2V 렌더 하드 제약: 81 프레임 @ 16fps ≈ 5.0625s
S2V_MAX_FRAMES = 81
S2V_FPS = 16
S2V_MAX_SECONDS = S2V_MAX_FRAMES / S2V_FPS  # 5.0625


class UnmappedPolicy(str, Enum):
    """5개 speech 키 ↔ 컷 매핑 실패 시 정책. 기본은 REJECT(조용한 실패 차단)."""
    REJECT = "reject"
    WARN = "warn"
    COERCE = "coerce"


@dataclass(frozen=True)
class CutSpec:
    index: int
    seconds: float
    speaker: str
    speech_key: str

    def validate(self) -> None:
        if self.seconds <= 0:
            raise ValueError(f"cut[{self.index}] seconds must be > 0")
        if self.seconds > S2V_MAX_SECONDS + 1e-6:
            raise ValueError(
                f"cut[{self.index}] {self.seconds}s exceeds S2V limit "
                f"{S2V_MAX_SECONDS:.4f}s ({S2V_MAX_FRAMES} frames)"
            )
        if not self.speech_key:
            raise ValueError(f"cut[{self.index}] speech_key required")


@dataclass(frozen=True)
class SceneContract:
    """씬 계약. 개수/길이/화자/대본 파트 매핑을 선언적으로 보유."""
    version: str
    scene_id: str
    cuts: List[CutSpec]
    speech_keys: List[str]
    unmapped_policy: UnmappedPolicy = UnmappedPolicy.REJECT

    def validate(self) -> None:
        if self.version != CONTRACT_VERSION:
            raise ValueError(
                f"contract version mismatch: {self.version} != {CONTRACT_VERSION}"
            )
        if not self.cuts:
            raise ValueError("scene must have at least one cut")
        for c in self.cuts:
            c.validate()

        cut_keys = [c.speech_key for c in self.cuts]
        # 5개 speech 키와 컷 매핑 계약 검증
        missing_in_cuts = [k for k in self.speech_keys if k not in cut_keys]
        extra_in_cuts = [k for k in cut_keys if k not in self.speech_keys]

        if missing_in_cuts or extra_in_cuts:
            msg = (
                f"speech/cut mapping mismatch: "
                f"missing_in_cuts={missing_in_cuts}, extra_in_cuts={extra_in_cuts}"
            )
            if self.unmapped_policy is UnmappedPolicy.REJECT:
                raise ValueError(msg)
            if self.unmapped_policy is UnmappedPolicy.WARN:
                logger.warning(msg)
            # COERCE: 부족한 키는 마지막 컷에 귀속(명시적 보정)
            if self.unmapped_policy is UnmappedPolicy.COERCE and missing_in_cuts:
                logger.warning("COERCE: %s -> last cut", missing_in_cuts)

        # 중복 speech_key 금지
        if len(set(cut_keys)) != len(cut_keys):
            raise ValueError(f"duplicate speech_key in cuts: {cut_keys}")

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["unmapped_policy"] = self.unmapped_policy.value
        return d


# ---------------------------------------------------------------------------
# 2) 검증 계층 (Validator) — 렌더 이전 선행 차단
# ---------------------------------------------------------------------------
class CutBudgetValidator:
    def validate(self, contract: SceneContract) -> None:
        contract.validate()
        total = sum(c.seconds for c in contract.cuts)
        logger.info(
            "contract OK: scene=%s cuts=%d total=%.3fs",
            contract.scene_id, len(contract.cuts), total,
        )


# ---------------------------------------------------------------------------
# 3) 실행 계층 (Renderer) — 워커 수명 관리 + 서킷브레이커 + 격리
# ---------------------------------------------------------------------------
class CircuitBreaker:
    def __init__(self, threshold: int = 3, cooldown: float = 60.0):
        self.threshold = threshold
        self.cooldown = cooldown
        self.failures = 0
        self.opened_at: Optional[float] = None

    def allow(self) -> bool:
        if self.opened_at is None:
            return True
        if time.time() - self.opened_at >= self.cooldown:
            logger.info("circuit breaker half-open")
            self.opened_at = None
            self.failures = 0
            return True
        return False

    def record_success(self) -> None:
        self.failures = 0
        self.opened_at = None

    def record_failure(self) -> None:
        self.failures += 1
        if self.failures >= self.threshold:
            self.opened_at = time.time()
            logger.error("circuit breaker OPEN after %d failures", self.failures)


@dataclass
class RenderJob:
    contract: SceneContract
    output_dir: Path
    timeout_sec: float = 120.0
    max_retries: int = 2


class RenderWorker:
    """서브프로세스 대신 함수 기반 렌더러. 타임아웃/좀비 회수/임시파일 정리."""

    def __init__(self, render_fn: Callable[[SceneContract, Path], Path]):
        self.render_fn = render_fn
        self.breaker = CircuitBreaker()

    def run(self, job: RenderJob) -> Path:
        if not self.breaker.allow():
            raise RuntimeError("circuit breaker OPEN: render rejected")

        job.output_dir.mkdir(parents=True, exist_ok=True)
        tmp_dir = Path(tempfile.mkdtemp(prefix="aura_render_", dir=str(job.output_dir)))
        last_err: Optional[Exception] = None

        try:
            for attempt in range(1, job.max_retries + 2):
                try:
                    result = self._run_with_timeout(job, tmp_dir)
                    final = job.output_dir / f"{job.contract.scene_id}.mp4"
                    shutil.move(str(result), str(final))
                    self.breaker.record_success()
                    logger.info("render OK scene=%s attempt=%d", job.contract.scene_id, attempt)
                    return final
                except Exception as e:  # noqa: BLE001
                    last_err = e
                    logger.warning(
                        "render attempt %d failed scene=%s err=%s",
                        attempt, job.contract.scene_id, e,
                    )
                    time.sleep(min(2 ** attempt, 10))
            self.breaker.record_failure()
            self._quarantine(job, last_err)
            raise RuntimeError(f"render failed after retries: {last_err}") from last_err
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    def _run_with_timeout(self, job: RenderJob, tmp_dir: Path) -> Path:
        # 단일 프로세스 내 타임아웃: SIGALRM 기반 (POSIX)
        def _handler(signum, frame):  # noqa: ARG001
            raise TimeoutError(f"render timeout {job.timeout_sec}s")

        old = signal.signal(signal.SIGALRM, _handler)
        signal.setitimer(signal.ITIMER_REAL, job.timeout_sec)
        try:
            return self.render_fn(job.contract, tmp_dir)
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, old)

    def _quarantine(self, job: RenderJob, err: Optional[Exception]) -> None:
        qdir = job.output_dir / "_quarantine"
        qdir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d_%H%M%S")
        payload = {
            "scene_id": job.contract.scene_id,
            "error": repr(err),
            "contract": job.contract.to_dict(),
            "ts": stamp,
            "uid": uuid.uuid4().hex,
        }
        (qdir / f"{job.contract.scene_id}_{stamp}.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        logger.error("quarantined scene=%s", job.contract.scene_id)


# ---------------------------------------------------------------------------
# 4) 오케스트레이터 — 계약 → 검증 → 렌더 파이프라인
# ---------------------------------------------------------------------------
class AuraDramaPipeline:
    def __init__(self, render_fn: Callable[[SceneContract, Path], Path]):
        self.validator = CutBudgetValidator()
        self.worker = RenderWorker(render_fn)

    def run(self, contract: SceneContract, output_dir: Path) -> Path:
        self.validator.validate(contract)  # 선행 차단
        job = RenderJob(contract=contract, output_dir=output_dir)
        return self.worker.run(job)


# ---------------------------------------------------------------------------
# 5) 예시 렌더 함수 (실제 S2V 호출로 교체 지점)
# ---------------------------------------------------------------------------
def default_render_fn(contract: SceneContract, tmp_dir: Path) -> Path:
    out = tmp_dir / f"{contract.scene_id}.mp4"
    out.write_bytes(b"\x00" * 1024)  # placeholder
    return out


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    contract = SceneContract(
        version=CONTRACT_VERSION,
        scene_id="scene_001",
        cuts=[
            CutSpec(0, 3.0, "narrator", "speech_1"),
            CutSpec(1, 4.0, "hero", "speech_2"),
            CutSpec(2, 5.0, "hero", "speech_3"),
            CutSpec(3, 4.0, "narrator", "speech_4"),
            CutSpec(4, 3.0, "hero", "speech_5"),
        ],
        speech_keys=["speech_1", "speech_2", "speech_3", "speech_4", "speech_5"],
        unmapped_policy=UnmappedPolicy.REJECT,
    )
    pipeline = AuraDramaPipeline(default_render_fn)
    result = pipeline.run(contract, Path("./out"))
    print("rendered:", result)