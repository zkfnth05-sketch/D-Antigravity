# -*- coding: utf-8 -*-
"""
================================================================================
 DeepSeek 5인 협업체 거버넌스 전역 배포 엔진 (deploy_council_global.py)
================================================================================
 목적:
   1) 글로벌 전역(~/.gemini/config) 및 ~/.gemini/hooks.json 에
      gatekeeper_hook.py / hooks.json / AGENTS.md / JOINT_COUNCIL_SPEC.md /
      01-joint-council-governance.md 를 완전 멱등 동기화(Idempotent Sync).
   2) 모든 대상 워크스페이스(한국 마케팅봇, 한국 마케팅봇_Sandbox 등)에
      .agents/ 하위 구조로 동일 자산을 멱등 동기화.
   3) 배포 후 py_compile / JSON 파싱 / 거버넌스 필수조항 검증을 수행하고,
      단 하나라도 실패하면 fail-closed 로 즉시 배포 중단(DeployAbort).

 설계 원칙:
   - 원자적 쓰기(atomic_write): 임시파일 -> os.replace 로 크래시 내성 확보.
   - 해시 기반 멱등성: SHA-256 동일 시 [SKIP], 상이 시 [SYNC].
   - 이원 스키마 대응: 전역 hooks.json 과 워크스페이스 hooks.json 은
     스키마/경로/타임아웃이 다르므로 각각 독립 생성한다.
   - 백업(.bak) 보존: 기존 파일은 덮어쓰기 전 백업.
   - fail-closed: 검증 실패 시 예외로 중단, 부분 배포 방지.

 [긴급 수정 이력 — DeepSeek-V3 선임 엔지니어]
   - FIX-1: build_workspace_hooks() 의 matcher 를 쓰기/실행 도구 하드코딩
            ('write_to_file|replace_file_content|multi_replace_file_content|run_command')
            에서 '*' 로 전면 확장. view_file / read_file / list_dir / grep_search 등
            모든 읽기·조사 도구까지 100% 포섭하여 훅 미동작(사각지대) 원천 제거.
   - FIX-2: build_workspace_hooks() 에 PreInvocation 훅 그룹 신설 등록
            (전역 스키마와 동일한 이벤트 커버리지 확보).
   - FIX-3: 글로벌 훅 경로를 ~/.gemini/config/hooks.json 및 ~/.gemini/hooks.json
            양쪽에 동일 페이로드로 동기화(이중 경로 정합성 보장).
   - FIX-4: 배포 후 hooks.json 스키마 실측 검증(verify_hooks_schema) 추가 —
            matcher '*' 및 PreInvocation/PreToolUse 이벤트 존재를 fail-closed 로 확인.
   - FIX-5: EXCLUDE_PATTERNS 에 개발 부산물(_patch_*, _append_part*, .b64, .tmp,
            .bak2) 추가 — 패치용 임시 스크립트가 워크스페이스로 유출되는 것을 차단.
            (거버넌스 자산만 배포한다는 원칙 준수)

 작성: DeepSeek-V3 선임 구현 엔지니어 (독점 집필)
================================================================================
"""

import hashlib
import json
import os
import py_compile
import shutil
import sys
import tempfile
import traceback
from datetime import datetime

# ============================================================
# 0. 상수 / 경로 설정
# ============================================================

BASE_DIR = r"c:\Users\zkfnt\Desktop\aider-main"

# 원본(Source of Truth) 자산 경로
SRC_AGENTS_MD = os.path.join(BASE_DIR, "AGENTS.md")
SRC_SPEC_MD = os.path.join(BASE_DIR, "JOINT_COUNCIL_SPEC.md")
SRC_RULE_MD = os.path.join(BASE_DIR, ".agents", "rules", "01-joint-council-governance.md")
SRC_HOOKS_JSON = os.path.join(BASE_DIR, ".agents", "hooks.json")
SRC_GATEKEEPER = os.path.join(BASE_DIR, ".agents", "scripts", "gatekeeper_hook.py")

# 글로벌 전역 경로
GLOBAL_GEMINI_DIR = os.path.expanduser(r"~\.gemini")
GLOBAL_CONFIG_DIR = os.path.join(GLOBAL_GEMINI_DIR, "config")
GLOBAL_HOOKS_JSON = os.path.join(GLOBAL_GEMINI_DIR, "hooks.json")
GLOBAL_CONFIG_HOOKS_JSON = os.path.join(GLOBAL_CONFIG_DIR, "hooks.json")
GLOBAL_GEMINI_MD = os.path.join(GLOBAL_CONFIG_DIR, "GEMINI.md")

# 대상 워크스페이스 목록
TARGET_WORKSPACES = [
    r"C:\Users\zkfnt\Desktop\한국 마케팅봇",
    r"C:\Users\zkfnt\Desktop\한국 마케팅봇_Sandbox",
]

# 동기화 제외 패턴 (런타임 산출물 + 개발 부산물)
# [FIX-5] _patch_* / _append_part* / .b64 / .tmp / .bak2 는 거버넌스 자산이 아닌
#         패치 작업용 임시 스크립트이므로 워크스페이스 배포 대상에서 원천 제외한다.
EXCLUDE_PATTERNS = (
    "__pycache__",
    ".pyc",
    ".pyo",
    ".bak",
    ".bak2",
    ".tmp",
    ".b64",
    "_patch_",
    "_append_part",
    "gatekeeper_audit.log",
    "token_store.json",
    "session_store.json",
    "gatekeeper_health.json",
)

# 거버넌스 문서 필수 조항 (gatekeeper_hook.py load_governance 와 동일 기준)
REQUIRED_GOVERNANCE_CLAUSES = ["제미나이", "딥시크", "물리적 차단", "단방향 파이프라인"]

# GEMINI.md 제7조 병합 마커
GEMINI_SECTION_MARKER = "6인 합동 회의체"

# 훅 이벤트 상수 (전역/워크스페이스 공통 커버리지)
HOOK_EVENTS = ("PreInvocation", "PreToolUse")

# 모든 도구를 포섭하는 만능 matcher
MATCH_ALL = "*"

GEMINI_COUNCIL_SECTION = """

## 7. 🏛️ [6인 합동 회의체 (제미나이 1인 + 딥시크 5인체제) 상시 작동 거버넌스]
* 본 시스템은 **[제미나이 토론위원 + 딥시크 5인 협업체]**의 6인 분업 시스템 표준 규약에 의해 상시 통제됩니다.
* **제미나이 (Gemini)**: 사용자 요구사항 브리핑, 아이디어 발제, 대안 제시 (코드 단독 수정 및 무단 배포 금지).
* **DeepSeek-R1 (수석설계 아키텍트)**: 근본 원인 분석 및 무결점 모듈형 아키텍처 설계.
* **DeepSeek-V3 (비판적 감사검토관)**: 보안 위험 및 땜질 코딩(try-except pass 등) 전수 감사.
* **DeepSeek-V3 (선임 구현 엔지니어)**: 승인된 설계에 따른 **실제 운영 소스코드 100% 독점 집필**.
* **QA 샌드박스 검증관**: `py_compile` 구문 무결점 검증 (구문 오류 0건 필수).
* **Git Diff 안전관리관**: 인라인 Diff 대조 및 사용자 승인 대기.
* Antigravity IDE에서 열리는 모든 파일과 작업은 딥시크 5인 협업체와 상의 및 심의를 거쳐 안전하게 수행되어야 합니다.
* **물리적 차단**: 게이트키퍼 훅이 PreToolUse 단계에서 쓰기/실행 도구를 물리적으로 차단합니다.
* **단방향 파이프라인**: 제미나이(발제) -> 딥시크(설계/감사/구현) -> QA(검증) -> 승인 순으로만 흐릅니다.
"""


# ============================================================
# 1. 예외 / 로깅
# ============================================================

class DeployAbort(Exception):
    """fail-closed 배포 중단 예외."""
    pass


class Reporter:
    """배포 진행 상황 카운터 및 로깅."""

    def __init__(self):
        self.synced = 0
        self.skipped = 0
        self.created = 0
        self.backed_up = 0
        self.verified = 0
        self.errors = []

    def log(self, msg):
        print(msg, flush=True)

    def summary(self):
        self.log("")
        self.log("=" * 72)
        self.log(" 배포 결과 요약")
        self.log("=" * 72)
        self.log(f"  [SYNC]  갱신된 파일      : {self.synced} 건")
        self.log(f"  [SKIP]  변경 없음(멱등)  : {self.skipped} 건")
        self.log(f"  [NEW]   신규 생성        : {self.created} 건")
        self.log(f"  [BAK]   백업 보존        : {self.backed_up} 건")
        self.log(f"  [OK]    검증 통과        : {self.verified} 건")
        self.log(f"  [ERR]   오류             : {len(self.errors)} 건")
        self.log("=" * 72)


REPORT = Reporter()


# ============================================================
# 2. 유틸리티 — 해시 / 원자적 쓰기 / 백업
# ============================================================

def file_sha256(path):
    """파일의 SHA-256 해시 반환. 파일 부재 시 None."""
    if not os.path.isfile(path):
        return None
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def is_excluded(name):
    """런타임 산출물 및 개발 부산물 제외 판정."""
    return any(pat in name for pat in EXCLUDE_PATTERNS)


def backup(path):
    """기존 파일을 .bak 으로 백업 (내용 상이 시에만)."""
    if not os.path.isfile(path):
        return False
    bak = path + ".bak"
    try:
        if os.path.isfile(bak) and file_sha256(bak) == file_sha256(path):
            return False
        shutil.copy2(path, bak)
        REPORT.backed_up += 1
        return True
    except OSError as e:
        REPORT.log(f"    [!] 백업 실패(무시하고 진행): {path} ({e})")
        return False


def atomic_write(path, data, encoding="utf-8"):
    """임시파일 -> os.replace 원자적 쓰기. 크래시 내성 확보."""
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=parent or ".", prefix=".deploy_", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            if isinstance(data, str):
                f.write(data.encode(encoding))
            else:
                f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
        raise


def sync_bytes(dest, data, label):
    """바이트/문자열 데이터를 멱등 동기화."""
    payload = data.encode("utf-8") if isinstance(data, str) else data
    new_hash = hashlib.sha256(payload).hexdigest()
    old_hash = file_sha256(dest)

    if old_hash == new_hash:
        REPORT.skipped += 1
        REPORT.log(f"    [SKIP] {label} (해시 동일)")
        return "skip"

    existed = old_hash is not None
    if existed:
        backup(dest)
    atomic_write(dest, payload)

    if existed:
        REPORT.synced += 1
        REPORT.log(f"    [SYNC] {label}")
        return "sync"
    else:
        REPORT.created += 1
        REPORT.log(f"    [NEW ] {label}")
        return "new"


def sync_file(src, dest, label):
    """원본 파일 -> 대상 파일 멱등 동기화."""
    if not os.path.isfile(src):
        raise DeployAbort(f"원본 자산 부재: {src}")
    with open(src, "rb") as f:
        data = f.read()
    return sync_bytes(dest, data, label)


def sync_tree(src_dir, dest_dir, label_prefix):
    """디렉터리 재귀 멱등 동기화 (제외 패턴 적용)."""
    if not os.path.isdir(src_dir):
        raise DeployAbort(f"원본 디렉터리 부재: {src_dir}")
    os.makedirs(dest_dir, exist_ok=True)
    count = 0
    for root, dirs, files in os.walk(src_dir):
        dirs[:] = [d for d in dirs if not is_excluded(d)]
        rel = os.path.relpath(root, src_dir)
        target_root = dest_dir if rel == "." else os.path.join(dest_dir, rel)
        os.makedirs(target_root, exist_ok=True)
        for name in files:
            if is_excluded(name):
                continue
            s = os.path.join(root, name)
            d = os.path.join(target_root, name)
            rel_label = os.path.join(label_prefix, name) if rel == "." else os.path.join(label_prefix, rel, name)
            sync_file(s, d, rel_label)
            count += 1
    return count


# ============================================================
# 3. 검증기 — py_compile / JSON / 거버넌스 조항 / 훅 스키마
# ============================================================

def verify_python(path, label):
    """py_compile 구문 무결점 검증. 실패 시 DeployAbort."""
    if not os.path.isfile(path):
        raise DeployAbort(f"[검증실패] 파이썬 파일 부재: {path}")
    try:
        py_compile.compile(path, doraise=True, cfile=os.path.join(
            tempfile.gettempdir(), "deploy_verify_" + os.path.basename(path) + "c"))
    except py_compile.PyCompileError as e:
        raise DeployAbort(f"[검증실패] py_compile 구문 오류 ({label}): {e}")
    except Exception as e:
        raise DeployAbort(f"[검증실패] py_compile 예외 ({label}): {e}")
    REPORT.verified += 1
    REPORT.log(f"    [OK  ] py_compile 통과: {label}")
    return True


def verify_json(path, label):
    """JSON 파싱 검증. 실패 시 DeployAbort."""
    if not os.path.isfile(path):
        raise DeployAbort(f"[검증실패] JSON 파일 부재: {path}")
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        raise DeployAbort(f"[검증실패] JSON 파싱 오류 ({label}): {e}")
    except Exception as e:
        raise DeployAbort(f"[검증실패] JSON 읽기 예외 ({label}): {e}")
    if not isinstance(data, dict) or not data:
        raise DeployAbort(f"[검증실패] JSON 구조 이상(빈 객체/비객체) ({label})")
    REPORT.verified += 1
    REPORT.log(f"    [OK  ] JSON 유효: {label}")
    return data


def _iter_hook_groups(node):
    """hooks.json 트리에서 hook 그룹(dict)들을 재귀 수집."""
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from _iter_hook_groups(v)
    elif isinstance(node, list):
        for v in node:
            yield from _iter_hook_groups(v)


def verify_hooks_schema(path, label, require_events=HOOK_EVENTS, require_match_all=True):
    """
    hooks.json 스키마 실측 검증 (fail-closed).
    - require_events: 반드시 존재해야 하는 이벤트 키 목록
    - require_match_all: matcher 가 '*' 인 그룹이 최소 1개 이상 존재해야 함
    """
    data = verify_json(path, label)
    text = json.dumps(data, ensure_ascii=False)

    missing_events = [ev for ev in require_events if ev not in text]
    if missing_events:
        raise DeployAbort(f"[검증실패] 훅 이벤트 누락 ({label}): {missing_events}")

    if require_match_all:
        has_match_all = any(
            isinstance(g, dict) and g.get("matcher") == MATCH_ALL
            for g in _iter_hook_groups(data)
        )
        if not has_match_all:
            raise DeployAbort(f"[검증실패] matcher '*' 그룹 부재 — 읽기 도구 사각지대 ({label})")

    REPORT.verified += 1
    REPORT.log(f"    [OK  ] 훅 스키마 검증(matcher '*', {','.join(require_events)}): {label}")
    return data


def verify_governance(path, label):
    """거버넌스 문서 필수 조항 4종 존재 검증."""
    if not os.path.isfile(path):
        raise DeployAbort(f"[검증실패] 거버넌스 문서 부재: {path}")
    try:
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
    except Exception as e:
        raise DeployAbort(f"[검증실패] 거버넌스 읽기 예외 ({label}): {e}")
    missing = [k for k in REQUIRED_GOVERNANCE_CLAUSES if k not in text]
    if missing:
        raise DeployAbort(f"[검증실패] 거버넌스 필수조항 누락 ({label}): {missing}")
    REPORT.verified += 1
    REPORT.log(f"    [OK  ] 거버넌스 조항 4종 완비: {label}")
    return True


def verify_markdown(path, label, must_contain=None):
    """마크다운 문서 존재 + 필수 문자열 검증."""
    if not os.path.isfile(path):
        raise DeployAbort(f"[검증실패] 문서 부재: {path}")
    try:
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
    except Exception as e:
        raise DeployAbort(f"[검증실패] 문서 읽기 예외 ({label}): {e}")
    if len(text.strip()) < 10:
        raise DeployAbort(f"[검증실패] 문서 내용 비어있음 ({label})")
    if must_contain:
        for token in must_contain:
            if token not in text:
                raise DeployAbort(f"[검증실패] 문서 필수 문자열 누락 ({label}): {token}")
    REPORT.verified += 1
    REPORT.log(f"    [OK  ] 문서 유효: {label}")
    return True


# ============================================================
# 4. hooks.json 스키마 빌더 (이원 스키마 대응)
# ============================================================

def build_global_hooks():
    """
    글로벌 전역(~/.gemini/hooks.json, ~/.gemini/config/hooks.json) 스키마.
    - 절대경로 사용 (워크스페이스 무관 전역 실행)
    - timeout 15000 (ms)
    - PreInvocation + PreToolUse(matcher:"*") 모두 등록
    """
    gatekeeper_abs = os.path.join(GLOBAL_CONFIG_DIR, "gatekeeper_hook.py")
    return {
        "hooks": {
            "PreInvocation": [
                {
                    "matcher": MATCH_ALL,
                    "hooks": [
                        {
                            "type": "command",
                            "command": f'python "{gatekeeper_abs}" --event PreInvocation',
                            "timeout": 15000,
                        }
                    ],
                }
            ],
            "PreToolUse": [
                {
                    "matcher": MATCH_ALL,
                    "hooks": [
                        {
                            "type": "command",
                            "command": f'python "{gatekeeper_abs}"',
                            "timeout": 15000,
                        }
                    ],
                }
            ],
        }
    }


def build_workspace_hooks():
    """
    워크스페이스(.agents/hooks.json) 스키마.
    - 상대경로 사용 (워크스페이스 루트 기준)
    - timeout 10 (초)
    - named hook 그룹 스키마
    - [FIX-1] matcher 를 '*' 로 전면 확장:
              view_file / read_file / list_dir / grep_search 등
              모든 읽기·조사 도구까지 100% 포섭 (사각지대 제거)
    - [FIX-2] PreInvocation 훅 그룹 신설 등록 (전역과 동일 이벤트 커버리지)
    """
    return {
        "deepseek-council-gatekeeper": {
            "enabled": True,
            "PreInvocation": [
                {
                    "matcher": MATCH_ALL,
                    "hooks": [
                        {
                            "type": "command",
                            "command": "python .agents/scripts/gatekeeper_hook.py --event PreInvocation",
                            "timeout": 10,
                        }
                    ],
                }
            ],
            "PreToolUse": [
                {
                    "matcher": MATCH_ALL,
                    "hooks": [
                        {
                            "type": "command",
                            "command": "python .agents/scripts/gatekeeper_hook.py",
                            "timeout": 10,
                        }
                    ],
                }
            ],
        }
    }


def dump_json(obj):
    """일관된 포맷의 JSON 문자열 생성."""
    return json.dumps(obj, ensure_ascii=False, indent=2) + "\n"


# ============================================================
# 5. GEMINI.md 제7조 멱등 병합
# ============================================================

def merge_gemini_section(path):
    """GEMINI.md 에 제7조(6인 합동 회의체) 멱등 병합."""
    if not os.path.isfile(path):
        # 파일 자체가 없으면 헤더와 함께 신규 생성
        content = "# GEMINI.md — 전역 거버넌스\n" + GEMINI_COUNCIL_SECTION
        sync_bytes(path, content, "Global GEMINI.md (신규 생성)")
        return "new"

    with open(path, "r", encoding="utf-8") as f:
        current = f.read()

    if GEMINI_SECTION_MARKER in current:
        REPORT.skipped += 1
        REPORT.log("    [SKIP] Global GEMINI.md 제7조 이미 존재")
        return "skip"

    merged = current.rstrip() + "\n" + GEMINI_COUNCIL_SECTION
    sync_bytes(path, merged, "Global GEMINI.md 제7조 병합")
    return "sync"


# ============================================================
# 6. 배포 단계 — 글로벌 전역
# ============================================================

def deploy_global():
    """1단계: 글로벌 전역(~/.gemini/config, ~/.gemini/hooks.json) 배포."""
    REPORT.log("")
    REPORT.log("=" * 72)
    REPORT.log(f"[1단계] 글로벌 전역 거버넌스 승격 -> {GLOBAL_CONFIG_DIR}")
    REPORT.log("=" * 72)

    os.makedirs(GLOBAL_CONFIG_DIR, exist_ok=True)
    os.makedirs(GLOBAL_GEMINI_DIR, exist_ok=True)

    # 1-1. 문서 자산
    sync_file(SRC_AGENTS_MD, os.path.join(GLOBAL_CONFIG_DIR, "AGENTS.md"), "Global AGENTS.md")
    sync_file(SRC_SPEC_MD, os.path.join(GLOBAL_CONFIG_DIR, "JOINT_COUNCIL_SPEC.md"), "Global JOINT_COUNCIL_SPEC.md")

    # 1-2. 거버넌스 규칙 (게이트키퍼가 참조하는 경로와 정합)
    global_rules_dir = os.path.join(GLOBAL_CONFIG_DIR, "rules")
    os.makedirs(global_rules_dir, exist_ok=True)
    sync_file(SRC_RULE_MD, os.path.join(global_rules_dir, "01-joint-council-governance.md"),
              "Global rules/01-joint-council-governance.md")

    # 1-3. 게이트키퍼 스크립트
    global_gatekeeper = os.path.join(GLOBAL_CONFIG_DIR, "gatekeeper_hook.py")
    sync_file(SRC_GATEKEEPER, global_gatekeeper, "Global gatekeeper_hook.py")

    # 1-4. 전역 hooks.json — [FIX-3] 이중 경로 동시 동기화
    #      ~/.gemini/hooks.json  +  ~/.gemini/config/hooks.json (동일 페이로드)
    global_hooks_payload = dump_json(build_global_hooks())
    sync_bytes(GLOBAL_HOOKS_JSON, global_hooks_payload, "Global ~/.gemini/hooks.json")
    sync_bytes(GLOBAL_CONFIG_HOOKS_JSON, global_hooks_payload, "Global ~/.gemini/config/hooks.json")

    # 1-5. GEMINI.md 제7조 병합
    merge_gemini_section(GLOBAL_GEMINI_MD)

    # 1-6. 검증 (fail-closed)
    REPORT.log("  [검증] 글로벌 전역 자산 무결성 확인")
    verify_python(global_gatekeeper, "Global gatekeeper_hook.py")
    verify_hooks_schema(GLOBAL_HOOKS_JSON, "Global ~/.gemini/hooks.json")
    verify_hooks_schema(GLOBAL_CONFIG_HOOKS_JSON, "Global ~/.gemini/config/hooks.json")
    verify_governance(os.path.join(global_rules_dir, "01-joint-council-governance.md"),
                      "Global rules/01-joint-council-governance.md")
    verify_markdown(os.path.join(GLOBAL_CONFIG_DIR, "AGENTS.md"), "Global AGENTS.md")
    verify_markdown(os.path.join(GLOBAL_CONFIG_DIR, "JOINT_COUNCIL_SPEC.md"), "Global JOINT_COUNCIL_SPEC.md")
    verify_markdown(GLOBAL_GEMINI_MD, "Global GEMINI.md", must_contain=[GEMINI_SECTION_MARKER])

    # 1-7. 이중 경로 해시 정합성 최종 확인
    h1 = file_sha256(GLOBAL_HOOKS_JSON)
    h2 = file_sha256(GLOBAL_CONFIG_HOOKS_JSON)
    if h1 != h2:
        raise DeployAbort("[검증실패] 글로벌 hooks.json 이중 경로 해시 불일치")
    REPORT.verified += 1
    REPORT.log("    [OK  ] 글로벌 hooks.json 이중 경로 해시 일치")

    REPORT.log("  [+] 글로벌 전역 배포 및 검증 완료")


# ============================================================
# 7. 배포 단계 — 워크스페이스
# ============================================================

def deploy_workspace(ws):
    """2단계: 개별 워크스페이스 동기화."""
    REPORT.log("")
    REPORT.log("-" * 72)
    REPORT.log(f"[2단계] 워크스페이스 동기화 -> {ws}")
    REPORT.log("-" * 72)

    if not os.path.isdir(ws):
        REPORT.log(f"  [-] 워크스페이스 미존재 (건너뜀): {ws}")
        return False

    agents_dir = os.path.join(ws, ".agents")
    rules_dir = os.path.join(agents_dir, "rules")
    scripts_dir = os.path.join(agents_dir, "scripts")
    os.makedirs(rules_dir, exist_ok=True)
    os.makedirs(scripts_dir, exist_ok=True)

    # 2-1. 루트 문서
    sync_file(SRC_AGENTS_MD, os.path.join(ws, "AGENTS.md"), "AGENTS.md")
    sync_file(SRC_SPEC_MD, os.path.join(ws, "JOINT_COUNCIL_SPEC.md"), "JOINT_COUNCIL_SPEC.md")

    # 2-2. .agents/rules/ 재귀 동기화
    sync_tree(os.path.dirname(SRC_RULE_MD), rules_dir, ".agents/rules")

    # 2-3. .agents/scripts/ 재귀 동기화 (게이트키퍼 포함, 개발 부산물 제외)
    sync_tree(os.path.dirname(SRC_GATEKEEPER), scripts_dir, ".agents/scripts")

    # 2-4. .agents/hooks.json (워크스페이스 전용 스키마 — matcher '*', PreInvocation 포함)
    ws_hooks_path = os.path.join(agents_dir, "hooks.json")
    sync_bytes(ws_hooks_path, dump_json(build_workspace_hooks()), ".agents/hooks.json")

    # 2-5. 검증 (fail-closed)
    REPORT.log("  [검증] 워크스페이스 자산 무결성 확인")
    verify_python(os.path.join(scripts_dir, "gatekeeper_hook.py"), ".agents/scripts/gatekeeper_hook.py")
    verify_hooks_schema(ws_hooks_path, ".agents/hooks.json")
    verify_governance(os.path.join(rules_dir, "01-joint-council-governance.md"),
                      ".agents/rules/01-joint-council-governance.md")
    verify_markdown(os.path.join(ws, "AGENTS.md"), "AGENTS.md")
    verify_markdown(os.path.join(ws, "JOINT_COUNCIL_SPEC.md"), "JOINT_COUNCIL_SPEC.md")

    REPORT.log(f"  [+] {os.path.basename(ws)} 딥시크 5인체제 동기화 100% 완료")
    return True


# ============================================================
# 8. 사전 점검 — 원본 자산 존재 확인
# ============================================================

def preflight():
    """배포 전 원본 자산 5종 존재 확인. 부재 시 즉시 중단."""
    REPORT.log("=" * 72)
    REPORT.log("[0단계] 원본 자산 사전 점검 (Preflight)")
    REPORT.log("=" * 72)

    required = [
        (SRC_AGENTS_MD, "AGENTS.md"),
        (SRC_SPEC_MD, "JOINT_COUNCIL_SPEC.md"),
        (SRC_RULE_MD, ".agents/rules/01-joint-council-governance.md"),
        (SRC_HOOKS_JSON, ".agents/hooks.json"),
        (SRC_GATEKEEPER, ".agents/scripts/gatekeeper_hook.py"),
    ]
    missing = []
    for path, label in required:
        if os.path.isfile(path):
            size = os.path.getsize(path)
            REPORT.log(f"  [OK  ] {label} ({size:,} bytes)")
        else:
            REPORT.log(f"  [MISS] {label} -> {path}")
            missing.append(label)

    if missing:
        raise DeployAbort(f"원본 자산 누락으로 배포 중단: {missing}")

    # 원본 게이트키퍼 자체 구문 검증 (배포 전 fail-fast)
    REPORT.log("  [검증] 원본 게이트키퍼 py_compile 사전 확인")
    verify_python(SRC_GATEKEEPER, "원본 .agents/scripts/gatekeeper_hook.py")
    REPORT.log("  [+] 사전 점검 통과")


# ============================================================
# 9. 메인 오케스트레이터
# ============================================================

def main():
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass

    started = datetime.now()
    REPORT.log("")
    REPORT.log("#" * 72)
    REPORT.log("#  DeepSeek 5인 협업체 거버넌스 전역 배포 엔진")
    REPORT.log(f"#  시작 시각: {started.strftime('%Y-%m-%d %H:%M:%S')}")
    REPORT.log(f"#  소스 루트: {BASE_DIR}")
    REPORT.log("#" * 72)

    try:
        # 0) 사전 점검
        preflight()

        # 1) 글로벌 전역 배포
        deploy_global()

        # 2) 워크스페이스 배포
        deployed_ws = 0
        for ws in TARGET_WORKSPACES:
            if deploy_workspace(ws):
                deployed_ws += 1

        # 3) 최종 요약
        elapsed = (datetime.now() - started).total_seconds()
        REPORT.summary()
        REPORT.log(f"  소요 시간: {elapsed:.2f}초")
        REPORT.log(f"  동기화된 워크스페이스: {deployed_ws} / {len(TARGET_WORKSPACES)}")
        REPORT.log("")
        REPORT.log("[SUCCESS] 전역(Global) 및 마케팅봇 프로젝트 전체에")
        REPORT.log("          딥시크 5인체제 거버넌스 배포 100% 완료!")
        REPORT.log("#" * 72)
        return 0

    except DeployAbort as e:
        REPORT.log("")
        REPORT.log("!" * 72)
        REPORT.log(f"[FAIL-CLOSED] 배포 중단: {e}")
        REPORT.log("!" * 72)
        REPORT.errors.append(str(e))
        REPORT.summary()
        return 1

    except Exception as e:
        REPORT.log("")
        REPORT.log("!" * 72)
        REPORT.log(f"[FAIL-CLOSED] 예기치 못한 예외로 배포 중단: {e}")
        REPORT.log(traceback.format_exc())
        REPORT.log("!" * 72)
        REPORT.errors.append(str(e))
        REPORT.summary()
        return 2


if __name__ == "__main__":
    sys.exit(main())
