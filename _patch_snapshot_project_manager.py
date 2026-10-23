# -*- coding: utf-8 -*-
"""
project_manager.py 에 BackupDiffRollbackEngine(snapshot_engine) 을 결합하는
SEARCH/REPLACE 정밀 패치 스크립트.

적용 항목:
  1) import 블록 — snapshot_engine 결합 import (방어적 try/except)
  2) __init__ — self.snapshot_engine 초기화
  3) set_active_project — 프로젝트 전환 시 snapshot_engine 재바인딩
  4) save_file_content — 저장 직전 시간별 스냅샷 + timeline.jsonl 기록
  5) 신규 메서드 — get_timeline / get_diff / rollback_file(하위호환 확장)
"""
import io
import os
import sys

TARGET = os.path.join("backend", "core", "project_manager.py")

PATCHES = []


def patch(name, search, replace):
    PATCHES.append((name, search, replace))


# ----------------------------------------------------------------------
# 1) import 블록
# ----------------------------------------------------------------------
patch(
    "import-snapshot-engine",
    """import os
import re
import json
import logging
import difflib
import shutil
import time
import datetime
from typing import Dict, List, Any, Optional, Tuple

logger = logging.getLogger(__name__)
""",
    """import os
import re
import json
import logging
import difflib
import shutil
import time
import datetime
from typing import Dict, List, Any, Optional, Tuple

# ----------------------------------------------------------------------
# [SNAPSHOT-ENGINE 결합]
# 시간별 스냅샷 / timeline.jsonl 원장 / Diff / 롤백 엔진.
# 모듈 부재 시에도 ProjectManager 자체는 정상 동작해야 하므로 방어적 import.
# ----------------------------------------------------------------------
try:
    from .snapshot_engine import (
        BackupDiffRollbackEngine,
        SnapshotEngineError,
        SnapshotNotFoundError,
        SnapshotIntegrityError,
        LedgerCorruptError,
    )
    _SNAPSHOT_ENGINE_AVAILABLE = True
except ImportError:  # 패키지 컨텍스트가 아닌 단독 실행 대비
    try:
        from snapshot_engine import (  # type: ignore
            BackupDiffRollbackEngine,
            SnapshotEngineError,
            SnapshotNotFoundError,
            SnapshotIntegrityError,
            LedgerCorruptError,
        )
        _SNAPSHOT_ENGINE_AVAILABLE = True
    except ImportError as _snap_exc:  # pragma: no cover
        BackupDiffRollbackEngine = None  # type: ignore
        SnapshotEngineError = Exception  # type: ignore
        SnapshotNotFoundError = Exception  # type: ignore
        SnapshotIntegrityError = Exception  # type: ignore
        LedgerCorruptError = Exception  # type: ignore
        _SNAPSHOT_ENGINE_AVAILABLE = False
        logging.getLogger(__name__).warning(
            "[ProjectManager] snapshot_engine 미탑재 — 스냅샷/롤백 기능 비활성: %s", _snap_exc
        )

logger = logging.getLogger(__name__)
""",
)

# ----------------------------------------------------------------------
# 2) __init__ — snapshot_engine 초기화
# ----------------------------------------------------------------------
patch(
    "init-snapshot-engine",
    """        # 최종 방어: active_project 가 비었거나 존재하지 않으면 Desktop 으로 폴백
        if not self.active_project or not os.path.isdir(self.active_project):
            self.active_project = self.desktop_path

        self.active_project = os.path.abspath(self.active_project)
""",
    """        # 최종 방어: active_project 가 비었거나 존재하지 않으면 Desktop 으로 폴백
        if not self.active_project or not os.path.isdir(self.active_project):
            self.active_project = self.desktop_path

        self.active_project = os.path.abspath(self.active_project)

        # [SNAPSHOT-ENGINE] 시간별 스냅샷 / 타임라인 / 롤백 엔진 결합
        self.snapshot_engine: Optional[Any] = None
        self._init_snapshot_engine()
""",
)

# ----------------------------------------------------------------------
# 3) set_active_project — 재바인딩
# ----------------------------------------------------------------------
patch(
    "set-active-project-rebind",
    """        ok = self.save_config({"active_project": self.active_project})
        logger.info("[ProjectManager] 활성 프로젝트 전환: %s → %s (저장=%s)",
                    previous, self.active_project, ok)
        return True
""",
    """        ok = self.save_config({"active_project": self.active_project})

        # [SNAPSHOT-ENGINE] 프로젝트가 바뀌면 스냅샷 저장소도 새 루트로 재바인딩
        self._init_snapshot_engine()

        logger.info("[ProjectManager] 활성 프로젝트 전환: %s → %s (저장=%s)",
                    previous, self.active_project, ok)
        return True
""",
)

# ----------------------------------------------------------------------
# 4) save_file_content — 저장 직전 스냅샷 + timeline.jsonl 기록
# ----------------------------------------------------------------------
patch(
    "save-file-content-snapshot",
    """            backup_path = self._create_backup(target, original_content) or ""
""",
    """            backup_path = self._create_backup(target, original_content) or ""

            # [SNAPSHOT-ENGINE] 시간별 스냅샷 생성 + timeline.jsonl 원장 기록
            #   - 원본 바이트를 그대로 스냅샷으로 보존 (시간별 타임머신)
            #   - 직전 스냅샷 대비 unified diff 를 자동 생성하여 함께 보관
            #   - 스냅샷 실패가 파일 저장 자체를 막아서는 안 되므로 예외를 격리한다.
            self._snapshot_before_save(target, note="save_file_content")
""",
)

# ----------------------------------------------------------------------
# 5) 신규 메서드 — _init_snapshot_engine / _snapshot_before_save /
#                  get_timeline / get_diff / rollback_file(확장)
# ----------------------------------------------------------------------
patch(
    "add-snapshot-methods",
    """    # ------------------------------------------------------------------
    # 편의 접근자
    # ------------------------------------------------------------------
    def get_active_project(self) -> str:
        return self.active_project
""",
    """    # ==================================================================
    # [SNAPSHOT-ENGINE 연동 계층]
    # 시간별 스냅샷 / timeline.jsonl 원장 / Diff / 롤백 위임 API
    # ==================================================================

    # ------------------------------------------------------------------
    # 1. 엔진 초기화 / 재바인딩
    # ------------------------------------------------------------------
    def _init_snapshot_engine(self) -> Optional[Any]:
        \"\"\"
        활성 프로젝트 루트 기준으로 BackupDiffRollbackEngine 을 (재)생성한다.

        Returns:
            BackupDiffRollbackEngine 인스턴스, 또는 비활성/실패 시 None

        동작:
          - snapshot_engine 모듈이 없으면 조용히 None 으로 비활성화한다.
          - 엔진 생성 실패가 ProjectManager 초기화를 깨뜨리지 않도록 예외 격리.
          - 저장소 위치: <active_project>/.deepseek_snapshots
        \"\"\"
        self.snapshot_engine = None

        if not _SNAPSHOT_ENGINE_AVAILABLE or BackupDiffRollbackEngine is None:
            logger.debug("[ProjectManager] snapshot_engine 비활성 — 초기화 생략")
            return None

        root = self.active_project or self.desktop_path
        if not root or not os.path.isdir(root):
            logger.warning("[ProjectManager] 스냅샷 엔진 초기화 불가(루트 없음): %s", root)
            return None

        try:
            self.snapshot_engine = BackupDiffRollbackEngine(os.path.abspath(root))
            logger.info(
                "[ProjectManager] 스냅샷 엔진 결합 완료: %s",
                self.snapshot_engine.base_dir,
            )
        except Exception as e:
            logger.error("[ProjectManager] 스냅샷 엔진 초기화 실패: %s", e)
            self.snapshot_engine = None

        return self.snapshot_engine

    def _ensure_snapshot_engine(self) -> Optional[Any]:
        \"\"\"스냅샷 엔진을 지연 초기화(lazy) 후 반환한다.\"\"\"
        if self.snapshot_engine is None:
            self._init_snapshot_engine()
        return self.snapshot_engine

    # ------------------------------------------------------------------
    # 2. 저장 직전 스냅샷 생성 (내부 훅)
    # ------------------------------------------------------------------
    def _snapshot_before_save(self, target: str, note: str = "") -> Optional[Any]:
        \"\"\"
        파일 저장 직전 원본 상태를 스냅샷으로 보존하고 timeline.jsonl 에 기록한다.

        Args:
            target : 대상 파일 절대경로
            note   : 변경 요약 메모

        Returns:
            SnapshotRecord 또는 None (스킵/실패)

        원칙:
          - 스냅샷 실패는 파일 저장을 절대 방해하지 않는다 (예외 격리).
          - 내용이 직전 스냅샷과 동일하면 엔진이 자동으로 스킵한다.
        \"\"\"
        engine = self._ensure_snapshot_engine()
        if engine is None:
            return None

        try:
            rec = engine.create_backup(target, note=note)
            if rec is not None:
                logger.info(
                    "[ProjectManager] 시간별 스냅샷 기록: %s (%s)",
                    rec.snapshot_id, os.path.basename(target),
                )
            return rec
        except Exception as e:
            logger.warning(
                "[ProjectManager] 스냅샷 생성 실패(저장은 계속): %s (%s)", target, e
            )
            return None

    # ------------------------------------------------------------------
    # 3. 타임라인 조회
    # ------------------------------------------------------------------
    def get_timeline(self, file_path: Optional[str] = None,
                     limit: int = 20) -> Dict[str, Any]:
        \"\"\"
        시간별 변경 이력(타임라인)을 조회한다.

        Args:
            file_path : 대상 파일 (None 이면 활성 프로젝트 전체 이력)
            limit     : 최대 반환 건수 (최신순)

        Returns:
            {
              "success": bool,
              "path": str,                 # 조회 대상 절대경로 (전체면 "")
              "count": int,                # 실제 반환 건수
              "total": int,                # 전체 이력 건수
              "timeline": [ {...}, ... ],  # 최신순 스냅샷 레코드
              "message": str,
            }
        \"\"\"
        result: Dict[str, Any] = {
            "success": False,
            "path": "",
            "count": 0,
            "total": 0,
            "timeline": [],
            "message": "",
        }

        engine = self._ensure_snapshot_engine()
        if engine is None:
            result["message"] = "스냅샷 엔진이 비활성 상태입니다."
            return result

        try:
            target = self.resolve_path(file_path) if file_path else None
            result["path"] = target or ""

            records = engine.get_timeline(target, reverse=True)
            result["total"] = len(records)

            try:
                cap = int(limit)
            except (TypeError, ValueError):
                cap = 20
            if cap <= 0:
                cap = 20

            result["timeline"] = records[:cap]
            result["count"] = len(result["timeline"])
            result["success"] = True
            result["message"] = "타임라인 %d건 조회 완료 (전체 %d건)" % (
                result["count"], result["total"]
            )
            return result

        except Exception as e:
            logger.error("[ProjectManager] 타임라인 조회 실패: %s", e)
            result["message"] = "타임라인 조회 실패: %s" % e
            return result

    # ------------------------------------------------------------------
    # 4. Diff 조회
    # ------------------------------------------------------------------
    def get_diff(self, snapshot_id: str,
                 file_path: Optional[str] = None) -> Dict[str, Any]:
        \"\"\"
        특정 스냅샷의 직전 대비 unified diff 를 조회한다.

        Args:
            snapshot_id : 조회할 스냅샷 id
            file_path   : 대상 파일 (생략 시 원장에서 snapshot_id 로 자동 역추적)

        Returns:
            {
              "success": bool,
              "snapshot_id": str,
              "from_snapshot_id": str,
              "changed": bool,
              "added_lines": int,
              "removed_lines": int,
              "diff": str,
              "message": str,
            }
        \"\"\"
        result: Dict[str, Any] = {
            "success": False,
            "snapshot_id": snapshot_id or "",
            "from_snapshot_id": "",
            "changed": False,
            "added_lines": 0,
            "removed_lines": 0,
            "diff": "",
            "message": "",
        }

        engine = self._ensure_snapshot_engine()
        if engine is None:
            result["message"] = "스냅샷 엔진이 비활성 상태입니다."
            return result

        if not snapshot_id:
            result["message"] = "snapshot_id 가 필요합니다."
            return result

        try:
            target = self.resolve_path(file_path) if file_path else ""

            # file_path 미지정 시 원장에서 snapshot_id 로 대상 파일을 역추적
            if not target:
                for rec in engine.ledger.load_all(reverse=True):
                    if rec.snapshot_id == snapshot_id:
                        target = rec.target_path
                        break

            if not target:
                result["message"] = "스냅샷을 찾을 수 없습니다: %s" % snapshot_id
                return result

            data = engine.get_diff(target, snapshot_id)
            result.update({
                "success": True,
                "snapshot_id": data.get("snapshot_id", snapshot_id),
                "from_snapshot_id": data.get("from_snapshot_id", ""),
                "changed": bool(data.get("changed")),
                "added_lines": int(data.get("added_lines", 0)),
                "removed_lines": int(data.get("removed_lines", 0)),
                "diff": data.get("diff", "") or "",
                "message": "diff 조회 완료 (+%d/-%d)" % (
                    int(data.get("added_lines", 0)),
                    int(data.get("removed_lines", 0)),
                ),
            })
            return result

        except Exception as e:
            logger.error("[ProjectManager] diff 조회 실패: %s", e)
            result["message"] = "diff 조회 실패: %s" % e
            return result

    # ------------------------------------------------------------------
    # 5. 롤백 (하위 호환 확장)
    # ------------------------------------------------------------------
    def rollback_file(self, file_path: str,
                      snapshot_id: Optional[str] = None,
                      target_time: Any = None,
                      previous: bool = False) -> Dict[str, Any]:
        \"\"\"
        파일을 스냅샷 상태로 복원한다 (3경로 통합 + 기존 .bak 하위 호환).

        복원 경로 우선순위:
          1) snapshot_id  : 특정 스냅샷으로 복원
          2) target_time  : 지정 시각 이전 상태로 복원 (시간별 타임머신)
          3) previous     : 직전 스냅샷 상태로 복원
          4) (인자 없음)  : 기존 동작 유지 — .deepseek_backups 의 최신 .bak 으로 복원

        Args:
            file_path   : 롤백할 파일 경로 (스마트 경로 해석 적용)
            snapshot_id : 특정 스냅샷 id
            target_time : 기준 시각 (datetime / ISO 문자열 / epoch)
            previous    : 직전 스냅샷으로 복원

        Returns:
            {
              "success": bool,
              "path": str,
              "snapshot_id": str,
              "backup_path": str,
              "diff": str,
              "stats": {"added": n, "removed": n},
              "message": str,
            }
        \"\"\"
        result: Dict[str, Any] = {
            "success": False,
            "path": "",
            "snapshot_id": "",
            "backup_path": "",
            "diff": "",
            "stats": {"added": 0, "removed": 0},
            "message": "",
        }

        # (A) 스냅샷 기반 복원 — snapshot_id / target_time / previous
        if snapshot_id or target_time is not None or previous:
            engine = self._ensure_snapshot_engine()
            if engine is None:
                result["message"] = "스냅샷 엔진이 비활성 상태입니다."
                return result

            try:
                target = self.resolve_path(file_path)
                result["path"] = target

                # 복원 전 현재 내용 (diff 산출용)
                current_content = ""
                if os.path.isfile(target):
                    try:
                        with open(target, "r", encoding="utf-8", newline="") as f:
                            current_content = f.read()
                    except Exception:
                        current_content = ""

                res = engine.rollback_file(
                    target,
                    snapshot_id=snapshot_id or "",
                    target_time=target_time,
                    previous=bool(previous),
                )

                # 복원 후 내용 → 롤백 diff 산출
                restored_content = ""
                if os.path.isfile(target):
                    try:
                        with open(target, "r", encoding="utf-8", newline="") as f:
                            restored_content = f.read()
                    except Exception:
                        restored_content = ""

                rollback_diff = self._generate_unified_diff(
                    current_content, restored_content, target
                )

                result["success"] = bool(res.get("ok"))
                result["snapshot_id"] = res.get("snapshot_id", "")
                result["backup_path"] = res.get("preserved_snapshot_id", "")
                result["diff"] = rollback_diff
                result["stats"] = self._diff_stats(rollback_diff)
                result["message"] = "스냅샷 롤백 완료: %s ← %s" % (
                    os.path.basename(target), result["snapshot_id"]
                )

                logger.info(
                    "[ProjectManager] 스냅샷 롤백 완료: %s ← %s (+%d/-%d)",
                    target, result["snapshot_id"],
                    result["stats"]["added"], result["stats"]["removed"],
                )
                return result

            except Exception as e:
                logger.error("[ProjectManager] 스냅샷 롤백 실패: %s (%s)", file_path, e)
                result["success"] = False
                result["message"] = "스냅샷 롤백 실패: %s" % e
                return result

        # (B) 하위 호환 — 기존 .bak 최신본 복원 (인자 없는 기존 호출)
        return self._rollback_latest_bak(file_path)

    def _rollback_latest_bak(self, file_path: str) -> Dict[str, Any]:
        \"\"\"
        기존 .deepseek_backups 최신 .bak 파일로 복원한다 (레거시 경로).

        rollback_file() 에 스냅샷 인자가 전달되지 않았을 때의 하위 호환 동작.
        \"\"\"
        result: Dict[str, Any] = {
            "success": False,
            "path": "",
            "snapshot_id": "",
            "backup_path": "",
            "diff": "",
            "stats": {"added": 0, "removed": 0},
            "message": "",
        }

        try:
            target = self.resolve_path(file_path)
            result["path"] = target

            backups = self.list_backups(target)
            if not backups:
                result["message"] = "롤백할 백업이 존재하지 않습니다: %s" % target
                logger.warning("[Rollback] 백업 없음: %s", target)
                return result

            latest = backups[0]
            backup_path = latest["path"]

            with open(backup_path, "r", encoding="utf-8", newline="") as f:
                backup_content = f.read()

            current_content = ""
            if os.path.isfile(target):
                try:
                    with open(target, "r", encoding="utf-8", newline="") as f:
                        current_content = f.read()
                except Exception:
                    current_content = ""

            # 롤백 직전 상태도 백업 (롤백의 롤백 대비)
            if os.path.isfile(target):
                self._create_backup(target, current_content)

            rollback_diff = self._generate_unified_diff(
                current_content, backup_content, target
            )

            parent = os.path.dirname(target)
            if parent and not os.path.isdir(parent):
                os.makedirs(parent, exist_ok=True)
            with open(target, "w", encoding="utf-8", newline="") as f:
                f.write(backup_content)

            result["success"] = True
            result["backup_path"] = backup_path
            result["diff"] = rollback_diff
            result["stats"] = self._diff_stats(rollback_diff)
            result["message"] = "롤백 완료: %s (백업: %s)" % (
                os.path.basename(target), os.path.basename(backup_path)
            )

            logger.info(
                "[Rollback] .bak 롤백 완료: %s ← %s (+%d/-%d)",
                target, backup_path,
                result["stats"]["added"], result["stats"]["removed"],
            )
            return result

        except Exception as e:
            logger.error("[Rollback] .bak 롤백 실패: %s (%s)", file_path, e)
            result["success"] = False
            result["message"] = "롤백 실패: %s" % e
            return result

    # ------------------------------------------------------------------
    # 6. 스냅샷 무결성 검증
    # ------------------------------------------------------------------
    def verify_snapshots(self, file_path: Optional[str] = None) -> Dict[str, Any]:
        \"\"\"스냅샷 원장/객체 무결성을 전수 검증한다.\"\"\"
        engine = self._ensure_snapshot_engine()
        if engine is None:
            return {"success": False, "message": "스냅샷 엔진이 비활성 상태입니다."}

        try:
            target = self.resolve_path(file_path) if file_path else None
            report = engine.verify(target)
            return {
                "success": bool(report.get("ok")),
                "report": report,
                "message": "무결성 검증 %s (검사 %d건)" % (
                    "통과" if report.get("ok") else "실패",
                    report.get("objects_checked", 0),
                ),
            }
        except Exception as e:
            logger.error("[ProjectManager] 무결성 검증 실패: %s", e)
            return {"success": False, "message": "무결성 검증 실패: %s" % e}

    # ------------------------------------------------------------------
    # 편의 접근자
    # ------------------------------------------------------------------
    def get_active_project(self) -> str:
        return self.active_project
""",
)


def main():
    with io.open(TARGET, "r", encoding="utf-8") as fh:
        text = fh.read()

    original_len = len(text)
    applied = 0

    for name, search, replace in PATCHES:
        count = text.count(search)
        if count == 0:
            print("[SKIP] %s — SEARCH 미발견" % name)
            continue
        if count > 1:
            print("[ABORT] %s — SEARCH 가 %d회 중복 발견 (모호한 치환)" % (name, count))
            return 1
        text = text.replace(search, replace, 1)
        applied += 1
        print("[OK] %s" % name)

    if applied != len(PATCHES):
        print("일부 패치 미적용 — 파일을 기록하지 않습니다. (applied=%d/%d)"
              % (applied, len(PATCHES)))
        return 1

    with io.open(TARGET, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)

    print("PATCH_OK applied=%d/%d" % (applied, len(PATCHES)))
    print("LINES %d -> %d" % (original_len, len(text)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
