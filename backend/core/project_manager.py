# -*- coding: utf-8 -*-
"""
ProjectManager — 프로젝트 컨텍스트 관리자 (전면 개편판)

핵심 계약:
  1) __init__(config_or_path)  : 디렉터리 경로 / config.json 파일 경로 모두 수용
  2) resolve_path(path)        : 지능형 단일 경로 탐색 (스마트 검색)
                                 - 절대경로 존재 → 그대로
                                 - active_project 결합 존재 → 그대로
                                 - basename/상대경로 일부만 전달 → active_project 하위
                                   전체 스캔(SKIP_DIRS 제외, .agents 포함)으로 실파일 탐색
  3) auto_bind_project_for_file(file_path) : 파일이 속한 프로젝트로 active_project 자동 전환
  4) set_active_project(path)  : 전환 + config.json 영구 저장
  5) list_available_projects() : Desktop 하위 유효 프로젝트 동적 스캔

[Windows 경로 정규화 원칙]
  Windows 는 드라이브 문자 대소문자(c: vs C:)와 경로 구분자(\\ vs /)를
  대소문자 무관하게 취급하지만, 파이썬 문자열 비교는 이를 구분한다.
  따라서 모든 경로 비교는 반드시 _norm() 을 경유해야 한다.
  _norm(p) = os.path.normcase(os.path.abspath(p))
    - abspath  : 상대경로 → 절대경로, '..' 및 '.' 해소
    - normcase : Windows 에서 소문자화 + '/' → '\\' 통일 (POSIX 에서는 무변환)
"""

import os
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

# 프로젝트 루트로 인정하는 표식 파일/디렉터리 (강한 표식 우선)
STRONG_MARKERS = (
    ".git",
    "pyproject.toml",
    "package.json",
    "setup.py",
    "requirements.txt",
    "AGENTS.md",
    "config.json",
)

WEAK_MARKERS = (
    "README.md",
    "main.py",
    "index.js",
)

PROJECT_MARKERS = STRONG_MARKERS + WEAK_MARKERS

# 파일 트리 탐색 시 제외할 디렉터리
SKIP_DIRS = {
    "__pycache__", ".git", ".idea", ".vscode", "node_modules", "venv", ".venv",
    "env", "gfpgan", "chrome_profile", "reddit_profiles", "Cache", "Code Cache",
    "dist", "build", ".pytest_cache", ".mypy_cache", "site-packages",
}

CONFIG_FILENAME = "config.json"

# 스마트 검색(Smart Search) 정책
#   - SKIP_DIRS 는 제외하되, '.agents' 처럼 점(.)으로 시작하는 업무용 디렉터리는
#     반드시 탐색 대상에 포함한다. (요구사항: ".agents 포함")
SMART_SEARCH_MAX_DEPTH = 12          # 무한 재귀 방지용 최대 깊이
SMART_SEARCH_MAX_VISITS = 200000     # 대형 트리 폭주 방지용 방문 노드 상한
SMART_SEARCH_INCLUDE_DOT_DIRS = True  # 점(.) 시작 디렉터리 포함 여부


# ======================================================================
# 경로 정규화 헬퍼 (모듈 레벨 — 어디서든 재사용)
# ======================================================================
def _norm(path: Any) -> str:
    """
    경로 비교용 정규화 문자열을 반환한다.

    Windows 드라이브 문자 대소문자(c: vs C:) 및 경로 구분자(\\ vs /) 불일치로
    인한 비교 실패를 원천 차단한다.

    - None / 빈 문자열 → ""
    - 그 외 → os.path.normcase(os.path.abspath(os.path.expanduser(str(path))))
    """
    if path is None:
        return ""
    raw = str(path).strip().strip('"').strip("'")
    if not raw:
        return ""
    try:
        return os.path.normcase(os.path.abspath(os.path.expanduser(raw)))
    except Exception:
        # 비정상 경로(널 문자 등) 방어 — 비교 실패로 인한 예외 전파 차단
        return os.path.normcase(raw)


class ProjectManager:
    """프로젝트 컨텍스트(활성 프로젝트) 관리자."""

    # ------------------------------------------------------------------
    # 1. 초기화 — 디렉터리 경로 / config.json 파일 경로 모두 수용
    # ------------------------------------------------------------------
    def __init__(self, config_or_path: str):
        self.desktop_path = self._detect_desktop_path()

        self.config_path: str = ""
        self.config: Dict[str, Any] = {}
        self.active_project: str = ""

        raw = (config_or_path or "").strip().strip('"').strip("'")
        if not raw:
            raw = self.desktop_path

        raw_abs = os.path.abspath(os.path.expanduser(raw))

        if os.path.isdir(raw_abs):
            # (A) 디렉터리 경로로 초기화
            self.active_project = raw_abs
            self.config_path = self._discover_config_path(raw_abs)
            self.config = self._load_config()
            # config.json 에 저장된 active_project 가 유효하면 그것을 우선 채택
            saved = self.config.get("active_project")
            if saved and os.path.isdir(saved):
                self.active_project = os.path.abspath(saved)
            logger.info("[ProjectManager] 디렉터리 초기화: active_project=%s", self.active_project)
        else:
            # (B) 파일 경로(config.json)로 초기화
            self.config_path = raw_abs
            self.config = self._load_config()
            saved = self.config.get("active_project")
            if saved and os.path.isdir(saved):
                self.active_project = os.path.abspath(saved)
            else:
                # config.json 이 있는 폴더를 프로젝트 루트로 간주
                parent = os.path.dirname(raw_abs)
                self.active_project = parent if os.path.isdir(parent) else self.desktop_path
            logger.info("[ProjectManager] 파일 초기화: config=%s, active_project=%s",
                        self.config_path, self.active_project)

        # 최종 방어: active_project 가 비었거나 존재하지 않으면 Desktop 으로 폴백
        if not self.active_project or not os.path.isdir(self.active_project):
            self.active_project = self.desktop_path

        self.active_project = os.path.abspath(self.active_project)

        # [SNAPSHOT-ENGINE] 시간별 스냅샷 / 타임라인 / 롤백 엔진 결합
        self.snapshot_engine: Optional[Any] = None
        self._init_snapshot_engine()

    # ------------------------------------------------------------------
    # 내부 유틸
    # ------------------------------------------------------------------
    @staticmethod
    def _detect_desktop_path() -> str:
        """사용자 Desktop 경로를 견고하게 탐지."""
        candidates = [
            os.path.join(os.path.expanduser("~"), "Desktop"),
            os.path.join(os.path.expanduser("~"), "OneDrive", "Desktop"),
            os.path.join(os.path.expanduser("~"), "바탕 화면"),
            os.path.join(os.path.expanduser("~"), "OneDrive", "바탕 화면"),
        ]
        for c in candidates:
            if os.path.isdir(c):
                return os.path.abspath(c)
        return os.path.abspath(os.path.expanduser("~"))

    def _discover_config_path(self, start_dir: str) -> str:
        """
        start_dir 및 상위 폴더를 순회하며 config.json 을 탐색.
        발견 시 해당 경로, 미발견 시 start_dir/config.json 을 기본값으로 반환.
        """
        current = os.path.abspath(start_dir)
        visited = set()
        while current and current not in visited:
            visited.add(current)
            candidate = os.path.join(current, CONFIG_FILENAME)
            if os.path.isfile(candidate):
                return candidate
            parent = os.path.dirname(current)
            if parent == current:
                break
            current = parent
        return os.path.join(os.path.abspath(start_dir), CONFIG_FILENAME)

    def _load_config(self) -> Dict[str, Any]:
        """config.json 로드 (실패 시 빈 dict)."""
        if self.config_path and os.path.isfile(self.config_path):
            try:
                with open(self.config_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    return data
            except Exception as e:
                logger.warning("[ProjectManager] config 로드 실패: %s", e)
        return {}

    def save_config(self, new_cfg: Dict[str, Any]) -> bool:
        """config.json 병합 저장."""
        self.config.update(new_cfg or {})
        if not self.config_path:
            logger.warning("[ProjectManager] config_path 미설정 — 저장 생략")
            return False
        try:
            parent = os.path.dirname(self.config_path)
            if parent and not os.path.isdir(parent):
                os.makedirs(parent, exist_ok=True)
            with open(self.config_path, "w", encoding="utf-8") as f:
                json.dump(self.config, f, indent=2, ensure_ascii=False)
            return True
        except Exception as e:
            logger.error("[ProjectManager] config 저장 실패: %s", e)
            return False

    # ------------------------------------------------------------------
    # 2. 경로 해석 — 지능형 단일 경로 탐색 (Smart Search)
    # ------------------------------------------------------------------
    @staticmethod
    def _is_skippable_dir(name: str) -> bool:
        """
        스마트 검색에서 건너뛸 디렉터리인지 판정.

        - SKIP_DIRS 에 포함되면 제외 (__pycache__, node_modules, .git 등)
        - 점(.)으로 시작하는 디렉터리는 '.agents' 처럼 업무 자산일 수 있으므로
          SMART_SEARCH_INCLUDE_DOT_DIRS 가 True 이면 탐색 대상에 포함한다.
        """
        if not name:
            return True
        if name in SKIP_DIRS:
            return True
        if name.startswith(".") and not SMART_SEARCH_INCLUDE_DOT_DIRS:
            return True
        return False

    def _smart_search_file(self, root: str, target_name: str) -> Optional[str]:
        """
        root 하위 디렉터리를 전수 스캔하여 target_name(소문자 비교)과
        일치하는 실제 파일의 절대경로를 반환한다.

        - SKIP_DIRS 제외, '.agents' 등 점(.) 시작 업무 디렉터리는 포함
        - 깊이 우선(BFS) 탐색으로 얕은 경로의 파일을 우선 발견
        - 깊이 상한 / 방문 노드 상한으로 폭주 방지
        - 심볼릭 링크 순환 방어(방문 디렉터리 정규화 집합)
        """
        if not root or not os.path.isdir(root) or not target_name:
            return None

        target_lower = target_name.lower()
        root_abs = os.path.abspath(root)

        # BFS 큐: (디렉터리 절대경로, 깊이)
        queue: List[Any] = [(root_abs, 0)]
        visited_dirs = set()
        visits = 0

        while queue:
            current, depth = queue.pop(0)

            cur_norm = _norm(current)
            if cur_norm in visited_dirs:
                continue
            visited_dirs.add(cur_norm)

            if depth > SMART_SEARCH_MAX_DEPTH:
                continue

            try:
                entries = list(os.scandir(current))
            except Exception:
                continue

            # 같은 디렉터리 내에서는 파일을 먼저 확인 (정확 일치 우선)
            sub_dirs: List[str] = []
            for entry in entries:
                visits += 1
                if visits > SMART_SEARCH_MAX_VISITS:
                    logger.warning(
                        "[ProjectManager] 스마트 검색 상한 도달(%s) — 탐색 중단: %s",
                        SMART_SEARCH_MAX_VISITS, root_abs,
                    )
                    return None
                try:
                    if entry.is_file(follow_symlinks=False) or entry.is_file():
                        if entry.name.lower() == target_lower:
                            found = os.path.abspath(entry.path)
                            logger.info(
                                "[ProjectManager] 스마트 검색 성공: '%s' → %s",
                                target_name, found,
                            )
                            return found
                    elif entry.is_dir(follow_symlinks=False) or entry.is_dir():
                        if self._is_skippable_dir(entry.name):
                            continue
                        sub_dirs.append(entry.path)
                except Exception:
                    continue

            # 정렬하여 결정적(deterministic) 탐색 순서 보장
            for d in sorted(sub_dirs, key=lambda p: os.path.basename(p).lower()):
                queue.append((d, depth + 1))

        return None

    def resolve_path(self, path: str) -> str:
        """
        지능형 단일 경로 탐색 (Smart Resolve).

        해석 순서:
          (1) 빈 경로            → active_project 절대경로 반환
          (2) 절대 경로 + 존재   → 해당 경로 그대로 반환
          (3) active_project 결합 경로가 실제 존재 → 그 경로 반환
          (4) 파일명(basename)만 전달되었거나 상대경로 일부가 전달된 경우
              → active_project 하위 전체 스캔(SKIP_DIRS 제외, .agents 포함)으로
                target_name.lower() 와 일치하는 실파일의 절대경로 반환
                예) "council_runtime.py" → <active>/.agents/scripts/council_runtime.py
          (5) 그래도 없으면 active_project 기준 결합 경로 반환 (신규 파일 생성 대비)

        절대 경로가 존재하지 않는 경우에도 (4) 스마트 검색을 수행하여
        다른 위치의 동명 파일을 찾아준다. (예: 드라이브 문자만 다른 경우)
        """
        # (1) 빈 경로 → 활성 프로젝트 루트
        if not path or not str(path).strip():
            return os.path.abspath(self.active_project)

        raw = str(path).strip().strip('"').strip("'")
        raw = os.path.expanduser(raw)
        if not raw:
            return os.path.abspath(self.active_project)

        base = self.active_project or self.desktop_path

        # (2) 절대 경로이며 실제 존재 → 그대로 반환
        if os.path.isabs(raw):
            abs_candidate = os.path.abspath(raw)
            if os.path.exists(abs_candidate):
                return abs_candidate

            # (2-b) 절대 경로지만 미존재 → basename 으로 스마트 검색 시도
            target_name = os.path.basename(abs_candidate.rstrip("\\/"))
            if target_name:
                found = self._smart_search_file(base, target_name)
                if found:
                    return found
            # 스마트 검색 실패 → 요청된 절대 경로 그대로 반환
            return abs_candidate

        # (3) active_project 결합 경로가 실제 존재 → 반환
        joined = os.path.abspath(os.path.join(base, raw))
        if os.path.exists(joined):
            return joined

        # (4) 파일명/상대경로 일부만 전달된 경우 → 하위 전수 스캔
        target_name = os.path.basename(raw.rstrip("\\/"))
        if target_name:
            found = self._smart_search_file(base, target_name)
            if found:
                return found

        # (5) 최종 폴백 — active_project 기준 결합 경로
        return joined

    # ------------------------------------------------------------------
    # 3. 파일 경로 기반 프로젝트 자동 바인딩
    # ------------------------------------------------------------------
    def _find_project_root_for(self, file_path: str) -> Optional[str]:
        """
        주어진 파일 경로에서 상위로 올라가며 프로젝트 루트를 판별.

        판정 우선순위:
          1) Desktop 직계 하위 디렉터리 (가장 바깥쪽 = 실제 프로젝트 루트)
          2) 강한 표식(.git / pyproject.toml / package.json 등) 보유 디렉터리
          3) 약한 표식(README.md / main.py) 보유 디렉터리
        Desktop 외부 경로는 자동 전환 대상에서 제외한다.

        [버그 수정] Windows 드라이브 문자 대소문자(c: vs C:) 불일치로
        `os.path.dirname(current) == desktop` 비교가 실패하여 자동 바인딩이
        전혀 동작하지 않던 문제를 _norm() 기반 비교로 해결.
        """
        target = os.path.abspath(file_path)
        if not os.path.exists(target):
            target = os.path.dirname(target)

        current = os.path.dirname(target) if os.path.isfile(target) else target

        # Desktop 경로를 대소문자 무관 정규화 형태로 보관
        desktop_norm = _norm(self.desktop_path)
        desktop_child: Optional[str] = None
        strong_root: Optional[str] = None
        weak_root: Optional[str] = None

        visited = set()
        while current and current not in visited:
            visited.add(current)

            # Desktop 직계 하위 디렉터리 — 가장 바깥쪽(먼저 만난 것)을 프로젝트 루트로 확정
            # (대소문자 무관 비교: c:\Users\... 와 C:\Users\... 를 동일 취급)
            if desktop_child is None and _norm(os.path.dirname(current)) == desktop_norm:
                desktop_child = current

            if strong_root is None:
                for marker in STRONG_MARKERS:
                    if os.path.exists(os.path.join(current, marker)):
                        strong_root = current
                        break

            if weak_root is None:
                for marker in WEAK_MARKERS:
                    if os.path.exists(os.path.join(current, marker)):
                        weak_root = current
                        break

            parent = os.path.dirname(current)
            if parent == current:
                break
            current = parent

        def _under_desktop(p: Optional[str]) -> bool:
            """p 가 Desktop 내부(또는 Desktop 자신)인지 대소문자 무관하게 판정."""
            if not p:
                return False
            p_norm = _norm(p)
            if not p_norm:
                return False
            return p_norm == desktop_norm or p_norm.startswith(desktop_norm + os.sep)

        # 우선순위 1: Desktop 직계 하위 디렉터리 = 프로젝트 루트
        if _under_desktop(desktop_child):
            return os.path.abspath(desktop_child)
        # 우선순위 2: 강한 표식 디렉터리
        if _under_desktop(strong_root):
            return os.path.abspath(strong_root)
        # 우선순위 3: 약한 표식 디렉터리
        if _under_desktop(weak_root):
            return os.path.abspath(weak_root)
        return None

    def auto_bind_project_for_file(self, file_path: str) -> str:
        """
        파일 경로가 속한 프로젝트로 active_project 를 즉시 자동 전환(rebind).
        전환 발생 시 config.json 에 영구 저장하고 로그를 남긴다.
        반환값: 최종 active_project 경로.

        [버그 수정] `os.path.abspath(root) == os.path.abspath(self.active_project)`
        비교는 드라이브 문자 대소문자 차이(c: vs C:)를 구분하여, 이미 올바른
        프로젝트에 바인딩되어 있어도 매 호출마다 불필요한 재전환/재저장이
        발생하던 문제를 _norm() 기반 비교로 해결.
        """
        if not file_path:
            return self.active_project

        root = self._find_project_root_for(file_path)
        if not root:
            logger.debug("[ProjectManager] 자동 바인딩 대상 아님(Desktop 외부): %s", file_path)
            return self.active_project

        # 대소문자 무관 동일 경로 판정 — 이미 바인딩된 프로젝트면 조기 반환
        if _norm(root) == _norm(self.active_project):
            return self.active_project

        previous = self.active_project
        self.active_project = os.path.abspath(root)

        # config.json 경로도 새 프로젝트 기준으로 재탐색
        new_config = self._discover_config_path(self.active_project)
        if new_config and os.path.isfile(new_config):
            self.config_path = new_config
            self.config = self._load_config()

        self.save_config({"active_project": self.active_project})

        logger.info(
            "[ProjectManager] 프로젝트 자동 전환: %s → %s (트리거 파일: %s)",
            previous, self.active_project, file_path,
        )
        return self.active_project

    # ------------------------------------------------------------------
    # 4. 활성 프로젝트 전환 + 영구 저장
    # ------------------------------------------------------------------
    def set_active_project(self, project_path: str) -> bool:
        """프로젝트 전환. 유효 디렉터리일 때만 성공하며 config.json 에 저장."""
        if not project_path:
            return False

        candidate = os.path.abspath(os.path.expanduser(str(project_path).strip().strip('"')))
        if not (os.path.exists(candidate) and os.path.isdir(candidate)):
            logger.warning("[ProjectManager] 전환 실패(디렉터리 아님): %s", candidate)
            return False

        previous = self.active_project
        self.active_project = candidate

        new_config = self._discover_config_path(candidate)
        if new_config and os.path.isfile(new_config):
            self.config_path = new_config
            self.config = self._load_config()

        ok = self.save_config({"active_project": self.active_project})

        # [SNAPSHOT-ENGINE] 프로젝트가 바뀌면 스냅샷 저장소도 새 루트로 재바인딩
        self._init_snapshot_engine()

        logger.info("[ProjectManager] 활성 프로젝트 전환: %s → %s (저장=%s)",
                    previous, self.active_project, ok)
        return True

    # ------------------------------------------------------------------
    # 5. Desktop 내 유효 프로젝트 동적 스캔
    # ------------------------------------------------------------------
    def _is_valid_project_dir(self, path: str) -> bool:
        """프로젝트로 인정할 만한 디렉터리인지 판정."""
        if not os.path.isdir(path):
            return False
        name = os.path.basename(path)
        if name.startswith(".") or name in SKIP_DIRS:
            return False
        for marker in PROJECT_MARKERS:
            if os.path.exists(os.path.join(path, marker)):
                return True
        # 표식이 없어도 하위에 소스 파일이 있으면 프로젝트로 간주
        try:
            for entry in os.scandir(path):
                if entry.is_file() and entry.name.lower().endswith((".py", ".js", ".ts", ".bat")):
                    return True
        except Exception:
            return False
        return False

    def list_available_projects(self) -> List[Dict[str, Any]]:
        """Desktop 하위의 모든 유효 프로젝트 디렉터리를 동적으로 스캔."""
        projects: List[Dict[str, Any]] = []
        desktop = os.path.abspath(self.desktop_path)
        desktop_norm = _norm(desktop)
        active_norm = _norm(self.active_project) if self.active_project else ""

        if not os.path.isdir(desktop):
            logger.warning("[ProjectManager] Desktop 경로 없음: %s", desktop)
            return projects

        try:
            entries = sorted(os.scandir(desktop), key=lambda e: e.name.lower())
        except Exception as e:
            logger.error("[ProjectManager] Desktop 스캔 실패: %s", e)
            return projects

        for entry in entries:
            try:
                if not entry.is_dir():
                    continue
            except Exception:
                continue

            path = os.path.abspath(entry.path)
            if not self._is_valid_project_dir(path):
                continue

            projects.append({
                "name": entry.name,
                "path": path,
                "is_active": (_norm(path) == active_norm),
                "has_config": os.path.isfile(os.path.join(path, CONFIG_FILENAME)),
            })

        # 활성 프로젝트가 Desktop 하위인데 목록에 없으면 강제 포함
        if active_norm and active_norm.startswith(desktop_norm + os.sep):
            if not any(_norm(p["path"]) == active_norm for p in projects):
                projects.insert(0, {
                    "name": os.path.basename(self.active_project),
                    "path": os.path.abspath(self.active_project),
                    "is_active": True,
                    "has_config": os.path.isfile(os.path.join(self.active_project, CONFIG_FILENAME)),
                })

        return projects

    # ------------------------------------------------------------------
    # 기존 기능 유지 — 파일 트리
    # ------------------------------------------------------------------
    def get_file_tree(self, max_depth: int = 4) -> Dict[str, Any]:
        root = self.active_project
        if not root or not os.path.exists(root):
            return {"name": "Not Found", "type": "dir", "children": []}

        def build_tree(current_path: str, depth: int) -> Dict[str, Any]:
            name = os.path.basename(current_path) or current_path
            if depth > max_depth:
                return {"name": name, "path": current_path, "type": "dir", "children": []}

            children: List[Dict[str, Any]] = []
            try:
                entries = sorted(os.scandir(current_path),
                                 key=lambda e: (not e.is_dir(), e.name.lower()))
                for entry in entries:
                    if entry.name in SKIP_DIRS or entry.name.startswith(".git"):
                        continue
                    try:
                        if entry.is_dir():
                            children.append(build_tree(entry.path, depth + 1))
                        else:
                            ext = os.path.splitext(entry.name)[1].lower()
                            children.append({
                                "name": entry.name,
                                "path": entry.path,
                                "type": "file",
                                "ext": ext,
                                "size": entry.stat().st_size,
                            })
                    except Exception:
                        continue
            except Exception:
                pass

            return {"name": name, "path": current_path, "type": "dir", "children": children}

        return build_tree(root, 1)

    # ------------------------------------------------------------------
    # 기존 기능 유지 — 파일 읽기/쓰기
    # ------------------------------------------------------------------
    def read_file_content(self, file_path: str) -> Dict[str, Any]:
        target = self.resolve_path(file_path)
        if not os.path.exists(target) or os.path.isdir(target):
            return {"success": False, "error": "File does not exist", "path": target}

        for enc in ("utf-8", "utf-8-sig", "cp949", "euc-kr", "latin1"):
            try:
                with open(target, "r", encoding=enc) as f:
                    content = f.read()
                return {"success": True, "content": content, "encoding": enc, "path": target}
            except UnicodeDecodeError:
                continue
            except Exception as e:
                return {"success": False, "error": str(e), "path": target}

        return {"success": False, "error": "Unable to decode file", "path": target}

    def save_file_content(self, file_path: str, content: str) -> Dict[str, Any]:
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
        is_new_file = not os.path.isfile(target)
        if not is_new_file:
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
            # [WRITE-GUARD] 대용량 원본을 과도하게 축소하는 덮어쓰기를 물리적으로 차단
            from core.write_guard import check_overwrite
            _guard_ok, _guard_reason = check_overwrite(original_content, new_content)
            if not _guard_ok:
                logger.error("[ProjectManager] %s | 대상: %s", _guard_reason, target)
                return {
                    "success": False,
                    "error": _guard_reason,
                    "path": target,
                    "diff": "",
                    "backup_path": "",
                    "stats": {"added": 0, "removed": 0},
                }
            backup_path = self._create_backup(target, original_content) or ""

            # [SNAPSHOT-ENGINE] 시간별 스냅샷 생성 + timeline.jsonl 원장 기록
            #   - 원본 바이트를 그대로 스냅샷으로 보존 (시간별 타임머신)
            #   - 직전 스냅샷 대비 unified diff 를 자동 생성하여 함께 보관
            #   - 스냅샷 실패가 파일 저장 자체를 막아서는 안 되므로 예외를 격리한다.
            self._snapshot_before_save(target, note="save_file_content", force=True)

        # (4) parent 자동 생성 + utf-8 안전 기록
        try:
            parent = os.path.dirname(target)
            if parent and not os.path.isdir(parent):
                os.makedirs(parent, exist_ok=True)
            with open(target, "w", encoding="utf-8", newline="\n") as f:
                f.write(new_content)
            logger.info("[ProjectManager] 저장 완료: %s", target)

            # [SNAPSHOT-ENGINE] 신규 파일 최초 스냅샷
            #   - 기존 파일이 없던 경로이므로 '수정 직전 원본'이 존재하지 않는다.
            #   - 대신 저장 완료 직후의 상태를 최초 스냅샷으로 원장에 등록하여
            #     timeline.jsonl 에 파일 생성 시점이 정상 기록되도록 한다.
            if is_new_file:
                self._snapshot_after_create(target, note="initial creation")
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

    # ------------------------------------------------------------------
    # 대용량 파일 정밀 부분 패치 엔진 (SEARCH / REPLACE)
    # ------------------------------------------------------------------
    # 패치 블록 형식 (Aider / Claude 계열 표준):
    #
    #   <<<<<<< SEARCH
    #   ...원본에서 찾을 코드...
    #   =======
    #   ...대체할 코드...
    #   >>>>>>> REPLACE
    #
    # 대용량 파일(수천 줄)을 통째로 재작성하지 않고, 국소 구간만 정밀 치환하여
    # 토큰 낭비와 회귀 위험을 동시에 줄이는 것이 목적이다.
    # ------------------------------------------------------------------
    PATCH_BLOCK_PATTERN = re.compile(
        r'<<<<<<< SEARCH\r?\n(.*?)\r?\n=======\r?\n(.*?)\r?\n>>>>>>> REPLACE',
        re.DOTALL,
    )

    @staticmethod
    def _normalize_newlines(text: str) -> str:
        """CRLF / CR / LF 를 모두 LF 로 통일한다."""
        if not text:
            return ""
        return text.replace("\r\n", "\n").replace("\r", "\n")

    @staticmethod
    def _strip_all_whitespace(text: str) -> str:
        """모든 공백 문자(스페이스/탭/개행)를 제거한 비교용 문자열을 만든다."""
        if not text:
            return ""
        return re.sub(r'\s+', '', text)

    def _find_chunk_span(self, haystack: str, needle: str) -> Optional[tuple]:
        """
        haystack(원본) 안에서 needle(search_chunk)의 (start, end) 위치를 찾는다.

        치환 안정성 3단 폴백:
          (1) 정확 일치            — 원본 그대로 존재
          (2) 줄바꿈 정규화 일치    — CRLF/LF 혼용 차이 흡수
          (3) 공백 제거 일치        — 들여쓰기/공백 변형 흡수

        반환: (start, end) 튜플 또는 None
        """
        if not needle:
            return None

        # (1) 정확 일치
        idx = haystack.find(needle)
        if idx >= 0:
            return (idx, idx + len(needle))

        # (2) 줄바꿈 정규화 일치 — 원본과 needle 을 모두 LF 로 통일한 뒤 탐색.
        #     정규화 문자열의 인덱스는 원본과 1:1 대응하지 않으므로,
        #     원본을 LF 로 통일한 사본에서 위치를 찾고 그 사본을 기준으로 치환한다.
        norm_hay = self._normalize_newlines(haystack)
        norm_needle = self._normalize_newlines(needle)
        idx = norm_hay.find(norm_needle)
        if idx >= 0:
            return ("__NORMALIZED__", idx, idx + len(norm_needle))

        # (3) 공백 제거 일치 — 공백을 모두 제거한 뒤 부분 문자열 탐색.
        #     위치 역산이 불가능하므로 원본을 '라인 단위'로 재조립하며 매칭한다.
        target_sig = self._strip_all_whitespace(needle)
        if not target_sig:
            return None

        lines = haystack.splitlines(keepends=True)
        # 라인 누적 시그니처를 만들어, 연속 라인 구간의 시그니처가 target_sig 와
        # 일치하는 최초 구간을 찾는다.
        for start_line in range(len(lines)):
            acc = ""
            for end_line in range(start_line, len(lines)):
                acc += self._strip_all_whitespace(lines[end_line])
                if acc == target_sig:
                    start_off = sum(len(l) for l in lines[:start_line])
                    end_off = start_off + sum(len(l) for l in lines[start_line:end_line + 1])
                    return (start_off, end_off)
                if len(acc) > len(target_sig):
                    break

        return None

    def apply_search_replace(self, file_path: str, patch_text: str) -> Dict[str, Any]:
        """
        대용량 파일 정밀 부분 패치 엔진.

        patch_text 안의 모든 SEARCH/REPLACE 블록을 순서대로 적용한다.

        반환 계약:
          {
            "success": bool,          # 모든 블록이 적용되었는지 여부
            "applied": int,           # 실제 치환에 성공한 블록 수
            "total": int,             # 파싱된 전체 블록 수
            "failed": [ {...}, ... ], # 실패한 블록 상세 (index / reason / search 미리보기)
            "path": str,              # 실제 기록된 절대 경로
            "message": str,           # 사람이 읽는 요약
            "saved": bool,            # 디스크 기록 성공 여부
          }

        동작 원칙:
          - 블록이 하나도 파싱되지 않으면 파일을 절대 건드리지 않는다.
          - 적용된 블록이 0건이면 디스크에 쓰지 않는다(불필요한 mtime 변경 방지).
          - 일부 실패하더라도 성공한 블록은 저장하고, 실패 내역을 함께 반환한다.
        """
        result: Dict[str, Any] = {
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

        # (0) 입력 방어
        if not file_path or not str(file_path).strip():
            result["message"] = "대상 파일 경로가 비어 있습니다."
            return result
        if not patch_text or not str(patch_text).strip():
            result["message"] = "패치 텍스트가 비어 있습니다."
            return result

        # (1) 대상 파일 읽기
        read_res = self.read_file_content(file_path)
        if not read_res.get("success"):
            result["path"] = read_res.get("path", "")
            result["message"] = "대상 파일 읽기 실패: %s" % read_res.get("error")
            return result

        original = read_res.get("content", "") or ""
        target_path = read_res.get("path", "")
        result["path"] = target_path

        # (2) 모든 SEARCH/REPLACE 블록 파싱
        blocks = self.PATCH_BLOCK_PATTERN.findall(patch_text)
        result["total"] = len(blocks)
        if not blocks:
            result["message"] = (
                "패치 블록을 찾지 못했습니다. "
                "'<<<<<<< SEARCH / ======= / >>>>>>> REPLACE' 형식을 확인하십시오."
            )
            return result

        # (3) 순차 치환
        current = original
        applied = 0
        failed: List[Dict[str, Any]] = []

        for idx, (search_chunk, replace_chunk) in enumerate(blocks, start=1):
            span = self._find_chunk_span(current, search_chunk)

            if span is None:
                failed.append({
                    "index": idx,
                    "reason": "SEARCH 구간을 원본에서 찾지 못했습니다.",
                    "search_preview": (search_chunk or "")[:200],
                })
                logger.warning(
                    "[ProjectManager] 패치 블록 %d/%d 실패 — SEARCH 미발견",
                    idx, len(blocks),
                )
                continue

            # (2) 줄바꿈 정규화 폴백으로 찾은 경우 — 정규화 사본 기준으로 치환
            if isinstance(span, tuple) and len(span) == 3 and span[0] == "__NORMALIZED__":
                _, s, e = span
                norm_current = self._normalize_newlines(current)
                norm_replacement = self._normalize_newlines(replace_chunk)
                current = norm_current[:s] + norm_replacement + norm_current[e:]
            else:
                s, e = span
                current = current[:s] + replace_chunk + current[e:]

            applied += 1
            logger.info(
                "[ProjectManager] 패치 블록 %d/%d 적용 완료 (%s)",
                idx, len(blocks), target_path,
            )

        result["applied"] = applied
        result["failed"] = failed

        # (4) 적용 0건이면 디스크 기록 생략 (불필요한 파일 변경 방지)
        if applied == 0:
            result["success"] = False
            result["message"] = (
                "적용된 블록이 없어 파일을 수정하지 않았습니다. (총 %d블록 전부 실패)" % len(blocks)
            )
            return result

        # (5) save_file_content 로 안전 저장
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

        # (6) 최종 결과 정리
        result["success"] = (len(failed) == 0)
        if failed:
            result["message"] = (
                "%d/%d 블록 적용 후 저장 완료 (실패 %d건)" % (applied, len(blocks), len(failed))
            )
        else:
            result["message"] = "%d/%d 블록 전량 적용 후 저장 완료" % (applied, len(blocks))

        logger.info(
            "[ProjectManager] apply_search_replace 완료: %s (%d/%d, 실패 %d)",
            result["path"], applied, len(blocks), len(failed),
        )
        return result


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
                out_lines.append(line.rstrip("\r\n"))

            return "\n".join(out_lines)

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

    # ==================================================================
    # [SNAPSHOT-ENGINE 연동 계층]
    # 시간별 스냅샷 / timeline.jsonl 원장 / Diff / 롤백 위임 API
    # ==================================================================

    # ------------------------------------------------------------------
    # 1. 엔진 초기화 / 재바인딩
    # ------------------------------------------------------------------
    def _init_snapshot_engine(self) -> Optional[Any]:
        """
        활성 프로젝트 루트 기준으로 BackupDiffRollbackEngine 을 (재)생성한다.

        Returns:
            BackupDiffRollbackEngine 인스턴스, 또는 비활성/실패 시 None

        동작:
          - snapshot_engine 모듈이 없으면 조용히 None 으로 비활성화한다.
          - 엔진 생성 실패가 ProjectManager 초기화를 깨뜨리지 않도록 예외 격리.
          - 저장소 위치: <active_project>/.deepseek_snapshots
        """
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
        """스냅샷 엔진을 지연 초기화(lazy) 후 반환한다."""
        if self.snapshot_engine is None:
            self._init_snapshot_engine()
        return self.snapshot_engine

    # ------------------------------------------------------------------
    # 2. 저장 직전 스냅샷 생성 (내부 훅)
    # ------------------------------------------------------------------
    def _snapshot_before_save(self, target: str, note: str = "",
                              force: bool = True) -> Optional[Any]:
        """
        파일 저장 직전 원본 상태를 스냅샷으로 보존하고 timeline.jsonl 에 기록한다.

        Args:
            target : 대상 파일 절대경로
            note   : 변경 요약 메모
            force  : True(기본) 면 내용 무변경이어도 스냅샷을 강제 기록한다.
                     수정 시마다 빠짐없이 pre-edit 스냅샷을 남기기 위한 기본값.

        Returns:
            SnapshotRecord 또는 None (스킵/실패)

        원칙:
          - 스냅샷 실패는 파일 저장을 절대 방해하지 않는다 (예외 격리).
          - force=True 이므로 엔진의 '내용 무변경 스킵' 로직이 개입하지 않는다.
            (동일 내용 재저장이라도 수정 시점 자체를 이력으로 남긴다)
        """
        engine = self._ensure_snapshot_engine()
        if engine is None:
            return None

        try:
            rec = engine.create_backup(target, note=note, force=bool(force))
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
    # 2-b. 신규 파일 최초 스냅샷 생성 (내부 훅)
    # ------------------------------------------------------------------
    def _snapshot_after_create(self, target: str,
                               note: str = "initial creation") -> Optional[Any]:
        """
        신규 파일 생성 직후 최초 상태를 스냅샷으로 보존하고 원장에 기록한다.

        Args:
            target : 방금 생성된 파일 절대경로
            note   : 변경 요약 메모 (기본값 "initial creation")

        Returns:
            SnapshotRecord 또는 None (스킵/실패)

        원칙:
          - create_backup 은 디스크의 실제 파일을 읽으므로, 반드시 저장 완료 후 호출한다.
          - 스냅샷 실패는 파일 저장 결과를 절대 훼손하지 않는다 (예외 격리).
          - 신규 파일이므로 직전 스냅샷이 없어 내용 무변경 스킵에 걸리지 않는다.
        """
        engine = self._ensure_snapshot_engine()
        if engine is None:
            return None

        try:
            if not os.path.isfile(target):
                logger.debug("[ProjectManager] 최초 스냅샷 생략(파일 없음): %s", target)
                return None

            rec = engine.create_backup(target, note=note)
            if rec is not None:
                logger.info(
                    "[ProjectManager] 최초 스냅샷 기록: %s (%s)",
                    rec.snapshot_id, os.path.basename(target),
                )
            return rec
        except Exception as e:
            logger.warning(
                "[ProjectManager] 최초 스냅샷 생성 실패(저장은 유지): %s (%s)", target, e
            )
            return None

    # ------------------------------------------------------------------
    # 3. 타임라인 조회
    # ------------------------------------------------------------------
    def get_timeline(self, file_path: Optional[str] = None,
                     limit: int = 20) -> Dict[str, Any]:
        """
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
        """
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
        """
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
        """
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
        """
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
        """
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
        """
        기존 .deepseek_backups 최신 .bak 파일로 복원한다 (레거시 경로).

        rollback_file() 에 스냅샷 인자가 전달되지 않았을 때의 하위 호환 동작.
        """
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
        """스냅샷 원장/객체 무결성을 전수 검증한다."""
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

    def to_dict(self) -> Dict[str, Any]:
        return {
            "active_project": self.active_project,
            "config_path": self.config_path,
            "desktop_path": self.desktop_path,
            "config": self.config,
        }
