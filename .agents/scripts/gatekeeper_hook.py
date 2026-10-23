#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
한국 마케팅봇_Sandbox — Gatekeeper Hook (fail-closed, 24h 무인 작동)
================================================================================
[거버넌스] 6인 합동 회의체: 제미나이 1인 토론위원 + 딥시크 5인 협업체
[핵심 원칙] 제미나이의 독단적 코드 열람/조사/수정/실행을 물리적으로 원천 차단.
            판정 불가/예외/타임아웃 상황에서는 무조건 deny (fail-closed).

[Antigravity 훅 표준 스키마]
  - PreInvocation : {"injectSteps": [{"ephemeralMessage": "..."}]}
  - PreToolUse    : {"decision": "deny"|"allow", "reason": "..."}

구성:
  - AuditLog    : SHA-256 해시체인 기반 불변 감사 로그 (append-only + 무결성 검증)
  - TokenIssuer : 딥시크 5인 협업체 전원 승인 서명 검증 토큰 발급/검증
  - Gatekeeper  : PreToolUse 단일 집행 관문
                  · 쓰기/실행 계열(write_to_file, run_command 등) → 토큰 필수
                  · 조사 계열(read/view/grep/search/list 등) → 토큰 필수 (하드 블록)
  - SessionGuard: PreInvocation 세션 상태 점검 + 만료 세션 정리(sweep_zombies) + 거버넌스 로드
  - Watchdog    : 헬스체크 + 좀비/잔류 프로세스 회수 (24h 무인 가동)

[CWD 비의존성]
  모든 저장 경로는 __file__ 기준 절대경로로 확정한다.
  어떤 작업 디렉터리에서 훅이 호출되더라도 동일한 감사로그/토큰/세션 저장소를 사용한다.
"""

import os
import re
import sys
import json
import time
import hmac
import hashlib
import signal
import threading
import traceback
from dataclasses import dataclass, asdict
from typing import Optional, Dict, Any, List, Tuple
from datetime import datetime, timezone

# ============================================================
# 0. 상수 / 설정 — CWD 비의존 절대경로 확정
# ============================================================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
AGENTS_DIR = os.path.dirname(BASE_DIR)
PROJECT_ROOT = os.path.dirname(AGENTS_DIR)

AUDIT_LOG_PATH = os.path.join(BASE_DIR, "gatekeeper_audit.log")
TOKEN_STORE_PATH = os.path.join(BASE_DIR, "token_store.json")
SESSION_STORE_PATH = os.path.join(BASE_DIR, "session_store.json")
HEALTH_PATH = os.path.join(BASE_DIR, "gatekeeper_health.json")
GOVERNANCE_PATH = os.path.join(AGENTS_DIR, "rules", "01-joint-council-governance.md")

# 서명 키: 환경변수 기본값 보장 (설정 없을 시 로컬 기본 비밀키 적용)
SECRET_KEY = os.environ.get("GATEKEEPER_SECRET", "deepseek-council-immutable-secret-2026").encode("utf-8")

TOKEN_TTL_SEC = 300                 # 검증 토큰 유효기간 5분
SESSION_TTL_SEC = 3600              # 세션 유효기간 1시간
HOOK_TIMEOUT_SEC = 10               # 훅 실행 상한 (hooks.json timeout 과 동일)
WATCHDOG_INTERVAL_SEC = 30          # 헬스체크 주기
MAX_AUDIT_BYTES = 50 * 1024 * 1024  # 감사 로그 로테이션 기준
MAX_TOKEN_STORE_ENTRIES = 5000      # 토큰 저장소 폭주 방지 상한

# ------------------------------------------------------------
# 차단 대상 도구 — 쓰기/실행 계열 (GUARDED_WRITE)
# ------------------------------------------------------------
GUARDED_WRITE_TOOLS = {
    "write_to_file",
    "replace_file_content",
    "multi_replace_file_content",
    "run_command",
    "execute_command",
    "shell",
    "bash",
    "python_exec",
    "delete_file",
    "move_file",
    "create_file",
    "edit_file",
    "apply_patch",
    "git_commit",
    "git_push",
}

# ------------------------------------------------------------
# 차단 대상 도구 — 조사/열람 계열 (GUARDED_READ)
# 제미나이의 독단적 코드 열람/조사 원천 차단
# ------------------------------------------------------------
GUARDED_READ_TOOLS = {
    "read_file",
    "view_file",
    "view_code_item",
    "open_file",
    "cat",
    "head",
    "tail",
    "grep",
    "grep_search",
    "search",
    "codebase_search",
    "semantic_search",
    "find_by_name",
    "find_file",
    "list_files",
    "list_dir",
    "list_directory",
    "glob",
    "ls",
    "tree",
    "read_url_content",
    "read_notebook",
    "get_file_contents",
}

GUARDED_TOOLS = GUARDED_WRITE_TOOLS | GUARDED_READ_TOOLS

# 도구명 정규화용 패턴 (MCP 프리픽스/카멜케이스/하이픈 변형 흡수)
_TOOL_NORMALIZE_RE = re.compile(r"[^a-z0-9]+")

# 조사 계열 판별 키워드 (정규화된 도구명에 포함되면 조사 계열로 간주)
_READ_KEYWORDS = (
    "read", "view", "grep", "search", "list", "glob", "cat", "head", "tail",
    "open", "find", "tree", "ls", "inspect", "scan", "dump", "peek",
)
# 쓰기/실행 계열 판별 키워드
_WRITE_KEYWORDS = (
    "write", "replace", "edit", "create", "delete", "move", "run", "exec",
    "shell", "bash", "patch", "commit", "push", "apply", "install", "kill",
)

# 딥시크 5인 협업체 식별자 (거버넌스 문서와 1:1 대응)
DEEPSEEK_COLLABORATORS = [
    "ds_r1_architect",      # AI 1 — 수석설계 아키텍트
    "ds_v3_auditor",        # AI 2 — 비판적 감사검토관
    "ds_v3_engineer",       # AI 3 — 선임 엔지니어 (코드 독점 집필)
    "ds_qa_verifier",       # AI 4 — QA 샌드박스 검증관
    "ds_git_guardian",      # AI 5 — Git Diff 안전관리관
]
REQUIRED_APPROVALS = len(DEEPSEEK_COLLABORATORS)

# 하위 호환 별칭 (기존 토큰/스크립트 호환)
COLLABORATOR_ALIASES = {
    "ds_architect": "ds_r1_architect",
    "ds_reviewer": "ds_v3_auditor",
    "ds_auditor": "ds_v3_auditor",
    "ds_engineer": "ds_v3_engineer",
    "ds_verifier": "ds_qa_verifier",
}

# 토큰 전달 경로 (payload 최상위 / args 내부 / 환경변수)
TOKEN_ARG_KEYS = ("verification_token", "token_id", "gatekeeper_token", "council_token")
TOKEN_ENV_KEYS = ("GATEKEEPER_TOKEN", "DEEPSEEK_COUNCIL_TOKEN")

# ------------------------------------------------------------
# Antigravity 표준 스키마 메시지 (단일 출처)
# ------------------------------------------------------------
PRE_INVOCATION_MESSAGE = (
    "🚨 [6인 합동 회의체 상시 거버넌스] 제미나이는 소스코드 직접 수정 권한이 "
    "물리적으로 차단되어 있습니다. 모든 코드 수정은 딥시크 5인 협업체 도구"
    "(deepseek_council_debate, deepseek_autonomous_codegen)를 호출하여 "
    "딥시크에게 코드를 제시하고 독점 집필하게 하십시오."
)

DENY_REASON = (
    "⛔ [GATEKEEPER 물리적 차단] 제미나이의 소스코드 직접 수정이 차단되었습니다. "
    "deepseek-council의 deepseek_council_debate 또는 deepseek_autonomous_codegen을 "
    "호출하여 딥시크 5인 협업체와 코드를 작성하십시오."
)

ALLOW_REASON = "OK"


# ============================================================
# 1. 감사 로그 (append-only + SHA-256 해시 체인 + 무결성 검증)
# ============================================================
class AuditLog:
    _lock = threading.Lock()

    @staticmethod
    def _last_hash() -> str:
        if not os.path.exists(AUDIT_LOG_PATH):
            return "GENESIS"
        try:
            with open(AUDIT_LOG_PATH, "rb") as f:
                f.seek(0, os.SEEK_END)
                size = f.tell()
                f.seek(max(0, size - 8192))
                tail = f.read().decode("utf-8", errors="ignore").strip().splitlines()
                if not tail:
                    return "GENESIS"
                return json.loads(tail[-1]).get("hash", "GENESIS")
        except Exception:
            return "GENESIS"

    @classmethod
    def write(cls, event: Dict[str, Any]) -> None:
        with cls._lock:
            try:
                if os.path.exists(AUDIT_LOG_PATH) and os.path.getsize(AUDIT_LOG_PATH) > MAX_AUDIT_BYTES:
                    os.replace(AUDIT_LOG_PATH, AUDIT_LOG_PATH + f".{int(time.time())}")
                prev = cls._last_hash()
                event = dict(event)
                event["ts"] = datetime.now(timezone.utc).isoformat()
                event["prev_hash"] = prev
                payload = json.dumps(event, sort_keys=True, ensure_ascii=False)
                event["hash"] = hashlib.sha256((prev + payload).encode("utf-8")).hexdigest()
                with open(AUDIT_LOG_PATH, "a", encoding="utf-8") as f:
                    f.write(json.dumps(event, ensure_ascii=False) + "\n")
            except Exception:
                sys.stderr.write("[AUDIT-FAIL] " + traceback.format_exc())

    @classmethod
    def verify_chain(cls) -> Tuple[bool, str]:
        """감사 로그 해시체인 무결성 검증 (변조/삭제 탐지)."""
        if not os.path.exists(AUDIT_LOG_PATH):
            return True, "로그 없음 (검증 생략)"
        prev = "GENESIS"
        count = 0
        try:
            with open(AUDIT_LOG_PATH, "r", encoding="utf-8") as f:
                for lineno, line in enumerate(f, 1):
                    line = line.strip()
                    if not line:
                        continue
                    rec = json.loads(line)
                    stored_hash = rec.pop("hash", "")
                    if rec.get("prev_hash") != prev:
                        return False, f"라인 {lineno}: prev_hash 불일치 (체인 단절)"
                    payload = json.dumps(rec, sort_keys=True, ensure_ascii=False)
                    expect = hashlib.sha256((prev + payload).encode("utf-8")).hexdigest()
                    if not hmac.compare_digest(expect, stored_hash):
                        return False, f"라인 {lineno}: 해시 위조 감지"
                    prev = stored_hash
                    count += 1
            return True, f"무결성 OK ({count}건 검증)"
        except Exception as e:
            return False, f"검증 예외: {e}"


# ============================================================
# 2. 토큰 발급기 (신뢰 경계 분리 — 딥시크 5인 협업체 전용)
# ============================================================
@dataclass
class VerificationToken:
    token_id: str
    request_fingerprint: str
    approvals: List[str]
    issued_at: float
    expires_at: float
    signature: str


class TokenIssuer:
    """딥시크 5인 협업체만 호출 가능. 제미나이 컨텍스트에서는 접근 불가."""

    @staticmethod
    def _sign(payload: str) -> str:
        if not SECRET_KEY:
            raise RuntimeError("GATEKEEPER_SECRET 미설정 — 발급 거부")
        return hmac.new(SECRET_KEY, payload.encode("utf-8"), hashlib.sha256).hexdigest()

    @staticmethod
    def normalize_approvals(approvals: List[str]) -> List[str]:
        """별칭 → 정식 식별자 정규화 후 중복 제거."""
        out = set()
        for a in approvals or []:
            key = str(a).strip().lower()
            out.add(COLLABORATOR_ALIASES.get(key, key))
        return sorted(out)

    @staticmethod
    def fingerprint(tool_name: str, args: Dict[str, Any]) -> str:
        """요청 지문 — 토큰 자체 필드는 제외하여 재사용/위조 방지."""
        safe_args = {}
        if isinstance(args, dict):
            safe_args = {k: v for k, v in args.items() if k not in TOKEN_ARG_KEYS}
        canonical = json.dumps(
            {"tool": tool_name, "args": safe_args},
            sort_keys=True,
            ensure_ascii=False,
            default=str,
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @classmethod
    def issue(cls, tool_name: str, args: Dict[str, Any], approvals: List[str]) -> VerificationToken:
        normalized = cls.normalize_approvals(approvals)
        if normalized != sorted(DEEPSEEK_COLLABORATORS) or len(normalized) < REQUIRED_APPROVALS:
            raise PermissionError(
                f"딥시크 5인 협업체 전원 승인 필요 (현재 {len(normalized)}/{REQUIRED_APPROVALS})"
            )
        fp = cls.fingerprint(tool_name, args)
        now = time.time()
        token_id = hashlib.sha256(f"{fp}{now}{os.urandom(8).hex()}".encode()).hexdigest()[:32]
        payload = f"{token_id}|{fp}|{','.join(normalized)}|{now}|{now + TOKEN_TTL_SEC}"
        tok = VerificationToken(
            token_id=token_id,
            request_fingerprint=fp,
            approvals=normalized,
            issued_at=now,
            expires_at=now + TOKEN_TTL_SEC,
            signature=cls._sign(payload),
        )
        cls._persist(tok)
        AuditLog.write({
            "event": "token_issued",
            "token_id": token_id,
            "tool": tool_name,
            "fingerprint": fp,
            "approvals": normalized,
        })
        return tok

    @staticmethod
    def _persist(tok: VerificationToken) -> None:
        store = {}
        if os.path.exists(TOKEN_STORE_PATH):
            try:
                with open(TOKEN_STORE_PATH, "r", encoding="utf-8") as f:
                    store = json.load(f)
            except Exception:
                store = {}
        store[tok.token_id] = asdict(tok)
        now = time.time()
        # 만료 토큰 정리 + 폭주 방지 상한
        store = {k: v for k, v in store.items() if v.get("expires_at", 0) > now}
        if len(store) > MAX_TOKEN_STORE_ENTRIES:
            ordered = sorted(store.items(), key=lambda kv: kv[1].get("issued_at", 0), reverse=True)
            store = dict(ordered[:MAX_TOKEN_STORE_ENTRIES])
        tmp = TOKEN_STORE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(store, f, ensure_ascii=False)
        os.replace(tmp, TOKEN_STORE_PATH)

    @classmethod
    def verify(cls, token_id: str, tool_name: str, args: Dict[str, Any]) -> Tuple[bool, str]:
        if not SECRET_KEY:
            return False, "SECRET 미설정"
        if not token_id:
            return False, "토큰 없음 (제미나이 독단 시도 감지)"
        if not os.path.exists(TOKEN_STORE_PATH):
            return False, "토큰 저장소 없음 (딥시크 미승인)"
        try:
            with open(TOKEN_STORE_PATH, "r", encoding="utf-8") as f:
                store = json.load(f)
        except Exception:
            return False, "토큰 저장소 손상"
        rec = store.get(token_id)
        if not rec:
            return False, "미등록 토큰 (딥시크 미승인)"
        if time.time() > rec.get("expires_at", 0):
            return False, "토큰 만료"
        fp = cls.fingerprint(tool_name, args)
        if not hmac.compare_digest(fp, rec.get("request_fingerprint", "")):
            return False, "요청 지문 불일치(재사용 의심)"
        payload = (
            f"{rec['token_id']}|{rec['request_fingerprint']}|"
            f"{','.join(rec['approvals'])}|{rec['issued_at']}|{rec['expires_at']}"
        )
        if not hmac.compare_digest(cls._sign(payload), rec.get("signature", "")):
            return False, "서명 위조"
        if cls.normalize_approvals(rec.get("approvals", [])) != sorted(DEEPSEEK_COLLABORATORS):
            return False, "승인자 불완전"
        return True, "OK"


# ============================================================
# 3. 게이트키퍼 (PreToolUse 단일 집행 관문 — fail-closed)
# ============================================================
class Gatekeeper:
    @staticmethod
    def normalize_tool_name(raw: str) -> str:
        """MCP 프리픽스/카멜케이스/하이픈 변형을 표준 스네이크 표기로 정규화."""
        if not raw:
            return ""
        name = str(raw).strip()
        # mcp__server__tool / server.tool 형태에서 마지막 세그먼트 추출
        for sep in ("__", ".", "/", ":"):
            if sep in name:
                name = name.split(sep)[-1]
        name = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", name)  # camelCase → snake_case
        name = _TOOL_NORMALIZE_RE.sub("_", name.lower()).strip("_")
        return name

    @classmethod
    def classify(cls, raw_tool: str) -> str:
        """도구 분류: 'write' | 'read' | 'other'."""
        name = cls.normalize_tool_name(raw_tool)
        if not name:
            return "other"
        if name in GUARDED_WRITE_TOOLS:
            return "write"
        if name in GUARDED_READ_TOOLS:
            return "read"
        # 정확 매칭 실패 시 키워드 기반 보수적 분류 (fail-closed 지향)
        if any(k in name for k in _WRITE_KEYWORDS):
            return "write"
        if any(k in name for k in _READ_KEYWORDS):
            return "read"
        return "unknown"

    @staticmethod
    def extract(payload: Dict[str, Any]) -> Tuple[str, Dict[str, Any], str]:
        """
        Antigravity / Claude Code / MCP 등 다양한 훅 payload 스키마 흡수.

        [표준] toolCall = payload.get('toolCall') or payload.get('tool_call') or {}
               → toolCall.name / toolCall.args 에서 도구명·인자 추출.
        """
        tool_call = payload.get("toolCall") or payload.get("tool_call") or {}
        if not isinstance(tool_call, dict):
            tool_call = {}
        tool = (
            tool_call.get("name")
            or payload.get("tool_name")
            or payload.get("toolName")
            or payload.get("tool")
            or ""
        )
        args = (
            tool_call.get("args")
            or tool_call.get("input")
            or payload.get("tool_input")
            or payload.get("toolInput")
            or payload.get("args")
            or payload.get("input")
            or {}
        )
        if not isinstance(args, dict):
            args = {"_raw": args}

        token_id = ""
        for key in TOKEN_ARG_KEYS:
            if args.get(key):
                token_id = str(args.get(key))
                break
        if not token_id:
            for key in TOKEN_ARG_KEYS:
                if payload.get(key):
                    token_id = str(payload.get(key))
                    break
        if not token_id:
            for key in TOKEN_ENV_KEYS:
                if os.environ.get(key):
                    token_id = os.environ[key]
                    break
        return str(tool), args, token_id

    @classmethod
    def decide(cls, payload: Dict[str, Any]) -> Dict[str, Any]:
        """
        PreToolUse 판정 — Antigravity 표준 스키마 반환.

        반환:
          - 차단: {"decision": "deny",  "reason": DENY_REASON}
          - 허용: {"decision": "allow", "reason": "OK"}
        """
        tool, args, token_id = cls.extract(payload)
        category = cls.classify(tool)

        # fail-closed: 도구명 누락 또는 unknown 카테고리는 즉시 deny
        if not tool or category == "unknown":
            AuditLog.write({"event": "gatekeeper_deny_unknown", "tool": tool, "payload": payload})
            return {"decision": "deny", "reason": DENY_REASON}

        # fail-closed: 예외 발생 시 무조건 deny
        try:
            ok, reason = TokenIssuer.verify(token_id, tool, args)
        except Exception as e:
            ok, reason = False, f"검증 예외: {e}"

        try:
            fp = TokenIssuer.fingerprint(tool, args)
        except Exception:
            fp = "n/a"

        AuditLog.write({
            "event": "gatekeeper_decision",
            "tool": tool,
            "category": category,
            "token_id": token_id,
            "fingerprint": fp,
            "decision": "allow" if ok else "deny",
            "reason": reason,
        })

        # 딥시크 5인 서명 토큰이 유효한 경우에만 allow
        if ok:
            return {"decision": "allow", "reason": ALLOW_REASON}

        return {"decision": "deny", "reason": DENY_REASON}


# ============================================================
# 4. 세션 가드 (PreInvocation — 세션 점검 / 좀비 정리 / 거버넌스 로드)
# ============================================================
class SessionGuard:
    @staticmethod
    def _load_store() -> Dict[str, Any]:
        if not os.path.exists(SESSION_STORE_PATH):
            return {}
        try:
            with open(SESSION_STORE_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    @staticmethod
    def _save_store(store: Dict[str, Any]) -> None:
        tmp = SESSION_STORE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(store, f, ensure_ascii=False)
        os.replace(tmp, SESSION_STORE_PATH)

    @classmethod
    def sweep_zombies(cls) -> Dict[str, Any]:
        """만료/좀비 세션 및 만료 토큰 일괄 회수."""
        now = time.time()
        swept_sessions = 0
        swept_tokens = 0

        store = cls._load_store()
        alive = {}
        for sid, rec in store.items():
            try:
                if float(rec.get("expires_at", 0)) > now and rec.get("state") != "zombie":
                    alive[sid] = rec
                else:
                    swept_sessions += 1
            except Exception:
                swept_sessions += 1
        try:
            cls._save_store(alive)
        except Exception:
            pass

        if os.path.exists(TOKEN_STORE_PATH):
            try:
                with open(TOKEN_STORE_PATH, "r", encoding="utf-8") as f:
                    tokens = json.load(f)
                if isinstance(tokens, dict):
                    kept = {k: v for k, v in tokens.items() if float(v.get("expires_at", 0)) > now}
                    swept_tokens = len(tokens) - len(kept)
                    if swept_tokens:
                        tmp = TOKEN_STORE_PATH + ".tmp"
                        with open(tmp, "w", encoding="utf-8") as f:
                            json.dump(kept, f, ensure_ascii=False)
                        os.replace(tmp, TOKEN_STORE_PATH)
            except Exception:
                pass

        return {
            "swept_sessions": swept_sessions,
            "swept_tokens": swept_tokens,
            "alive_sessions": len(alive),
        }

    @staticmethod
    def load_governance() -> Dict[str, Any]:
        """거버넌스 문서 로드 + 필수 조항 존재 검증."""
        if not os.path.exists(GOVERNANCE_PATH):
            return {"loaded": False, "reason": "거버넌스 문서 부재"}
        try:
            with open(GOVERNANCE_PATH, "r", encoding="utf-8") as f:
                text = f.read()
            required = ["제미나이", "딥시크", "물리적 차단", "단방향 파이프라인"]
            missing = [k for k in required if k not in text]
            return {
                "loaded": True,
                "bytes": len(text.encode("utf-8")),
                "missing_clauses": missing,
                "intact": not missing,
            }
        except Exception as e:
            return {"loaded": False, "reason": f"로드 예외: {e}"}

    @classmethod
    def pre_invocation(cls, payload: Dict[str, Any]) -> Dict[str, Any]:
        """
        PreInvocation 진입점 — 세션 점검 + 좀비 정리 + 거버넌스 로드.

        [Antigravity 표준 스키마]
          {"injectSteps": [{"ephemeralMessage": "..."}]}
        """
        session_id = (
            payload.get("sessionId")
            or payload.get("session_id")
            or payload.get("conversationId")
            or "default"
        )
        now = time.time()

        store = cls._load_store()
        rec = store.get(str(session_id))
        if rec and float(rec.get("expires_at", 0)) > now:
            rec["last_seen"] = now
            rec["invocations"] = int(rec.get("invocations", 0)) + 1
            store[str(session_id)] = rec
        else:
            store[str(session_id)] = {
                "created_at": now,
                "last_seen": now,
                "expires_at": now + SESSION_TTL_SEC,
                "invocations": 1,
                "state": "active",
            }
        try:
            cls._save_store(store)
        except Exception:
            pass

        sweep = cls.sweep_zombies()
        gov = cls.load_governance()

        AuditLog.write({
            "event": "pre_invocation",
            "session_id": str(session_id),
            "sweep": sweep,
            "governance_intact": gov.get("intact", False),
        })

        # 거버넌스 문서 훼손 시 경고를 ephemeralMessage 에 병기 (세션 자체는 유지)
        message = PRE_INVOCATION_MESSAGE
        if not gov.get("intact", False):
            message += f" ⚠️ 거버넌스 문서 점검 필요: {gov}"

        return {"injectSteps": [{"ephemeralMessage": message}]}


# ============================================================
# 5. 워치독 (24h 무인 안전장치)
# ============================================================
class Watchdog:
    def __init__(self):
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def _beat(self):
        try:
            with open(HEALTH_PATH, "w", encoding="utf-8") as f:
                json.dump({"alive_at": time.time(), "pid": os.getpid()}, f)
        except Exception:
            pass

    def _reap_zombies(self):
        if sys.platform != "win32":
            try:
                while True:
                    pid, _ = os.waitpid(-1, os.WNOHANG)
                    if pid == 0:
                        break
            except ChildProcessError:
                pass
            except Exception:
                pass

    def _loop(self):
        while not self._stop.wait(WATCHDOG_INTERVAL_SEC):
            self._beat()
            self._reap_zombies()

    def start(self):
        self._beat()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="gatekeeper-watchdog")
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)


# ============================================================
# 6. 훅 실행기 (타임아웃 + 데드락 방지 + fail-closed)
# ============================================================
def _run_with_timeout(fn, timeout_sec: int) -> Dict[str, Any]:
    result: Dict[str, Any] = {}

    def _target():
        try:
            result["value"] = fn()
        except Exception as e:
            result["error"] = e

    t = threading.Thread(target=_target, daemon=True)
    t.start()
    t.join(timeout_sec)
    if t.is_alive():
        # 데드락 방지: 데몬 스레드는 프로세스 종료 시 자동 회수
        return {"decision": "deny", "reason": DENY_REASON}
    if "error" in result:
        return {"decision": "deny", "reason": DENY_REASON}
    return result.get("value", {"decision": "deny", "reason": DENY_REASON})


def _signal_handler(signum, frame):
    AuditLog.write({"event": "signal", "signum": signum})
    sys.exit(0)


# ============================================================
# 7. 진입점 (PreToolUse / PreInvocation 라우팅)
# ============================================================
def _detect_event(payload: Dict[str, Any]) -> str:
    raw = str(
        payload.get("hookEventName")
        or payload.get("hook_event_name")
        or payload.get("event")
        or payload.get("eventName")
        or ""
    ).strip().lower()
    if "preinvocation" in raw or "pre_invocation" in raw or "invocation" in raw:
        return "pre_invocation"
    if "pretooluse" in raw or "pre_tool_use" in raw or "tool" in raw:
        return "pre_tool_use"
    # 이벤트 미표기 시 payload 구조로 추론
    if payload.get("toolCall") or payload.get("tool_call") or payload.get("tool_name") \
            or payload.get("toolName") or payload.get("tool"):
        return "pre_tool_use"
    return "pre_invocation"


def _emit(obj: Dict[str, Any]) -> None:
    """표준 스키마 JSON 을 stdout 에 단일 라인으로 출력."""
    try:
        sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
        sys.stdout.flush()
    except Exception:
        # 출력 실패 시에도 fail-closed (deny) 보장
        try:
            sys.stdout.write(json.dumps({"decision": "deny", "reason": DENY_REASON}, ensure_ascii=False) + "\n")
            sys.stdout.flush()
        except Exception:
            pass


def main() -> int:
    try:
        signal.signal(signal.SIGTERM, _signal_handler)
        signal.signal(signal.SIGINT, _signal_handler)
    except Exception:
        pass

    wd = Watchdog()
    wd.start()

    try:
        raw = sys.stdin.read()
        if not raw.strip():
            AuditLog.write({"event": "empty_input"})
            _emit({"decision": "deny", "reason": DENY_REASON})
            return 0
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as e:
            AuditLog.write({"event": "bad_json", "error": str(e)})
            _emit({"decision": "deny", "reason": DENY_REASON})
            return 0
        if not isinstance(payload, dict):
            AuditLog.write({"event": "bad_payload_type", "type": type(payload).__name__})
            _emit({"decision": "deny", "reason": DENY_REASON})
            return 0

        event = _detect_event(payload)
        if event == "pre_invocation":
            result = _run_with_timeout(lambda: SessionGuard.pre_invocation(payload), HOOK_TIMEOUT_SEC)
            # PreInvocation 은 injectSteps 스키마만 허용 — 실패 시에도 표준 스키마 유지
            if not isinstance(result, dict) or "injectSteps" not in result:
                result = {"injectSteps": [{"ephemeralMessage": PRE_INVOCATION_MESSAGE}]}
        else:
            result = _run_with_timeout(lambda: Gatekeeper.decide(payload), HOOK_TIMEOUT_SEC)
            # PreToolUse 는 decision 스키마만 허용 — 실패 시 fail-closed deny
            if not isinstance(result, dict) or "decision" not in result:
                result = {"decision": "deny", "reason": DENY_REASON}

        _emit(result)
        return 0
    except Exception:
        AuditLog.write({"event": "fatal", "trace": traceback.format_exc()})
        _emit({"decision": "deny", "reason": DENY_REASON})
        return 0
    finally:
        wd.stop()


if __name__ == "__main__":
    sys.exit(main())
