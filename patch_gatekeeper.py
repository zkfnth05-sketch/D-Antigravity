#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
patch_gatekeeper.py — Gatekeeper Hook 자율 패치 스크립트
================================================================================
대상:
  1) C:\\Users\\zkfnt\\.gemini\\config\\gatekeeper_hook.py
  2) C:\\Users\\zkfnt\\Desktop\\한국 마케팅봇\\.agents\\scripts\\gatekeeper_hook.py
  3) C:\\Users\\zkfnt\\.gemini\\config\\hooks.json
  4) C:\\Users\\zkfnt\\Desktop\\한국 마케팅봇\\.agents\\hooks.json

적용 항목:
  [1] 파일 최상단 sys.stdin/stdout/stderr UTF-8 reconfigure
  [2] 모든 print(json.dumps(...)) → ensure_ascii=True
  [3] PreInvocation → {"injectSteps": [{"ephemeralMessage": "..."}]} 반환
  [4] PreToolUse → 토큰 없는 guarded 도구 즉시 {"decision": "deny", ...}
  [5] hooks.json 공식 스키마 정규화

설계 원칙:
  · 멱등(idempotent) — 여러 번 실행해도 안전
  · 원자적 쓰기(tmp → os.replace)
  · 백업(.bak_patch) 자동 생성
  · 패치 후 py_compile 로 문법 자동 검증
"""

import io
import os
import re
import sys
import json
import shutil
import hashlib
import py_compile

# ============================================================
# 0. 인코딩 안전 (이 스크립트 자체도 cp949 콘솔에서 안전해야 함)
# ============================================================
for _name in ("stdin", "stdout", "stderr"):
    _s = getattr(sys, _name, None)
    if _s is not None and hasattr(_s, "reconfigure"):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

# ============================================================
# 1. 경로 상수
# ============================================================
GEMINI_DIR = r"C:\Users\zkfnt\.gemini\config"
MARKETING_DIR = r"C:\Users\zkfnt\Desktop\한국 마케팅봇\.agents"

HOOK_TARGETS = [
    os.path.join(GEMINI_DIR, "gatekeeper_hook.py"),
    os.path.join(MARKETING_DIR, "scripts", "gatekeeper_hook.py"),
]

HOOKS_JSON_TARGETS = [
    os.path.join(GEMINI_DIR, "hooks.json"),
    os.path.join(MARKETING_DIR, "hooks.json"),
]

COUNCIL_MESSAGE = (
    "🚨 [6인 합동 회의체 절대 수칙] 제미나이는 코드를 직접 수정하거나 혼자 조사할 수 없습니다. "
    "모든 작업은 반드시 call_mcp_tool의 deepseek-council 도구를 호출하여 "
    "딥시크 5인 협업체와 합동으로 수행해야 합니다."
)

# ============================================================
# 2. 공통 유틸
# ============================================================
REPORT = []


def log(ok: bool, msg: str) -> None:
    REPORT.append((ok, msg))
    print(("[OK]   " if ok else "[FAIL] ") + msg)


def read_text(path: str) -> str:
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        return f.read()


def atomic_write(path: str, text: str) -> None:
    """원자적 쓰기 — tmp 파일에 쓴 뒤 os.replace 로 교체."""
    tmp = path + ".patchtmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    os.replace(tmp, path)


def backup(path: str) -> str:
    bak = path + ".bak_patch"
    shutil.copy2(path, bak)
    return bak


# ============================================================
# 3. [패치 1] UTF-8 스트림 reconfigure 블록 삽입
# ============================================================
UTF8_BLOCK = '''
# ============================================================
# [PATCH-1] 표준 스트림 UTF-8 강제 재설정 (최우선 실행)
#   Windows cp949 콘솔/파이프 환경에서 유니코드 입출력 크래시 원천 차단
# ============================================================
def _force_utf8_streams() -> None:
    """sys.stdin/stdout/stderr 를 UTF-8 로 reconfigure (실패해도 절대 중단 금지)."""
    for _name in ("stdin", "stdout", "stderr"):
        _stream = getattr(sys, _name, None)
        if _stream is None:
            continue
        _reconfigure = getattr(_stream, "reconfigure", None)
        if callable(_reconfigure):
            try:
                _reconfigure(encoding="utf-8", errors="replace")
                continue
            except Exception:
                pass
        # reconfigure 미지원(구버전/래핑된 스트림) → TextIOWrapper 재포장
        try:
            _buffer = getattr(_stream, "buffer", None)
            if _buffer is not None:
                setattr(
                    sys,
                    _name,
                    io.TextIOWrapper(_buffer, encoding="utf-8", errors="replace", line_buffering=True),
                )
        except Exception:
            pass


_force_utf8_streams()
'''

# ============================================================
# 4. [패치 2] 인코딩 안전 JSON 출력 헬퍼
# ============================================================
EMIT_BLOCK = '''
# ============================================================
# [PATCH-2] 인코딩 안전 JSON 출력 헬퍼
#   ensure_ascii=True → 모든 비ASCII 문자를 \\uXXXX 로 이스케이프
#   → cp949 콘솔/파이프에서 UnicodeEncodeError 발생 불가
# ============================================================
def emit(obj) -> None:
    """훅 응답을 표준출력으로 방출. 어떤 상황에서도 예외를 던지지 않는다."""
    try:
        text = json.dumps(obj, ensure_ascii=True)
    except Exception:
        text = json.dumps(
            {"decision": "deny", "reason": "[GATEKEEPER] 응답 직렬화 실패 - fail-closed"},
            ensure_ascii=True,
        )
    try:
        sys.stdout.write(text + "\\n")
        sys.stdout.flush()
    except Exception:
        # 최후 수단: 바이너리 버퍼에 직접 ASCII 바이트 기록
        try:
            _buf = getattr(sys.stdout, "buffer", None)
            if _buf is not None:
                _buf.write(text.encode("ascii", errors="backslashreplace") + b"\\n")
                _buf.flush()
        except Exception:
            pass


def emit_deny(reason: str) -> None:
    emit({"decision": "deny", "reason": reason})
'''


def patch_utf8_streams(src: str) -> str:
    """[패치 1] import 블록 직후에 UTF-8 reconfigure 코드 삽입."""
    if "_force_utf8_streams" in src:
        return src  # 이미 적용됨 (멱등)

    # io 모듈 import 보장
    if not re.search(r"^import io\s*$", src, re.M):
        src = re.sub(r"^import os\s*$", "import io\nimport os", src, count=1, flags=re.M)

    # 마지막 import 문 뒤에 삽입
    import_lines = list(re.finditer(r"^(?:import|from)\s+\S+.*$", src, re.M))
    if not import_lines:
        raise RuntimeError("import 블록을 찾지 못했습니다.")
    insert_at = import_lines[-1].end()
    return src[:insert_at] + "\n" + UTF8_BLOCK + src[insert_at:]


def patch_emit_helper(src: str) -> str:
    """[패치 2] emit() 헬퍼 삽입 (상수 섹션 뒤)."""
    if "def emit(obj)" in src:
        return src  # 멱등

    anchor = "TOKEN_ENV_KEYS = (\"GATEKEEPER_TOKEN\", \"DEEPSEEK_COUNCIL_TOKEN\")"
    idx = src.find(anchor)
    if idx == -1:
        raise RuntimeError("TOKEN_ENV_KEYS 앵커를 찾지 못했습니다.")
    insert_at = idx + len(anchor)
    return src[:insert_at] + "\n\n" + EMIT_BLOCK + src[insert_at:]


def patch_ensure_ascii(src: str) -> str:
    """[패치 2] 모든 print(json.dumps(...)) 를 emit(...) 로 치환 + ensure_ascii=True."""
    # (a) print(json.dumps(X, ensure_ascii=False)) → emit(X)
    src = re.sub(
        r"print\(\s*json\.dumps\(\s*(.+?)\s*,\s*ensure_ascii\s*=\s*False\s*\)\s*\)",
        r"emit(\1)",
        src,
        flags=re.S,
    )
    # (b) print(json.dumps(X)) → emit(X)
    src = re.sub(
        r"print\(\s*json\.dumps\(\s*(.+?)\s*\)\s*\)",
        r"emit(\1)",
        src,
        flags=re.S,
    )
    # (c) 남은 ensure_ascii=False 중 출력 계열은 True 로 (감사로그 파일 기록은 UTF-8 파일이므로 유지)
    #     → print/emit 계열에만 적용되도록 이미 (a)(b)에서 처리됨
    return src


# ============================================================
# 5. [패치 3] PreInvocation → injectSteps 반환
# ============================================================
NEW_PRE_INVOCATION = '''    @classmethod
    def pre_invocation(cls, payload: Dict[str, Any]) -> Dict[str, Any]:
        """[PATCH-3] PreInvocation 진입점.

        Antigravity hooks 규격에 맞춰 injectSteps(ephemeralMessage) 를 반환하여
        6인 합동 회의체 절대 수칙을 세션 컨텍스트에 강제 주입한다.
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

        # ── Antigravity hooks 규격: injectSteps / ephemeralMessage ──
        message = COUNCIL_MANDATE_MESSAGE
        if not gov.get("intact", False):
            message += " (거버넌스 문서 점검 필요: rules/01-joint-council-governance.md)"

        return {
            "injectSteps": [
                {"ephemeralMessage": message}
            ]
        }
'''


def patch_pre_invocation(src: str) -> str:
    """[패치 3] SessionGuard.pre_invocation 전체 교체 + 상수 추가."""
    # 상수 추가 (멱등)
    if "COUNCIL_MANDATE_MESSAGE" not in src:
        anchor = "MAX_TOKEN_STORE_ENTRIES = 5000      # 토큰 저장소 폭주 방지 상한"
        idx = src.find(anchor)
        if idx == -1:
            raise RuntimeError("MAX_TOKEN_STORE_ENTRIES 앵커를 찾지 못했습니다.")
        insert_at = idx + len(anchor)
        const_block = (
            "\n\n# ------------------------------------------------------------\n"
            "# [PATCH-3] PreInvocation 시 주입할 6인 합동 회의체 절대 수칙\n"
            "# ------------------------------------------------------------\n"
            "COUNCIL_MANDATE_MESSAGE = (\n"
            '    "🚨 [6인 합동 회의체 절대 수칙] 제미나이는 코드를 직접 수정하거나 혼자 조사할 수 없습니다. "\n'
            '    "모든 작업은 반드시 call_mcp_tool의 deepseek-council 도구를 호출하여 "\n'
            '    "딥시크 5인 협업체와 합동으로 수행해야 합니다."\n'
            ")\n"
        )
        src = src[:insert_at] + const_block + src[insert_at:]

    # pre_invocation 메서드 교체
    start_marker = "    @classmethod\n    def pre_invocation(cls, payload: Dict[str, Any]) -> Dict[str, Any]:"
    start = src.find(start_marker)
    if start == -1:
        raise RuntimeError("pre_invocation 메서드를 찾지 못했습니다.")

    # 다음 클래스/섹션 경계까지 잘라내기
    end_marker = "\n\n# ============================================================\n# 5. 워치독"
    end = src.find(end_marker, start)
    if end == -1:
        raise RuntimeError("pre_invocation 종료 경계를 찾지 못했습니다.")

    return src[:start] + NEW_PRE_INVOCATION + src[end:]


# ============================================================
# 6. [패치 4] PreToolUse → 토큰 없는 guarded 도구 즉시 deny
# ============================================================
def patch_pre_tool_use(src: str) -> str:
    """[패치 4] Gatekeeper.decide 에 '토큰 없음 즉시 하드 블록' 분기 삽입."""
    if "즉시 하드 블록" in src:
        return src  # 멱등

    anchor = (
        '        # 비감시 도구(순수 메타/통신 계열)만 통과\n'
        '        if category == "other":\n'
        '            AuditLog.write({"event": "allow_non_guarded", "tool": tool})\n'
        '            return {"decision": "allow", "reason": "비감시 도구 (메타/통신 계열)"}\n'
    )
    idx = src.find(anchor)
    if idx == -1:
        raise RuntimeError("Gatekeeper.decide 의 비감시 도구 분기를 찾지 못했습니다.")

    hard_block = (
        '\n'
        '        # ── [PATCH-4] 즉시 하드 블록: guarded 도구인데 토큰이 아예 없음 ──\n'
        '        if not token_id:\n'
        '            label = "쓰기/실행" if category == "write" else "조사/열람"\n'
        '            reason = (\n'
        '                "⛔ [GATEKEEPER 하드 블록] 토큰 없음 (제미나이 독단 시도 감지). "\n'
        '                f"제미나이의 독단적 {label} 도구 호출은 물리적으로 차단됩니다. "\n'
        '                "딥시크 5인 협업체(R1 아키텍트·V3 감사관·V3 선임엔지니어·QA 검증관·Git 안전관) "\n'
        '                "전원 승인 서명 토큰이 필요합니다."\n'
        '            )\n'
        '            AuditLog.write({\n'
        '                "event": "gatekeeper_decision",\n'
        '                "tool": tool,\n'
        '                "category": category,\n'
        '                "token_id": "",\n'
        '                "fingerprint": "n/a",\n'
        '                "decision": "deny",\n'
        '                "reason": "토큰 없음 (즉시 하드 블록)",\n'
        '            })\n'
        '            return {"decision": "deny", "reason": reason}\n'
    )
    insert_at = idx + len(anchor)
    return src[:insert_at] + hard_block + src[insert_at:]


# ============================================================
# 7. [패치 5] --event CLI 인자 지원 (hooks.json 규격 대응)
# ============================================================
def patch_cli_event(src: str) -> str:
    """[패치 5] --event PreInvocation / --event PreToolUse CLI 인자 인식."""
    if "--event" in src:
        return src  # 멱등

    anchor = "def _detect_event(payload: Dict[str, Any]) -> str:"
    idx = src.find(anchor)
    if idx == -1:
        raise RuntimeError("_detect_event 함수를 찾지 못했습니다.")

    cli_block = (
        'def _cli_event_override() -> str:\n'
        '    """[PATCH-5] --event <Name> CLI 인자로 이벤트를 강제 지정."""\n'
        '    argv = sys.argv[1:]\n'
        '    for i, a in enumerate(argv):\n'
        '        if a == "--event" and i + 1 < len(argv):\n'
        '            return str(argv[i + 1]).strip().lower()\n'
        '        if a.startswith("--event="):\n'
        '            return a.split("=", 1)[1].strip().lower()\n'
        '    return ""\n'
        '\n'
        '\n'
    )
    src = src[:idx] + cli_block + src[idx:]

    # main() 에서 CLI 오버라이드 우선 적용
    old = '        event = _detect_event(payload)'
    new = (
        '        _cli = _cli_event_override()\n'
        '        if "preinvocation" in _cli or "pre_invocation" in _cli:\n'
        '            event = "pre_invocation"\n'
        '        elif "pretooluse" in _cli or "pre_tool_use" in _cli:\n'
        '            event = "pre_tool_use"\n'
        '        else:\n'
        '            event = _detect_event(payload)'
    )
    if old not in src:
        raise RuntimeError("main() 의 이벤트 감지 라인을 찾지 못했습니다.")
    return src.replace(old, new, 1)


# ============================================================
# 8. 훅 파일 패치 실행
# ============================================================
def patch_hook_file(path: str) -> bool:
    print("\n" + "=" * 70)
    print(f"[HOOK] {path}")
    print("=" * 70)

    if not os.path.exists(path):
        log(False, f"파일 없음: {path}")
        return False

    original = read_text(path)
    src = original

    steps = [
        ("PATCH-1 UTF-8 reconfigure", patch_utf8_streams),
        ("PATCH-2 emit() 헬퍼", patch_emit_helper),
        ("PATCH-2 ensure_ascii=True", patch_ensure_ascii),
        ("PATCH-3 injectSteps 반환", patch_pre_invocation),
        ("PATCH-4 PreToolUse 즉시 deny", patch_pre_tool_use),
        ("PATCH-5 --event CLI 인자", patch_cli_event),
    ]

    for name, fn in steps:
        try:
            before = src
            src = fn(src)
            log(True, f"{name} — {'적용' if src != before else '이미 적용됨(스킵)'}")
        except Exception as e:
            log(False, f"{name} — 실패: {e}")
            return False

    if src == original:
        log(True, "변경 사항 없음 (이미 완전 패치됨)")
        return True

    bak = backup(path)
    log(True, f"백업 생성: {bak}")

    try:
        atomic_write(path, src)
        log(True, f"파일 기록 완료 ({len(original.splitlines())}줄 → {len(src.splitlines())}줄)")
    except Exception as e:
        log(False, f"파일 기록 실패: {e}")
        return False

    # 문법 검증
    try:
        py_compile.compile(path, doraise=True)
        log(True, "py_compile 문법 검증 통과")
    except Exception as e:
        log(False, f"py_compile 실패: {e}")
        return False

    return True


# ============================================================
# 9. hooks.json 공식 스키마 정규화
# ============================================================
def build_hooks_schema(hook_script_path: str) -> dict:
    """공식 스키마 생성 — 지정된 경로의 훅 스크립트를 사용."""
    return {
        "deepseek-gatekeeper": {
            "PreToolUse": [
                {
                    "matcher": "*",
                    "hooks": [
                        {
                            "type": "command",
                            "command": f'python "{hook_script_path}"',
                            "timeout": 15,
                        }
                    ],
                }
            ],
            "PreInvocation": [
                {
                    "type": "command",
                    "command": f'python "{hook_script_path}" --event PreInvocation',
                    "timeout": 15,
                }
            ],
        }
    }


def patch_hooks_json(path: str, hook_script_path: str) -> bool:
    print("\n" + "=" * 70)
    print(f"[HOOKS.JSON] {path}")
    print("=" * 70)

    if not os.path.exists(path):
        log(False, f"파일 없음: {path}")
        return False

    try:
        old_text = read_text(path)
        old_json = json.loads(old_text)
        log(True, f"기존 JSON 파싱 성공 (최상위 키: {list(old_json.keys())})")
    except Exception as e:
        log(False, f"기존 JSON 파싱 실패: {e}")
        return False

    schema = build_hooks_schema(hook_script_path)

    bak = backup(path)
    log(True, f"백업 생성: {bak}")

    try:
        atomic_write(path, json.dumps(schema, ensure_ascii=False, indent=2) + "\n")
        log(True, "공식 스키마로 기록 완료")
    except Exception as e:
        log(False, f"기록 실패: {e}")
        return False

    # 재파싱 검증
    try:
        check = json.loads(read_text(path))
        assert "deepseek-gatekeeper" in check
        assert "PreToolUse" in check["deepseek-gatekeeper"]
        assert "PreInvocation" in check["deepseek-gatekeeper"]
        log(True, "JSON 재파싱 및 스키마 검증 통과")
    except Exception as e:
        log(False, f"스키마 검증 실패: {e}")
        return False

    return True


# ============================================================
# 10. 메인
# ============================================================
def main() -> int:
    print("=" * 70)
    print("Gatekeeper Hook 자율 패치 시작")
    print("=" * 70)

    results = []

    # 훅 스크립트 패치
    for p in HOOK_TARGETS:
        results.append(patch_hook_file(p))

    # hooks.json 패치 (각 디렉터리의 훅 스크립트를 가리키도록)
    hooks_map = [
        (HOOKS_JSON_TARGETS[0], HOOK_TARGETS[0]),
        (HOOKS_JSON_TARGETS[1], HOOK_TARGETS[1]),
    ]
    for json_path, script_path in hooks_map:
        results.append(patch_hooks_json(json_path, script_path))

    print("\n" + "=" * 70)
    print("패치 결과 요약")
    print("=" * 70)
    ok_count = sum(1 for r in results if r)
    print(f"성공: {ok_count} / {len(results)}")

    fails = [m for ok, m in REPORT if not ok]
    if fails:
        print("\n실패 항목:")
        for f in fails:
            print("  - " + f)
        return 1

    print("\n모든 패치가 성공적으로 적용되었습니다.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
