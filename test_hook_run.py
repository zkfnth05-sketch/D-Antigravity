#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_hook_run.py — Gatekeeper Hook 실측 검증 하네스
================================================================================
검증 항목:
  [TEST-1] PreToolUse  : view_file 호출 → {"decision": "deny", ...} 정확 반환
  [TEST-2] PreInvocation: --event PreInvocation → {"injectSteps": [{"ephemeralMessage": ...}]}

실행 방식:
  실제 훅 프로세스를 subprocess 로 구동하여 stdin 에 JSON 페이로드를 주입하고
  stdout 을 파싱한다. (모킹 없음 — 100% 실측)

인코딩:
  cp949 콘솔 크래시 방지를 위해 바이너리 파이프 + UTF-8 디코딩 사용.
"""

import io
import os
import sys
import json
import subprocess

# ============================================================
# 0. 콘솔 인코딩 안전
# ============================================================
for _n in ("stdin", "stdout", "stderr"):
    _s = getattr(sys, _n, None)
    if _s is not None and hasattr(_s, "reconfigure"):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

# ============================================================
# 1. 대상 훅 스크립트
# ============================================================
HOOK = r"C:\Users\zkfnt\.gemini\config\gatekeeper_hook.py"

PASS = []
FAIL = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        PASS.append(name)
        print(f"  [PASS] {name}" + (f" — {detail}" if detail else ""))
    else:
        FAIL.append(name)
        print(f"  [FAIL] {name}" + (f" — {detail}" if detail else ""))


def run_hook(payload: dict, extra_args=None, timeout=20):
    """훅 프로세스를 실제 구동하고 (rc, stdout, stderr) 를 반환."""
    cmd = [sys.executable, HOOK] + list(extra_args or [])
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=os.path.dirname(HOOK),
    )
    out_b, err_b = proc.communicate(
        json.dumps(payload, ensure_ascii=False).encode("utf-8"), timeout=timeout
    )
    out = out_b.decode("utf-8", errors="replace") if out_b else ""
    err = err_b.decode("utf-8", errors="replace") if err_b else ""
    return proc.returncode, out, err


def parse_json(out: str):
    """stdout 에서 첫 JSON 객체를 파싱."""
    s = out.strip()
    if not s:
        return None
    try:
        return json.loads(s)
    except Exception:
        # 로그가 섞여 있을 경우 첫 '{' ~ 마지막 '}' 추출 재시도
        i, j = s.find("{"), s.rfind("}")
        if i != -1 and j > i:
            try:
                return json.loads(s[i:j + 1])
            except Exception:
                return None
        return None


# ============================================================
# 2. TEST-1 : PreToolUse → deny
# ============================================================
def test_pre_tool_use() -> None:
    print("\n" + "=" * 72)
    print("[TEST-1] PreToolUse — view_file 호출 (토큰 없음) → deny 기대")
    print("=" * 72)

    payload = {
        "hookEventName": "PreToolUse",
        "toolCall": {
            "name": "view_file",
            "args": {"AbsolutePath": "test.py"},
        },
    }
    print("  ▶ 주입 페이로드:")
    print("    " + json.dumps(payload, ensure_ascii=False))

    rc, out, err = run_hook(payload)

    print(f"  ▶ RC     = {rc}")
    print(f"  ▶ STDOUT = {out.strip()!r}")
    print(f"  ▶ STDERR = {err.strip()!r}")

    parsed = parse_json(out)
    print("  ▶ PARSED = " + (json.dumps(parsed, ensure_ascii=False, indent=2) if parsed else "None"))

    check("T1-1 훅이 JSON 을 반환함", parsed is not None, "stdout 비어있음" if parsed is None else "")
    if parsed is None:
        return

    check("T1-2 decision == 'deny'", parsed.get("decision") == "deny",
          f"실제값={parsed.get('decision')!r}")
    reason = str(parsed.get("reason", ""))
    check("T1-3 reason 에 'GATEKEEPER 하드 블록' 포함", "GATEKEEPER 하드 블록" in reason,
          reason[:80])
    check("T1-4 reason 에 '토큰 없음' 포함", "토큰 없음" in reason, reason[:80])
    check("T1-5 reason 에 'deepseek' 협업체 문구 포함", "딥시크 5인 협업체" in reason, reason[:80])
    check("T1-6 STDERR 비어있음 (크래시 없음)", err.strip() == "", err.strip()[:120])


# ============================================================
# 3. TEST-2 : PreInvocation → injectSteps
# ============================================================
def test_pre_invocation() -> None:
    print("\n" + "=" * 72)
    print("[TEST-2] PreInvocation (--event PreInvocation) → injectSteps 기대")
    print("=" * 72)

    payload = {
        "hookEventName": "PreInvocation",
        "sessionId": "test-session-antigravity-001",
    }
    print("  ▶ 주입 페이로드:")
    print("    " + json.dumps(payload, ensure_ascii=False))
    print("  ▶ CLI 인자: --event PreInvocation")

    rc, out, err = run_hook(payload, extra_args=["--event", "PreInvocation"])

    print(f"  ▶ RC     = {rc}")
    print(f"  ▶ STDOUT = {out.strip()!r}")
    print(f"  ▶ STDERR = {err.strip()!r}")

    parsed = parse_json(out)
    print("  ▶ PARSED = " + (json.dumps(parsed, ensure_ascii=False, indent=2) if parsed else "None"))

    check("T2-1 훅이 JSON 을 반환함", parsed is not None, "stdout 비어있음" if parsed is None else "")
    if parsed is None:
        return

    check("T2-2 최상위 키에 'injectSteps' 존재", "injectSteps" in parsed,
          f"실제 키={list(parsed.keys())}")
    steps = parsed.get("injectSteps")
    check("T2-3 injectSteps 가 list", isinstance(steps, list), f"타입={type(steps).__name__}")
    if not isinstance(steps, list):
        return

    check("T2-4 injectSteps 길이 >= 1", len(steps) >= 1, f"길이={len(steps)}")
    if not steps:
        return

    first = steps[0]
    check("T2-5 steps[0] 이 dict", isinstance(first, dict), f"타입={type(first).__name__}")
    if not isinstance(first, dict):
        return

    check("T2-6 steps[0] 에 'ephemeralMessage' 존재", "ephemeralMessage" in first,
          f"실제 키={list(first.keys())}")

    msg = str(first.get("ephemeralMessage", ""))
    print(f"  ▶ ephemeralMessage = {msg!r}")
    check("T2-7 ephemeralMessage 비어있지 않음", bool(msg.strip()), "빈 문자열")
    check("T2-8 '6인 합동 회의체 절대 수칙' 포함", "6인 합동 회의체 절대 수칙" in msg, msg[:80])
    check("T2-9 'deepseek-council' 도구 호출 지시 포함", "deepseek-council" in msg, msg[:80])
    check("T2-10 'call_mcp_tool' 포함", "call_mcp_tool" in msg, msg[:80])
    check("T2-11 '딥시크 5인 협업체' 포함", "딥시크 5인 협업체" in msg, msg[:80])
    check("T2-12 STDERR 비어있음 (크래시 없음)", err.strip() == "", err.strip()[:120])


# ============================================================
# 4. TEST-3 : ensure_ascii 실측 (cp949 파이프 안전성)
# ============================================================
def test_ascii_safety() -> None:
    print("\n" + "=" * 72)
    print("[TEST-3] ensure_ascii=True 실측 — 출력이 순수 ASCII 인지 확인")
    print("=" * 72)

    payload = {"hookEventName": "PreToolUse",
               "toolCall": {"name": "view_file", "args": {"AbsolutePath": "test.py"}}}
    rc, out, err = run_hook(payload)
    raw = out.strip()

    try:
        raw.encode("ascii")
        is_ascii = True
    except UnicodeEncodeError:
        is_ascii = False

    print(f"  ▶ 출력 길이 = {len(raw)}")
    print(f"  ▶ 순수 ASCII 여부 = {is_ascii}")
    check("T3-1 출력이 순수 ASCII (\\uXXXX 이스케이프)", is_ascii,
          "비ASCII 문자 잔존 → cp949 파이프 크래시 위험")
    check("T3-2 '\\u' 이스케이프 시퀀스 존재", "\\u" in raw, "이스케이프 미적용")


# ============================================================
# 5. 메인
# ============================================================
def main() -> int:
    print("=" * 72)
    print("Gatekeeper Hook 실측 검증 시작")
    print(f"대상 훅: {HOOK}")
    print(f"존재 여부: {os.path.exists(HOOK)}")
    print("=" * 72)

    if not os.path.exists(HOOK):
        print("[FATAL] 훅 파일이 존재하지 않습니다.")
        return 2

    test_pre_tool_use()
    test_pre_invocation()
    test_ascii_safety()

    print("\n" + "=" * 72)
    print("최종 결과")
    print("=" * 72)
    print(f"  PASS: {len(PASS)}")
    print(f"  FAIL: {len(FAIL)}")
    if FAIL:
        print("\n  실패 항목:")
        for f in FAIL:
            print("    ✗ " + f)
        return 1
    print("\n  ✅ 모든 검증 통과 — PreToolUse deny / PreInvocation injectSteps 정상 동작")
    return 0


if __name__ == "__main__":
    sys.exit(main())
