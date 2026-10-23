# -*- coding: utf-8 -*-
"""
_patch_time_snapshot.py

project_manager.py 에 '시간별 원본 스냅샷 보존 + 깃 스타일 Hunk Diff + 시간별 롤백 엔진'을 주입한다.

주입 항목:
  1) import 확장 (hashlib)
  2) 상수: SNAPSHOT_DIR_NAME / TIMELINE_FILENAME / SNAPSHOT_MAX_PER_FILE
  3) _snapshot_root() / _relative_to_project() / _snapshot_dir_for()
  4) _create_time_snapshot(target, content, summary) -> Optional[Dict]
  5) _append_timeline(record) / _read_timeline(file_path) — timeline.jsonl append-only
  6) _prune_snapshots(target) — 파일당 최대 50개 보존
  7) get_file_timeline(file_path) — 시간별 스냅샷 목록 최신순
  8) rollback_file(file_path, target_time=None) — 시간별 롤백 (기존 즉시 롤백 대체)
  9) save_file_content 연동 — snapshot_path / timestamp / ts_str 반환
 10) apply_search_replace 연동 — snapshot_path / timestamp 승계

설계 원칙:
  - 멱등성: 이미 주입되어 있으면 아무 것도 하지 않는다.
  - 앵커 기반 정밀 치환: 앵커가 정확히 1회 등장할 때만 치환한다.
  - 원자적 저장: 임시 파일 -> os.replace
  - 실패 시 원본 무손상 (백업 후 자동 복원)
"""

import io
import os
import sys
import shutil
import py_compile

TARGET = os.path.join("backend", "core", "project_manager.py")
MARKER = "# [TIME-SNAPSHOT-ENGINE]"


# ======================================================================
# 유틸
# ======================================================================
def read_text(path):
    with io.open(path, "r", encoding="utf-8", newline="") as f:
        return f.read()


def write_text_atomic(path, text):
    tmp = path + ".tmp_patch"
    with io.open(tmp, "w", encoding="utf-8", newline="") as f:
        f.write(text)
    os.replace(tmp, path)


def count_occurrences(haystack, needle):
    if not needle:
        return 0
    return haystack.count(needle)


def fail(msg):
    print("  X " + msg)
    sys.exit(1)


# ======================================================================
# 1. import 확장 (hashlib 추가)
# ======================================================================
IMPORT_ANCHOR = """import os
import re
import json
import logging
import difflib
import shutil
import time
import datetime
from typing import Dict, List, Any, Optional, Tuple
"""

IMPORT_NEW = """import os
import re
import json
import logging
import difflib
import shutil
import time
import hashlib
import datetime
from typing import Dict, List, Any, Optional, Tuple
"""


# ======================================================================
# 2. 시간별 스냅샷 엔진 본문 (PART 1: 경로/스냅샷 생성/timeline)
# ======================================================================
ENGINE_PART1 = '''
    # ==================================================================
    # [TIME-SNAPSHOT-ENGINE]
    # 시간별 원본 스냅샷 보존 + 깃 스타일 Hunk Diff + 시간별 롤백 엔진
    #
    # 저장 구조:
    #   <active_project>/.deepseek_snapshots/
    #       |-- timeline.jsonl                       <- append-only 변경 이력
    #       +-- <파일상대경로>/
    #               +-- <파일명>.<YYYYMMDD_HHMMSS>.bak
    #
    # 예) backend/core/project_manager.py 수정 시
    #   .deepseek_snapshots/backend/core/project_manager.py/
    #       project_manager.py.20261023_234501.bak
    #
    # timeline.jsonl 한 줄(JSONL) 레코드:
    #   {"ts":"2026-10-23 23:45:01","epoch":1761...,
    #    "file":"backend/core/project_manager.py",
    #    "snapshot":".../project_manager.py.20261023_234501.bak",
    #    "summary":"save_file_content (+12/-3)","size":48213,"sha1":"..."}
    # ==================================================================

    SNAPSHOT_DIR_NAME = ".deepseek_snapshots"
    TIMELINE_FILENAME = "timeline.jsonl"
    SNAPSHOT_MAX_PER_FILE = 50          # 파일당 스냅샷 보존 상한 (초과분 자동 회수)
    SNAPSHOT_TS_FORMAT = "%Y%m%d_%H%M%S"

    # ------------------------------------------------------------------
    # 경로 해석
    # ------------------------------------------------------------------
    def _snapshot_root(self) -> str:
        """활성 프로젝트 하위 스냅샷 루트 절대경로."""
        base = self.active_project or self.desktop_path
        return os.path.join(os.path.abspath(base), self.SNAPSHOT_DIR_NAME)

    def _relative_to_project(self, target: str) -> str:
        """
        대상 파일의 active_project 기준 상대경로를 반환한다.
        프로젝트 외부 파일이면 파일명만 사용한다.
        (스냅샷 디렉터리 구조를 프로젝트 트리와 동일하게 유지하기 위함)
        """
        try:
            base = os.path.abspath(self.active_project or self.desktop_path)
            tgt = os.path.abspath(target)
            rel = os.path.relpath(tgt, base)
            # 상위로 탈출하는 경로(..)는 프로젝트 외부 -> 파일명만 사용
            if rel.startswith("..") or os.path.isabs(rel):
                return os.path.basename(tgt)
            return rel
        except Exception:
            return os.path.basename(target or "unknown")

    def _snapshot_dir_for(self, target: str) -> str:
        """
        대상 파일 전용 스냅샷 디렉터리 절대경로.
        <snapshot_root>/<파일상대경로 디렉터리>/
        """
        rel = self._relative_to_project(target)
        rel_dir = os.path.dirname(rel)
        root = self._snapshot_root()
        if rel_dir:
            return os.path.join(root, rel_dir)
        return root

    # ------------------------------------------------------------------
    # 1. 시간별 스냅샷 생성
    # ------------------------------------------------------------------
    def _create_time_snapshot(self, target: str, content: str,
                              summary: str = "") -> Optional[Dict[str, Any]]:
        """
        파일 수정 '직전' 원본 내용을 타임스탬프 스냅샷으로 보존한다.

        Args:
            target  : 대상 파일 절대경로
            content : 보존할 원본 내용
            summary : 변경 요약 (timeline.jsonl 에 기록)

        Returns:
            {
              "snapshot_path": str,   # 스냅샷 파일 절대경로
              "timestamp": str,       # "YYYYMMDD_HHMMSS"
              "ts_str": str,          # "YYYY-MM-DD HH:MM:SS"
              "epoch": float,
              "size": int,
              "sha1": str,
            } 또는 None (신규 파일/실패)

        동작:
          - 대상 파일이 디스크에 존재하지 않으면(신규 생성) 스냅샷 생략.
          - 동일 초에 여러 번 저장되면 마이크로초 접미사로 충돌 회피.
          - 파일당 SNAPSHOT_MAX_PER_FILE 개 초과 시 오래된 것부터 회수.
          - timeline.jsonl 에 append-only 로 이력 영구 기록.
        """
        try:
            if not target:
                return None
            if not os.path.isfile(target):
                # 신규 파일 - 되돌릴 원본이 없으므로 스냅샷 생략
                logger.debug("[Snapshot] 신규 파일 - 스냅샷 생략: %s", target)
                return None

            snap_dir = self._snapshot_dir_for(target)
            os.makedirs(snap_dir, exist_ok=True)

            filename = os.path.basename(target)
            now = datetime.datetime.now()
            ts = now.strftime(self.SNAPSHOT_TS_FORMAT)
            ts_str = now.strftime("%Y-%m-%d %H:%M:%S")

            snap_name = "%s.%s.bak" % (filename, ts)
            snap_path = os.path.join(snap_dir, snap_name)

            # 동일 초 충돌 방지 - 이미 존재하면 마이크로초 접미사 추가
            if os.path.exists(snap_path):
                snap_name = "%s.%s_%06d.bak" % (filename, ts, now.microsecond)
                snap_path = os.path.join(snap_dir, snap_name)

            body = content if isinstance(content, str) else str(content or "")

            # 원본 그대로 기록 (newline="" 로 개행 변형 없이 보존)
            with open(snap_path, "w", encoding="utf-8", newline="") as f:
                f.write(body)

            sha1 = hashlib.sha1(body.encode("utf-8", errors="replace")).hexdigest()
            size = os.path.getsize(snap_path)

            record = {
                "snapshot_path": os.path.abspath(snap_path),
                "timestamp": ts,
                "ts_str": ts_str,
                "epoch": now.timestamp(),
                "size": size,
                "sha1": sha1,
            }

            logger.info("[Snapshot] 시간별 스냅샷 생성: %s -> %s", target, snap_path)

            # 오래된 스냅샷 자동 회수
            self._prune_snapshots(target)

            # timeline.jsonl append-only 기록
            self._append_timeline({
                "ts": ts_str,
                "epoch": record["epoch"],
                "file": self._relative_to_project(target).replace(os.sep, "/"),
                "abs_path": os.path.abspath(target),
                "snapshot": os.path.abspath(snap_path),
                "summary": summary or "file modified",
                "size": size,
                "sha1": sha1,
            })

            return record

        except Exception as e:
            logger.error("[Snapshot] 스냅샷 생성 실패: %s (%s)", target, e)
            return None

    def _prune_snapshots(self, target: str) -> int:
        """
        특정 파일의 스냅샷이 SNAPSHOT_MAX_PER_FILE 개를 초과하면
        mtime 기준 오래된 것부터 삭제한다. 삭제 개수 반환.
        """
        removed = 0
        try:
            snap_dir = self._snapshot_dir_for(target)
            if not os.path.isdir(snap_dir):
                return 0

            filename = os.path.basename(target)
            prefix = filename + "."
            candidates = []
            for entry in os.scandir(snap_dir):
                try:
                    if not entry.is_file():
                        continue
                    if not (entry.name.startswith(prefix) and entry.name.endswith(".bak")):
                        continue
                    candidates.append((entry.stat().st_mtime, entry.path))
                except Exception:
                    continue

            candidates.sort(key=lambda x: x[0], reverse=True)
            for _, path in candidates[self.SNAPSHOT_MAX_PER_FILE:]:
                try:
                    os.remove(path)
                    removed += 1
                    logger.info("[Snapshot] 오래된 스냅샷 회수: %s", path)
                except Exception as e:
                    logger.warning("[Snapshot] 스냅샷 회수 실패: %s (%s)", path, e)
        except Exception as e:
            logger.warning("[Snapshot] 스냅샷 정리 중 오류: %s", e)
        return removed
'''


# ======================================================================
# 3. 시간별 스냅샷 엔진 본문 (PART 2: timeline I/O + 타임라인 조회)
# ======================================================================
ENGINE_PART2 = '''
    # ------------------------------------------------------------------
    # 2. timeline.jsonl - append-only 영구 이력
    # ------------------------------------------------------------------
    def _timeline_path(self) -> str:
        return os.path.join(self._snapshot_root(), self.TIMELINE_FILENAME)

    def _append_timeline(self, record: Dict[str, Any]) -> bool:
        """
        timeline.jsonl 에 변경 이력을 한 줄(JSONL) append 한다.
        append-only 이므로 기존 이력은 절대 수정/삭제되지 않는다.
        """
        try:
            root = self._snapshot_root()
            os.makedirs(root, exist_ok=True)
            line = json.dumps(record, ensure_ascii=False)
            with open(self._timeline_path(), "a", encoding="utf-8", newline="\\n") as f:
                f.write(line + "\\n")
            return True
        except Exception as e:
            logger.error("[Snapshot] timeline 기록 실패: %s", e)
            return False

    def _read_timeline(self, file_path: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        timeline.jsonl 을 읽어 레코드 리스트를 반환한다.
        file_path 가 주어지면 해당 파일 레코드만 필터링한다.
        손상된 라인은 건너뛴다(방어적 파싱).
        """
        records: List[Dict[str, Any]] = []
        try:
            path = self._timeline_path()
            if not os.path.isfile(path):
                return records

            want_rel = ""
            if file_path:
                target = self.resolve_path(file_path)
                want_rel = self._relative_to_project(target).replace(os.sep, "/")

            with open(path, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except Exception:
                        continue
                    if not isinstance(rec, dict):
                        continue
                    if want_rel and rec.get("file") != want_rel:
                        continue
                    records.append(rec)
        except Exception as e:
            logger.warning("[Snapshot] timeline 읽기 실패: %s", e)
        return records

    # ------------------------------------------------------------------
    # 3. 시간별 스냅샷 목록 조회
    # ------------------------------------------------------------------
    def get_file_timeline(self, file_path: str) -> List[Dict[str, Any]]:
        """
        특정 파일의 시간별 스냅샷 목록을 최신순으로 반환한다.

        Returns:
            [
              {
                "timestamp": "20261023_234501",
                "ts_str": "2026-10-23 23:45:01",
                "epoch": 1761...,
                "size": 48213,
                "snapshot_path": ".../project_manager.py.20261023_234501.bak",
                "summary": "save_file_content (+12/-3)",
                "sha1": "...",
              }, ...
            ]

        디스크 스캔과 timeline.jsonl 을 병합하여, timeline 에 기록이 없더라도
        실제 존재하는 스냅샷 파일은 모두 목록에 포함한다.
        """
        result: List[Dict[str, Any]] = []
        try:
            target = self.resolve_path(file_path)
            snap_dir = self._snapshot_dir_for(target)
            filename = os.path.basename(target)

            # (a) timeline.jsonl 레코드 -> snapshot_path 기준 인덱스
            timeline_map: Dict[str, Dict[str, Any]] = {}
            for rec in self._read_timeline(target):
                sp = rec.get("snapshot")
                if sp:
                    timeline_map[os.path.normcase(os.path.abspath(sp))] = rec

            # (b) 디스크 스캔
            if os.path.isdir(snap_dir):
                prefix = filename + "."
                for entry in os.scandir(snap_dir):
                    try:
                        if not entry.is_file():
                            continue
                        if not (entry.name.startswith(prefix) and entry.name.endswith(".bak")):
                            continue

                        st = entry.stat()
                        abs_path = os.path.abspath(entry.path)
                        rec = timeline_map.get(os.path.normcase(abs_path), {})

                        # 파일명에서 타임스탬프 파싱: <name>.<YYYYMMDD_HHMMSS>[_usec].bak
                        stem = entry.name[len(prefix):-len(".bak")]
                        parts = stem.split("_")
                        if len(parts) >= 2:
                            ts = parts[0] + "_" + parts[1]
                        else:
                            ts = stem

                        result.append({
                            "timestamp": rec.get("timestamp", ts),
                            "ts_str": rec.get("ts", datetime.datetime.fromtimestamp(
                                st.st_mtime).strftime("%Y-%m-%d %H:%M:%S")),
                            "epoch": rec.get("epoch", st.st_mtime),
                            "size": st.st_size,
                            "snapshot_path": abs_path,
                            "summary": rec.get("summary", ""),
                            "sha1": rec.get("sha1", ""),
                        })
                    except Exception:
                        continue

            result.sort(key=lambda x: x["epoch"], reverse=True)
        except Exception as e:
            logger.warning("[Snapshot] 타임라인 조회 실패: %s", e)
        return result
'''


# ======================================================================
# 4. 시간별 스냅샷 엔진 본문 (PART 3: 시간별 롤백)
# ======================================================================
ENGINE_PART3 = '''
    # ------------------------------------------------------------------
    # 4. 시간별 롤백
    # ------------------------------------------------------------------
    def _parse_target_time(self, target_time: Any) -> Optional[float]:
        """
        target_time 을 epoch(float) 로 정규화한다.
        지원 형식:
          - None            -> None (최신 스냅샷 사용)
          - int / float     -> epoch 그대로
          - "YYYYMMDD_HHMMSS"
          - "YYYY-MM-DD HH:MM:SS"
          - "YYYY-MM-DDTHH:MM:SS"
          - "YYYY-MM-DD HH:MM"
          - "YYYY-MM-DD"
        """
        if target_time is None:
            return None
        if isinstance(target_time, (int, float)):
            return float(target_time)

        s = str(target_time).strip()
        if not s:
            return None

        formats = (
            "%Y%m%d_%H%M%S",
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%dT%H:%M:%S",
            "%Y-%m-%d %H:%M",
            "%Y-%m-%d",
        )
        for fmt in formats:
            try:
                return datetime.datetime.strptime(s, fmt).timestamp()
            except Exception:
                continue

        # 숫자 문자열이면 epoch 로 간주
        try:
            return float(s)
        except Exception:
            return None

    def rollback_file(self, file_path: str, target_time: Any = None) -> Dict[str, Any]:
        """
        시간별 스냅샷으로 파일을 원상 복구한다.

        Args:
            file_path   : 롤백할 파일 경로 (스마트 경로 해석 적용)
            target_time : None 이면 '직전 시점'(가장 최근 스냅샷)으로 롤백.
                          지정 시 해당 시각 이하의 가장 가까운 스냅샷으로 복구.
                          지원 형식: epoch / "YYYYMMDD_HHMMSS" / "YYYY-MM-DD HH:MM:SS"

        Returns:
            {
              "success": bool,
              "path": str,              # 롤백된 대상 파일 절대경로
              "snapshot_path": str,     # 사용된 스냅샷 경로
              "backup_path": str,       # (호환) snapshot_path 와 동일
              "timestamp": str,         # 사용된 스냅샷 타임스탬프
              "ts_str": str,            # 사람이 읽는 시각
              "diff": str,              # 롤백 diff (현재 -> 스냅샷 원본)
              "stats": {"added": n, "removed": n},
              "message": str,
            }

        동작:
          - 스냅샷이 없으면 success=False 로 안전하게 실패한다.
          - 롤백 직전 상태도 스냅샷으로 보존하여 '롤백의 롤백'이 가능하다.
          - target_time 이 미래이거나 매칭 스냅샷이 없으면 가장 가까운 것을 사용.
        """
        result: Dict[str, Any] = {
            "success": False,
            "path": "",
            "snapshot_path": "",
            "backup_path": "",
            "timestamp": "",
            "ts_str": "",
            "diff": "",
            "stats": {"added": 0, "removed": 0},
            "message": "",
        }

        try:
            target = self.resolve_path(file_path)
            result["path"] = target

            timeline = self.get_file_timeline(target)
            if not timeline:
                result["message"] = "롤백할 스냅샷이 존재하지 않습니다: %s" % target
                logger.warning("[Rollback] 스냅샷 없음: %s", target)
                return result

            # (a) 대상 스냅샷 선택
            want_epoch = self._parse_target_time(target_time)
            if want_epoch is None:
                chosen = timeline[0]          # 직전 시점 = 가장 최근
            else:
                # target_time 이하에서 가장 가까운(최신) 스냅샷
                candidates = [t for t in timeline if t["epoch"] <= want_epoch]
                if candidates:
                    chosen = candidates[0]    # timeline 은 최신순 정렬
                else:
                    # 지정 시각보다 이전 스냅샷이 없으면 가장 오래된 것 사용
                    chosen = timeline[-1]
                    logger.warning(
                        "[Rollback] 지정 시각(%s) 이전 스냅샷 없음 - 가장 오래된 스냅샷 사용",
                        target_time,
                    )

            snap_path = chosen["snapshot_path"]
            if not os.path.isfile(snap_path):
                result["message"] = "스냅샷 파일이 실제로 존재하지 않습니다: %s" % snap_path
                return result

            # (b) 스냅샷 원본 읽기
            with open(snap_path, "r", encoding="utf-8", newline="") as f:
                snap_content = f.read()

            # (c) 현재 파일 내용
            current_content = ""
            if os.path.isfile(target):
                try:
                    with open(target, "r", encoding="utf-8", newline="") as f:
                        current_content = f.read()
                except Exception:
                    current_content = ""

            # (d) 롤백 직전 상태도 스냅샷 보존 (롤백의 롤백 대비)
            if os.path.isfile(target):
                self._create_time_snapshot(
                    target, current_content,
                    summary="pre-rollback (before restoring %s)" % chosen["timestamp"],
                )

            # (e) 롤백 diff 생성 (현재 -> 스냅샷 원본)
            rollback_diff = self._generate_unified_diff(
                current_content, snap_content, target
            )

            # (f) 원자적 복원
            parent = os.path.dirname(target)
            if parent and not os.path.isdir(parent):
                os.makedirs(parent, exist_ok=True)
            with open(target, "w", encoding="utf-8", newline="") as f:
                f.write(snap_content)

            result["success"] = True
            result["snapshot_path"] = snap_path
            result["backup_path"] = snap_path
            result["timestamp"] = chosen["timestamp"]
            result["ts_str"] = chosen["ts_str"]
            result["diff"] = rollback_diff
            result["stats"] = self._diff_stats(rollback_diff)
            result["message"] = "롤백 완료: %s <- %s (%s)" % (
                os.path.basename(target), os.path.basename(snap_path), chosen["ts_str"]
            )

            logger.info(
                "[Rollback] 시간별 롤백 완료: %s <- %s (+%d/-%d)",
                target, snap_path,
                result["stats"]["added"], result["stats"]["removed"],
            )
            return result

        except Exception as e:
            logger.error("[Rollback] 롤백 실패: %s (%s)", file_path, e)
            result["success"] = False
            result["message"] = "롤백 실패: %s" % e
            return result

'''
