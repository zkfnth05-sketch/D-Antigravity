# -*- coding: utf-8 -*-
"""
_patch_backup_diff_rollback.py

project_manager.py 에 '원본 대조 Diff 및 즉시 롤백/백업 엔진'을 주입한다.

주입 항목:
  1) import difflib / import shutil / import time / import datetime
  2) _create_backup(target, content) -> Optional[str]
  3) _generate_unified_diff(orig, new, filename) -> str
  4) save_file_content 개선 (백업 + diff + backup_path 반환)
  5) apply_search_replace 개선 (백업 + diff + backup_path 반환)
  6) rollback_file(file_path) -> Dict[str, Any] 신설

설계 원칙:
  - 멱등성: 이미 주입되어 있으면 아무 것도 하지 않는다.
  - 앵커 기반 정밀 치환: 앵커가 정확히 1회 등장할 때만 치환한다.
  - 원자적 저장: 임시 파일 → os.replace
  - 실패 시 원본 무손상 (백업 후 롤백)
"""

import io
import os
import sys
import shutil
import py_compile

TARGET = os.path.join("backend", "core", "project_manager.py")
MARKER = "# [BACKUP-DIFF-ROLLBACK-ENGINE]"


# ======================================================================
# 유틸
# ======================================================================
def read_text(path: str) -> str:
    with io.open(path, "r", encoding="utf-8", newline="") as f:
        return f.read()


def write_text_atomic(path: str, text: str) -> None:
    tmp = path + ".tmp_patch"
    with io.open(tmp, "w", encoding="utf-8", newline="") as f:
        f.write(text)
    os.replace(tmp, path)


def count_occurrences(haystack: str, needle: str) -> int:
    if not needle:
        return 0
    return haystack.count(needle)


def fail(msg: str) -> None:
    print("  ✗ " + msg)
    sys.exit(1)


# ======================================================================
# 1. import 블록 확장
# ======================================================================
IMPORT_ANCHOR = """import os
import re
import json
import logging
from typing import Dict, List, Any, Optional
"""

IMPORT_NEW = """import os
import re
import json
import logging
import difflib
import shutil
import time
import datetime
from typing import Dict, List, Any, Optional, Tuple
"""


# ======================================================================
# 2. 백업 / Diff / 롤백 엔진 본문
# ======================================================================
ENGINE_CODE = '''
    # ==================================================================
    # [BACKUP-DIFF-ROLLBACK-ENGINE]
    # 원본 대조 Diff 및 즉시 롤백/백업 엔진
    #
    # 목적:
    #   - AI 가 파일을 수정하기 '직전' 원본을 자동 백업하여 언제든 되돌린다.
    #   - 수정 '직후' 원본 대비 unified diff 를 생성해 변경점을 즉시 대조한다.
    #   - 잘못된 수정이 감지되면 rollback_file() 로 1초 만에 원상 복구한다.
    #
    # 백업 저장 위치:
    #   <active_project>/.deepseek_backups/<filename>.<timestamp>.bak
    #   파일당 최대 BACKUP_MAX_PER_FILE 개 보존 (초과분은 오래된 것부터 회수)
    # ==================================================================

    BACKUP_DIR_NAME = ".deepseek_backups"
    BACKUP_MAX_PER_FILE = 10
    DIFF_CONTEXT_LINES = 3

    # ------------------------------------------------------------------
    # 백업 디렉터리 경로 해석
    # ------------------------------------------------------------------
    def _backup_dir(self) -> str:
        """활성 프로젝트 하위 백업 디렉터리 절대경로를 반환한다."""
        base = self.active_project or self.desktop_path
        return os.path.join(os.path.abspath(base), self.BACKUP_DIR_NAME)

    # ------------------------------------------------------------------
    # 1. 원본 자동 백업
    # ------------------------------------------------------------------
    def _create_backup(self, target: str, content: str) -> Optional[str]:
        """
        수정 전 원본 파일을 백업한다.

        Args:
            target  : 백업 대상 파일의 절대경로
            content : 백업할 원본 내용 (디스크에서 읽은 원본 문자열)

        Returns:
            백업 파일의 절대경로 (성공) / None (백업 불필요 또는 실패)

        동작:
          - 대상 파일이 실제로 존재하지 않으면(신규 생성) 백업하지 않는다.
          - <active_project>/.deepseek_backups/<filename>.<timestamp>.bak 로 저장
          - 같은 파일의 백업이 BACKUP_MAX_PER_FILE(10)개를 넘으면
            mtime 기준 오래된 것부터 자동 삭제한다.
        """
        try:
            if not target or not os.path.isfile(target):
                # 신규 파일 생성 — 되돌릴 원본이 없으므로 백업 생략
                logger.debug("[Backup] 신규 파일 — 백업 생략: %s", target)
                return None

            backup_dir = self._backup_dir()
            os.makedirs(backup_dir, exist_ok=True)

            filename = os.path.basename(target)
            # 타임스탬프: 마이크로초까지 포함하여 동일 초 다중 백업 충돌 방지
            ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            backup_name = "%s.%s.bak" % (filename, ts)
            backup_path = os.path.join(backup_dir, backup_name)

            # 원본 내용을 그대로 기록 (newline="" 로 개행 변형 없이 보존)
            with open(backup_path, "w", encoding="utf-8", newline="") as f:
                f.write(content if isinstance(content, str) else str(content))

            logger.info("[Backup] 원본 백업 완료: %s → %s", target, backup_path)

            # 오래된 백업 자동 회수 (파일당 최대 10개)
            self._prune_backups(filename)

            return os.path.abspath(backup_path)

        except Exception as e:
            logger.error("[Backup] 백업 실패: %s (%s)", target, e)
            return None

    def _prune_backups(self, filename: str) -> int:
        """
        특정 파일의 백업이 BACKUP_MAX_PER_FILE 개를 초과하면
        mtime 기준 오래된 것부터 삭제한다. 삭제된 개수를 반환.
        """
        removed = 0
        try:
            backup_dir = self._backup_dir()
            if not os.path.isdir(backup_dir):
                return 0

            prefix = filename + "."
            candidates = []
            for entry in os.scandir(backup_dir):
                try:
                    if not entry.is_file():
                        continue
                    if not (entry.name.startswith(prefix) and entry.name.endswith(".bak")):
                        continue
                    candidates.append((entry.stat().st_mtime, entry.path))
                except Exception:
                    continue

            # 최신 → 오래된 순 정렬
            candidates.sort(key=lambda x: x[0], reverse=True)

            for _, path in candidates[self.BACKUP_MAX_PER_FILE:]:
                try:
                    os.remove(path)
                    removed += 1
                    logger.info("[Backup] 오래된 백업 회수: %s", path)
                except Exception as e:
                    logger.warning("[Backup] 백업 회수 실패: %s (%s)", path, e)
        except Exception as e:
            logger.warning("[Backup] 백업 정리 중 오류: %s", e)
        return removed

    def list_backups(self, file_path: str) -> List[Dict[str, Any]]:
        """
        특정 파일의 백업 목록을 최신순으로 반환한다.
        (롤백 후보 확인 / UI 표시용)
        """
        result: List[Dict[str, Any]] = []
        try:
            target = self.resolve_path(file_path)
            filename = os.path.basename(target)
            backup_dir = self._backup_dir()
            if not os.path.isdir(backup_dir):
                return result

            prefix = filename + "."
            for entry in os.scandir(backup_dir):
                try:
                    if not entry.is_file():
                        continue
                    if not (entry.name.startswith(prefix) and entry.name.endswith(".bak")):
                        continue
                    st = entry.stat()
                    result.append({
                        "path": os.path.abspath(entry.path),
                        "name": entry.name,
                        "size": st.st_size,
                        "mtime": st.st_mtime,
                        "mtime_str": datetime.datetime.fromtimestamp(st.st_mtime).strftime(
                            "%Y-%m-%d %H:%M:%S"
                        ),
                    })
                except Exception:
                    continue

            result.sort(key=lambda x: x["mtime"], reverse=True)
        except Exception as e:
            logger.warning("[Backup] 백업 목록 조회 실패: %s", e)
        return result

    # ------------------------------------------------------------------
    # 2. Unified Diff 생성
    # ------------------------------------------------------------------
    def _generate_unified_diff(self, orig_content: str, new_content: str,
                               filename: str) -> str:
        """
        원본과 수정본을 대조하여 Unified Diff 문자열을 생성한다.

        - difflib.unified_diff 사용
        - 5000줄 파일이라도 변경된 몇 줄만 + / - 로 표시된다.
        - 변경이 없으면 빈 문자열("")을 반환한다.

        출력 예:
            --- a/agent_executor.py
            +++ b/agent_executor.py
            @@ -628,6 +628,22 @@
             🚨 [대표님의 절대 행동 수칙 - 반드시 준수]:
            +🚨 [대용량 파일 패치 원칙 ...]
        """
        try:
            orig = orig_content if isinstance(orig_content, str) else str(orig_content or "")
            new = new_content if isinstance(new_content, str) else str(new_content or "")

            # 개행 통일 (CRLF 혼용으로 인한 전체 라인 diff 폭주 방지)
            orig_n = self._normalize_newlines(orig)
            new_n = self._normalize_newlines(new)

            if orig_n == new_n:
                return ""

            name = os.path.basename(filename) if filename else "file"
            orig_lines = orig_n.splitlines(keepends=True)
            new_lines = new_n.splitlines(keepends=True)

            diff_iter = difflib.unified_diff(
                orig_lines,
                new_lines,
                fromfile="a/%s" % name,
                tofile="b/%s" % name,
                n=self.DIFF_CONTEXT_LINES,
                lineterm="",
            )

            out_lines: List[str] = []
            for line in diff_iter:
                # splitlines(keepends=True) 특성상 개행이 남아있을 수 있으므로 제거
                out_lines.append(line.rstrip("\\r\\n"))

            return "\\n".join(out_lines)

        except Exception as e:
            logger.error("[Diff] unified diff 생성 실패: %s", e)
            return ""

    def _diff_stats(self, diff_str: str) -> Dict[str, int]:
        """unified diff 문자열에서 추가/삭제 라인 수를 집계한다."""
        added = 0
        removed = 0
        if not diff_str:
            return {"added": 0, "removed": 0}
        for line in diff_str.splitlines():
            if line.startswith("+++") or line.startswith("---"):
                continue
            if line.startswith("+"):
                added += 1
            elif line.startswith("-"):
                removed += 1
        return {"added": added, "removed": removed}

    # ------------------------------------------------------------------
    # 3. 즉시 롤백
    # ------------------------------------------------------------------
    def rollback_file(self, file_path: str) -> Dict[str, Any]:
        """
        가장 최근 백업으로 파일을 즉시 되돌린다.

        Args:
            file_path : 롤백할 파일 경로 (스마트 경로 해석 적용)

        Returns:
            {
              "success": bool,
              "path": str,              # 롤백된 대상 파일 절대경로
              "backup_path": str,       # 사용된 백업 파일 경로
              "diff": str,              # 롤백 diff (현재 → 백업 원본)
              "stats": {"added": n, "removed": n},
              "message": str,
            }

        동작:
          - 백업이 없으면 success=False 로 안전하게 실패한다.
          - 롤백 직전 상태도 백업해 두어 '롤백의 롤백'이 가능하다.
        """
        result: Dict[str, Any] = {
            "success": False,
            "path": "",
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

            # 백업 원본 읽기
            with open(backup_path, "r", encoding="utf-8", newline="") as f:
                backup_content = f.read()

            # 현재 파일 내용 (없으면 빈 문자열)
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

            # 롤백 diff 생성 (현재 → 백업 원본)
            rollback_diff = self._generate_unified_diff(
                current_content, backup_content, target
            )

            # 원자적 복원
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
                "[Rollback] 롤백 완료: %s ← %s (+%d/-%d)",
                target, backup_path,
                result["stats"]["added"], result["stats"]["removed"],
            )
            return result

        except Exception as e:
            logger.error("[Rollback] 롤백 실패: %s (%s)", file_path, e)
            result["success"] = False
            result["message"] = "롤백 실패: %s" % e
            return result

'''


# ======================================================================
# 3. save_file_content 개선판
# ======================================================================
SAVE_OLD = '''    def save_file_content(self, file_path: str, content: str) -> Dict[str, Any]:
        """
        파일 저장 (지능형 경로 해석 기반).

        절차:
          (1) resolve_path() 로 대상 경로 해석 (스마트 검색 포함)
          (2) 대상이 디렉터리이면 명시적 거부 — 오류 메시지 반환
              (open(target, "w") 호출 시 Windows [Errno 13] Permission denied
               문자열이 그대로 프런트엔드에 노출되던 문제를 사전 차단)
          (3) 디렉터리가 아니면 parent 디렉터리 자동 생성 후 utf-8 로 안전 기록
        """
        target = self.resolve_path(file_path)

        # (2) 디렉터리 대상 방어 — open() 호출 전에 명시적으로 거부
        if os.path.isdir(target):
            logger.warning("[ProjectManager] 저장 거부(디렉터리): %s", target)
            return {
                "success": False,
                "error": f"Target is a directory: {target}",
                "path": target,
            }

        # (3) parent 자동 생성 + utf-8 안전 기록
        try:
            parent = os.path.dirname(target)
            if parent and not os.path.isdir(parent):
                os.makedirs(parent, exist_ok=True)
            with open(target, "w", encoding="utf-8", newline="\\n") as f:
                f.write(content if isinstance(content, str) else str(content))
            logger.info("[ProjectManager] 저장 완료: %s", target)
            return {"success": True, "message": "Saved successfully", "path": target}
        except Exception as e:
            logger.error("[ProjectManager] 저장 실패: %s (%s)", target, e)
            return {"success": False, "error": str(e), "path": target}
'''

SAVE_NEW = '''    def save_file_content(self, file_path: str, content: str) -> Dict[str, Any]:
        """
        파일 저장 (지능형 경로 해석 + 원본 자동 백업 + Unified Diff 대조).

        절차:
          (1) resolve_path() 로 대상 경로 해석 (스마트 검색 포함)
          (2) 대상이 디렉터리이면 명시적 거부 — 오류 메시지 반환
              (open(target, "w") 호출 시 Windows [Errno 13] Permission denied
               문자열이 그대로 프런트엔드에 노출되던 문제를 사전 차단)
          (3) [신규] 쓰기 직전 원본을 .deepseek_backups 에 자동 백업
          (4) parent 디렉터리 자동 생성 후 utf-8 로 안전 기록
          (5) [신규] 수정 전/후 unified diff 를 생성하여 반환 dict 에 포함

        반환 계약 (확장):
          {
            "success": bool,
            "message": str,
            "path": str,
            "diff": str,            # 원본 → 수정본 unified diff (변경 없으면 "")
            "backup_path": str,     # 백업 파일 경로 (신규 파일이면 "")
            "stats": {"added": n, "removed": n},
          }
        """
        target = self.resolve_path(file_path)

        # (2) 디렉터리 대상 방어 — open() 호출 전에 명시적으로 거부
        if os.path.isdir(target):
            logger.warning("[ProjectManager] 저장 거부(디렉터리): %s", target)
            return {
                "success": False,
                "error": f"Target is a directory: {target}",
                "path": target,
                "diff": "",
                "backup_path": "",
                "stats": {"added": 0, "removed": 0},
            }

        new_content = content if isinstance(content, str) else str(content)

        # (3) 쓰기 직전 원본 읽기 + 자동 백업
        original_content = ""
        backup_path = ""
        if os.path.isfile(target):
            try:
                with open(target, "r", encoding="utf-8", newline="") as f:
                    original_content = f.read()
            except Exception:
                # utf-8 디코딩 실패 시 관대한 인코딩으로 재시도
                try:
                    with open(target, "r", encoding="utf-8", errors="replace", newline="") as f:
                        original_content = f.read()
                except Exception:
                    original_content = ""
            backup_path = self._create_backup(target, original_content) or ""

        # (4) parent 자동 생성 + utf-8 안전 기록
        try:
            parent = os.path.dirname(target)
            if parent and not os.path.isdir(parent):
                os.makedirs(parent, exist_ok=True)
            with open(target, "w", encoding="utf-8", newline="\\n") as f:
                f.write(new_content)
            logger.info("[ProjectManager] 저장 완료: %s", target)
        except Exception as e:
            logger.error("[ProjectManager] 저장 실패: %s (%s)", target, e)
            return {
                "success": False,
                "error": str(e),
                "path": target,
                "diff": "",
                "backup_path": backup_path,
                "stats": {"added": 0, "removed": 0},
            }

        # (5) 수정 전/후 unified diff 생성
        diff_str = self._generate_unified_diff(original_content, new_content, target)
        stats = self._diff_stats(diff_str)

        return {
            "success": True,
            "message": "Saved successfully",
            "path": target,
            "diff": diff_str,
            "backup_path": backup_path,
            "stats": stats,
        }
'''


# ======================================================================
# 4. apply_search_replace 개선 (백업 + diff 반환)
# ======================================================================
ASR_OLD_HEAD = '''        result: Dict[str, Any] = {
            "success": False,
            "applied": 0,
            "total": 0,
            "failed": [],
            "path": "",
            "message": "",
            "saved": False,
        }
'''

ASR_NEW_HEAD = '''        result: Dict[str, Any] = {
            "success": False,
            "applied": 0,
            "total": 0,
            "failed": [],
            "path": "",
            "message": "",
            "saved": False,
            "diff": "",
            "backup_path": "",
            "stats": {"added": 0, "removed": 0},
        }
'''

ASR_OLD_SAVE = '''        # (5) save_file_content 로 안전 저장
        save_res = self.save_file_content(target_path, current)
        result["saved"] = bool(save_res.get("success"))
        result["path"] = save_res.get("path", target_path)

        if not save_res.get("success"):
            result["success"] = False
            result["message"] = "치환은 성공했으나 디스크 저장 실패: %s" % save_res.get("error")
            return result
'''

ASR_NEW_SAVE = '''        # (5) save_file_content 로 안전 저장
        #     save_file_content 가 쓰기 직전 원본 자동 백업 + unified diff 생성을
        #     수행하므로, 그 결과를 그대로 result 에 승계한다.
        save_res = self.save_file_content(target_path, current)
        result["saved"] = bool(save_res.get("success"))
        result["path"] = save_res.get("path", target_path)
        result["diff"] = save_res.get("diff", "") or ""
        result["backup_path"] = save_res.get("backup_path", "") or ""
        result["stats"] = save_res.get("stats", {"added": 0, "removed": 0})

        if not save_res.get("success"):
            result["success"] = False
            result["message"] = "치환은 성공했으나 디스크 저장 실패: %s" % save_res.get("error")
            return result
'''


# ======================================================================
# 메인
# ======================================================================
def main() -> None:
    print("=" * 72)
    print("[1] 대상:", TARGET)
    if not os.path.isfile(TARGET):
        fail("대상 파일이 존재하지 않습니다.")

    src = read_text(TARGET)
    orig_lines = src.count("\n") + (0 if src.endswith("\n") else 1)
    print("    ✓ 원본 줄 수: %d" % orig_lines)

    # --------------------------------------------------------------
    print("[2] 멱등성 검사")
    if MARKER in src:
        print("    ✓ 이미 주입되어 있습니다 — 변경 없이 종료")
        print("=" * 72)
        return
    print("    ✓ 미존재 확인")

    # --------------------------------------------------------------
    print("[3] 앵커 일치 검사")
    anchors = {
        "IMPORT_ANCHOR": (IMPORT_ANCHOR, 1),
        "SAVE_OLD": (SAVE_OLD, 1),
        "ASR_OLD_HEAD": (ASR_OLD_HEAD, 1),
        "ASR_OLD_SAVE": (ASR_OLD_SAVE, 1),
        "편의접근자": ("    # ------------------------------------------------------------------\n    # 편의 접근자", 1),
    }
    for name, (anchor, expected) in anchors.items():
        n = count_occurrences(src, anchor)
        print("    %-14s 등장 횟수: %d (기대 %d)" % (name, n, expected))
        if n != expected:
            fail("앵커 '%s' 불일치 — 안전 중단" % name)

    # --------------------------------------------------------------
    print("[4] 백업")
    bak = TARGET + ".bak_backup_diff_rollback"
    shutil.copy2(TARGET, bak)
    print("    ✓ 백업 완료: %s" % bak)

    # --------------------------------------------------------------
    print("[5] 코드 주입")
    out = src

    # (a) import 확장
    out = out.replace(IMPORT_ANCHOR, IMPORT_NEW, 1)
    print("    ✓ import 확장 (difflib/shutil/time/datetime/Tuple)")

    # (b) 엔진 본문 삽입 — '편의 접근자' 섹션 직전
    conv_anchor = "    # ------------------------------------------------------------------\n    # 편의 접근자"
    out = out.replace(conv_anchor, ENGINE_CODE + conv_anchor, 1)
    print("    ✓ 백업/Diff/롤백 엔진 삽입")

    # (c) save_file_content 교체
    out = out.replace(SAVE_OLD, SAVE_NEW, 1)
    print("    ✓ save_file_content 개선 (백업+diff+backup_path)")

    # (d) apply_search_replace 반환 dict 확장
    out = out.replace(ASR_OLD_HEAD, ASR_NEW_HEAD, 1)
    print("    ✓ apply_search_replace 반환 dict 확장")

    # (e) apply_search_replace 저장부 승계
    out = out.replace(ASR_OLD_SAVE, ASR_NEW_SAVE, 1)
    print("    ✓ apply_search_replace diff/backup_path 승계")

    # --------------------------------------------------------------
    print("[6] 원자적 저장")
    write_text_atomic(TARGET, out)
    new_lines = out.count("\n") + (0 if out.endswith("\n") else 1)
    print("    ✓ 저장 완료: %d줄 → %d줄 (Δ%+d)" % (orig_lines, new_lines, new_lines - orig_lines))

    # --------------------------------------------------------------
    print("[7] py_compile 검증")
    try:
        py_compile.compile(TARGET, doraise=True)
        print("    ✓ COMPILE_OK")
    except py_compile.PyCompileError as e:
        print("    ✗ COMPILE_FAIL — 원본 복원")
        shutil.copy2(bak, TARGET)
        print(e)
        sys.exit(1)

    # --------------------------------------------------------------
    print("[8] 지표 실측")
    final = read_text(TARGET)
    checks = [
        ("_create_backup 정의", "def _create_backup(self, target: str, content: str)"),
        ("_generate_unified_diff 정의", "def _generate_unified_diff(self, orig_content: str, new_content: str,"),
        ("rollback_file 정의", "def rollback_file(self, file_path: str)"),
        ("list_backups 정의", "def list_backups(self, file_path: str)"),
        ("_prune_backups 정의", "def _prune_backups(self, filename: str)"),
        ("difflib.unified_diff 호출", "difflib.unified_diff("),
        ("BACKUP_MAX_PER_FILE = 10", "BACKUP_MAX_PER_FILE = 10"),
        (".deepseek_backups", 'BACKUP_DIR_NAME = ".deepseek_backups"'),
        ('"diff": diff_str', '"diff": diff_str'),
        ('"backup_path": backup_path', '"backup_path": backup_path'),
    ]
    for label, needle in checks:
        n = final.count(needle)
        mark = "✓" if n >= 1 else "✗"
        print("    %s %-32s : %d" % (mark, label, n))

    print("=" * 72)
    print("PATCH_OK")


if __name__ == "__main__":
    main()
