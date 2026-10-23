# -*- coding: utf-8 -*-
"""
snapshot_engine.py — 시간별 스냅샷 원장 & 원본 대조 Diff & 타임머신 롤백 전용 모듈
================================================================================

[모듈 분리 원칙]
  project_manager.py 에서 분리된 독립 서비스 모듈.
  ProjectManager 는 본 모듈의 BackupDiffRollbackEngine 을 조립(composition)하여
  스냅샷/원장/롤백 기능을 위임한다. 본 모듈은 ProjectManager 를 import 하지 않는다.
  (단방향 의존: project_manager -> snapshot_engine)

[저장 구조]
  <project_root>/.deepseek_snapshots/
      |-- timeline.jsonl                       # append-only 원장 (JSONL)
      |-- .ledger.lock                         # 원장/스냅샷 동시성 잠금 파일
      |-- objects/<sha256 앞2자>/<sha256>.snap # 스냅샷 바이너리 (내용 주소 저장)
      |-- diffs/<sha256 앞2자>/<sha256>.diff   # unified diff 텍스트
      +-- tmp/                                 # 원자적 쓰기 임시 파일 (좀비 자동 회수)

[핵심 계약]
  1) SnapshotRecord / DiffRecord  : 불변 데이터 모델 (to_dict / from_dict)
  2) atomic_write_bytes           : 임시파일 -> fsync -> 해시검증 -> os.replace
  3) LedgerLock                   : 스레드 락 + OS 파일 락 + 타임아웃 방어
  4) SnapshotStore                : 내용 주소(content-addressed) 저장/해시 대조 읽기
  5) TimelineLedger               : append-only 원장, parent 체인 무결성 검증
  6) DiffEngine                   : difflib unified diff + +/- 라인 집계
  7) RollbackOrchestrator         : snapshot_id / timestamp / 직전 시점 복원
  8) BackupDiffRollbackEngine     : 파사드 (create_backup/get_timeline/get_diff/
                                    rollback_file/verify/좀비 회수)

[설계 원칙]
  - 땜질 금지: `except: pass` 없음. 모든 예외는 명시적으로 잡아 로깅 후
    도메인 예외로 승격하거나 명시적 실패 결과를 반환한다.
  - 원자성: 모든 파일 쓰기는 atomic_write_bytes 를 경유한다.
  - 무결성: 모든 읽기는 sha256 대조를 수행하고 불일치 시 SnapshotIntegrityError.
  - 멱등성: 동일 내용 재저장 시 객체 파일을 재사용한다(내용 주소 저장).
"""

from __future__ import annotations

import os
import io
import json
import time
import errno
import shutil
import hashlib
import logging
import difflib
import datetime
import threading
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, Iterable, Iterator, List, Optional, Tuple

logger = logging.getLogger(__name__)


# ======================================================================
# 전역 상수
# ======================================================================
BACKUP_DIR_NAME = ".deepseek_snapshots"
LEDGER_FILE_NAME = "timeline.jsonl"
LOCK_FILE_NAME = ".ledger.lock"

OBJECTS_DIR_NAME = "objects"
DIFFS_DIR_NAME = "diffs"
TMP_DIR_NAME = "tmp"

SNAPSHOT_EXT = ".snap"
DIFF_EXT = ".diff"

TS_FORMAT = "%Y%m%d_%H%M%S"
ISO_FORMAT = "%Y-%m-%dT%H:%M:%S"

DEFAULT_LOCK_TIMEOUT = 10.0            # 초
DEFAULT_MAX_SNAPSHOTS_PER_FILE = 200   # 파일당 원장 보존 상한 (초과분 객체 회수)
ZOMBIE_TMP_MAX_AGE = 3600.0            # 좀비 임시파일 판정 기준 (초)
READ_CHUNK = 1024 * 1024

MAX_SNAPSHOT_BYTES = 64 * 1024 * 1024  # 단일 스냅샷 최대 크기 (64 MiB)
DIFF_CONTEXT_LINES = 3                 # unified diff 컨텍스트 라인 수
MIN_FREE_DISK_BYTES = 8 * 1024 * 1024  # 원장 append 시 최소 여유 공간 (8 MiB)

# 롤백 직전 자동 보존 스냅샷을 식별하는 note 접두사.
# latest()/find_previous() 는 이 접두사 레코드를 '사용자 이력'에서 제외하여
# rollback_previous() 가 자기 자신을 복원하는 무한 루프를 원천 차단한다.
PRE_ROLLBACK_NOTE_PREFIX = "[pre-rollback]"


# ======================================================================
# 도메인 예외
# ======================================================================
class SnapshotEngineError(Exception):
    """snapshot_engine 의 모든 오류의 최상위 예외."""


class SnapshotIntegrityError(SnapshotEngineError):
    """해시 불일치 / 원장 체인 손상 등 무결성 위반."""


class SnapshotNotFoundError(SnapshotEngineError):
    """요청한 스냅샷 또는 대상 파일이 존재하지 않음."""


class LedgerLockTimeout(SnapshotEngineError):
    """원장 잠금 획득 타임아웃."""


class LedgerCorruptError(SnapshotEngineError):
    """원장(JSONL) 라인 파싱 불가 등 복구 불가 손상."""


class DiskSpaceError(SnapshotEngineError):
    """디스크 여유 공간 부족."""


# ======================================================================
# 경로 유틸
# ======================================================================
def norm_path(path: Any) -> str:
    """
    경로 비교용 정규화 문자열.

    Windows 드라이브 문자 대소문자(c: vs C:) 및 구분자(\\ vs /) 불일치로 인한
    비교 실패를 원천 차단한다. project_manager._norm 과 동일 규약.
    """
    if path is None:
        return ""
    raw = str(path).strip().strip('"').strip("'")
    if not raw:
        return ""
    return os.path.normcase(os.path.abspath(os.path.expanduser(raw)))


def to_posix_rel(path: str) -> str:
    """원장 기록용 상대경로를 '/' 구분자로 통일한다."""
    return str(path).replace(os.sep, "/").replace("\\", "/")


def utc_now() -> datetime.datetime:
    """타임존 없는 UTC 현재 시각 (원장 기록 일관성 확보)."""
    return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)


def iso_now() -> str:
    return utc_now().strftime(ISO_FORMAT)


def parse_when(value: Any) -> Optional[datetime.datetime]:
    """
    다양한 시각 표현을 datetime 으로 정규화한다.

    지원 형식:
      - None                        -> None
      - datetime                    -> 그대로 (naive 는 UTC 로 간주)
      - int / float                 -> epoch 초
      - "YYYYMMDD_HHMMSS"
      - "YYYY-MM-DD HH:MM:SS"
      - "YYYY-MM-DDTHH:MM:SS"
      - "YYYY-MM-DD HH:MM"
      - "YYYY-MM-DD"
      - 숫자 문자열                  -> epoch 초
    """
    if value is None:
        return None
    if isinstance(value, datetime.datetime):
        if value.tzinfo is not None:
            return value.astimezone(datetime.timezone.utc).replace(tzinfo=None)
        return value
    if isinstance(value, (int, float)):
        return datetime.datetime.fromtimestamp(float(value), datetime.timezone.utc).replace(tzinfo=None)

    text = str(value).strip()
    if not text:
        return None

    for fmt in (TS_FORMAT, ISO_FORMAT, "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.datetime.strptime(text, fmt)
        except ValueError:
            continue

    try:
        return datetime.datetime.fromtimestamp(float(text), datetime.timezone.utc).replace(tzinfo=None)
    except (TypeError, ValueError):
        return None


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: str) -> str:
    """파일 전체를 청크 단위로 읽어 sha256 을 계산한다 (대용량 안전)."""
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(READ_CHUNK)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def ensure_disk_space(path: str, required_bytes: int,
                      min_free_bytes: int = MIN_FREE_DISK_BYTES) -> None:
    """
    지정 경로가 속한 볼륨의 여유 공간을 검사한다.

    Args:
        path           : 검사 기준 경로 (파일 또는 디렉터리)
        required_bytes : 이번 쓰기로 소모될 바이트
        min_free_bytes : 쓰기 후 최소한 남아 있어야 할 여유 바이트

    Raises:
        DiskSpaceError: 여유 공간 부족
        SnapshotEngineError: 여유 공간 조회 자체가 불가한 경우
    """
    if required_bytes < 0:
        raise ValueError("required_bytes 는 0 이상이어야 합니다: %r" % (required_bytes,))

    probe = os.path.abspath(path)
    while probe and not os.path.exists(probe):
        parent = os.path.dirname(probe)
        if parent == probe:
            break
        probe = parent

    try:
        usage = shutil.disk_usage(probe)
    except OSError as exc:
        raise SnapshotEngineError(
            "디스크 여유 공간 조회 실패: %s (%s)" % (probe, exc)
        ) from exc

    need = int(required_bytes) + int(min_free_bytes)
    if usage.free < need:
        raise DiskSpaceError(
            "디스크 여유 공간 부족: free=%d B, need=%d B (요청 %d B + 예비 %d B) @ %s"
            % (usage.free, need, required_bytes, min_free_bytes, probe)
        )


# ======================================================================
# 데이터 모델
# ======================================================================
@dataclass
class SnapshotRecord:
    """
    스냅샷 1건의 원장 레코드.

    Attributes:
        snapshot_id        : 고유 식별자 (시각 마이크로초 + pid + 내용해시 12자)
        target_path        : 대상 파일의 절대경로
        created_at         : 생성 시각 (UTC ISO, "YYYY-MM-DDTHH:MM:SS")
        size               : 스냅샷 바이트 크기
        sha256             : 스냅샷 내용 sha256
        snapshot_relpath   : 스냅샷 객체 상대경로 (BACKUP_DIR_NAME 기준, '/' 구분)
        parent_snapshot_id : 직전 스냅샷 id (체인 무결성 검증용, 최초는 "")
        diff_relpath       : 직전 스냅샷 대비 unified diff 상대경로 (없으면 "")
        diff_sha256        : diff 파일 sha256 (없으면 "")
        note               : 변경 요약 메모
    """

    snapshot_id: str
    target_path: str
    created_at: str
    size: int
    sha256: str
    snapshot_relpath: str
    parent_snapshot_id: str = ""
    diff_relpath: str = ""
    diff_sha256: str = ""
    note: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def to_json_line(self) -> str:
        """원장 1줄(JSON) 직렬화. 개행 문자는 포함하지 않는다."""
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=False)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "SnapshotRecord":
        if not isinstance(data, dict):
            raise LedgerCorruptError("SnapshotRecord 는 dict 여야 합니다: %r" % (type(data),))
        try:
            return cls(
                snapshot_id=str(data["snapshot_id"]),
                target_path=str(data["target_path"]),
                created_at=str(data["created_at"]),
                size=int(data.get("size", 0)),
                sha256=str(data.get("sha256", "")),
                snapshot_relpath=str(data.get("snapshot_relpath", "")),
                parent_snapshot_id=str(data.get("parent_snapshot_id") or ""),
                diff_relpath=str(data.get("diff_relpath") or ""),
                diff_sha256=str(data.get("diff_sha256") or ""),
                note=str(data.get("note") or ""),
            )
        except KeyError as exc:
            raise LedgerCorruptError("SnapshotRecord 필수 키 누락: %s" % exc) from exc

    @property
    def created_dt(self) -> Optional[datetime.datetime]:
        return parse_when(self.created_at)

    @property
    def is_pre_rollback(self) -> bool:
        """롤백 직전 자동 보존 스냅샷인지 여부."""
        return self.note.startswith(PRE_ROLLBACK_NOTE_PREFIX)


@dataclass
class DiffRecord:
    """
    두 스냅샷 사이의 diff 레코드.

    Attributes:
        from_snapshot_id : 비교 기준(이전) 스냅샷 id (최초 스냅샷이면 "")
        to_snapshot_id   : 비교 대상(이후) 스냅샷 id
        created_at       : 생성 시각 (UTC ISO)
        added_lines      : 추가된 라인 수 ('+' 라인)
        removed_lines    : 삭제된 라인 수 ('-' 라인)
        changed          : 내용 변경 여부
        unified_diff     : unified diff 전문
    """

    from_snapshot_id: str
    to_snapshot_id: str
    created_at: str
    added_lines: int
    removed_lines: int
    changed: bool
    unified_diff: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def to_json_line(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=False)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "DiffRecord":
        if not isinstance(data, dict):
            raise LedgerCorruptError("DiffRecord 는 dict 여야 합니다: %r" % (type(data),))
        try:
            return cls(
                from_snapshot_id=str(data.get("from_snapshot_id") or ""),
                to_snapshot_id=str(data.get("to_snapshot_id") or ""),
                created_at=str(data.get("created_at") or ""),
                added_lines=int(data.get("added_lines", 0)),
                removed_lines=int(data.get("removed_lines", 0)),
                changed=bool(data.get("changed", False)),
                unified_diff=str(data.get("unified_diff") or ""),
            )
        except (TypeError, ValueError) as exc:
            raise LedgerCorruptError("DiffRecord 필드 형식 오류: %s" % exc) from exc


# ======================================================================
# 원자적 쓰기
# ======================================================================
def atomic_write_bytes(path: str, data: bytes, expected_sha256: Optional[str] = None) -> str:
    """
    바이트를 원자적으로 기록한다.

    절차:
      1) 대상 디렉터리 생성
      2) 동일 디렉터리에 임시 파일 생성 ('.tmp-<pid>-<tid>-<ns>' 접미사)
      3) 임시 파일에 전체 기록 후 flush + os.fsync (디스크 반영 강제)
      4) 기록된 바이트의 sha256 을 재계산하여 expected_sha256 과 대조
      5) os.replace 로 원자적 교체 (동일 볼륨이므로 원자성 보장)
      6) 실패 시 임시 파일을 반드시 제거

    Returns:
        기록된 데이터의 sha256 (16진 문자열)

    Raises:
        SnapshotIntegrityError: 해시 검증 실패
        OSError: 파일 시스템 오류
    """
    if not isinstance(data, (bytes, bytearray)):
        raise TypeError("atomic_write_bytes 는 bytes 를 요구합니다: %r" % (type(data),))

    payload = bytes(data)
    digest = sha256_bytes(payload)

    if expected_sha256 and digest != expected_sha256:
        raise SnapshotIntegrityError(
            "기록 전 해시 불일치: expected=%s actual=%s" % (expected_sha256, digest)
        )

    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)

    tmp_name = ".tmp-%d-%d-%d" % (os.getpid(), threading.get_ident(), time.time_ns())
    tmp_path = os.path.join(parent, tmp_name)

    try:
        with open(tmp_path, "wb") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())

        # 디스크에 실제로 기록된 바이트를 다시 읽어 해시 검증
        written = sha256_file(tmp_path)
        if written != digest:
            raise SnapshotIntegrityError(
                "기록 후 해시 불일치: expected=%s actual=%s (%s)" % (digest, written, tmp_path)
            )

        os.replace(tmp_path, path)
        return digest

    except BaseException:
        # 실패 시 임시 파일 정리 (정리 실패는 로깅만, 원 예외를 가리지 않음)
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError as cleanup_exc:
                logger.warning("[atomic_write] 임시 파일 정리 실패: %s (%s)", tmp_path, cleanup_exc)
        raise


def atomic_write_text(path: str, text: str, encoding: str = "utf-8",
                      expected_sha256: Optional[str] = None) -> str:
    """문자열을 원자적으로 기록한다 (개행 변형 없이 그대로 보존)."""
    if not isinstance(text, str):
        raise TypeError("atomic_write_text 는 str 을 요구합니다: %r" % (type(text),))
    return atomic_write_bytes(path, text.encode(encoding), expected_sha256=expected_sha256)


# ======================================================================
# 동시성 / 잠금
# ======================================================================
class LedgerLock:
    """
    원장 및 스냅샷 저장소에 대한 상호배제 잠금.

    이중 방어:
      1) threading.Lock  : 동일 프로세스 내 스레드 간 배제
      2) OS 파일 락      : 프로세스 간 배제
         - POSIX  : fcntl.flock(LOCK_EX | LOCK_NB) 재시도
         - Windows: msvcrt.locking(LK_NBLCK) 재시도

    타임아웃 내에 획득하지 못하면 LedgerLockTimeout 을 발생시킨다.
    """

    def __init__(self, lock_path: str, timeout: float = DEFAULT_LOCK_TIMEOUT,
                 poll_interval: float = 0.02) -> None:
        self.lock_path = os.path.abspath(lock_path)
        self.timeout = float(timeout)
        self.poll_interval = float(poll_interval)
        self._thread_lock = threading.Lock()
        self._fh: Optional[io.IOBase] = None
        self._depth = 0

    # -- 컨텍스트 매니저 ------------------------------------------------
    def __enter__(self) -> "LedgerLock":
        self.acquire()
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        self.release()
        return False

    # -- 획득 / 해제 ----------------------------------------------------
    def acquire(self) -> None:
        # 재진입(중첩) 지원: 동일 인스턴스가 이미 보유 중이면 깊이만 증가
        if self._depth > 0:
            self._depth += 1
            return

        deadline = time.monotonic() + self.timeout

        if not self._thread_lock.acquire(timeout=self.timeout):
            raise LedgerLockTimeout(
                "스레드 락 획득 실패 (timeout=%.2fs): %s" % (self.timeout, self.lock_path)
            )

        try:
            parent = os.path.dirname(self.lock_path)
            if parent:
                os.makedirs(parent, exist_ok=True)

            while True:
                try:
                    self._fh = open(self.lock_path, "a+b")
                    self._os_lock(self._fh)
                    break
                except OSError as exc:
                    if self._fh is not None:
                        try:
                            self._fh.close()
                        except OSError as close_exc:
                            logger.debug("[LedgerLock] 실패 핸들 정리 오류: %s", close_exc)
                        self._fh = None

                    if time.monotonic() >= deadline:
                        raise LedgerLockTimeout(
                            "파일 락 획득 타임아웃 (%.2fs): %s (%s)"
                            % (self.timeout, self.lock_path, exc)
                        ) from exc
                    time.sleep(self.poll_interval)

            self._depth = 1
        except BaseException:
            self._thread_lock.release()
            raise

    def release(self) -> None:
        if self._depth == 0:
            return
        self._depth -= 1
        if self._depth > 0:
            return

        if self._fh is not None:
            try:
                self._os_unlock(self._fh)
            except OSError as exc:
                logger.warning("[LedgerLock] 파일 락 해제 실패: %s (%s)", self.lock_path, exc)
            finally:
                try:
                    self._fh.close()
                except OSError as exc:
                    logger.warning("[LedgerLock] 핸들 종료 실패: %s (%s)", self.lock_path, exc)
                self._fh = None

        self._thread_lock.release()

    # -- OS 락 구현 -----------------------------------------------------
    @staticmethod
    def _os_lock(fh: io.IOBase) -> None:
        if os.name == "nt":
            import msvcrt
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            return
        try:
            import fcntl
        except ImportError as exc:  # 비 POSIX/비 Windows 환경
            raise OSError(errno.ENOSYS, "fcntl 미지원 플랫폼") from exc
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    @staticmethod
    def _os_unlock(fh: io.IOBase) -> None:
        if os.name == "nt":
            import msvcrt
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
            return
        try:
            import fcntl
        except ImportError:
            return
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


# ======================================================================
# 4. SnapshotStore — 스냅샷 / Diff 원본 내용 주소 저장소
# ======================================================================
class SnapshotStore:
    """
    스냅샷 바이너리 및 unified diff 파일의 물리 저장소.

    저장 규약(내용 주소 저장, content-addressed):
        objects/<sha256 앞2자>/<sha256>.snap
        diffs/<sha256 앞2자>/<sha256>.diff

    장점:
      - 동일 내용은 1회만 저장 (멱등성 / 디스크 절약)
      - 읽기 시 해시 대조로 손상 즉시 탐지
    """

    def __init__(self, base_dir: str) -> None:
        self.base_dir = os.path.abspath(base_dir)
        self.objects_dir = os.path.join(self.base_dir, OBJECTS_DIR_NAME)
        self.diffs_dir = os.path.join(self.base_dir, DIFFS_DIR_NAME)
        self.tmp_dir = os.path.join(self.base_dir, TMP_DIR_NAME)
        self.ensure_dirs()

    # -- 초기화 ---------------------------------------------------------
    def ensure_dirs(self) -> None:
        os.makedirs(self.objects_dir, exist_ok=True)
        os.makedirs(self.diffs_dir, exist_ok=True)
        os.makedirs(self.tmp_dir, exist_ok=True)

    # -- 경로 계산 ------------------------------------------------------
    def _object_path(self, digest: str) -> str:
        if not digest or len(digest) < 2:
            raise SnapshotEngineError("잘못된 해시 값: %r" % (digest,))
        return os.path.join(self.objects_dir, digest[:2], digest + SNAPSHOT_EXT)

    def _diff_path(self, digest: str) -> str:
        if not digest or len(digest) < 2:
            raise SnapshotEngineError("잘못된 해시 값: %r" % (digest,))
        return os.path.join(self.diffs_dir, digest[:2], digest + DIFF_EXT)

    def relpath(self, abs_path: str) -> str:
        """BACKUP_DIR_NAME 기준 상대경로('/' 구분)를 반환한다."""
        rel = os.path.relpath(os.path.abspath(abs_path), self.base_dir)
        return to_posix_rel(rel)

    def abspath(self, relpath: str) -> str:
        """BACKUP_DIR_NAME 기준 상대경로를 절대경로로 복원한다."""
        if not relpath:
            raise SnapshotEngineError("빈 상대경로는 해석할 수 없습니다.")
        return os.path.abspath(os.path.join(self.base_dir, relpath.replace("/", os.sep)))

    # -- 쓰기 -----------------------------------------------------------
    def write_snapshot(self, target_path: str, data: bytes, note: str = "") -> SnapshotRecord:
        """
        스냅샷 바이너리를 저장하고 SnapshotRecord 를 반환한다.

        snapshot_id = "<YYYYmmdd_HHMMSS_ffffff>_<pid>_<sha256 앞12자>"
        (시각 정렬 가능 + 프로세스 구분 + 내용 식별 동시 확보)

        Raises:
            DiskSpaceError: 스냅샷 크기 초과 또는 디스크 여유 부족
        """
        if not isinstance(data, (bytes, bytearray)):
            raise TypeError("write_snapshot 은 bytes 를 요구합니다: %r" % (type(data),))

        payload = bytes(data)
        if len(payload) > MAX_SNAPSHOT_BYTES:
            raise DiskSpaceError(
                "스냅샷 크기 초과: %d B > %d B (MAX_SNAPSHOT_BYTES)"
                % (len(payload), MAX_SNAPSHOT_BYTES)
            )

        self.ensure_dirs()
        ensure_disk_space(self.base_dir, len(payload))

        digest = sha256_bytes(payload)
        ts_str = utc_now().strftime("%Y%m%d_%H%M%S_%f")
        snapshot_id = "%s_%d_%s" % (ts_str, os.getpid(), digest[:12])

        abs_path = self._object_path(digest)

        # 내용 주소 저장: 동일 내용이 이미 있으면 재사용 (멱등)
        if os.path.isfile(abs_path):
            existing = sha256_file(abs_path)
            if existing == digest:
                logger.debug("[SnapshotStore] 스냅샷 객체 재사용: %s", abs_path)
            else:
                logger.warning(
                    "[SnapshotStore] 손상된 스냅샷 객체 복구: %s (expected=%s actual=%s)",
                    abs_path, digest, existing,
                )
                atomic_write_bytes(abs_path, payload, expected_sha256=digest)
        else:
            atomic_write_bytes(abs_path, payload, expected_sha256=digest)

        return SnapshotRecord(
            snapshot_id=snapshot_id,
            target_path=os.path.abspath(target_path),
            created_at=iso_now(),
            size=len(payload),
            sha256=digest,
            snapshot_relpath=self.relpath(abs_path),
            parent_snapshot_id="",
            diff_relpath="",
            diff_sha256="",
            note=note or "",
        )

    def write_diff(self, diff_text: str) -> Tuple[str, str]:
        """
        unified diff 텍스트를 저장한다 (내용 주소 저장, 멱등).

        Returns:
            (diff_relpath, sha256)
        """
        if not isinstance(diff_text, str):
            raise TypeError("write_diff 는 str 을 요구합니다: %r" % (type(diff_text),))

        self.ensure_dirs()
        payload = diff_text.encode("utf-8")
        ensure_disk_space(self.base_dir, len(payload))

        digest = sha256_bytes(payload)
        abs_path = self._diff_path(digest)

        if os.path.isfile(abs_path):
            existing = sha256_file(abs_path)
            if existing == digest:
                logger.debug("[SnapshotStore] diff 객체 재사용: %s", abs_path)
            else:
                logger.warning(
                    "[SnapshotStore] 손상된 diff 객체 복구: %s (expected=%s actual=%s)",
                    abs_path, digest, existing,
                )
                atomic_write_bytes(abs_path, payload, expected_sha256=digest)
        else:
            atomic_write_bytes(abs_path, payload, expected_sha256=digest)

        return self.relpath(abs_path), digest

    # -- 읽기 (해시 대조) -----------------------------------------------
    def read_snapshot(self, rec: SnapshotRecord) -> bytes:
        """
        스냅샷을 읽고 sha256 을 대조한다.

        Raises:
            SnapshotNotFoundError : 파일 없음
            SnapshotIntegrityError: 해시 불일치
        """
        abs_path = self.abspath(rec.snapshot_relpath)
        if not os.path.isfile(abs_path):
            raise SnapshotNotFoundError("스냅샷 객체가 존재하지 않습니다: %s" % abs_path)

        with open(abs_path, "rb") as fh:
            data = fh.read()

        actual = sha256_bytes(data)
        if rec.sha256 and actual != rec.sha256:
            raise SnapshotIntegrityError(
                "스냅샷 해시 불일치: %s (expected=%s actual=%s)"
                % (abs_path, rec.sha256, actual)
            )
        return data

    def read_diff(self, relpath: str, expected_sha256: str = "") -> str:
        """diff 텍스트를 읽고 (지정 시) sha256 을 대조한다."""
        abs_path = self.abspath(relpath)
        if not os.path.isfile(abs_path):
            raise SnapshotNotFoundError("diff 객체가 존재하지 않습니다: %s" % abs_path)

        with open(abs_path, "rb") as fh:
            data = fh.read()

        actual = sha256_bytes(data)
        if expected_sha256 and actual != expected_sha256:
            raise SnapshotIntegrityError(
                "diff 해시 불일치: %s (expected=%s actual=%s)"
                % (abs_path, expected_sha256, actual)
            )

        try:
            return data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise SnapshotIntegrityError(
                "diff 객체 UTF-8 디코딩 실패: %s (%s)" % (abs_path, exc)
            ) from exc

    # -- 좀비 임시파일 회수 ---------------------------------------------
    def sweep_zombie_tmp(self, max_age: float = ZOMBIE_TMP_MAX_AGE) -> List[str]:
        """
        tmp/ 및 objects/·diffs/ 하위에 남은 고아 임시파일('.tmp-*')을 회수한다.

        Returns:
            삭제된 파일 절대경로 목록
        """
        removed: List[str] = []
        now = time.time()

        for base in (self.tmp_dir, self.objects_dir, self.diffs_dir):
            if not os.path.isdir(base):
                continue
            for dirpath, _dirnames, filenames in os.walk(base):
                for name in filenames:
                    if not name.startswith(".tmp-"):
                        continue
                    abs_path = os.path.join(dirpath, name)
                    try:
                        age = now - os.path.getmtime(abs_path)
                    except OSError as exc:
                        logger.warning("[SnapshotStore] 임시파일 stat 실패: %s (%s)", abs_path, exc)
                        continue
                    if age < max_age:
                        continue
                    try:
                        os.remove(abs_path)
                        removed.append(abs_path)
                        logger.info("[SnapshotStore] 좀비 임시파일 회수: %s (age=%.0fs)", abs_path, age)
                    except OSError as exc:
                        logger.warning("[SnapshotStore] 임시파일 삭제 실패: %s (%s)", abs_path, exc)

        return removed


# ======================================================================
# 5. DiffEngine — unified diff 생성 및 +/- 라인 집계
# ======================================================================
class DiffEngine:
    """
    두 스냅샷(바이트) 사이의 unified diff 를 생성하고 변경량을 집계한다.

    [개행 정규화]
      CRLF(\r\n) 와 LF(\n) 차이만 있는 파일은 '변경 없음'으로 판정한다.
      Windows IDE 에서 저장할 때마다 전체 라인이 변경으로 잡히는 노이즈를 차단한다.
      단, CR 단독(\r)은 정규화 대상이 아니며 실제 변경으로 취급한다.

    [라인 집계 정확성]
      difflib.unified_diff 는 '---' / '+++' 파일 헤더 라인을 포함하므로,
      헤더를 제외한 뒤 '+' / '-' 접두사 라인만 집계한다.
      '+++' / '---' 헤더를 추가/삭제 라인으로 오집계하는 버그를 원천 차단한다.
    """

    def __init__(self, context_lines: int = DIFF_CONTEXT_LINES) -> None:
        if context_lines < 0:
            raise ValueError("context_lines 는 0 이상이어야 합니다: %r" % (context_lines,))
        self.context_lines = int(context_lines)

    # -- 개행 정규화 ----------------------------------------------------
    @staticmethod
    def normalize_newlines(data: bytes) -> bytes:
        """CRLF -> LF 정규화 (CR 단독은 보존)."""
        if not isinstance(data, (bytes, bytearray)):
            raise TypeError("normalize_newlines 는 bytes 를 요구합니다: %r" % (type(data),))
        return bytes(data).replace(b"\r\n", b"\n")

    @staticmethod
    def decode_text(data: bytes) -> str:
        """
        바이트를 텍스트로 디코딩한다.

        UTF-8 우선, 실패 시 cp949(한국어 Windows 기본) 폴백.
        두 인코딩 모두 실패하면 SnapshotEngineError 로 승격한다.
        """
        if not isinstance(data, (bytes, bytearray)):
            raise TypeError("decode_text 는 bytes 를 요구합니다: %r" % (type(data),))

        payload = bytes(data)
        for encoding in ("utf-8", "cp949"):
            try:
                return payload.decode(encoding)
            except UnicodeDecodeError as exc:
                logger.debug("[DiffEngine] %s 디코딩 실패, 다음 인코딩 시도: %s", encoding, exc)

        raise SnapshotEngineError(
            "텍스트 디코딩 실패 (utf-8/cp949 모두 실패, %d bytes)" % len(payload)
        )

    def _to_lines(self, data: bytes) -> List[str]:
        """
        바이트를 개행 정규화된 라인 리스트로 변환한다.

        splitlines() 를 사용하여 말미 개행 유무 차이를 흡수한다.
        (파일 끝 개행 추가/제거만으로 전체 라인이 변경되는 노이즈 차단)
        """
        normalized = self.normalize_newlines(data)
        text = self.decode_text(normalized)
        return text.splitlines()

    # -- 라인 집계 ------------------------------------------------------
    @staticmethod
    def count_changes(diff_lines: Iterable[str]) -> Tuple[int, int]:
        """
        unified diff 라인 목록에서 (added, removed) 를 정확히 집계한다.

        규칙:
          - '+++' / '---' 로 시작하는 파일 헤더 라인은 집계에서 제외
          - '+' 로 시작하는 라인 = 추가
          - '-' 로 시작하는 라인 = 삭제
          - ' ' / '@@' / '\\' 로 시작하는 라인 = 무시

        Returns:
            (added_lines, removed_lines)
        """
        added = 0
        removed = 0

        for line in diff_lines:
            if line.startswith("+++") or line.startswith("---"):
                continue
            if line.startswith("+"):
                added += 1
            elif line.startswith("-"):
                removed += 1

        return added, removed

    # -- diff 생성 ------------------------------------------------------
    def build(self, old_data: bytes, new_data: bytes,
              old_label: str = "before", new_label: str = "after",
              from_snapshot_id: str = "", to_snapshot_id: str = "") -> DiffRecord:
        """
        두 스냅샷 바이트로 DiffRecord 를 생성한다.

        변경이 없으면 changed=False, unified_diff="" 를 반환한다.
        (개행 스타일 차이만 있는 경우도 변경 없음으로 판정)

        Args:
            old_data         : 이전 스냅샷 바이트
            new_data         : 이후 스냅샷 바이트
            old_label        : diff 헤더에 표기할 이전 파일 라벨
            new_label        : diff 헤더에 표기할 이후 파일 라벨
            from_snapshot_id : 이전 스냅샷 id
            to_snapshot_id   : 이후 스냅샷 id

        Returns:
            DiffRecord
        """
        if not isinstance(old_data, (bytes, bytearray)):
            raise TypeError("build 의 old_data 는 bytes 여야 합니다: %r" % (type(old_data),))
        if not isinstance(new_data, (bytes, bytearray)):
            raise TypeError("build 의 new_data 는 bytes 여야 합니다: %r" % (type(new_data),))

        old_lines = self._to_lines(bytes(old_data))
        new_lines = self._to_lines(bytes(new_data))

        # 정규화 후 완전 동일 -> 변경 없음 (diff 계산 자체를 생략)
        if old_lines == new_lines:
            return DiffRecord(
                from_snapshot_id=from_snapshot_id,
                to_snapshot_id=to_snapshot_id,
                created_at=iso_now(),
                added_lines=0,
                removed_lines=0,
                changed=False,
                unified_diff="",
            )

        diff_iter = difflib.unified_diff(
            old_lines,
            new_lines,
            fromfile=old_label,
            tofile=new_label,
            n=self.context_lines,
            lineterm="",
        )
        diff_lines = list(diff_iter)

        added, removed = self.count_changes(diff_lines)
        unified = "\n".join(diff_lines)

        return DiffRecord(
            from_snapshot_id=from_snapshot_id,
            to_snapshot_id=to_snapshot_id,
            created_at=iso_now(),
            added_lines=added,
            removed_lines=removed,
            changed=bool(diff_lines),
            unified_diff=unified,
        )

    def build_from_text(self, old_text: str, new_text: str,
                        old_label: str = "before", new_label: str = "after",
                        from_snapshot_id: str = "", to_snapshot_id: str = "") -> DiffRecord:
        """텍스트 두 개로 DiffRecord 를 생성한다 (편의 API)."""
        if not isinstance(old_text, str) or not isinstance(new_text, str):
            raise TypeError("build_from_text 는 str 두 개를 요구합니다.")
        return self.build(
            old_text.encode("utf-8"),
            new_text.encode("utf-8"),
            old_label=old_label,
            new_label=new_label,
            from_snapshot_id=from_snapshot_id,
            to_snapshot_id=to_snapshot_id,
        )


# ======================================================================
# 6. TimelineLedger — append-only 원장 (timeline.jsonl)
# ======================================================================
class TimelineLedger:
    """
    스냅샷 이력의 append-only 원장.

    파일: <base_dir>/timeline.jsonl  (1줄 = 1 SnapshotRecord JSON)
    잠금: <base_dir>/.ledger.lock    (LedgerLock)

    [append-only 원칙]
      기존 라인은 절대 수정/삭제하지 않는다. 추가는 항상 파일 끝에만 수행한다.
      이로써 크래시 시에도 부분 기록된 마지막 줄만 손상되고, 그 이전 이력은
      온전히 보존된다.

    [손상 라인 정책]
      - load_all()      : 손상 라인은 경고 로깅 후 건너뛴다 (가용성 우선)
      - validate()      : 손상 라인을 전수 수집하여 LedgerCorruptError 로 승격
                          (무결성 감사 우선)
    """

    def __init__(self, base_dir: str, lock_timeout: float = DEFAULT_LOCK_TIMEOUT) -> None:
        self.base_dir = os.path.abspath(base_dir)
        self.ledger_path = os.path.join(self.base_dir, LEDGER_FILE_NAME)
        self.lock_path = os.path.join(self.base_dir, LOCK_FILE_NAME)
        self.lock_timeout = float(lock_timeout)
        os.makedirs(self.base_dir, exist_ok=True)

    # -- 잠금 -----------------------------------------------------------
    def _lock(self) -> LedgerLock:
        return LedgerLock(self.lock_path, timeout=self.lock_timeout)

    # -- append ---------------------------------------------------------
    def append(self, rec: SnapshotRecord) -> SnapshotRecord:
        """
        레코드 1건을 원장 끝에 추가한다.

        절차:
          1) 디스크 여유 공간 검사 (DiskSpaceError)
          2) LedgerLock 획득 (LedgerLockTimeout)
          3) JSON 1줄 직렬화 후 개행 추가
          4) 파일 끝에 append + flush + fsync (부분 기록 최소화)

        Raises:
            TypeError        : rec 이 SnapshotRecord 가 아닌 경우
            DiskSpaceError   : 디스크 여유 부족
            LedgerLockTimeout: 잠금 획득 실패
            SnapshotEngineError: 파일 기록 실패
        """
        if not isinstance(rec, SnapshotRecord):
            raise TypeError("append 는 SnapshotRecord 를 요구합니다: %r" % (type(rec),))

        line = rec.to_json_line()
        if "\n" in line or "\r" in line:
            raise LedgerCorruptError(
                "직렬화된 레코드에 개행이 포함되어 원장 1줄 규약을 위반합니다: %s"
                % rec.snapshot_id
            )

        payload = (line + "\n").encode("utf-8")
        ensure_disk_space(self.base_dir, len(payload))

        with self._lock():
            try:
                with open(self.ledger_path, "ab") as fh:
                    fh.write(payload)
                    fh.flush()
                    os.fsync(fh.fileno())
            except OSError as exc:
                raise SnapshotEngineError(
                    "원장 append 실패: %s (%s)" % (self.ledger_path, exc)
                ) from exc

        logger.debug("[TimelineLedger] append 완료: %s (%s)", rec.snapshot_id, rec.target_path)
        return rec

    # -- 원시 라인 읽기 -------------------------------------------------
    def _read_raw_lines(self) -> List[Tuple[int, str]]:
        """
        원장을 (줄번호, 원문) 목록으로 반환한다. 빈 줄은 제외.

        줄번호는 1부터 시작하며, 손상 라인 보고에 사용된다.
        """
        if not os.path.isfile(self.ledger_path):
            return []

        try:
            with open(self.ledger_path, "r", encoding="utf-8", newline="") as fh:
                raw = fh.read()
        except OSError as exc:
            raise SnapshotEngineError(
                "원장 읽기 실패: %s (%s)" % (self.ledger_path, exc)
            ) from exc
        except UnicodeDecodeError as exc:
            raise LedgerCorruptError(
                "원장 UTF-8 디코딩 실패: %s (%s)" % (self.ledger_path, exc)
            ) from exc

        result: List[Tuple[int, str]] = []
        for idx, line in enumerate(raw.splitlines(), start=1):
            if not line.strip():
                continue
            result.append((idx, line))
        return result

    # -- load_all -------------------------------------------------------
    def load_all(self, target_path: Optional[str] = None,
                 reverse: bool = False) -> List[SnapshotRecord]:
        """
        전체 또는 특정 파일의 스냅샷 이력을 로드한다.

        Args:
            target_path : None 이면 전체, 지정 시 해당 파일 이력만
            reverse     : True 면 최신순(역순), False 면 과거순(정순)

        Returns:
            SnapshotRecord 목록

        Note:
            손상 라인은 경고 로깅 후 건너뛴다. 엄격 감사는 validate() 사용.
        """
        wanted = norm_path(target_path) if target_path else ""
        records: List[SnapshotRecord] = []

        for lineno, line in self._read_raw_lines():
            try:
                data = json.loads(line)
                rec = SnapshotRecord.from_dict(data)
            except (json.JSONDecodeError, LedgerCorruptError) as exc:
                logger.warning(
                    "[TimelineLedger] 손상 라인 건너뜀 (line %d): %s", lineno, exc
                )
                continue

            if wanted and norm_path(rec.target_path) != wanted:
                continue
            records.append(rec)

        if reverse:
            records.reverse()
        return records

    # -- validate -------------------------------------------------------
    def validate(self) -> Dict[str, Any]:
        """
        원장 전수 감사.

        검사 항목:
          1) JSON 파싱 가능 여부
          2) 필수 필드 존재 여부 (SnapshotRecord.from_dict)
          3) snapshot_id 중복 여부
          4) snapshot_id / target_path / created_at 비어있음 여부
          5) created_at 파싱 가능 여부
          6) parent_snapshot_id 체인 무결성 (동일 파일 내 존재 여부)

        Returns:
            {
              "total": int, "valid": int, "corrupt": int,
              "duplicates": [...], "chain_breaks": [...], "errors": [...]
            }

        Raises:
            LedgerCorruptError: 손상/중복/체인 단절이 1건이라도 발견된 경우
        """
        raw_lines = self._read_raw_lines()

        errors: List[str] = []
        duplicates: List[str] = []
        chain_breaks: List[str] = []

        seen_ids: Dict[str, int] = {}
        records: List[SnapshotRecord] = []
        corrupt = 0

        for lineno, line in raw_lines:
            try:
                data = json.loads(line)
            except json.JSONDecodeError as exc:
                corrupt += 1
                errors.append("line %d: JSON 파싱 실패 (%s)" % (lineno, exc))
                continue

            try:
                rec = SnapshotRecord.from_dict(data)
            except LedgerCorruptError as exc:
                corrupt += 1
                errors.append("line %d: 레코드 변환 실패 (%s)" % (lineno, exc))
                continue

            if not rec.snapshot_id:
                corrupt += 1
                errors.append("line %d: snapshot_id 가 비어 있습니다" % lineno)
                continue
            if not rec.target_path:
                corrupt += 1
                errors.append("line %d: target_path 가 비어 있습니다 (%s)" % (lineno, rec.snapshot_id))
                continue
            if not rec.created_at:
                corrupt += 1
                errors.append("line %d: created_at 이 비어 있습니다 (%s)" % (lineno, rec.snapshot_id))
                continue
            if rec.created_dt is None:
                corrupt += 1
                errors.append(
                    "line %d: created_at 파싱 불가 (%s, %r)"
                    % (lineno, rec.snapshot_id, rec.created_at)
                )
                continue

            if rec.snapshot_id in seen_ids:
                duplicates.append(
                    "snapshot_id 중복: %s (line %d, 최초 line %d)"
                    % (rec.snapshot_id, lineno, seen_ids[rec.snapshot_id])
                )
            else:
                seen_ids[rec.snapshot_id] = lineno

            records.append(rec)

        # parent 체인 무결성: 동일 파일 내에서 parent 가 실재해야 한다
        by_target: Dict[str, set] = {}
        for rec in records:
            by_target.setdefault(norm_path(rec.target_path), set()).add(rec.snapshot_id)

        for rec in records:
            if not rec.parent_snapshot_id:
                continue
            siblings = by_target.get(norm_path(rec.target_path), set())
            if rec.parent_snapshot_id not in siblings:
                chain_breaks.append(
                    "체인 단절: %s 의 parent=%s 가 동일 파일 이력에 없음"
                    % (rec.snapshot_id, rec.parent_snapshot_id)
                )

        report = {
            "total": len(raw_lines),
            "valid": len(records),
            "corrupt": corrupt,
            "duplicates": duplicates,
            "chain_breaks": chain_breaks,
            "errors": errors,
        }

        if corrupt or duplicates or chain_breaks:
            raise LedgerCorruptError(
                "원장 무결성 감사 실패: total=%d valid=%d corrupt=%d 중복=%d 체인단절=%d | %s"
                % (
                    report["total"], report["valid"], corrupt,
                    len(duplicates), len(chain_breaks),
                    "; ".join((errors + duplicates + chain_breaks)[:5]),
                )
            )

        logger.info("[TimelineLedger] 원장 감사 통과: %d 건", report["valid"])
        return report

    # -- latest ---------------------------------------------------------
    def latest(self, target_path: Optional[str] = None,
               include_pre_rollback: bool = False) -> Optional[SnapshotRecord]:
        """
        최신 스냅샷을 조회한다.

        Args:
            target_path          : None 이면 전체 최신, 지정 시 해당 파일 최신
            include_pre_rollback : False 면 롤백 직전 자동 보존 스냅샷을 제외
                                   (rollback_previous 무한 루프 방지)

        Returns:
            SnapshotRecord 또는 None (이력 없음)
        """
        records = self.load_all(target_path=target_path, reverse=True)

        for rec in records:
            if not include_pre_rollback and rec.is_pre_rollback:
                continue
            return rec

        return None

    # -- find_at_or_before ----------------------------------------------
    def find_at_or_before(self, target_path: str, target_time: Any,
                          include_pre_rollback: bool = False) -> Optional[SnapshotRecord]:
        """
        지정 시각과 같거나 그 이전의 가장 최근 스냅샷을 조회한다.

        Args:
            target_path          : 대상 파일 경로 (필수)
            target_time          : datetime / epoch / 문자열 시각
            include_pre_rollback : 롤백 직전 스냅샷 포함 여부

        Returns:
            SnapshotRecord 또는 None

        Raises:
            ValueError: target_time 파싱 불가
        """
        if not target_path:
            raise ValueError("find_at_or_before 는 target_path 가 필수입니다.")

        when = parse_when(target_time)
        if when is None:
            raise ValueError("target_time 을 해석할 수 없습니다: %r" % (target_time,))

        records = self.load_all(target_path=target_path, reverse=True)

        for rec in records:
            if not include_pre_rollback and rec.is_pre_rollback:
                continue
            created = rec.created_dt
            if created is None:
                logger.warning(
                    "[TimelineLedger] created_at 파싱 불가 레코드 건너뜀: %s (%r)",
                    rec.snapshot_id, rec.created_at,
                )
                continue
            if created <= when:
                return rec

        return None

    # -- find_previous --------------------------------------------------
    def find_previous(self, target_path: str, snapshot_id: str,
                      include_pre_rollback: bool = False) -> Optional[SnapshotRecord]:
        """
        지정 스냅샷의 직전 스냅샷을 조회한다.

        parent_snapshot_id 체인을 우선 신뢰하되, 체인이 비어 있거나
        참조 대상이 없으면 시간순 인접 레코드로 폴백한다.

        Args:
            target_path          : 대상 파일 경로 (필수)
            snapshot_id          : 기준 스냅샷 id (필수)
            include_pre_rollback : 롤백 직전 스냅샷 포함 여부

        Returns:
            직전 SnapshotRecord 또는 None (기준이 최초이거나 미발견)

        Raises:
            ValueError           : 인자 누락
            SnapshotNotFoundError: 기준 snapshot_id 가 이력에 없음
        """
        if not target_path:
            raise ValueError("find_previous 는 target_path 가 필수입니다.")
        if not snapshot_id:
            raise ValueError("find_previous 는 snapshot_id 가 필수입니다.")

        records = self.load_all(target_path=target_path, reverse=False)

        index = -1
        for i, rec in enumerate(records):
            if rec.snapshot_id == snapshot_id:
                index = i
                break

        if index < 0:
            raise SnapshotNotFoundError(
                "기준 스냅샷을 찾을 수 없습니다: %s (%s)" % (snapshot_id, target_path)
            )

        current = records[index]

        # 1순위: parent 체인
        if current.parent_snapshot_id:
            for rec in records:
                if rec.snapshot_id == current.parent_snapshot_id:
                    if not include_pre_rollback and rec.is_pre_rollback:
                        break
                    return rec
            logger.warning(
                "[TimelineLedger] parent 체인 참조 실패, 시간순 폴백: %s -> %s",
                current.snapshot_id, current.parent_snapshot_id,
            )

        # 2순위: 시간순 인접 레코드 폴백
        for i in range(index - 1, -1, -1):
            rec = records[i]
            if not include_pre_rollback and rec.is_pre_rollback:
                continue
            return rec

        return None

    # -- 유틸 -----------------------------------------------------------
    def count(self, target_path: Optional[str] = None) -> int:
        """원장 레코드 수를 반환한다."""
        return len(self.load_all(target_path=target_path))

    def exists(self) -> bool:
        """원장 파일 존재 여부."""
        return os.path.isfile(self.ledger_path)


# ======================================================================
# 7. RollbackOrchestrator — 스냅샷 복원 실행기
# ======================================================================
class RollbackOrchestrator:
    """
    스냅샷 복원(타임머신) 실행기.

    [복원 3경로]
      1) rollback_to_snapshot(snapshot_id)  : 특정 스냅샷으로 복원
      2) rollback_to_time(target_time)      : 특정 시각 이전 상태로 복원
      3) rollback_previous()                : 현재 파일의 직전 상태로 복원

    [안전장치]
      - 복원 직전 현재 파일 상태를 '[pre-rollback]' note 로 자동 스냅샷 보존.
        -> 복원 자체를 되돌릴 수 있는 안전망 확보.
      - 이 pre-rollback 레코드는 latest()/find_previous() 에서 기본 제외되어
        rollback_previous() 가 자기 자신을 복원하는 무한 루프를 원천 차단.
      - 복원 쓰기는 atomic_write_bytes 경유 (부분 기록으로 인한 파일 파손 방지).
      - 복원 후 sha256 재검증으로 무결성 확정.
    """

    def __init__(self, store: SnapshotStore, ledger: TimelineLedger,
                 diff_engine: DiffEngine) -> None:
        if not isinstance(store, SnapshotStore):
            raise TypeError("store 는 SnapshotStore 여야 합니다: %r" % (type(store),))
        if not isinstance(ledger, TimelineLedger):
            raise TypeError("ledger 는 TimelineLedger 여야 합니다: %r" % (type(ledger),))
        if not isinstance(diff_engine, DiffEngine):
            raise TypeError("diff_engine 은 DiffEngine 여야 합니다: %r" % (type(diff_engine),))

        self.store = store
        self.ledger = ledger
        self.diff_engine = diff_engine

    # -- 내부: 복원 직전 현재 상태 보존 ---------------------------------
    def _preserve_current(self, target_path: str, reason: str) -> Optional[SnapshotRecord]:
        """
        복원 직전 현재 파일 상태를 스냅샷으로 보존한다.

        파일이 존재하지 않으면 보존하지 않고 None 을 반환한다
        (신규 생성 파일을 롤백하는 정당한 시나리오).
        """
        abs_path = os.path.abspath(target_path)
        if not os.path.isfile(abs_path):
            logger.info("[Rollback] 복원 직전 보존 생략 (파일 없음): %s", abs_path)
            return None

        try:
            with open(abs_path, "rb") as fh:
                current = fh.read()
        except OSError as exc:
            raise SnapshotEngineError(
                "복원 직전 현재 파일 읽기 실패: %s (%s)" % (abs_path, exc)
            ) from exc

        note = "%s %s" % (PRE_ROLLBACK_NOTE_PREFIX, reason)
        rec = self.store.write_snapshot(abs_path, current, note=note)

        previous = self.ledger.latest(target_path=abs_path, include_pre_rollback=True)
        if previous is not None:
            rec.parent_snapshot_id = previous.snapshot_id
            diff = self.diff_engine.build(
                self.store.read_snapshot(previous),
                current,
                old_label=previous.snapshot_id,
                new_label=rec.snapshot_id,
                from_snapshot_id=previous.snapshot_id,
                to_snapshot_id=rec.snapshot_id,
            )
            if diff.changed:
                relpath, digest = self.store.write_diff(diff.unified_diff)
                rec.diff_relpath = relpath
                rec.diff_sha256 = digest

        self.ledger.append(rec)
        logger.info("[Rollback] 복원 직전 상태 보존 완료: %s", rec.snapshot_id)
        return rec

    # -- 내부: 실제 복원 실행 -------------------------------------------
    def _apply(self, rec: SnapshotRecord, reason: str) -> Dict[str, Any]:
        """
        스냅샷 레코드를 실제 파일로 복원한다.

        Returns:
            {"ok": True, "snapshot_id":..., "target_path":..., "size":...,
             "sha256":..., "preserved_snapshot_id":...}
        """
        abs_path = os.path.abspath(rec.target_path)

        # 1) 복원 직전 현재 상태 보존 (안전망)
        preserved = self._preserve_current(abs_path, reason)

        # 2) 스냅샷 바이트 로드 (해시 대조 포함)
        data = self.store.read_snapshot(rec)

        # 3) 원자적 복원
        ensure_disk_space(abs_path, len(data))
        written = atomic_write_bytes(abs_path, data, expected_sha256=rec.sha256)

        # 4) 복원 후 재검증
        actual = sha256_file(abs_path)
        if actual != rec.sha256:
            raise SnapshotIntegrityError(
                "복원 후 해시 불일치: %s (expected=%s actual=%s)"
                % (abs_path, rec.sha256, actual)
            )

        logger.info(
            "[Rollback] 복원 완료: %s -> %s (%d bytes, sha=%s)",
            rec.snapshot_id, abs_path, len(data), written[:12],
        )

        return {
            "ok": True,
            "snapshot_id": rec.snapshot_id,
            "target_path": abs_path,
            "size": len(data),
            "sha256": written,
            "preserved_snapshot_id": preserved.snapshot_id if preserved else "",
            "reason": reason,
        }

    # -- 공개 API -------------------------------------------------------
    def rollback_to_snapshot(self, snapshot_id: str) -> Dict[str, Any]:
        """
        특정 snapshot_id 로 복원한다.

        Raises:
            ValueError           : snapshot_id 누락
            SnapshotNotFoundError: 해당 id 가 원장에 없음
        """
        if not snapshot_id:
            raise ValueError("rollback_to_snapshot 은 snapshot_id 가 필수입니다.")

        for rec in self.ledger.load_all(reverse=True):
            if rec.snapshot_id == snapshot_id:
                return self._apply(rec, reason="rollback_to_snapshot(%s)" % snapshot_id)

        raise SnapshotNotFoundError("복원 대상 스냅샷을 찾을 수 없습니다: %s" % snapshot_id)

    def rollback_to_time(self, target_path: str, target_time: Any) -> Dict[str, Any]:
        """
        지정 시각과 같거나 그 이전 상태로 복원한다.

        Raises:
            ValueError           : 인자 누락 / 시각 파싱 불가
            SnapshotNotFoundError: 해당 시점 이전 스냅샷 없음
        """
        if not target_path:
            raise ValueError("rollback_to_time 은 target_path 가 필수입니다.")

        rec = self.ledger.find_at_or_before(target_path, target_time)
        if rec is None:
            raise SnapshotNotFoundError(
                "지정 시각 이전 스냅샷이 없습니다: %s @ %r" % (target_path, target_time)
            )

        return self._apply(rec, reason="rollback_to_time(%r)" % (target_time,))

    def rollback_previous(self, target_path: str) -> Dict[str, Any]:
        """
        현재 파일의 직전 스냅샷 상태로 복원한다.

        pre-rollback 보존 스냅샷은 기본 제외되므로,
        반복 호출 시에도 자기 자신을 복원하는 무한 루프가 발생하지 않는다.

        Raises:
            ValueError           : target_path 누락
            SnapshotNotFoundError: 직전 스냅샷 없음
        """
        if not target_path:
            raise ValueError("rollback_previous 는 target_path 가 필수입니다.")

        latest = self.ledger.latest(target_path=target_path)
        if latest is None:
            raise SnapshotNotFoundError(
                "복원할 스냅샷 이력이 없습니다: %s" % target_path
            )

        previous = self.ledger.find_previous(target_path, latest.snapshot_id)
        if previous is None:
            raise SnapshotNotFoundError(
                "직전 스냅샷이 없습니다 (최초 스냅샷): %s" % latest.snapshot_id
            )

        return self._apply(previous, reason="rollback_previous(%s)" % latest.snapshot_id)


# ======================================================================
# 8. BackupDiffRollbackEngine — 파사드 (ProjectManager 위임 대상)
# ======================================================================
class BackupDiffRollbackEngine:
    """
    스냅샷 / 원장 / Diff / 롤백 기능의 단일 진입점 파사드.

    ProjectManager 는 본 클래스 하나만 조립하면 되며,
    내부 컴포넌트(Store/Ledger/DiffEngine/Orchestrator)는 은닉된다.

    사용 예:
        engine = BackupDiffRollbackEngine(project_root)
        rec    = engine.create_backup("src/main.py", note="자동 저장")
        hist   = engine.get_timeline("src/main.py")
        diff   = engine.get_diff("src/main.py", rec.snapshot_id)
        result = engine.rollback_file("src/main.py", snapshot_id=rec.snapshot_id)
    """

    def __init__(self, project_root: str,
                 max_snapshots_per_file: int = DEFAULT_MAX_SNAPSHOTS_PER_FILE,
                 lock_timeout: float = DEFAULT_LOCK_TIMEOUT) -> None:
        if not project_root:
            raise ValueError("project_root 가 필요합니다.")

        self.project_root = os.path.abspath(project_root)
        self.base_dir = os.path.join(self.project_root, BACKUP_DIR_NAME)
        self.max_snapshots_per_file = int(max_snapshots_per_file)

        self.store = SnapshotStore(self.base_dir)
        self.ledger = TimelineLedger(self.base_dir, lock_timeout=lock_timeout)
        self.diff_engine = DiffEngine(DIFF_CONTEXT_LINES)
        self.orchestrator = RollbackOrchestrator(self.store, self.ledger, self.diff_engine)

        logger.info("[BackupEngine] 초기화 완료: %s", self.base_dir)

    # -- 경로 해석 ------------------------------------------------------
    def _resolve(self, target_path: str) -> str:
        """
        상대경로를 프로젝트 루트 기준 절대경로로 해석한다.

        Raises:
            ValueError: 경로 누락
        """
        if not target_path:
            raise ValueError("target_path 가 필요합니다.")

        raw = str(target_path).strip().strip('"').strip("'")
        if os.path.isabs(raw):
            return os.path.abspath(raw)
        return os.path.abspath(os.path.join(self.project_root, raw))

    # -- 1) 백업 생성 ---------------------------------------------------
    def create_backup(self, target_path: str, note: str = "",
                      force: bool = False) -> Optional[SnapshotRecord]:
        """
        대상 파일의 스냅샷을 생성하고 원장에 기록한다.

        Args:
            target_path : 대상 파일 (상대/절대)
            note        : 변경 요약 메모
            force       : True 면 내용 무변경이어도 스냅샷 생성

        Returns:
            SnapshotRecord, 또는 내용 무변경이고 force=False 이면 None

        Raises:
            SnapshotNotFoundError: 대상 파일 없음
            SnapshotEngineError  : 읽기/기록 실패
        """
        abs_path = self._resolve(target_path)

        if not os.path.isfile(abs_path):
            raise SnapshotNotFoundError("백업 대상 파일이 없습니다: %s" % abs_path)

        try:
            with open(abs_path, "rb") as fh:
                data = fh.read()
        except OSError as exc:
            raise SnapshotEngineError(
                "백업 대상 읽기 실패: %s (%s)" % (abs_path, exc)
            ) from exc

        previous = self.ledger.latest(target_path=abs_path, include_pre_rollback=True)

        # 내용 무변경 스킵 (원장 비대화 방지)
        if previous is not None and not force:
            if previous.sha256 == sha256_bytes(data):
                logger.debug("[BackupEngine] 내용 무변경, 스냅샷 생략: %s", abs_path)
                return None

        rec = self.store.write_snapshot(abs_path, data, note=note)

        if previous is not None:
            rec.parent_snapshot_id = previous.snapshot_id
            try:
                old_data = self.store.read_snapshot(previous)
            except (SnapshotNotFoundError, SnapshotIntegrityError) as exc:
                logger.warning(
                    "[BackupEngine] 직전 스냅샷 로드 실패, diff 생략: %s (%s)",
                    previous.snapshot_id, exc,
                )
                old_data = None

            if old_data is not None:
                diff = self.diff_engine.build(
                    old_data,
                    data,
                    old_label=previous.snapshot_id,
                    new_label=rec.snapshot_id,
                    from_snapshot_id=previous.snapshot_id,
                    to_snapshot_id=rec.snapshot_id,
                )
                if diff.changed:
                    relpath, digest = self.store.write_diff(diff.unified_diff)
                    rec.diff_relpath = relpath
                    rec.diff_sha256 = digest

        self.ledger.append(rec)
        logger.info(
            "[BackupEngine] 스냅샷 생성: %s (%d bytes, +%s/-%s)",
            rec.snapshot_id, rec.size,
            "diff" if rec.diff_relpath else "0",
            "diff" if rec.diff_relpath else "0",
        )
        return rec

    # -- 2) 타임라인 조회 -----------------------------------------------
    def get_timeline(self, target_path: Optional[str] = None,
                     reverse: bool = True,
                     include_pre_rollback: bool = False) -> List[Dict[str, Any]]:
        """
        스냅샷 이력을 dict 목록으로 반환한다 (UI 직렬화 친화).

        Args:
            target_path          : None 이면 전체
            reverse              : True 면 최신순
            include_pre_rollback : 롤백 직전 보존 스냅샷 포함 여부
        """
        abs_path = self._resolve(target_path) if target_path else None
        records = self.ledger.load_all(target_path=abs_path, reverse=reverse)

        result: List[Dict[str, Any]] = []
        for rec in records:
            if not include_pre_rollback and rec.is_pre_rollback:
                continue
            item = rec.to_dict()
            item["is_pre_rollback"] = rec.is_pre_rollback
            result.append(item)
        return result

    # -- 3) Diff 조회 ---------------------------------------------------
    def get_diff(self, target_path: str, snapshot_id: str = "") -> Dict[str, Any]:
        """
        특정 스냅샷의 직전 대비 diff 를 반환한다.

        Args:
            target_path : 대상 파일
            snapshot_id : 조회할 스냅샷 id (빈 값이면 최신)

        Returns:
            {"snapshot_id":..., "from_snapshot_id":..., "changed":bool,
             "added_lines":int, "removed_lines":int, "diff":str}

        Raises:
            SnapshotNotFoundError: 스냅샷 또는 diff 없음
        """
        abs_path = self._resolve(target_path)

        if snapshot_id:
            target: Optional[SnapshotRecord] = None
            for rec in self.ledger.load_all(target_path=abs_path, reverse=True):
                if rec.snapshot_id == snapshot_id:
                    target = rec
                    break
            if target is None:
                raise SnapshotNotFoundError(
                    "스냅샷을 찾을 수 없습니다: %s (%s)" % (snapshot_id, abs_path)
                )
        else:
            target = self.ledger.latest(target_path=abs_path, include_pre_rollback=True)
            if target is None:
                raise SnapshotNotFoundError("스냅샷 이력이 없습니다: %s" % abs_path)

        if not target.diff_relpath:
            return {
                "snapshot_id": target.snapshot_id,
                "from_snapshot_id": target.parent_snapshot_id,
                "changed": False,
                "added_lines": 0,
                "removed_lines": 0,
                "diff": "",
            }

        text = self.store.read_diff(target.diff_relpath, target.diff_sha256)
        added, removed = DiffEngine.count_changes(text.splitlines())

        return {
            "snapshot_id": target.snapshot_id,
            "from_snapshot_id": target.parent_snapshot_id,
            "changed": True,
            "added_lines": added,
            "removed_lines": removed,
            "diff": text,
        }

    # -- 4) 롤백 --------------------------------------------------------
    def rollback_file(self, target_path: str, snapshot_id: str = "",
                      target_time: Any = None, previous: bool = False) -> Dict[str, Any]:
        """
        파일을 스냅샷 상태로 복원한다 (3경로 통합 진입점).

        우선순위: snapshot_id > target_time > previous

        Args:
            target_path : 대상 파일
            snapshot_id : 특정 스냅샷 id 로 복원
            target_time : 지정 시각 이전 상태로 복원
            previous    : 직전 상태로 복원

        Returns:
            복원 결과 dict

        Raises:
            ValueError: 복원 방식 미지정
        """
        abs_path = self._resolve(target_path)

        if snapshot_id:
            return self.orchestrator.rollback_to_snapshot(snapshot_id)
        if target_time is not None:
            return self.orchestrator.rollback_to_time(abs_path, target_time)
        if previous:
            return self.orchestrator.rollback_previous(abs_path)

        raise ValueError(
            "복원 방식이 지정되지 않았습니다: snapshot_id / target_time / previous 중 하나 필요"
        )

    # -- 5) 무결성 검증 -------------------------------------------------
    def verify(self, target_path: Optional[str] = None) -> Dict[str, Any]:
        """
        원장 및 스냅샷 객체의 무결성을 전수 검증한다.

        Returns:
            {"ledger": {...}, "objects_checked": int, "objects_missing": [...],
             "objects_corrupt": [...], "ok": bool}

        Raises:
            LedgerCorruptError: 원장 자체가 손상된 경우
        """
        report = self.ledger.validate()

        abs_path = self._resolve(target_path) if target_path else None
        records = self.ledger.load_all(target_path=abs_path)

        missing: List[str] = []
        corrupt: List[str] = []
        checked = 0

        for rec in records:
            checked += 1
            try:
                self.store.read_snapshot(rec)
            except SnapshotNotFoundError as exc:
                missing.append("%s: %s" % (rec.snapshot_id, exc))
            except SnapshotIntegrityError as exc:
                corrupt.append("%s: %s" % (rec.snapshot_id, exc))

        ok = not missing and not corrupt

        if not ok:
            logger.error(
                "[BackupEngine] 무결성 검증 실패: 누락=%d 손상=%d",
                len(missing), len(corrupt),
            )

        return {
            "ledger": report,
            "objects_checked": checked,
            "objects_missing": missing,
            "objects_corrupt": corrupt,
            "ok": ok,
        }

    # -- 6) 좀비 임시파일 회수 ------------------------------------------
    def sweep_zombie_tmp(self, max_age: float = ZOMBIE_TMP_MAX_AGE) -> List[str]:
        """고아 임시파일을 회수한다."""
        return self.store.sweep_zombie_tmp(max_age=max_age)

    # -- 7) 보존 정책 (오래된 스냅샷 정리) ------------------------------
    def prune(self, target_path: str,
              keep: Optional[int] = None) -> Dict[str, Any]:
        """
        파일당 스냅샷 보존 상한을 초과한 오래된 객체를 정리한다.

        원장(append-only)은 절대 수정하지 않으며, 물리 객체 파일만 회수한다.
        (원장 라인은 이력 증거로 영구 보존)

        Args:
            target_path : 대상 파일
            keep        : 보존 개수 (기본 max_snapshots_per_file)

        Returns:
            {"kept": int, "removed_objects": int, "removed_diffs": int}
        """
        abs_path = self._resolve(target_path)
        limit = int(keep) if keep is not None else self.max_snapshots_per_file
        if limit < 1:
            raise ValueError("keep 은 1 이상이어야 합니다: %r" % (keep,))

        records = self.ledger.load_all(target_path=abs_path, reverse=True)
        if len(records) <= limit:
            return {"kept": len(records), "removed_objects": 0, "removed_diffs": 0}

        keep_set = {rec.snapshot_id for rec in records[:limit]}
        keep_sha = {rec.sha256 for rec in records[:limit]}
        keep_diff = {rec.diff_sha256 for rec in records[:limit] if rec.diff_sha256}

        removed_objects = 0
        removed_diffs = 0

        for rec in records[limit:]:
            if rec.sha256 and rec.sha256 not in keep_sha:
                obj_path = self.store.abspath(rec.snapshot_relpath)
                if os.path.isfile(obj_path):
                    try:
                        os.remove(obj_path)
                        removed_objects += 1
                    except OSError as exc:
                        logger.warning(
                            "[BackupEngine] 스냅샷 객체 삭제 실패: %s (%s)", obj_path, exc
                        )
            if rec.diff_sha256 and rec.diff_sha256 not in keep_diff:
                diff_path = self.store.abspath(rec.diff_relpath)
                if os.path.isfile(diff_path):
                    try:
                        os.remove(diff_path)
                        removed_diffs += 1
                    except OSError as exc:
                        logger.warning(
                            "[BackupEngine] diff 객체 삭제 실패: %s (%s)", diff_path, exc
                        )

        logger.info(
            "[BackupEngine] 보존 정책 적용: kept=%d removed_objects=%d removed_diffs=%d",
            limit, removed_objects, removed_diffs,
        )
        return {
            "kept": limit,
            "removed_objects": removed_objects,
            "removed_diffs": removed_diffs,
        }
