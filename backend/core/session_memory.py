# -*- coding: utf-8 -*-
"""
SessionMemory — 프로젝트별 완전 격리 및 영구 기억 엔진
================================================================
[개편 핵심]
1) 하드코딩 더미 데이터(run_stock.py / 한국 마케팅봇_Sandbox) 완전 제거
   → modified_files_log 는 빈 리스트 [] 로 초기화.

2) 프로젝트별 대화/수정 이력 영구 기억 (Multi-Project Persistence)
   - bind_project(project_path) 호출 시:
       (a) 현재 프로젝트의 history + modified_files_log 를 디스크에 flush
       (b) 새 프로젝트의 저장 파일(.deepseek_session.json)을 로드하여 복원
   - 저장 위치 우선순위:
       1순위: <project_path>/.deepseek_session.json   (프로젝트 자체에 귀속)
       2순위: <backend>/sessions/<프로젝트해시>.json   (쓰기 불가 시 폴백)
   - 프로젝트 경로가 바뀔 때마다 완전 독립 격리 → 앱마다 고유 기억 100% 보존.

3) 기존 공개 인터페이스 100% 호환 보존
   record_file_modification / add_user_message / add_assistant_message /
   add_tool_result / get_messages_for_llm / _get_safe_history_slice /
   _trim_history / clear / get_chat_feed
   → 시그니처·반환형·동작 의미 모두 유지.

[설계 원칙]
- 모든 디스크 I/O 는 예외를 삼켜 절대 상위로 전파하지 않는다(메모리 엔진은
  실패해도 대화가 계속되어야 한다).
- 원자적 쓰기(temp → os.replace)로 파일 손상 방지.
- UTF-8 보장, Windows 경로/드라이브 문자 대소문자 차이 정규화.
"""

import os
import json
import time
import hashlib
import tempfile
import threading
from typing import List, Dict, Any, Optional


# ======================================================================
# 상수
# ======================================================================
SESSION_FILENAME = ".deepseek_session.json"
SESSION_SCHEMA_VERSION = 2

# backend/core/session_memory.py → backend/
_BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SESSIONS_DIR = os.path.join(_BACKEND_DIR, "sessions")


def _norm_path(path: str) -> str:
    """Windows 경로 정규화(대소문자/구분자/후행 슬래시 무시) — 비교 전용."""
    if not path:
        return ""
    try:
        p = os.path.abspath(os.path.expanduser(str(path).strip().strip('"')))
    except Exception:
        p = str(path)
    return os.path.normcase(os.path.normpath(p))


def _project_key(project_path: str) -> str:
    """프로젝트 경로 → 안정적인 짧은 해시 키(폴백 저장 파일명용)."""
    norm = _norm_path(project_path)
    if not norm:
        return "default"
    return hashlib.sha1(norm.encode("utf-8", "ignore")).hexdigest()[:16]


class SessionMemory:
    """
    Antigravity급 다중 대화 맥락 기억 엔진 (Multi-Turn Context and File State Tracker)
    - 대화의 모든 이전 맥락(User 질문, AI 답변, 도구 실행 결과)을 보존
    - 최근 수정된 파일 이력을 실시간 추적하여 AI에게 항시 주입
    - 토큰 한도 초과 방지를 위한 롤링 윈도우(Rolling Window) 관리
    - [신규] 프로젝트별 완전 격리 + 디스크 영구 저장/복원
    """

    def __init__(self, max_history_turns: int = 24, project_path: Optional[str] = None):
        self.max_history_turns = max_history_turns

        # ── 대화 히스토리 / 수정 파일 이력 (하드코딩 더미 완전 제거) ──
        self.history: List[Dict[str, Any]] = []
        self.modified_files_log: List[Dict[str, Any]] = []

        # ── 프로젝트 바인딩 상태 ──
        self.active_project: str = ""
        self._session_file: str = ""
        self._lock = threading.RLock()
        self._dirty: bool = False

        # 생성 시 프로젝트가 주어지면 즉시 바인딩(저장본 자동 복원)
        if project_path:
            self.bind_project(project_path)

    # ==================================================================
    # [1] 프로젝트 바인딩 — 격리 + 영구 저장/복원의 핵심
    # ==================================================================
    def bind_project(self, project_path: str) -> str:
        """
        프로젝트 전환. 반환값: 바인딩된 프로젝트 절대 경로.

        동작 순서:
          1) 현재 프로젝트가 있고 새 프로젝트와 다르면 → 현재 메모리를 디스크에 flush
          2) 새 프로젝트의 저장 파일 경로 결정
          3) 저장 파일이 존재하면 로드하여 history / modified_files_log 복원
          4) 저장 파일이 없으면 빈 상태로 시작(이전 프로젝트 기억은 절대 유입 금지)
        """
        raw = (project_path or "").strip().strip("\"'`").strip()
        if not raw:
            return self.active_project

        try:
            new_project = os.path.abspath(os.path.expanduser(raw))
        except Exception:
            return self.active_project

        with self._lock:
            # 이미 같은 프로젝트에 바인딩되어 있으면 재로드하지 않음(메모리 보존)
            if self.active_project and _norm_path(new_project) == _norm_path(self.active_project):
                return self.active_project

            # (1) 이전 프로젝트 메모리 보존
            if self.active_project:
                self._flush_locked()

            # (2) 새 프로젝트 상태로 초기화 (완전 격리)
            self.active_project = new_project
            self.history = []
            self.modified_files_log = []
            self._dirty = False
            self._session_file = self._resolve_session_file(new_project)

            # (3) 저장본 복원
            self._load_locked()

            return self.active_project

    def _resolve_session_file(self, project_path: str) -> str:
        """
        저장 파일 경로 결정.
        1순위: <project>/.deepseek_session.json (프로젝트 디렉터리에 쓰기 가능할 때)
        2순위: <backend>/sessions/<hash>.json (폴백 — 읽기 전용 프로젝트 대비)
        """
        primary = os.path.join(project_path, SESSION_FILENAME)
        try:
            if os.path.isdir(project_path) and os.access(project_path, os.W_OK):
                return primary
        except Exception:
            pass

        # 폴백: 백엔드 sessions 디렉터리
        try:
            os.makedirs(_SESSIONS_DIR, exist_ok=True)
            return os.path.join(_SESSIONS_DIR, f"{_project_key(project_path)}.json")
        except Exception:
            return primary

    # ==================================================================
    # [2] 디스크 저장 / 복원 (원자적 쓰기)
    # ==================================================================
    def _serialize(self) -> Dict[str, Any]:
        return {
            "schema": SESSION_SCHEMA_VERSION,
            "project": self.active_project,
            "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "history": self.history,
            "modified_files_log": self.modified_files_log,
        }

    def _flush_locked(self) -> bool:
        """현재 메모리를 디스크에 원자적으로 기록. (락 보유 상태에서 호출)"""
        if not self._session_file:
            return False
        try:
            payload = self._serialize()
            target_dir = os.path.dirname(self._session_file)
            if target_dir:
                os.makedirs(target_dir, exist_ok=True)

            fd, tmp_path = tempfile.mkstemp(
                prefix=".deepseek_session_", suffix=".tmp", dir=target_dir or None
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(payload, f, ensure_ascii=False, indent=2)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp_path, self._session_file)
            except Exception:
                try:
                    if os.path.exists(tmp_path):
                        os.remove(tmp_path)
                except Exception:
                    pass
                raise

            self._dirty = False
            return True
        except Exception:
            # 저장 실패는 대화 흐름을 막지 않는다.
            return False

    def _load_locked(self) -> bool:
        """저장 파일을 읽어 history / modified_files_log 복원. (락 보유 상태에서 호출)"""
        path = self._session_file
        if not path or not os.path.isfile(path):
            return False
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                return False

            history = data.get("history")
            if isinstance(history, list):
                self.history = [m for m in history if isinstance(m, dict)]

            files = data.get("modified_files_log")
            if isinstance(files, list):
                self.modified_files_log = [m for m in files if isinstance(m, dict)]

            self._dirty = False
            return True
        except Exception:
            # 손상된 파일은 무시하고 빈 상태로 진행
            self.history = []
            self.modified_files_log = []
            return False

    def save(self) -> bool:
        """공개 저장 API — 현재 프로젝트 메모리를 즉시 디스크에 기록."""
        with self._lock:
            return self._flush_locked()

    def load(self) -> bool:
        """공개 로드 API — 현재 프로젝트의 저장본을 즉시 복원."""
        with self._lock:
            return self._load_locked()

    def _mark_dirty(self):
        """변경 표시 후 즉시 저장(대화 유실 방지)."""
        self._dirty = True
        self._flush_locked()

    # ==================================================================
    # [3] 수정 파일 이력 기록 (기존 인터페이스 100% 호환)
    # ==================================================================
    def record_file_modification(self, filename: str, path: str, lines: int, summary: str = ""):
        """어떤 파일이 언제 몇 줄로 수정되었는지 작업 메모리에 영구 기록"""
        record = {
            "filename": filename,
            "path": path,
            "lines": lines,
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            "summary": summary or f"{lines}줄로 코드 수정 반영 완료"
        }
        with self._lock:
            # 중복 제거 후 최신으로 갱신
            self.modified_files_log = [f for f in self.modified_files_log if f.get("filename") != filename]
            self.modified_files_log.append(record)
            self._mark_dirty()

    # ==================================================================
    # [4] 대화 메시지 적재 (기존 인터페이스 100% 호환)
    # ==================================================================
    def add_user_message(self, content: str):
        with self._lock:
            self.history.append({"role": "user", "content": content})
            self._trim_history()
            self._mark_dirty()

    def add_assistant_message(self, content: str, tool_calls: Optional[List[Dict[str, Any]]] = None):
        msg: Dict[str, Any] = {"role": "assistant", "content": content or ""}
        if tool_calls:
            msg["tool_calls"] = tool_calls
        with self._lock:
            self.history.append(msg)
            self._trim_history()
            self._mark_dirty()

    def add_tool_result(self, tool_call_id: str, function_name: str, result: str):
        with self._lock:
            self.history.append({
                "role": "tool",
                "tool_call_id": tool_call_id,
                "name": function_name,
                "content": result
            })
            self._trim_history()
            self._mark_dirty()

    # ==================================================================
    # [5] LLM 메시지 조립 (기존 인터페이스 100% 호환)
    # ==================================================================
    def get_messages_for_llm(self, system_prompt: str) -> List[Dict[str, Any]]:
        """
        LLM 호출을 위해 [시스템 프롬프트 + 작업 메모리 + 이전 대화 맥락]을 단일 메시지 리스트로 조립
        """
        with self._lock:
            # 1. 최근 수정된 파일 상태를 시스템 컨텍스트에 동적 주입
            memory_banner = ""
            if self.modified_files_log:
                memory_banner = "\n[🧠 Antigravity 작업 메모리 - 최근 수정된 파일 이력]:\n"
                for m in self.modified_files_log[-5:]:
                    memory_banner += (
                        f"- 📁 {m.get('filename', '?')} ({m.get('lines', 0)}줄) | "
                        f"수정시각: {m.get('timestamp', '-')} | 상태: {m.get('summary', '')}\n"
                    )
                memory_banner += (
                    "위 파일들은 이미 수정된 파일입니다. '아무것도 안 했다'거나 "
                    "'1, 2, 3번 지시를 내려달라'고 하지 말고 수정된 상태를 기반으로 대화하십시오.\n"
                )

            # 2. 프로젝트 컨텍스트 배너 (프로젝트별 격리 사실을 LLM에 명시)
            project_banner = ""
            if self.active_project:
                project_banner = (
                    f"\n[📂 현재 바인딩된 프로젝트]: {self.active_project}\n"
                    f"이 대화의 기억은 위 프로젝트 전용으로 격리되어 있습니다.\n"
                )

            full_system = system_prompt + "\n" + project_banner + memory_banner

            # 3. 메시지 조립
            messages = [{"role": "system", "content": full_system}]

            # 4. 최근 히스토리 슬라이싱 (tool_calls와 tool 응답의 쌍이 깨지지 않도록 정렬)
            recent_history = self._get_safe_history_slice()
            messages.extend(recent_history)
            return messages

    def _get_safe_history_slice(self) -> List[Dict[str, Any]]:
        """히스토리의 앞부분을 잘라낼 때 tool_calls와 tool 응답의 정합성을 보존"""
        if len(self.history) <= self.max_history_turns:
            return list(self.history)

        # 앞에서부터 자르되 첫 번째가 'tool' 메시지가 되지 않도록 'user' 메시지부터 시작
        start_idx = len(self.history) - self.max_history_turns
        while start_idx < len(self.history) and self.history[start_idx].get("role") in ("tool", "assistant"):
            start_idx += 1

        if start_idx >= len(self.history):
            return list(self.history[-4:])
        return list(self.history[start_idx:])

    def _trim_history(self):
        """히스토리가 너무 길어지면 오래된 대화 정리 (최대 50개 유지)"""
        if len(self.history) > 50:
            self.history = self.history[-35:]

    # ==================================================================
    # [6] 초기화 / UI 피드 (기존 인터페이스 100% 호환)
    # ==================================================================
    def clear(self):
        """새 대화 시작 시 히스토리 초기화 (수정 파일 기록은 보존)"""
        with self._lock:
            self.history.clear()
            self._mark_dirty()

    def get_chat_feed(self) -> List[Dict[str, Any]]:
        """UI에 표시할 사용자/AI 순수 대화 목록 반환"""
        feed = []
        with self._lock:
            for m in self.history:
                role = m.get("role")
                content = m.get("content")
                if role in ("user", "assistant") and content:
                    feed.append({"role": role, "content": content})
        return feed

    # ==================================================================
    # [7] 진단용 유틸
    # ==================================================================
    def get_session_file(self) -> str:
        """현재 프로젝트의 저장 파일 경로 반환(디버깅/상태 표시용)."""
        return self._session_file

    def get_project_summary(self) -> Dict[str, Any]:
        """현재 프로젝트 기억 상태 요약(디버깅/상태 표시용)."""
        with self._lock:
            return {
                "active_project": self.active_project,
                "session_file": self._session_file,
                "history_count": len(self.history),
                "modified_files_count": len(self.modified_files_log),
                "dirty": self._dirty,
            }
