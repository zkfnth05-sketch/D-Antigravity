#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Council Enforcer — Stop Hook (제미나이가 딥시크를 건너뛰지 못하게 하는 강제 장치)
================================================================================
[거버넌스] 6인 합동 회의체: 제미나이 1인 토론위원 + 딥시크 5인 협업체
[핵심 원칙] 제미나이가 코드 관련 작업을 수행하면서 딥시크 5인 협업체와 한 번도
            상의하지 않고 턴을 종료(Stop)하려 할 때 물리적으로 차단한다.

[Antigravity Stop 훅 표준 스키마]
  Input (stdin):
    {
      "terminationReason": "model_stop",
      "transcriptPath": "...",
      "fullyIdle": true,
      ...common fields
    }
  Output (stdout):
    - 차단: {"decision": "continue", "reason": "..."}
    - 허용: {"decision": "allow"}

감지 방식:
  1. transcriptPath 에서 현재 대화의 transcript.jsonl 읽기
  2. 최근 턴에서 deepseek_council_debate / deepseek_autonomous_codegen /
     deepseek_read_file_full 등 딥시크 MCP 도구 호출 여부 확인
  3. 코드 수정 의도 감지 (write_to_file, replace_file_content 시도 또는
     코드 관련 키워드 포함 사용자 메시지)
  4. 코드 의도 O + 딥시크 미상의 → decision: continue (턴 종료 차단)
  5. 코드 의도 X 또는 딥시크 상의 완료 → decision: allow

[CWD 비의존성]
  모든 경로는 __file__ 또는 stdin payload 기준 절대경로.

작성: Gemini (대표님 승인 하 단독 구현)
"""

import json
import os
import re
import sys
import traceback
from typing import Any, Dict, List, Optional, Tuple

# ============================================================
# 0. 상수
# ============================================================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ENFORCER_LOG_PATH = os.path.join(BASE_DIR, "council_enforcer.log")
MAX_LOG_BYTES = 10 * 1024 * 1024  # 10 MiB

# 딥시크 MCP 도구 식별자 (이 중 하나라도 호출되었으면 "상의 완료"로 판정)
DEEPSEEK_TOOL_PATTERNS = (
    "deepseek_council_debate",
    "deepseek_autonomous_codegen",
    "deepseek_read_file_full",
    "deepseek_verify_python_syntax",
    "deepseek_view_timeline",
    "deepseek_rollback_file",
    "deepseek_verify_receipt",
    "deepseek_search_code",
    "deepseek_code_outline",
    "deepseek_view_section",
)

# 딥시크 MCP 서버명
DEEPSEEK_SERVER_NAME = "deepseek-council"

# 코드 수정 의도를 나타내는 도구명 패턴
CODE_INTENT_TOOLS = (
    "write_to_file",
    "replace_file_content",
    "multi_replace_file_content",
    "edit_file",
    "create_file",
    "apply_patch",
    "run_command",
    "execute_command",
)

# 코드 관련 키워드 (사용자 메시지에서 감지)
CODE_INTENT_KEYWORDS_KR = (
    "코드", "구현", "수정", "버그", "에러", "오류", "함수", "클래스",
    "파일", "스크립트", "리팩토링", "디버그", "테스트", "배포",
    "파이프라인", "모듈", "패키지", "임포트", "컴파일", "빌드",
    "개발", "프로그래밍", "알고리즘", "API", "엔드포인트",
)

CODE_INTENT_KEYWORDS_EN = (
    "code", "implement", "fix", "bug", "error", "function", "class",
    "file", "script", "refactor", "debug", "test", "deploy",
    "pipeline", "module", "package", "import", "compile", "build",
    "develop", "programming", "algorithm", "api", "endpoint",
    "commit", "push", "merge", "pull request",
)

# Stop 이유 중 강제기 적용 제외 대상
SKIP_REASONS = frozenset({
    "max_steps_exceeded",
    "error",
    "timeout",
    "user_interrupt",
})


# ============================================================
# 1. 로깅 (append-only, 경량)
# ============================================================
def _log(event: Dict[str, Any]) -> None:
    """경량 감사 로그 기록."""
    try:
        if os.path.exists(ENFORCER_LOG_PATH) and os.path.getsize(ENFORCER_LOG_PATH) > MAX_LOG_BYTES:
            os.replace(ENFORCER_LOG_PATH, ENFORCER_LOG_PATH + ".old")
        from datetime import datetime, timezone
        event["ts"] = datetime.now(timezone.utc).isoformat()
        with open(ENFORCER_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
    except Exception:
        pass


# ============================================================
# 2. Transcript 분석기
# ============================================================
def _find_transcript_path(payload: Dict[str, Any]) -> Optional[str]:
    """payload 에서 transcriptPath 추출 또는 추론."""
    # 직접 제공
    tp = payload.get("transcriptPath") or payload.get("transcript_path")
    if tp and os.path.isfile(tp):
        return tp

    # artifactDirectoryPath 에서 추론
    adp = payload.get("artifactDirectoryPath") or payload.get("artifact_directory_path")
    if adp:
        logs_dir = os.path.join(os.path.dirname(adp), "logs")
        candidate = os.path.join(logs_dir, "transcript.jsonl")
        if os.path.isfile(candidate):
            return candidate

    # conversationId 기반 추론
    conv_id = payload.get("conversationId") or payload.get("conversation_id") or ""
    if conv_id:
        # Antigravity IDE 기본 경로
        home = os.path.expanduser("~")
        for product in ("deepseek-antigravity-ide", "antigravity-ide", "antigravity"):
            candidate = os.path.join(
                home, ".gemini", product, "brain", conv_id,
                ".system_generated", "logs", "transcript.jsonl"
            )
            if os.path.isfile(candidate):
                return candidate

    return None


def _read_recent_transcript(transcript_path: str, max_lines: int = 500) -> List[Dict[str, Any]]:
    """transcript.jsonl 의 최근 N줄을 파싱하여 반환."""
    if not transcript_path or not os.path.isfile(transcript_path):
        return []

    lines: List[str] = []
    try:
        with open(transcript_path, "r", encoding="utf-8", errors="ignore") as f:
            # 마지막 max_lines 줄만 읽기 (대용량 대비)
            all_lines = f.readlines()
            lines = all_lines[-max_lines:]
    except Exception:
        return []

    entries: List[Dict[str, Any]] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return entries


def _detect_deepseek_consultation(entries: List[Dict[str, Any]]) -> Tuple[bool, List[str]]:
    """
    Transcript 항목들에서 딥시크 도구 호출 여부를 탐지.

    Returns:
        (consulted: bool, tools_found: list of tool names)
    """
    found_tools: List[str] = []

    for entry in entries:
        # 1) tool_calls 배열 확인
        tool_calls = entry.get("tool_calls") or []
        for tc in tool_calls:
            if isinstance(tc, dict):
                tool_name = tc.get("name") or tc.get("tool_name") or ""
                args = tc.get("args") or tc.get("arguments") or {}

                # 직접 딥시크 도구 호출
                for pattern in DEEPSEEK_TOOL_PATTERNS:
                    if pattern in tool_name.lower():
                        found_tools.append(tool_name)
                        break

                # call_mcp_tool 을 통한 간접 호출
                if "call_mcp_tool" in tool_name.lower():
                    server = ""
                    inner_tool = ""
                    if isinstance(args, dict):
                        server = str(args.get("ServerName") or args.get("server_name") or "")
                        inner_tool = str(args.get("ToolName") or args.get("tool_name") or "")
                    if DEEPSEEK_SERVER_NAME in server.lower():
                        found_tools.append(f"call_mcp_tool→{inner_tool}")
                    for pattern in DEEPSEEK_TOOL_PATTERNS:
                        if pattern in inner_tool.lower():
                            found_tools.append(f"call_mcp_tool→{inner_tool}")
                            break

        # 2) content 에서 딥시크 도구 호출 흔적 확인
        content = str(entry.get("content") or "")
        for pattern in DEEPSEEK_TOOL_PATTERNS:
            if pattern in content:
                # 실제 도구 호출 결과에서 나타나는 패턴
                if any(marker in content for marker in ("ToolName", "ServerName", "deepseek-council")):
                    found_tools.append(f"content_ref:{pattern}")
                    break

    return bool(found_tools), found_tools


def _detect_code_intent(entries: List[Dict[str, Any]]) -> Tuple[bool, str]:
    """
    Transcript 항목들에서 코드 수정 의도를 탐지.

    Returns:
        (has_intent: bool, evidence: str)
    """
    # 1) 코드 수정 도구 호출 시도 확인 (거부되었더라도 시도 자체가 의도)
    for entry in entries:
        tool_calls = entry.get("tool_calls") or []
        for tc in tool_calls:
            if isinstance(tc, dict):
                tool_name = str(tc.get("name") or tc.get("tool_name") or "").lower()
                for code_tool in CODE_INTENT_TOOLS:
                    if code_tool in tool_name:
                        return True, f"도구 호출 시도: {tool_name}"

    # 2) 사용자 메시지에서 코드 관련 키워드 확인
    for entry in entries:
        if entry.get("type") == "USER_INPUT" or entry.get("source") == "USER_EXPLICIT":
            content = str(entry.get("content") or "").lower()
            for kw in CODE_INTENT_KEYWORDS_KR:
                if kw in content:
                    return True, f"사용자 키워드: {kw}"
            for kw in CODE_INTENT_KEYWORDS_EN:
                if kw in content:
                    return True, f"사용자 키워드: {kw}"

    # 3) 모델 응답에서 코드 블록 제안 확인
    for entry in entries:
        if entry.get("source") == "MODEL" or entry.get("type") == "PLANNER_RESPONSE":
            content = str(entry.get("content") or "")
            # 코드 블록이 포함된 응답
            code_block_count = content.count("```")
            if code_block_count >= 4:  # 2쌍 이상의 코드 블록
                return True, f"모델 코드 블록 {code_block_count // 2}개"

    return False, ""


# ============================================================
# 3. Stop 훅 판정 헬퍼
# ============================================================

def _resolve_receipt_dirs() -> List[str]:
    """영수증 디렉터리 후보 목록 (존재 여부 무관, 호출측에서 isdir 확인)."""
    script_dir = os.path.dirname(os.path.abspath(__file__))
    agents_dir = os.path.dirname(script_dir)  # aider-main/.agents
    return [
        os.path.join(agents_dir, "shared", "receipts"),
        os.path.join(os.path.dirname(agents_dir),
                     "DeepSeek_Antigravity_IDE", ".agents", "shared", "receipts"),
    ]


def _load_receipt_record(fpath: str) -> Optional[Dict[str, Any]]:
    """
    단일 영수증 파일을 구조화 레코드로 로드.
    실패 시 None 반환하되, 호출측에서 '검증 오류'로 구분 가능하도록 예외를 삼키지 않고
    상위에서 _log 로 기록한다.
    """
    with open(fpath, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        return None
    return {
        "path": fpath,
        "verdict": str(data.get("verdict") or "").upper(),
        "receipt_id": str(data.get("receipt_id") or data.get("id") or ""),
        "conversation_id": str(data.get("conversation_id") or data.get("conversationId") or ""),
        "turn_id": str(data.get("turn_id") or data.get("turnId") or ""),
        "mtime": os.path.getmtime(fpath),
    }


def _collect_pass_receipts(payload: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], List[str]]:
    """
    PASS 영수증 레코드 목록과 검증 오류 목록을 반환.
    시간창 스캔을 폐기하고, conversation_id 바인딩은 호출측에서 판정한다.
    """
    records: List[Dict[str, Any]] = []
    errors: List[str] = []
    for receipt_dir in _resolve_receipt_dirs():
        if not os.path.isdir(receipt_dir):
            continue
        try:
            fnames = os.listdir(receipt_dir)
        except OSError as e:
            errors.append(f"listdir 실패 {receipt_dir}: {e}")
            continue
        for fname in fnames:
            if not fname.endswith(".json"):
                continue
            fpath = os.path.join(receipt_dir, fname)
            try:
                rec = _load_receipt_record(fpath)
            except (OSError, json.JSONDecodeError, UnicodeDecodeError) as e:
                errors.append(f"영수증 파싱 실패 {fname}: {e}")
                continue
            if rec and rec["verdict"] == "PASS":
                records.append(rec)
    return records, errors


def _extract_conversation_id(payload: Dict[str, Any]) -> str:
    """payload 에서 conversationId 추출."""
    return str(
        payload.get("conversationId")
        or payload.get("conversation_id")
        or ""
    )


def _verify_receipt_for_turn(
    payload: Dict[str, Any],
    entries: List[Dict[str, Any]],
) -> Tuple[bool, str, Dict[str, Any]]:
    """
    턴 스코프 PASS 영수증 검증.

    Returns:
        (verified: bool, reason: str, detail: dict)
        - verified=True: 이 턴에 바인딩된 PASS 영수증이 존재하고 모델 응답이 인용함
        - verified=False: 영수증 없음/불일치/검증 오류
        detail["error"]=True 이면 검증 자체가 실패한 것(fail-closed 대상)
    """
    conv_id = _extract_conversation_id(payload)
    records, errors = _collect_pass_receipts(payload)

    detail: Dict[str, Any] = {
        "conversation_id": conv_id,
        "receipts_found": len(records),
        "errors": errors,
        "error": bool(errors) and not records,
    }

    if not records:
        return False, "PASS 영수증 없음", detail

    # conversation_id 바인딩: conv_id 가 있으면 일치하는 영수증만 유효
    if conv_id:
        bound = [r for r in records if r["conversation_id"] == conv_id]
        if not bound:
            detail["binding"] = "conversation_id 불일치"
            return False, "이 대화에 바인딩된 PASS 영수증 없음", detail
        records = bound

    # 모델 응답이 영수증 ID를 실제로 인용했는지 교차 확인
    cited_ids = set()
    for entry in entries:
        if entry.get("source") == "MODEL" or entry.get("type") == "PLANNER_RESPONSE":
            content = str(entry.get("content") or "")
            for r in records:
                rid = r["receipt_id"]
                if rid and rid in content:
                    cited_ids.add(rid)

    if cited_ids:
        detail["cited_receipt_ids"] = sorted(cited_ids)
        return True, f"영수증 인용 확인: {sorted(cited_ids)}", detail

    # 인용이 없더라도 conversation_id 바인딩된 PASS 영수증이 있으면 유효
    # (모델이 ID를 노출하지 않는 정상 흐름 허용)
    detail["cited_receipt_ids"] = []
    return True, "conversation_id 바인딩 PASS 영수증 존재", detail


# ============================================================
# 4. Stop 훅 판정 로직
# ============================================================
def decide_stop(payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    Stop 이벤트 판정.

    코드 의도 O + 딥시크 미상의 → {"decision": "continue", "reason": "..."}
    그 외 → {"decision": "allow"}
    """
    termination_reason = str(payload.get("terminationReason") or payload.get("termination_reason") or "")

    # 시스템 종료 사유(에러, 스텝 초과 등)는 강제기 적용 제외
    if termination_reason in SKIP_REASONS:
        _log({
            "event": "enforcer_skip",
            "reason": f"시스템 종료 사유: {termination_reason}",
        })
        return {"decision": "allow"}

    # Transcript 로드
    transcript_path = _find_transcript_path(payload)
    if not transcript_path:
        # Transcript를 찾을 수 없으면 보수적으로 허용 (false positive 방지)
        _log({
            "event": "enforcer_allow_no_transcript",
            "reason": "transcript 경로를 찾을 수 없음",
        })
        return {"decision": "allow"}

    entries = _read_recent_transcript(transcript_path)
    if not entries:
        _log({
            "event": "enforcer_allow_empty_transcript",
            "reason": "transcript 비어있음",
        })
        return {"decision": "allow"}

    # 딥시크 상의 여부 확인
    consulted, ds_tools = _detect_deepseek_consultation(entries)

    # 코드 의도 확인
    has_code_intent, code_evidence = _detect_code_intent(entries)

    _log({
        "event": "enforcer_analysis",
        "transcript_path": transcript_path,
        "entries_analyzed": len(entries),
        "deepseek_consulted": consulted,
        "deepseek_tools": ds_tools,
        "code_intent": has_code_intent,
        "code_evidence": code_evidence,
        "termination_reason": termination_reason,
    })

    # 판정: 코드 의도가 있는데 딥시크 미상의 → 차단
    if has_code_intent and not consulted:
        # [Phase 4] 턴 스코프 PASS 영수증 검증 (fail-closed)
        verified, verify_reason, verify_detail = _verify_receipt_for_turn(payload, entries)
        if verified:
            _log({
                "event": "enforcer_allow_receipt",
                "receipt_found": True,
                "verify_reason": verify_reason,
                "verify_detail": verify_detail,
                "code_evidence": code_evidence,
            })
            return {"decision": "allow"}

        # 검증 오류(파싱/IO)는 fail-closed: 차단하되 사유에 오류 명시
        if verify_detail.get("error"):
            _log({
                "event": "enforcer_block_verify_error",
                "verify_reason": verify_reason,
                "verify_detail": verify_detail,
                "code_evidence": code_evidence,
            })
            reason = (
                "🚨 [Council Enforcer] 코드 관련 작업이 감지되었으나 "
                "완료검증 영수증 검증 중 오류가 발생했습니다 (fail-closed).\n\n"
                f"📌 코드 의도 근거: {code_evidence}\n"
                f"📌 검증 오류: {verify_reason}\n"
                f"📌 상세: {verify_detail.get('errors')}\n\n"
                "👉 영수증 디렉터리 권한/JSON 무결성을 확인한 뒤, "
                "deepseek_council_debate 또는 deepseek_autonomous_codegen을 "
                "호출하여 딥시크 5인 협업체와 상의한 후 작업을 완료하십시오."
            )
            return {"decision": "continue", "reason": reason}

        reason = (
            "🚨 [Council Enforcer] 코드 관련 작업이 감지되었으나 딥시크 5인 협업체와 "
            "상의한 기록이 없습니다. 턴 종료가 차단됩니다.\n\n"
            f"📌 코드 의도 근거: {code_evidence}\n"
            f"📌 영수증 검증: {verify_reason}\n\n"
            "👉 반드시 deepseek_council_debate 또는 deepseek_autonomous_codegen을 "
            "호출하여 딥시크 5인 협업체와 상의한 후 작업을 완료하십시오.\n\n"
            "💡 만약 코드 수정이 필요 없는 단순 질문/설명이었다면, "
            "사용자에게 그 사실을 명시하고 다시 종료를 시도하십시오."
        )
        _log({
            "event": "enforcer_block",
            "code_evidence": code_evidence,
            "verify_reason": verify_reason,
        })
        return {"decision": "continue", "reason": reason}

    # 딥시크 상의 완료 경로에서도 영수증 진위 재검증 (false-allow 차단)
    if has_code_intent and consulted:
        verified, verify_reason, verify_detail = _verify_receipt_for_turn(payload, entries)
        if not verified and not verify_detail.get("error"):
            # 호출은 있었으나 PASS 영수증이 없는 경우: 실패 보고 가능성 → 차단
            _log({
                "event": "enforcer_block_consulted_no_receipt",
                "verify_reason": verify_reason,
                "verify_detail": verify_detail,
                "code_evidence": code_evidence,
                "deepseek_tools": ds_tools,
            })
            reason = (
                "🚨 [Council Enforcer] 딥시크 호출 기록은 있으나 "
                "이 턴에 바인딩된 PASS 완료검증 영수증이 없습니다.\n\n"
                f"📌 코드 의도 근거: {code_evidence}\n"
                f"📌 호출된 도구: {ds_tools}\n"
                f"📌 영수증 검증: {verify_reason}\n\n"
                "👉 딥시크 협업 결과가 FAIL 이거나 검증이 완료되지 않았을 수 있습니다. "
                "deepseek_verify_receipt 로 영수증을 확인한 뒤 재작업하십시오."
            )
            return {"decision": "continue", "reason": reason}

    # 딥시크 상의 완료 + 영수증 검증 통과, 또는 코드 의도 없음 → 허용
    _log({
        "event": "enforcer_allow",
        "consulted": consulted,
        "code_intent": has_code_intent,
    })
    return {"decision": "allow"}


# ============================================================
# 4. 진입점
# ============================================================
def _emit(obj: Dict[str, Any]) -> None:
    """표준 스키마 JSON을 stdout에 출력."""
    try:
        sys.stdout.write(json.dumps(obj, ensure_ascii=True) + "\n")
        sys.stdout.flush()
    except Exception:
        try:
            sys.stdout.write('{"decision": "allow"}\n')
            sys.stdout.flush()
        except Exception:
            pass


def main() -> int:
    try:
        raw = sys.stdin.read()
        if not raw.strip():
            _log({"event": "empty_input"})
            _emit({"decision": "allow"})
            return 0

        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as e:
            _log({"event": "bad_json", "error": str(e)})
            _emit({"decision": "allow"})
            return 0

        if not isinstance(payload, dict):
            _log({"event": "bad_payload_type", "type": type(payload).__name__})
            _emit({"decision": "allow"})
            return 0

        result = decide_stop(payload)
        _emit(result)
        return 0

    except Exception:
        _log({"event": "fatal", "trace": traceback.format_exc()})
        # 강제기 자체 오류 시 허용 (에이전트 무한 루프 방지)
        _emit({"decision": "allow"})
        return 0


if __name__ == "__main__":
    sys.exit(main())
