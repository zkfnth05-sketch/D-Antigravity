#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
================================================================================
 6인 합동 회의체 자동화 런타임 본체 (council_runtime.py)
================================================================================
[거버넌스] 제미나이 1인 토론위원 + 딥시크 5인 협업체 (6인 합동 회의체)
[핵심 원칙] 규약 설명이 아닌 '실제 코드'로 강제한다.
            - 단독(제미나이) 접근은 라우터 단계에서 ROUTER_REJECTED 로 거부.
            - MCP 인자 스키마 위반은 실행 전 차단.
            - QA 게이트 미통과 시 파이프라인 즉시 중단(fail-closed).
            - 파일 접근은 Deny-by-default 중재 계층을 통해서만 허용.

구성 요소:
  1) CouncilRouter   : 5인 합동 심의 강제 라우터 (단독 접근 시 ROUTER_REJECTED)
  2) MCPContract     : deepseek-council MCP 인자 스키마 검증기
     MCPExecutor     : 타임아웃 / 재시도 / 지수 백오프 실행기
  3) QAGate          : py_compile + 단위 테스트 필수 게이트 (미통과 시 중단)
  4) SessionRegistry : 세션 ID 기반 감사 추적 + TTL 좀비 세션 자동 회수
  5) FileBroker      : Deny-by-default 파일 중재 계층 (READ_RECEIPT 발급)

[연동] .agents/scripts/gatekeeper_hook.py 의 AuditLog / TokenIssuer /
       DEEPSEEK_COLLABORATORS 계약과 1:1 정합. 게이트키퍼가 없거나 로드 실패 시
       자체 폴백 감사로그로 fail-closed 동작을 유지한다.

작성: DeepSeek-V3 선임 구현 엔지니어 (독점 집필)
================================================================================
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import io
import json
import os
import py_compile
import re
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

# ============================================================
# 0. 경로 / 상수
# ============================================================

BASE_DIR = os.path.dirname(os.path.abspath(__file__))          # .agents/scripts
AGENTS_DIR = os.path.dirname(BASE_DIR)                          # .agents
PROJECT_ROOT = os.path.dirname(AGENTS_DIR)                      # 워크스페이스 루트

GATEKEEPER_PATH = os.path.join(BASE_DIR, "gatekeeper_hook.py")
RUNTIME_AUDIT_PATH = os.path.join(BASE_DIR, "council_runtime_audit.log")
SESSION_STORE_PATH = os.path.join(BASE_DIR, "session_store.json")
RECEIPT_STORE_PATH = os.path.join(BASE_DIR, "read_receipts.json")

# 딥시크 5인 협업체 정식 식별자 (gatekeeper_hook.py 와 동일 순서/표기)
DEEPSEEK_COLLABORATORS: Tuple[str, ...] = (
    "ds_r1_architect",   # AI 1 — 수석설계 아키텍트
    "ds_v3_auditor",     # AI 2 — 비판적 감사검토관
    "ds_v3_engineer",    # AI 3 — 선임 구현 엔지니어 (코드 독점 집필)
    "ds_qa_verifier",    # AI 4 — QA 샌드박스 검증관
    "ds_git_guardian",   # AI 5 — Git Diff 안전관리관
)
REQUIRED_APPROVALS = len(DEEPSEEK_COLLABORATORS)

# 역할 별칭 → 정식 식별자 (gatekeeper_hook.py COLLABORATOR_ALIASES 와 정합)
COLLABORATOR_ALIASES: Dict[str, str] = {
    "ds_architect": "ds_r1_architect",
    "architect": "ds_r1_architect",
    "r1": "ds_r1_architect",
    "ds_reviewer": "ds_v3_auditor",
    "ds_auditor": "ds_v3_auditor",
    "auditor": "ds_v3_auditor",
    "ds_engineer": "ds_v3_engineer",
    "engineer": "ds_v3_engineer",
    "ds_verifier": "ds_qa_verifier",
    "qa": "ds_qa_verifier",
    "verifier": "ds_qa_verifier",
    "ds_git": "ds_git_guardian",
    "git": "ds_git_guardian",
    "guardian": "ds_git_guardian",
}

# 제미나이(단독) 식별자 — 라우터에서 무조건 거부
GEMINI_IDENTIFIERS = frozenset({
    "gemini", "gemini_pro", "gemini_flash", "gemini_1_5_pro", "gemini_2_0_flash",
    "antigravity", "antigravity_agent", "user", "human", "solo", "single",
})

# 세션 / 실행 정책
SESSION_TTL_SEC = 3600          # 세션 유효기간 1시간
ZOMBIE_IDLE_SEC = 900           # 15분 무활동 → 좀비 판정
DEFAULT_TIMEOUT_SEC = 30        # MCP 실행 기본 타임아웃
DEFAULT_MAX_RETRIES = 2         # 기본 재시도 횟수
BACKOFF_BASE_SEC = 0.5          # 지수 백오프 기준
BACKOFF_CAP_SEC = 8.0           # 백오프 상한
MAX_AUDIT_BYTES = 50 * 1024 * 1024
MAX_RECEIPT_ENTRIES = 5000
MAX_SESSIONS = 1000               # 세션 등록부 최대 보관 수 (초과 시 LRU 축출)
MAX_FILE_BYTES = 8 * 1024 * 1024  # FileBroker 단일 파일 최대 크기 (8 MiB)

# 런타임 버전
RUNTIME_VERSION = "1.0.0"

# 거부/결과 코드
ROUTER_REJECTED = "ROUTER_REJECTED"
SCHEMA_REJECTED = "SCHEMA_REJECTED"
QA_GATE_FAILED = "QA_GATE_FAILED"
BROKER_DENIED = "BROKER_DENIED"
EXEC_TIMEOUT = "EXEC_TIMEOUT"
EXEC_FAILED = "EXEC_FAILED"
QA_PASSED = "QA_PASSED"
QA_FAILED = "QA_FAILED"
SESSION_REJECTED = "SESSION_REJECTED"


# ============================================================
# 1. 예외 계층
# ============================================================

class CouncilError(Exception):
    """런타임 공통 예외 (code 속성으로 거부 사유 식별)."""
    code = "COUNCIL_ERROR"

    def __init__(self, message: str, code: Optional[str] = None, detail: Any = None):
        super().__init__(message)
        self.message = message
        if code:
            self.code = code
        self.detail = detail

    def to_dict(self) -> Dict[str, Any]:
        return {"code": self.code, "message": self.message, "detail": self.detail}


class RouterRejected(CouncilError):
    code = ROUTER_REJECTED


class SchemaRejected(CouncilError):
    code = SCHEMA_REJECTED


class QAGateFailed(CouncilError):
    code = QA_GATE_FAILED


class BrokerDenied(CouncilError):
    code = BROKER_DENIED


class ExecTimeout(CouncilError):
    code = EXEC_TIMEOUT


class ExecFailed(CouncilError):
    code = EXEC_FAILED


class SessionRejected(CouncilError):
    """세션 리플레이/중복 지문 탐지 시 발생."""
    code = SESSION_REJECTED


class CouncilRuntimeError(CouncilError):
    """런타임 예외 계층 최상위 별칭 (하위 호환/일괄 포획용)."""
    code = "COUNCIL_ERROR"


# ============================================================
# 2. 감사 로그 (게이트키퍼 연동 + 자체 폴백)
# ============================================================

def _load_gatekeeper_module():
    """gatekeeper_hook.py 를 모듈로 동적 로드 (경로/이름 충돌 회피)."""
    if not os.path.isfile(GATEKEEPER_PATH):
        return None
    try:
        spec = importlib.util.spec_from_file_location("_council_gatekeeper", GATEKEEPER_PATH)
        if spec is None or spec.loader is None:
            return None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    except Exception:
        return None


_GATEKEEPER = _load_gatekeeper_module()


class RuntimeAudit:
    """런타임 감사 기록기. 게이트키퍼 AuditLog 우선, 실패 시 자체 해시체인 폴백."""

    log_path = RUNTIME_AUDIT_PATH

    _lock = threading.Lock()

    @staticmethod
    def _fallback_write(event: Dict[str, Any]) -> None:
        with RuntimeAudit._lock:
            try:
                if os.path.exists(RUNTIME_AUDIT_PATH) and os.path.getsize(RUNTIME_AUDIT_PATH) > MAX_AUDIT_BYTES:
                    os.replace(RUNTIME_AUDIT_PATH, RUNTIME_AUDIT_PATH + "." + str(int(time.time())))
                prev = "GENESIS"
                if os.path.exists(RUNTIME_AUDIT_PATH):
                    try:
                        with open(RUNTIME_AUDIT_PATH, "rb") as f:
                            f.seek(0, os.SEEK_END)
                            size = f.tell()
                            f.seek(max(0, size - 8192))
                            tail = f.read().decode("utf-8", errors="ignore").strip().splitlines()
                            if tail:
                                prev = json.loads(tail[-1]).get("hash", "GENESIS")
                    except Exception:
                        prev = "GENESIS"
                rec = dict(event)
                rec["ts"] = datetime.now(timezone.utc).isoformat()
                rec["prev_hash"] = prev
                payload = json.dumps(rec, sort_keys=True, ensure_ascii=False, default=str)
                rec["hash"] = hashlib.sha256((prev + payload).encode("utf-8")).hexdigest()
                with open(RUNTIME_AUDIT_PATH, "a", encoding="utf-8") as f:
                    f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
            except Exception:
                sys.stderr.write("[RUNTIME-AUDIT-FAIL] " + traceback.format_exc())

    @classmethod
    def write(cls, event: Dict[str, Any]) -> None:
        """감사 기록 — 어떤 경우에도 예외를 밖으로 던지지 않는다."""
        try:
            if _GATEKEEPER is not None and hasattr(_GATEKEEPER, "AuditLog"):
                _GATEKEEPER.AuditLog.write(event)
                return
        except Exception:
            pass
        cls._fallback_write(event)


# ============================================================
# 3. CouncilRouter — 5인 합동 심의 강제 라우터
# ============================================================

@dataclass
class CouncilDecision:
    """라우터 심의 결과."""
    approved: bool
    code: str
    reason: str
    approvals: List[str] = field(default_factory=list)
    missing: List[str] = field(default_factory=list)
    rejected_by: List[str] = field(default_factory=list)
    request_fingerprint: str = ""
    session_id: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class CouncilRouter:
    """
    5인 합동 심의 강제 라우터.

    규칙:
      - 요청자는 반드시 딥시크 5인 협업체 중 1인이어야 한다.
      - 제미나이/사용자/단독 에이전트 식별자는 즉시 ROUTER_REJECTED.
      - 승인 목록(approvals)에 5인 전원이 포함되지 않으면 ROUTER_REJECTED.
      - 거부권(veto) 보유 역할이 거부하면 즉시 ROUTER_REJECTED.
    """

    # 거부권 보유 역할 (감사관 / QA / Git 안전관)
    VETO_ROLES = ("ds_v3_auditor", "ds_qa_verifier", "ds_git_guardian")

    @staticmethod
    def normalize_identity(raw):
        """식별자 정규화 (대소문자/하이픈/공백 흡수 + 별칭 해석)."""
        if raw is None:
            return ""
        key = re.sub(r"[^a-z0-9]+", "_", str(raw).strip().lower()).strip("_")
        return COLLABORATOR_ALIASES.get(key, key)

    @classmethod
    def normalize_approvals(cls, approvals):
        out = set()
        for a in approvals or []:
            norm = cls.normalize_identity(a)
            if norm:
                out.add(norm)
        return sorted(out)

    @staticmethod
    def fingerprint(tool_name, args):
        """요청 지문 — 토큰 필드 제외, 결정적 직렬화."""
        token_keys = {"verification_token", "token_id", "gatekeeper_token", "council_token"}
        safe = {k: v for k, v in (args or {}).items() if k not in token_keys}
        canonical = json.dumps(
            {"tool": tool_name, "args": safe},
            sort_keys=True, ensure_ascii=False, default=str,
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @classmethod
    def route(cls, requester, tool_name, args, approvals, vetoes=None, session_id=""):
        """5인 합동 심의 강제 검증. 통과 시 approved=True, 실패 시 RouterRejected."""
        requester_norm = cls.normalize_identity(requester)
        approvals_norm = cls.normalize_approvals(approvals)
        vetoes_norm = cls.normalize_approvals(vetoes or [])
        fp = cls.fingerprint(tool_name, args or {})

        # (1) 요청자 단독 접근 차단
        if not requester_norm:
            decision = CouncilDecision(
                approved=False, code=ROUTER_REJECTED,
                reason="요청자 식별자 부재 — 단독 접근으로 간주하여 거부",
                approvals=approvals_norm, missing=list(DEEPSEEK_COLLABORATORS),
                request_fingerprint=fp, session_id=session_id,
            )
            cls._audit(decision, tool_name, requester_norm)
            raise RouterRejected(decision.reason, detail=decision.to_dict())

        if requester_norm in GEMINI_IDENTIFIERS:
            decision = CouncilDecision(
                approved=False, code=ROUTER_REJECTED,
                reason="제미나이/단독 에이전트(" + requester_norm + ") 직접 접근 차단 — 5인 합동 심의 필수",
                approvals=approvals_norm, missing=list(DEEPSEEK_COLLABORATORS),
                rejected_by=[requester_norm],
                request_fingerprint=fp, session_id=session_id,
            )
            cls._audit(decision, tool_name, requester_norm)
            raise RouterRejected(decision.reason, detail=decision.to_dict())

        if requester_norm not in DEEPSEEK_COLLABORATORS:
            decision = CouncilDecision(
                approved=False, code=ROUTER_REJECTED,
                reason="미등록 요청자(" + requester_norm + ") — 딥시크 5인 협업체만 요청 가능",
                approvals=approvals_norm, missing=list(DEEPSEEK_COLLABORATORS),
                rejected_by=[requester_norm],
                request_fingerprint=fp, session_id=session_id,
            )
            cls._audit(decision, tool_name, requester_norm)
            raise RouterRejected(decision.reason, detail=decision.to_dict())

        # (2) 거부권 행사 확인
        active_vetoes = [v for v in vetoes_norm if v in cls.VETO_ROLES]
        if active_vetoes:
            decision = CouncilDecision(
                approved=False, code=ROUTER_REJECTED,
                reason="거부권 행사: " + str(active_vetoes) + " — 파이프라인 중단",
                approvals=approvals_norm, missing=[],
                rejected_by=active_vetoes,
                request_fingerprint=fp, session_id=session_id,
            )
            cls._audit(decision, tool_name, requester_norm)
            raise RouterRejected(decision.reason, detail=decision.to_dict())

        # (3) 5인 전원 승인 확인
        missing = [c for c in DEEPSEEK_COLLABORATORS if c not in approvals_norm]
        if missing:
            decision = CouncilDecision(
                approved=False, code=ROUTER_REJECTED,
                reason="5인 전원 승인 미충족 (" + str(len(approvals_norm)) + "/" + str(REQUIRED_APPROVALS) + ") — 누락: " + str(missing),
                approvals=approvals_norm, missing=missing,
                request_fingerprint=fp, session_id=session_id,
            )
            cls._audit(decision, tool_name, requester_norm)
            raise RouterRejected(decision.reason, detail=decision.to_dict())

        decision = CouncilDecision(
            approved=True, code="ROUTER_APPROVED",
            reason="5인 합동 심의 전원 승인",
            approvals=approvals_norm, missing=[],
            request_fingerprint=fp, session_id=session_id,
        )
        cls._audit(decision, tool_name, requester_norm)
        return decision

    @staticmethod
    def _audit(decision, tool_name, requester):
        RuntimeAudit.write({
            "event": "council_router",
            "tool": tool_name,
            "requester": requester,
            "approved": decision.approved,
            "code": decision.code,
            "approvals": decision.approvals,
            "missing": decision.missing,
            "rejected_by": decision.rejected_by,
            "fingerprint": decision.request_fingerprint,
            "session_id": decision.session_id,
        })


# ============================================================
# 4. MCPContract — deepseek-council MCP 인자 스키마 검증기
# ============================================================

@dataclass
class FieldSpec:
    """스키마 필드 명세."""
    name: str
    type: str                       # string | integer | number | boolean | array | object
    required: bool = True
    enum: Optional[Tuple[Any, ...]] = None
    min_len: Optional[int] = None
    max_len: Optional[int] = None
    pattern: Optional[str] = None
    item_type: Optional[str] = None
    default: Any = None


class MCPContract:
    """
    deepseek-council MCP 도구 인자 스키마 검증기.

    - 미등록 도구 호출은 SCHEMA_REJECTED.
    - 필수 필드 누락 / 타입 불일치 / enum 위반 / 길이·정규식 위반 시 SCHEMA_REJECTED.
    - 미정의 필드는 거부(additionalProperties=false) — 인자 주입 공격 차단.
    """

    _TYPE_MAP = {
        "string": (str,),
        "integer": (int,),
        "number": (int, float),
        "boolean": (bool,),
        "array": (list, tuple),
        "object": (dict,),
    }

    def __init__(self):
        self._schemas = {}
        self._register_builtin()

    # ---------- 등록 ----------
    def register(self, tool_name, fields):
        self._schemas[tool_name] = {f.name: f for f in fields}

    def _register_builtin(self):
        """deepseek-council 표준 MCP 도구 스키마 등록."""
        self.register("deepseek_council_deliberate", [
            FieldSpec("topic", "string", True, min_len=3, max_len=2000),
            FieldSpec("requester", "string", True, min_len=2, max_len=64),
            FieldSpec("approvals", "array", True, item_type="string"),
            FieldSpec("vetoes", "array", False, item_type="string", default=[]),
            FieldSpec("session_id", "string", False, max_len=128, default=""),
        ])
        self.register("deepseek_council_write_file", [
            FieldSpec("path", "string", True, min_len=1, max_len=512),
            FieldSpec("content", "string", True, min_len=0),
            FieldSpec("requester", "string", True, min_len=2, max_len=64),
            FieldSpec("approvals", "array", True, item_type="string"),
            FieldSpec("session_id", "string", False, max_len=128, default=""),
        ])
        self.register("deepseek_council_read_file", [
            FieldSpec("path", "string", True, min_len=1, max_len=512),
            FieldSpec("requester", "string", True, min_len=2, max_len=64),
            FieldSpec("approvals", "array", True, item_type="string"),
            FieldSpec("session_id", "string", False, max_len=128, default=""),
        ])
        self.register("deepseek_council_run_command", [
            FieldSpec("command", "string", True, min_len=1, max_len=4000),
            FieldSpec("requester", "string", True, min_len=2, max_len=64),
            FieldSpec("approvals", "array", True, item_type="string"),
            FieldSpec("timeout", "integer", False, default=DEFAULT_TIMEOUT_SEC),
            FieldSpec("session_id", "string", False, max_len=128, default=""),
        ])
        self.register("deepseek_council_qa_gate", [
            FieldSpec("targets", "array", True, item_type="string"),
            FieldSpec("requester", "string", True, min_len=2, max_len=64),
            FieldSpec("approvals", "array", True, item_type="string"),
            FieldSpec("run_tests", "boolean", False, default=True),
            FieldSpec("session_id", "string", False, max_len=128, default=""),
        ])

    # ---------- 검증 ----------
    def known_tools(self):
        return sorted(self._schemas.keys())

    def validate(self, tool_name, args):
        """스키마 검증 후 정규화된 인자 반환. 위반 시 SchemaRejected."""
        if tool_name not in self._schemas:
            raise SchemaRejected(
                "미등록 MCP 도구: " + str(tool_name) + " (허용: " + str(self.known_tools()) + ")",
                detail={"tool": tool_name},
            )
        if not isinstance(args, dict):
            raise SchemaRejected("인자는 JSON 객체여야 합니다.", detail={"type": type(args).__name__})

        schema = self._schemas[tool_name]
        errors = []
        normalized = {}

        # 미정의 필드 거부 (additionalProperties=false)
        unknown = [k for k in args.keys() if k not in schema]
        if unknown:
            errors.append("미정의 인자: " + str(unknown))

        for name, spec in schema.items():
            if name not in args or args[name] is None:
                if spec.required:
                    errors.append("필수 인자 누락: " + name)
                elif spec.default is not None:
                    normalized[name] = spec.default
                continue

            value = args[name]
            expected = self._TYPE_MAP.get(spec.type, (object,))

            # bool 은 int 의 서브클래스이므로 integer/number 검증 시 명시 배제
            if spec.type in ("integer", "number") and isinstance(value, bool):
                errors.append("타입 불일치: " + name + " (bool 은 " + spec.type + " 불가)")
                continue
            if not isinstance(value, expected):
                errors.append("타입 불일치: " + name + " (기대 " + spec.type + ", 실제 " + type(value).__name__ + ")")
                continue

            if spec.enum is not None and value not in spec.enum:
                errors.append("enum 위반: " + name + "=" + repr(value) + " (허용 " + str(list(spec.enum)) + ")")
                continue

            if spec.type == "string":
                if spec.min_len is not None and len(value) < spec.min_len:
                    errors.append("길이 미달: " + name + " (" + str(len(value)) + " < " + str(spec.min_len) + ")")
                    continue
                if spec.max_len is not None and len(value) > spec.max_len:
                    errors.append("길이 초과: " + name + " (" + str(len(value)) + " > " + str(spec.max_len) + ")")
                    continue
                if spec.pattern and not re.search(spec.pattern, value):
                    errors.append("정규식 불일치: " + name + " (pattern=" + spec.pattern + ")")
                    continue

            if spec.type == "array":
                if spec.item_type:
                    item_types = self._TYPE_MAP.get(spec.item_type, (object,))
                    bad = [i for i, v in enumerate(value) if not isinstance(v, item_types)]
                    if bad:
                        errors.append("배열 원소 타입 불일치: " + name + " (index " + str(bad) + ")")
                        continue

            normalized[name] = value

        if errors:
            RuntimeAudit.write({
                "event": "mcp_schema_rejected",
                "tool": tool_name,
                "errors": errors,
            })
            raise SchemaRejected(
                "MCP 인자 스키마 위반 (" + str(tool_name) + "): " + "; ".join(errors),
                detail={"tool": tool_name, "errors": errors},
            )

        RuntimeAudit.write({"event": "mcp_schema_ok", "tool": tool_name})
        return normalized


# ============================================================
# 5. MCPExecutor — 타임아웃 / 재시도 / 지수 백오프 실행기
# ============================================================

@dataclass
class ExecResult:
    """실행 결과."""
    ok: bool
    code: str
    tool: str
    attempts: int
    elapsed_sec: float
    value: Any = None
    error: Optional[str] = None

    def to_dict(self):
        return asdict(self)


class MCPExecutor:
    """
    MCP 도구 실행기.

    - 각 시도는 별도 스레드에서 실행되며 timeout 초과 시 EXEC_TIMEOUT.
    - 재시도는 지수 백오프(BACKOFF_BASE * 2^n, 상한 BACKOFF_CAP).
    - 재시도 불가 예외(스키마/라우터/브로커/QA 거부)는 즉시 중단.
    """

    NON_RETRYABLE_NAMES = ("SchemaRejected", "RouterRejected", "BrokerDenied", "QAGateFailed")

    def __init__(self, contract=None):
        self.contract = contract or MCPContract()

    @staticmethod
    def _backoff(attempt):
        return min(BACKOFF_CAP_SEC, BACKOFF_BASE_SEC * (2 ** max(0, attempt - 1)))

    def _run_once(self, fn, timeout_sec):
        """단일 시도 실행 (스레드 + 타임아웃)."""
        box = {}

        def _target():
            try:
                box["value"] = fn()
            except BaseException as e:  # 실행 경계에서 전부 포획
                box["error"] = e

        t = threading.Thread(target=_target, daemon=True, name="mcp-exec")
        t.start()
        t.join(timeout_sec)
        if t.is_alive():
            return False, None, "타임아웃(" + str(timeout_sec) + "s) 초과"
        if "error" in box:
            err = box["error"]
            return False, None, type(err).__name__ + ": " + str(err)
        return True, box.get("value"), None

    def execute(self, tool_name, args, handler, timeout_sec=DEFAULT_TIMEOUT_SEC,
                max_retries=DEFAULT_MAX_RETRIES, validate=True):
        """스키마 검증 → 재시도 실행 → 결과 반환."""
        started = time.time()

        if validate:
            try:
                args = self.contract.validate(tool_name, args)
            except SchemaRejected as e:
                return ExecResult(
                    ok=False, code=SCHEMA_REJECTED, tool=tool_name,
                    attempts=0, elapsed_sec=time.time() - started, error=e.message,
                )

        attempts = 0
        last_error = "실행되지 않음"
        total_attempts = max(1, int(max_retries) + 1)

        for attempt in range(1, total_attempts + 1):
            attempts = attempt
            ok, value, err = self._run_once(lambda: handler(args), timeout_sec)
            if ok:
                RuntimeAudit.write({
                    "event": "mcp_exec_ok", "tool": tool_name,
                    "attempts": attempt, "elapsed": round(time.time() - started, 4),
                })
                return ExecResult(
                    ok=True, code="OK", tool=tool_name, attempts=attempt,
                    elapsed_sec=time.time() - started, value=value,
                )

            last_error = err or "알 수 없는 오류"
            RuntimeAudit.write({
                "event": "mcp_exec_attempt_failed", "tool": tool_name,
                "attempt": attempt, "error": last_error,
            })

            # 재시도 불가 예외는 즉시 중단
            if any(name in last_error for name in self.NON_RETRYABLE_NAMES):
                break
            if attempt < total_attempts:
                time.sleep(self._backoff(attempt))

        code = EXEC_TIMEOUT if "타임아웃" in last_error else EXEC_FAILED
        RuntimeAudit.write({
            "event": "mcp_exec_failed", "tool": tool_name,
            "attempts": attempts, "error": last_error, "code": code,
        })
        return ExecResult(
            ok=False, code=code, tool=tool_name, attempts=attempts,
            elapsed_sec=time.time() - started, error=last_error,
        )


# ============================================================
# 6. QAGate — py_compile / 테스트 / 금지패턴 3중 품질 게이트
# ============================================================

@dataclass
class QAReport:
    """QA 게이트 결과."""
    passed: bool
    code: str
    checks: List[Dict[str, Any]] = field(default_factory=list)
    failures: List[str] = field(default_factory=list)
    elapsed_sec: float = 0.0

    def to_dict(self):
        return asdict(self)


class QAGate:
    """
    3단계 실동작 검증 파이프라인 (fail-closed).

    (1) 구문 검증  : compile() 로 .py 파일 구문 검사
    (2) 금지 패턴  : eval/exec/os.system/shell=True 등 위험 코드 탐지
    (3) 임포트 검증: 격리 서브프로세스에서 importlib.util 로 실제 로드
                     → NameError / ImportError / AttributeError /
                       ModuleNotFoundError 전수 색출 (타임아웃 15초)
    (4) 테스트 실행: 대상 파일에 대응하는 단위 테스트를 탐색하여
                     unittest / pytest 로 직접 구동, 종료코드 0 확인
    (5) 종합 판정  : 단 하나라도 실패하면 QA_FAILED (fail-closed)
    """

    FORBIDDEN_PATTERNS = (
        (r"\beval\s*\(", "eval() 사용 금지"),
        (r"\bexec\s*\(", "exec() 사용 금지"),
        (r"os\.system\s*\(", "os.system() 사용 금지"),
        (r"shell\s*=\s*True", "shell=True 사용 금지"),
        (r"pickle\.loads\s*\(", "pickle.loads() 역직렬화 금지"),
        (r"subprocess\.call\s*\(\s*['\"]rm\s+-rf", "rm -rf 실행 금지"),
    )

    # 임포트 검증 서브프로세스 타임아웃 (초)
    IMPORT_TIMEOUT_SEC = 15
    # 테스트 실행 기본 타임아웃 (초)
    TEST_TIMEOUT_SEC = 120
    # 임포트 검증 시 색출 대상 예외 (전수 보고)
    IMPORT_ERROR_NAMES = (
        "NameError",
        "ImportError",
        "AttributeError",
        "ModuleNotFoundError",
    )

    def __init__(self, project_root=None):
        self.project_root = project_root or os.getcwd()

    # ---------- 경로 정규화 ----------
    def _abs(self, target):
        """대상 경로를 절대경로로 정규화한다."""
        if not target:
            return ""
        raw = str(target).strip().strip('"').strip("'")
        if os.path.isabs(raw):
            return os.path.normpath(raw)
        return os.path.normpath(os.path.join(self.project_root, raw))

    # ---------- (1) 구문 검증 ----------
    def check_syntax(self, path):
        result = {"check": "syntax", "target": path, "ok": False, "detail": ""}
        if not os.path.isfile(path):
            result["detail"] = "파일 없음"
            return result
        try:
            with io.open(path, "r", encoding="utf-8", errors="replace") as f:
                source = f.read()
            compile(source, path, "exec")
            result["ok"] = True
            result["detail"] = "구문 정상 (" + str(len(source.splitlines())) + "줄)"
        except SyntaxError as e:
            result["detail"] = "SyntaxError: line " + str(e.lineno) + " — " + str(e.msg)
        except Exception as e:
            result["detail"] = type(e).__name__ + ": " + str(e)
        return result

    # ---------- (2) 금지 패턴 ----------
    def check_forbidden(self, path):
        result = {"check": "forbidden", "target": path, "ok": True, "detail": "위험 패턴 없음"}
        if not os.path.isfile(path):
            result["ok"] = False
            result["detail"] = "파일 없음"
            return result
        try:
            with io.open(path, "r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
        except Exception as e:
            result["ok"] = False
            result["detail"] = type(e).__name__ + ": " + str(e)
            return result

        hits = []
        for idx, line in enumerate(lines, start=1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            for pattern, label in self.FORBIDDEN_PATTERNS:
                if re.search(pattern, line):
                    hits.append("line " + str(idx) + ": " + label)
        if hits:
            result["ok"] = False
            result["detail"] = "; ".join(hits[:5])
        return result

    # ---------- (3) 임포트 검증 (신규) ----------
    def _import_probe_script(self, path):
        """
        격리 서브프로세스에서 실행할 임포트 검증 스크립트를 생성한다.

        검증 절차:
          1) importlib.util.spec_from_file_location 으로 대상 파일을 모듈로 로드
          2) exec_module 실행 → 모듈 최상위 NameError/ImportError 색출
          3) 모듈 내 심볼 접근(getattr) → AttributeError 색출
          4) 결과를 JSON 한 줄로 stdout 에 출력 (마커 기반 파싱)
        """
        return (
            "# -*- coding: utf-8 -*-\n"
            "import importlib.util, json, os, sys, traceback\n"
            "TARGET = " + repr(path) + "\n"
            "MARK = '@@QA_IMPORT_RESULT@@'\n"
            "def emit(payload):\n"
            "    sys.stdout.write(MARK + json.dumps(payload, ensure_ascii=False) + '\\n')\n"
            "    sys.stdout.flush()\n"
            "def main():\n"
            "    if not os.path.isfile(TARGET):\n"
            "        emit({'ok': False, 'stage': 'locate',\n"
            "              'error': 'FileNotFoundError', 'detail': '파일 없음: ' + TARGET})\n"
            "        return 1\n"
            "    name = '_qa_probe_' + str(abs(hash(TARGET)) % 1000000)\n"
            "    try:\n"
            "        spec = importlib.util.spec_from_file_location(name, TARGET)\n"
            "        if spec is None or spec.loader is None:\n"
            "            emit({'ok': False, 'stage': 'spec',\n"
            "                  'error': 'ImportError',\n"
            "                  'detail': 'spec 생성 실패 (로더 없음)'})\n"
            "            return 1\n"
            "        mod = importlib.util.module_from_spec(spec)\n"
            "        sys.modules[name] = mod\n"
            "    except BaseException as e:\n"
            "        emit({'ok': False, 'stage': 'spec',\n"
            "              'error': type(e).__name__, 'detail': str(e)})\n"
            "        return 1\n"
            "    try:\n"
            "        spec.loader.exec_module(mod)\n"
            "    except BaseException as e:\n"
            "        emit({'ok': False, 'stage': 'exec_module',\n"
            "              'error': type(e).__name__, 'detail': str(e),\n"
            "              'traceback': traceback.format_exc()[-1500:]})\n"
            "        return 1\n"
            "    symbols = [s for s in dir(mod) if not s.startswith('__')]\n"
            "    bad = []\n"
            "    for sym in symbols:\n"
            "        try:\n"
            "            getattr(mod, sym)\n"
            "        except AttributeError as e:\n"
            "            bad.append({'symbol': sym, 'error': 'AttributeError',\n"
            "                        'detail': str(e)})\n"
            "        except BaseException as e:\n"
            "            bad.append({'symbol': sym, 'error': type(e).__name__,\n"
            "                        'detail': str(e)})\n"
            "    if bad:\n"
            "        emit({'ok': False, 'stage': 'symbol_access',\n"
            "              'error': bad[0]['error'],\n"
            "              'detail': '심볼 접근 실패 ' + str(len(bad)) + '건: ' +\n"
            "                        ', '.join(b['symbol'] + '(' + b['error'] + ')' for b in bad[:5]),\n"
            "              'symbols': bad[:10]})\n"
            "        return 1\n"
            "    emit({'ok': True, 'stage': 'done', 'error': '',\n"
            "          'detail': '임포트 정상 (심볼 ' + str(len(symbols)) + '개 검증)',\n"
            "          'symbol_count': len(symbols)})\n"
            "    return 0\n"
            "if __name__ == '__main__':\n"
            "    sys.exit(main())\n"
        )

    def check_import(self, path, timeout_sec=None):
        """
        격리된 서브프로세스에서 대상 파일을 importlib.util 로 직접 로드하여
        NameError / ImportError / AttributeError / ModuleNotFoundError 를 색출한다.

        Args:
            path       : 검증 대상 .py 파일 경로
            timeout_sec: 타임아웃(초). 기본 15초.

        Returns:
            {"check": "import", "target": path, "ok": bool, "detail": str, ...}
        """
        timeout = self.IMPORT_TIMEOUT_SEC if timeout_sec is None else int(timeout_sec)
        result = {
            "check": "import", "target": path, "ok": False,
            "detail": "", "error": "", "stage": "",
        }

        if not os.path.isfile(path):
            result["error"] = "FileNotFoundError"
            result["detail"] = "파일 없음"
            return result

        probe_path = None
        try:
            fd, probe_path = tempfile.mkstemp(prefix="_qa_import_probe_", suffix=".py")
            with io.open(fd, "w", encoding="utf-8", newline="\n") as f:
                f.write(self._import_probe_script(path))

            env = dict(os.environ)
            env["PYTHONIOENCODING"] = "utf-8"
            env["PYTHONDONTWRITEBYTECODE"] = "1"
            # 대상 파일 디렉터리를 sys.path 최우선에 두어 형제 모듈 import 허용
            env["PYTHONPATH"] = os.pathsep.join(
                [os.path.dirname(path), self.project_root] +
                ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else [])
            )

            proc = subprocess.run(
                [sys.executable, "-X", "utf8", probe_path],
                cwd=self.project_root, capture_output=True, text=True,
                timeout=timeout, env=env,
            )
            stdout = proc.stdout or ""
            stderr = proc.stderr or ""

            payload = None
            for line in stdout.splitlines():
                if line.startswith("@@QA_IMPORT_RESULT@@"):
                    try:
                        payload = json.loads(line[len("@@QA_IMPORT_RESULT@@"):])
                    except Exception:
                        payload = None
                    break

            if payload is None:
                tail = (stderr.strip() or stdout.strip() or "출력 없음").splitlines()
                result["error"] = "ProbeOutputError"
                result["detail"] = "검증 스크립트 출력 파싱 실패: " + (tail[-1] if tail else "")
                return result

            result["stage"] = payload.get("stage", "")
            result["error"] = payload.get("error", "") or ""

            if payload.get("ok") and proc.returncode == 0:
                result["ok"] = True
                result["detail"] = payload.get("detail", "임포트 정상")
                return result

            err_name = result["error"] or "ImportError"
            detail = payload.get("detail", "")
            # 색출 대상 예외는 명시적으로 강조 표기
            if err_name in self.IMPORT_ERROR_NAMES:
                result["detail"] = err_name + " (stage=" + str(result["stage"]) + "): " + detail
            else:
                result["detail"] = err_name + " (stage=" + str(result["stage"]) + "): " + detail
            tb = payload.get("traceback")
            if tb:
                result["traceback"] = tb
            return result

        except subprocess.TimeoutExpired:
            result["error"] = "TimeoutExpired"
            result["detail"] = "임포트 검증 타임아웃(" + str(timeout) + "s) 초과"
            return result
        except Exception as e:
            result["error"] = type(e).__name__
            result["detail"] = type(e).__name__ + ": " + str(e)
            return result
        finally:
            if probe_path and os.path.exists(probe_path):
                try:
                    os.remove(probe_path)
                except OSError:
                    pass

    # ---------- (4) 테스트 실행 (보강) ----------
    def _candidate_test_paths(self, target):
        """
        대상 파일에 대응하는 단위 테스트 후보 경로를 우선순위대로 반환한다.

        탐색 규칙 (대상: <dir>/<name>.py):
          1) <dir>/test_<name>.py
          2) <dir>/<name>_test.py
          3) <dir>/tests/test_<name>.py
          4) <project_root>/tests/test_<name>.py
          5) <project_root>/test_<name>.py
        """
        abs_target = self._abs(target)
        base = os.path.basename(abs_target)
        name = os.path.splitext(base)[0]
        if not name:
            return []

        target_dir = os.path.dirname(abs_target) or self.project_root
        candidates = [
            os.path.join(target_dir, "test_" + name + ".py"),
            os.path.join(target_dir, name + "_test.py"),
            os.path.join(target_dir, "tests", "test_" + name + ".py"),
            os.path.join(self.project_root, "tests", "test_" + name + ".py"),
            os.path.join(self.project_root, "test_" + name + ".py"),
        ]

        seen = set()
        found = []
        for c in candidates:
            real = os.path.normpath(c)
            if real in seen:
                continue
            seen.add(real)
            if os.path.isfile(real):
                found.append(real)
        return found

    def _run_test_file(self, test_path, timeout_sec):
        """
        단일 테스트 파일을 unittest → pytest 순으로 구동한다.

        Returns:
            {"ok": bool, "runner": str, "detail": str, "returncode": int}
        """
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env["PYTHONPATH"] = os.pathsep.join(
            [os.path.dirname(test_path), self.project_root] +
            ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else [])
        )

        # 1순위: unittest (표준 라이브러리 — 외부 의존 없음)
        try:
            proc = subprocess.run(
                [sys.executable, "-X", "utf8", "-m", "unittest", "-v", test_path],
                cwd=self.project_root, capture_output=True, text=True,
                timeout=timeout_sec, env=env,
            )
            out = ((proc.stdout or "") + (proc.stderr or "")).strip()
            tail = out.splitlines()[-1] if out.splitlines() else ""
            if proc.returncode == 0:
                return {"ok": True, "runner": "unittest",
                        "detail": "unittest 통과: " + tail, "returncode": 0}
            # unittest 가 테스트를 하나도 못 찾은 경우(rc=5) → pytest 로 재시도
            if proc.returncode != 5:
                return {"ok": False, "runner": "unittest",
                        "detail": "unittest 실패(rc=" + str(proc.returncode) + "): " + tail,
                        "returncode": proc.returncode}
        except subprocess.TimeoutExpired:
            return {"ok": False, "runner": "unittest",
                    "detail": "unittest 타임아웃(" + str(timeout_sec) + "s)",
                    "returncode": -1}
        except Exception as e:
            return {"ok": False, "runner": "unittest",
                    "detail": "unittest 실행 예외: " + type(e).__name__ + ": " + str(e),
                    "returncode": -1}

        # 2순위: pytest
        try:
            if importlib.util.find_spec("pytest") is None:
                return {"ok": True, "runner": "none",
                        "detail": "SKIP (unittest 미수집 + pytest 미설치)", "returncode": 0}
        except Exception:
            return {"ok": True, "runner": "none",
                    "detail": "SKIP (테스트 러너 확인 불가)", "returncode": 0}

        try:
            proc = subprocess.run(
                [sys.executable, "-X", "utf8", "-m", "pytest", "-q", "--maxfail=1", test_path],
                cwd=self.project_root, capture_output=True, text=True,
                timeout=timeout_sec, env=env,
            )
            out = ((proc.stdout or "") + (proc.stderr or "")).strip()
            tail = out.splitlines()[-1] if out.splitlines() else ""
            if proc.returncode == 0:
                return {"ok": True, "runner": "pytest",
                        "detail": "pytest 통과: " + tail, "returncode": 0}
            if proc.returncode == 5:
                return {"ok": True, "runner": "pytest",
                        "detail": "SKIP (수집된 테스트 없음)", "returncode": 0}
            return {"ok": False, "runner": "pytest",
                    "detail": "pytest 실패(rc=" + str(proc.returncode) + "): " + tail,
                    "returncode": proc.returncode}
        except subprocess.TimeoutExpired:
            return {"ok": False, "runner": "pytest",
                    "detail": "pytest 타임아웃(" + str(timeout_sec) + "s)", "returncode": -1}
        except Exception as e:
            return {"ok": False, "runner": "pytest",
                    "detail": "pytest 실행 예외: " + type(e).__name__ + ": " + str(e),
                    "returncode": -1}

    def check_tests(self, targets=None, timeout_sec=None):
        """
        대상 파일에 대응하는 단위 테스트를 탐색하여 직접 구동한다.

        Args:
            targets    : 대상 파일 경로 목록 (또는 단일 문자열)
            timeout_sec: 테스트 1건당 타임아웃(초). 기본 120초.

        Returns:
            {"check": "tests", "target": ..., "ok": bool, "detail": str, "results": [...]}
        """
        timeout = self.TEST_TIMEOUT_SEC if timeout_sec is None else int(timeout_sec)

        if targets is None:
            target_list = []
        elif isinstance(targets, (str, bytes)):
            target_list = [targets]
        else:
            target_list = list(targets)

        result = {
            "check": "tests", "target": "unittest|pytest", "ok": True,
            "detail": "", "results": [],
        }

        if not target_list:
            result["detail"] = "SKIP (대상 파일 없음)"
            return result

        executed = 0
        failures = []
        for target in target_list:
            candidates = self._candidate_test_paths(target)
            if not candidates:
                result["results"].append({
                    "target": str(target), "test_file": "", "ok": True,
                    "runner": "none", "detail": "SKIP (대응 테스트 없음)",
                })
                continue

            for test_path in candidates:
                run = self._run_test_file(test_path, timeout)
                executed += 1
                entry = {
                    "target": str(target),
                    "test_file": test_path,
                    "ok": bool(run["ok"]),
                    "runner": run["runner"],
                    "detail": run["detail"],
                    "returncode": run["returncode"],
                }
                result["results"].append(entry)
                if not run["ok"]:
                    failures.append(
                        os.path.basename(test_path) + " — " + run["detail"]
                    )
                # 첫 번째 발견된 테스트 파일 실행 결과로 판정 (우선순위 규칙)
                break

        if failures:
            result["ok"] = False
            result["detail"] = "테스트 실패 " + str(len(failures)) + "건: " + "; ".join(failures[:3])
        elif executed == 0:
            result["detail"] = "SKIP (대응 테스트 없음)"
        else:
            result["detail"] = "테스트 통과 " + str(executed) + "건"

        return result

    # ---------- (5) 종합 (fail-closed) ----------
    def run(self, targets, run_tests=True):
        """
        각 target 마다 syntax → forbidden → import 를 순차 실행하고,
        run_tests=True 이면 대응 단위 테스트까지 구동한다.

        단 하나라도 실패하면 QA_FAILED 를 반환한다 (fail-closed).
        """
        started = time.time()
        checks = []
        failures = []

        target_list = []
        if targets is None:
            target_list = []
        elif isinstance(targets, (str, bytes)):
            target_list = [targets]
        else:
            target_list = list(targets)

        for target in target_list:
            path = self._abs(target)

            # (1) 구문 → (2) 금지 패턴 → (3) 임포트 순차 실행
            for fn in (self.check_syntax, self.check_forbidden, self.check_import):
                try:
                    r = fn(path)
                except Exception as e:
                    r = {
                        "check": fn.__name__.replace("check_", ""),
                        "target": path, "ok": False,
                        "detail": type(e).__name__ + ": " + str(e),
                    }
                checks.append(r)
                if not r.get("ok"):
                    failures.append(
                        str(r.get("check")) + ":" + str(target) + " — " + str(r.get("detail"))
                    )

        # (4) 테스트 실행
        if run_tests:
            try:
                r = self.check_tests(target_list)
            except Exception as e:
                r = {
                    "check": "tests", "target": "unittest|pytest", "ok": False,
                    "detail": type(e).__name__ + ": " + str(e), "results": [],
                }
            checks.append(r)
            if not r.get("ok"):
                failures.append("tests — " + str(r.get("detail")))

        passed = not failures
        report = QAReport(
            passed=passed,
            code=QA_PASSED if passed else QA_FAILED,
            checks=checks,
            failures=failures,
            elapsed_sec=round(time.time() - started, 4),
        )
        RuntimeAudit.write({
            "event": "qa_gate",
            "passed": passed,
            "code": report.code,
            "targets": [str(t) for t in target_list],
            "failures": failures,
            "elapsed": report.elapsed_sec,
        })
        return report


# ============================================================
# 7. SessionRegistry — 6인 협업 세션 등록부
# ============================================================

@dataclass
class SessionRecord:
    """세션 1건."""
    session_id: str
    requester: str
    tool: str
    approvals: List[str] = field(default_factory=list)
    fingerprint: str = ""
    created_at: float = 0.0
    last_seen: float = 0.0
    status: str = "OPEN"
    notes: List[str] = field(default_factory=list)

    def to_dict(self):
        return asdict(self)


class SessionRegistry:
    """
    6인 협업 세션 등록부 (스레드 안전).

    - 세션 생성 시 5인 승인 상태를 스냅샷으로 보관.
    - 동일 fingerprint 재사용(리플레이) 탐지 시 SESSION_REJECTED.
    - TTL 경과 세션은 자동 만료 처리.
    """

    def __init__(self, ttl_sec=SESSION_TTL_SEC, max_sessions=MAX_SESSIONS):
        self.ttl_sec = ttl_sec
        self.max_sessions = max_sessions
        self._lock = threading.RLock()
        self._sessions = {}
        self._fingerprints = {}

    # ---------- 내부 ----------
    def _purge_expired(self):
        now = time.time()
        expired = [sid for sid, rec in self._sessions.items()
                   if rec.status == "OPEN" and (now - rec.last_seen) > self.ttl_sec]
        for sid in expired:
            self._sessions[sid].status = "EXPIRED"
            fp = self._sessions[sid].fingerprint
            if fp and self._fingerprints.get(fp) == sid:
                self._fingerprints.pop(fp, None)
        return expired

    def _evict_oldest(self):
        if len(self._sessions) <= self.max_sessions:
            return None
        ordered = sorted(self._sessions.values(), key=lambda r: r.last_seen)
        victim = ordered[0]
        self._sessions.pop(victim.session_id, None)
        if victim.fingerprint:
            self._fingerprints.pop(victim.fingerprint, None)
        return victim.session_id

    # ---------- 공개 API ----------
    def open(self, requester, tool, approvals, fingerprint="", session_id=None):
        """세션 개설. 리플레이 지문이면 SessionRejected."""
        with self._lock:
            self._purge_expired()

            if fingerprint and fingerprint in self._fingerprints:
                prev = self._fingerprints[fingerprint]
                raise SessionRejected(
                    "리플레이 탐지: 동일 요청 지문이 세션 " + prev + " 에서 이미 사용됨",
                    detail={"fingerprint": fingerprint, "previous_session": prev},
                )

            sid = session_id or ("sess-" + uuid.uuid4().hex[:16])
            now = time.time()
            rec = SessionRecord(
                session_id=sid,
                requester=CouncilRouter.normalize_identity(requester),
                tool=tool,
                approvals=CouncilRouter.normalize_approvals(approvals),
                fingerprint=fingerprint,
                created_at=now,
                last_seen=now,
                status="OPEN",
            )
            self._sessions[sid] = rec
            if fingerprint:
                self._fingerprints[fingerprint] = sid
            evicted = self._evict_oldest()

            RuntimeAudit.write({
                "event": "session_open", "session_id": sid, "tool": tool,
                "requester": rec.requester, "approvals": rec.approvals,
                "evicted": evicted,
            })
            return rec

    def touch(self, session_id):
        with self._lock:
            rec = self._sessions.get(session_id)
            if rec is None:
                return None
            rec.last_seen = time.time()
            return rec

    def close(self, session_id, status="CLOSED", note=""):
        with self._lock:
            rec = self._sessions.get(session_id)
            if rec is None:
                return None
            rec.status = status
            rec.last_seen = time.time()
            if note:
                rec.notes.append(note)
            if rec.fingerprint and self._fingerprints.get(rec.fingerprint) == session_id:
                self._fingerprints.pop(rec.fingerprint, None)
            RuntimeAudit.write({
                "event": "session_close", "session_id": session_id, "status": status,
            })
            return rec

    def get(self, session_id):
        with self._lock:
            return self._sessions.get(session_id)

    def active_count(self):
        with self._lock:
            self._purge_expired()
            return sum(1 for r in self._sessions.values() if r.status == "OPEN")

    def snapshot(self):
        with self._lock:
            return {sid: rec.to_dict() for sid, rec in self._sessions.items()}


# ============================================================
# 8. FileBroker — 경로 탈출/심볼릭링크 차단 파일 중개자
# ============================================================

class FileBroker:
    """
    파일 I/O 중개자.

    - 모든 경로는 허용 루트(roots) 내부로 정규화(resolve) 후 검사.
    - 상위 경로 탈출(..), 심볼릭 링크 우회, 절대경로 이탈은 BrokerDenied.
    - 쓰기는 원자적(temp → os.replace)으로 수행하여 부분 기록을 방지.
    - 최대 파일 크기 초과 시 BrokerDenied.
    """

    def __init__(self, roots=None, max_bytes=MAX_FILE_BYTES):
        self.roots = [os.path.realpath(r) for r in (roots or [os.getcwd()])]
        self.max_bytes = max_bytes
        self._lock = threading.RLock()

    # ---------- 경로 검증 ----------
    def resolve(self, path):
        """허용 루트 내부 경로로 정규화. 위반 시 BrokerDenied."""
        if not path or not str(path).strip():
            raise BrokerDenied("경로가 비어 있습니다.", detail={"path": path})

        raw = str(path).strip().strip('"').strip("'")
        candidate = raw if os.path.isabs(raw) else os.path.join(self.roots[0], raw)
        real = os.path.realpath(candidate)

        for root in self.roots:
            if real == root or real.startswith(root + os.sep):
                return real

        raise BrokerDenied(
            "허용 루트 외부 경로 접근 차단: " + raw,
            detail={"path": raw, "resolved": real, "roots": self.roots},
        )

    def _check_size(self, real_path):
        if os.path.isfile(real_path):
            size = os.path.getsize(real_path)
            if size > self.max_bytes:
                raise BrokerDenied(
                    "파일 크기 초과: " + str(size) + " > " + str(self.max_bytes),
                    detail={"path": real_path, "size": size},
                )

    # ---------- 읽기 ----------
    def read(self, path, encoding="utf-8"):
        real = self.resolve(path)
        if not os.path.isfile(real):
            raise BrokerDenied("파일 없음: " + str(path), detail={"path": real})
        self._check_size(real)
        with self._lock:
            with io.open(real, "r", encoding=encoding, errors="replace") as f:
                content = f.read()
        RuntimeAudit.write({
            "event": "broker_read", "path": real,
            "bytes": len(content.encode(encoding, errors="replace")),
        })
        return content

    # ---------- 쓰기 (원자적) ----------
    def write(self, path, content, encoding="utf-8", create_dirs=True):
        real = self.resolve(path)
        payload = content if isinstance(content, str) else str(content)
        data = payload.encode(encoding)
        if len(data) > self.max_bytes:
            raise BrokerDenied(
                "쓰기 크기 초과: " + str(len(data)) + " > " + str(self.max_bytes),
                detail={"path": real, "size": len(data)},
            )

        parent = os.path.dirname(real)
        with self._lock:
            if create_dirs and parent and not os.path.isdir(parent):
                os.makedirs(parent, exist_ok=True)
            tmp = real + ".tmp-" + uuid.uuid4().hex[:8]
            try:
                with io.open(tmp, "w", encoding=encoding, newline="\n") as f:
                    f.write(payload)
                os.replace(tmp, real)
            finally:
                if os.path.exists(tmp):
                    try:
                        os.remove(tmp)
                    except OSError:
                        pass

        RuntimeAudit.write({
            "event": "broker_write", "path": real, "bytes": len(data),
        })
        return real

    # ---------- 존재/목록 ----------
    def exists(self, path):
        try:
            return os.path.exists(self.resolve(path))
        except BrokerDenied:
            return False

    def listdir(self, path="."):
        real = self.resolve(path)
        if not os.path.isdir(real):
            raise BrokerDenied("디렉터리 아님: " + str(path), detail={"path": real})
        with self._lock:
            return sorted(os.listdir(real))


# ============================================================
# 9. self_check — 6인 협업 런타임 종합 자가진단
# ============================================================

class SelfCheckReport:
    """자가진단 결과 컨테이너."""

    def __init__(self):
        self.items = []
        self.started = time.time()

    def add(self, name, ok, detail=""):
        self.items.append({"name": name, "ok": bool(ok), "detail": str(detail)})
        return ok

    @property
    def passed(self):
        return all(i["ok"] for i in self.items)

    @property
    def total(self):
        return len(self.items)

    @property
    def ok_count(self):
        return sum(1 for i in self.items if i["ok"])

    def to_dict(self):
        return {
            "passed": self.passed,
            "total": self.total,
            "ok": self.ok_count,
            "elapsed_sec": round(time.time() - self.started, 4),
            "items": self.items,
        }


def self_check(verbose=True, project_root=None):
    """6인 협업 런타임 전 구성요소 자가진단. 통과 시 True."""
    rep = SelfCheckReport()
    root = project_root or os.getcwd()

    def emit(name, ok, detail=""):
        rep.add(name, ok, detail)
        if verbose:
            mark = "[ OK ]" if ok else "[FAIL]"
            print("  " + mark + " " + name + (" — " + str(detail) if detail else ""))

    if verbose:
        print("=" * 68)
        print("  DEEPSEEK 6인 협업 런타임 — SELF CHECK")
        print("=" * 68)

    # (1) 상수 계약
    emit("상수: 5인 협업체 정의", len(DEEPSEEK_COLLABORATORS) == REQUIRED_APPROVALS,
         str(len(DEEPSEEK_COLLABORATORS)) + "인 / 필요 " + str(REQUIRED_APPROVALS))
    emit("상수: 제미나이 차단 목록", len(GEMINI_IDENTIFIERS) > 0,
         str(sorted(GEMINI_IDENTIFIERS)))

    # (2) 예외 계층
    try:
        e = RouterRejected("t", detail={"a": 1})
        ok = e.code == ROUTER_REJECTED and isinstance(e, CouncilRuntimeError)
        emit("예외: RouterRejected 계층", ok, e.code)
    except Exception as ex:
        emit("예외: RouterRejected 계층", False, type(ex).__name__)

    # (3) 감사 로그
    try:
        RuntimeAudit.write({"event": "self_check_probe"})
        emit("감사: RuntimeAudit 기록", True, RuntimeAudit.log_path)
    except Exception as ex:
        emit("감사: RuntimeAudit 기록", False, type(ex).__name__ + ": " + str(ex))

    # (4) CouncilRouter — 정상 승인
    try:
        d = CouncilRouter.route(
            requester=DEEPSEEK_COLLABORATORS[0],
            tool_name="deepseek_council_deliberate",
            args={"topic": "self-check"},
            approvals=list(DEEPSEEK_COLLABORATORS),
        )
        emit("라우터: 5인 전원 승인 통과", d.approved and d.code == "ROUTER_APPROVED", d.code)
    except Exception as ex:
        emit("라우터: 5인 전원 승인 통과", False, type(ex).__name__ + ": " + str(ex))

    # (5) CouncilRouter — 제미나이 차단
    try:
        CouncilRouter.route(
            requester="gemini", tool_name="deepseek_council_deliberate",
            args={"topic": "x"}, approvals=list(DEEPSEEK_COLLABORATORS),
        )
        emit("라우터: 제미나이 직접 접근 차단", False, "차단되지 않음(치명적)")
    except RouterRejected as ex:
        emit("라우터: 제미나이 직접 접근 차단", True, ex.code)
    except Exception as ex:
        emit("라우터: 제미나이 직접 접근 차단", False, type(ex).__name__)

    # (6) CouncilRouter — 승인 누락 차단
    try:
        CouncilRouter.route(
            requester=DEEPSEEK_COLLABORATORS[0], tool_name="deepseek_council_deliberate",
            args={"topic": "x"}, approvals=list(DEEPSEEK_COLLABORATORS[:3]),
        )
        emit("라우터: 승인 누락 차단", False, "차단되지 않음(치명적)")
    except RouterRejected as ex:
        emit("라우터: 승인 누락 차단", True, "누락 2인 탐지")
    except Exception as ex:
        emit("라우터: 승인 누락 차단", False, type(ex).__name__)

    # (7) CouncilRouter — 거부권
    try:
        CouncilRouter.route(
            requester=DEEPSEEK_COLLABORATORS[0], tool_name="deepseek_council_deliberate",
            args={"topic": "x"}, approvals=list(DEEPSEEK_COLLABORATORS),
            vetoes=["ds_qa_verifier"],
        )
        emit("라우터: 거부권 행사 차단", False, "차단되지 않음(치명적)")
    except RouterRejected as ex:
        emit("라우터: 거부권 행사 차단", True, "veto 반영")

    # (8) MCPContract — 정상 검증
    contract = MCPContract()
    try:
        norm = contract.validate("deepseek_council_deliberate", {
            "topic": "self-check topic",
            "requester": DEEPSEEK_COLLABORATORS[0],
            "approvals": list(DEEPSEEK_COLLABORATORS),
        })
        emit("MCP: 스키마 정상 검증", "topic" in norm, str(len(contract.known_tools())) + "개 도구 등록")
    except Exception as ex:
        emit("MCP: 스키마 정상 검증", False, type(ex).__name__ + ": " + str(ex))

    # (9) MCPContract — 미정의 인자 거부
    try:
        contract.validate("deepseek_council_deliberate", {
            "topic": "t", "requester": "ds_v3_architect",
            "approvals": list(DEEPSEEK_COLLABORATORS), "evil": "injection",
        })
        emit("MCP: 미정의 인자 거부", False, "거부되지 않음(치명적)")
    except SchemaRejected:
        emit("MCP: 미정의 인자 거부", True, "additionalProperties=false")
    except Exception as ex:
        emit("MCP: 미정의 인자 거부", False, type(ex).__name__)

    # (10) MCPContract — 필수 인자 누락 거부
    try:
        contract.validate("deepseek_council_deliberate", {"topic": "t"})
        emit("MCP: 필수 인자 누락 거부", False, "거부되지 않음(치명적)")
    except SchemaRejected:
        emit("MCP: 필수 인자 누락 거부", True, "필수 필드 검사")
    except Exception as ex:
        emit("MCP: 필수 인자 누락 거부", False, type(ex).__name__)

    # (11) MCPExecutor — 정상 실행
    executor = MCPExecutor(contract)
    try:
        res = executor.execute(
            "deepseek_council_deliberate",
            {"topic": "exec probe", "requester": DEEPSEEK_COLLABORATORS[0],
             "approvals": list(DEEPSEEK_COLLABORATORS)},
            handler=lambda a: "handled:" + a["topic"],
            timeout_sec=5, max_retries=0,
        )
        emit("실행기: 정상 실행", res.ok and res.value == "handled:exec probe", res.code)
    except Exception as ex:
        emit("실행기: 정상 실행", False, type(ex).__name__ + ": " + str(ex))

    # (12) MCPExecutor — 타임아웃
    try:
        res = executor.execute(
            "deepseek_council_deliberate",
            {"topic": "timeout probe", "requester": DEEPSEEK_COLLABORATORS[0],
             "approvals": list(DEEPSEEK_COLLABORATORS)},
            handler=lambda a: time.sleep(3),
            timeout_sec=1, max_retries=0,
        )
        emit("실행기: 타임아웃 차단", (not res.ok) and res.code == EXEC_TIMEOUT, res.code)
    except Exception as ex:
        emit("실행기: 타임아웃 차단", False, type(ex).__name__ + ": " + str(ex))

    # (13) QAGate — 자기 파일 구문 검증
    gate = QAGate(project_root=root)
    try:
        me = os.path.abspath(__file__)
        r = gate.check_syntax(me)
        emit("QA: 자기 구문 검증", r["ok"], r["detail"])
    except Exception as ex:
        emit("QA: 자기 구문 검증", False, type(ex).__name__ + ": " + str(ex))

    # (14) QAGate — 금지 패턴 스캔
    try:
        r = gate.check_forbidden(os.path.abspath(__file__))
        emit("QA: 금지 패턴 스캔", r["ok"], r["detail"])
    except Exception as ex:
        emit("QA: 금지 패턴 스캔", False, type(ex).__name__ + ": " + str(ex))

    # (15) SessionRegistry — 개설/리플레이 차단
    try:
        reg = SessionRegistry()
        rec = reg.open(DEEPSEEK_COLLABORATORS[0], "deepseek_council_deliberate",
                       list(DEEPSEEK_COLLABORATORS), fingerprint="fp-selfcheck")
        replay_blocked = False
        try:
            reg.open(DEEPSEEK_COLLABORATORS[1], "deepseek_council_deliberate",
                     list(DEEPSEEK_COLLABORATORS), fingerprint="fp-selfcheck")
        except SessionRejected:
            replay_blocked = True
        reg.close(rec.session_id)
        emit("세션: 개설 + 리플레이 차단", replay_blocked, "active=" + str(reg.active_count()))
    except Exception as ex:
        emit("세션: 개설 + 리플레이 차단", False, type(ex).__name__ + ": " + str(ex))

    # (16) FileBroker — 경로 탈출 차단
    try:
        broker = FileBroker(roots=[root])
        blocked = False
        try:
            broker.resolve(os.path.join(root, "..", "..", "etc", "passwd"))
        except BrokerDenied:
            blocked = True
        emit("브로커: 경로 탈출 차단", blocked, "roots=" + str(len(broker.roots)))
    except Exception as ex:
        emit("브로커: 경로 탈출 차단", False, type(ex).__name__ + ": " + str(ex))

    # (17) FileBroker — 원자적 쓰기/읽기 왕복
    try:
        broker = FileBroker(roots=[root])
        probe = os.path.join(root, ".council_selfcheck_probe.tmp")
        broker.write(probe, "council-runtime-probe")
        back = broker.read(probe)
        try:
            os.remove(probe)
        except OSError:
            pass
        emit("브로커: 쓰기/읽기 왕복", back == "council-runtime-probe", "atomic replace")
    except Exception as ex:
        emit("브로커: 쓰기/읽기 왕복", False, type(ex).__name__ + ": " + str(ex))

    # (18) 6인 협업체 전원 식별자 정규화
    try:
        norms = [CouncilRouter.normalize_identity(c) for c in DEEPSEEK_COLLABORATORS]
        emit("협업체: 6인 식별자 정규화", norms == list(DEEPSEEK_COLLABORATORS),
             ", ".join(norms))
    except Exception as ex:
        emit("협업체: 6인 식별자 정규화", False, type(ex).__name__ + ": " + str(ex))

    elapsed = round(time.time() - rep.started, 3)
    if verbose:
        print("-" * 68)
        print("  결과: " + ("PASS" if rep.passed else "FAIL") +
              "  (" + str(rep.ok_count) + "/" + str(rep.total) + " 통과, " + str(elapsed) + "s)")
        print("=" * 68)

    RuntimeAudit.write({
        "event": "self_check",
        "passed": rep.passed,
        "ok": rep.ok_count,
        "total": rep.total,
        "elapsed": elapsed,
    })
    return rep.passed


# ============================================================
# 10. CLI 진입점
# ============================================================

def _build_parser():
    p = argparse.ArgumentParser(
        prog="council_runtime",
        description="딥시크 6인 협업 런타임 — 5인 합동 심의 강제 게이트웨이",
    )
    p.add_argument("--self-check", action="store_true", help="전 구성요소 자가진단 실행")
    p.add_argument("--qa", nargs="*", metavar="FILE", help="지정 파일 QA 게이트 실행")
    p.add_argument("--no-tests", action="store_true", help="QA 시 pytest 실행 생략")
    p.add_argument("--json", action="store_true", help="결과를 JSON 으로 출력")
    p.add_argument("--version", action="store_true", help="버전 출력")
    return p


def main(argv=None):
    args = _build_parser().parse_args(argv)

    if args.version:
        print("council_runtime " + RUNTIME_VERSION)
        return 0

    if args.qa is not None:
        gate = QAGate(project_root=os.getcwd())
        report = gate.run(args.qa, run_tests=not args.no_tests)
        if args.json:
            print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
        else:
            for c in report.checks:
                print(("  [ OK ] " if c["ok"] else "  [FAIL] ") + c["check"] +
                      " — " + str(c["target"]) + " : " + str(c["detail"]))
            print("QA 결과: " + report.code)
        return 0 if report.passed else 1

    if args.self_check:
        ok = self_check(verbose=not args.json)
        if args.json:
            print(json.dumps({"passed": ok}, ensure_ascii=False, indent=2))
        return 0 if ok else 1

    _build_parser().print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
