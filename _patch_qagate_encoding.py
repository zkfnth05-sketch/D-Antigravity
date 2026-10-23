#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
================================================================================
  QAGate 3건 결함 정밀 수정 패처 (SEARCH/REPLACE 규격)
================================================================================
대상: .agents/scripts/council_runtime.py

수정 항목:
  1) check_forbidden  — FORBIDDEN_PATTERNS 정의 구간 / 주석 / docstring /
                        문자열 리터럴 자기참조 오탐 원천 차단
  2) check_import     — subprocess.run text=True 제거 → bytes 수신 후
                        utf-8 → cp949 → euc-kr 다중 디코딩
  3) _run_test_file   — unittest / pytest 두 곳 동일 처방

설계 원칙:
  - text=True 를 완전히 제거하고 bytes 로 받은 뒤 _decode_bytes() 로 해석한다.
  - _decode_bytes() 는 QAGate 클래스 메서드로 신설 (헬퍼 단일화).
  - 각 SEARCH 블록은 '원문 그대로' 일치할 때만 치환한다 (fail-closed).
================================================================================
"""

import io
import os
import sys

TARGET = os.path.join(".agents", "scripts", "council_runtime.py")

# ============================================================
# SEARCH / REPLACE 쌍 정의
# ============================================================

PATCHES = []

# ------------------------------------------------------------
# [P1] _decode_bytes 헬퍼 신설 + check_forbidden 오탐 차단
# ------------------------------------------------------------
PATCHES.append((
    "P1: _decode_bytes 헬퍼 신설 + check_forbidden 자기참조 오탐 차단",
    '''    # ---------- (2) 금지 패턴 ----------
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
''',
    '''    # ---------- 서브프로세스 출력 다중 디코딩 헬퍼 (신규) ----------
    # Windows 기본 로캘(cp949) 환경에서 UTF-8 한국어 출력을 text=True 로 받으면
    # UnicodeDecodeError 로 리더 스레드가 폭사하여 stdout 이 통째로 유실된다.
    # 따라서 bytes 로 수신한 뒤 아래 순서로 관대하게 해석한다.
    DECODE_ENCODINGS = ("utf-8", "cp949", "euc-kr")

    @classmethod
    def _decode_bytes(cls, raw):
        """
        서브프로세스가 뱉은 bytes 를 utf-8 → cp949 → euc-kr 순으로 해석한다.

        - BOM 이 있으면 utf-8-sig 로 우선 처리.
        - 어떤 인코딩으로도 실패하면 errors="replace" 로 손실 최소화 후 반환.
        - None / 빈 값은 빈 문자열로 정규화 (호출부 None 체크 불필요).
        """
        if raw is None:
            return ""
        if isinstance(raw, str):
            return raw
        if not raw:
            return ""
        if raw.startswith(b"\\xef\\xbb\\xbf"):
            try:
                return raw.decode("utf-8-sig")
            except Exception:
                pass
        for enc in cls.DECODE_ENCODINGS:
            try:
                return raw.decode(enc)
            except (UnicodeDecodeError, LookupError):
                continue
        return raw.decode("utf-8", errors="replace")

    # ---------- (2) 금지 패턴 ----------
    def _forbidden_scan_skip_lines(self, lines):
        """
        금지 패턴 스캔에서 제외할 라인 번호 집합을 계산한다 (자기참조 오탐 차단).

        제외 대상:
          1) FORBIDDEN_PATTERNS 상수 정의 블록 (패턴 문자열 자체가 담긴 구간)
          2) IMPORT_ERROR_NAMES 등 '_PATTERNS' / 'FORBIDDEN' 을 포함한 상수 정의 블록
          3) 삼중 따옴표 docstring 블록 전체
          4) 주석(#) 및 문자열 리터럴만으로 구성된 라인
        """
        skip = set()
        total = len(lines)

        # (1)(2) 상수 정의 블록 탐지 — 'FORBIDDEN_PATTERNS' 또는 '_PATTERNS =' 시작
        i = 0
        while i < total:
            text = lines[i]
            if re.search(r"^\\s*(FORBIDDEN_PATTERNS|_FORBIDDEN_PATTERNS|"
                         r"[A-Z_]*PATTERNS)\\s*=", text):
                # 여는 괄호/대괄호 균형이 맞을 때까지 블록으로 간주
                depth = 0
                opened = False
                j = i
                while j < total:
                    seg = lines[j]
                    depth += seg.count("(") + seg.count("[") + seg.count("{")
                    depth -= seg.count(")") + seg.count("]") + seg.count("}")
                    if seg.count("(") or seg.count("[") or seg.count("{"):
                        opened = True
                    skip.add(j + 1)
                    if opened and depth <= 0:
                        break
                    j += 1
                i = j + 1
                continue
            i += 1

        # (3) 삼중 따옴표 docstring 블록 탐지
        in_doc = False
        quote = ""
        for idx, text in enumerate(lines, start=1):
            if not in_doc:
                m = re.search(r'("""|\\'\\'\\')', text)
                if m:
                    quote = m.group(1)
                    # 같은 라인에서 닫히는지 확인
                    rest = text[m.end():]
                    if quote in rest:
                        skip.add(idx)
                        continue
                    in_doc = True
                    skip.add(idx)
                continue
            skip.add(idx)
            if quote in text:
                in_doc = False
                quote = ""

        # (4) 주석 / 문자열 리터럴 단독 라인
        for idx, text in enumerate(lines, start=1):
            stripped = text.strip()
            if not stripped:
                continue
            if stripped.startswith("#"):
                skip.add(idx)
                continue
            # 라인 전체가 문자열 리터럴 하나인 경우 (패턴 테이블의 원소 형태)
            if re.fullmatch(r'[rbuRBU]?([\\'"]).*\\1\\s*,?', stripped):
                skip.add(idx)
        return skip

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

        skip = self._forbidden_scan_skip_lines(lines)

        hits = []
        for idx, line in enumerate(lines, start=1):
            if idx in skip:
                continue
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
''',
))

# ------------------------------------------------------------
# [P2] check_import — text=True 제거 + 다중 디코딩
# ------------------------------------------------------------
PATCHES.append((
    "P2: check_import text=True 제거 + 다중 디코딩 적용",
    '''            proc = subprocess.run(
                [sys.executable, "-X", "utf8", probe_path],
                cwd=self.project_root, capture_output=True, text=True,
                timeout=timeout, env=env,
            )
            stdout = proc.stdout or ""
            stderr = proc.stderr or ""
''',
    '''            proc = subprocess.run(
                [sys.executable, "-X", "utf8", probe_path],
                cwd=self.project_root, capture_output=True,
                timeout=timeout, env=env,
            )
            # bytes 수신 → utf-8/cp949/euc-kr 다중 디코딩 (cp949 로캘 붕괴 차단)
            stdout = self._decode_bytes(proc.stdout)
            stderr = self._decode_bytes(proc.stderr)
''',
))

# ------------------------------------------------------------
# [P3] _run_test_file — unittest text=True 제거 + 다중 디코딩
# ------------------------------------------------------------
PATCHES.append((
    "P3: _run_test_file(unittest) text=True 제거 + 다중 디코딩 적용",
    '''            proc = subprocess.run(
                [sys.executable, "-X", "utf8", "-m", "unittest", "-v", test_path],
                cwd=self.project_root, capture_output=True, text=True,
                timeout=timeout_sec, env=env,
            )
            out = ((proc.stdout or "") + (proc.stderr or "")).strip()
''',
    '''            proc = subprocess.run(
                [sys.executable, "-X", "utf8", "-m", "unittest", "-v", test_path],
                cwd=self.project_root, capture_output=True,
                timeout=timeout_sec, env=env,
            )
            out = (self._decode_bytes(proc.stdout) +
                   self._decode_bytes(proc.stderr)).strip()
''',
))

# ------------------------------------------------------------
# [P4] _run_test_file — pytest text=True 제거 + 다중 디코딩
# ------------------------------------------------------------
PATCHES.append((
    "P4: _run_test_file(pytest) text=True 제거 + 다중 디코딩 적용",
    '''            proc = subprocess.run(
                [sys.executable, "-X", "utf8", "-m", "pytest", "-q", "--maxfail=1", test_path],
                cwd=self.project_root, capture_output=True, text=True,
                timeout=timeout_sec, env=env,
            )
            out = ((proc.stdout or "") + (proc.stderr or "")).strip()
''',
    '''            proc = subprocess.run(
                [sys.executable, "-X", "utf8", "-m", "pytest", "-q", "--maxfail=1", test_path],
                cwd=self.project_root, capture_output=True,
                timeout=timeout_sec, env=env,
            )
            out = (self._decode_bytes(proc.stdout) +
                   self._decode_bytes(proc.stderr)).strip()
''',
))


def main():
    if not os.path.isfile(TARGET):
        print("[FAIL] 대상 파일 없음: " + TARGET)
        return 1

    with io.open(TARGET, "r", encoding="utf-8", newline="") as f:
        source = f.read()

    original_len = len(source)
    applied = 0
    failed = []

    for name, search, replace in PATCHES:
        count = source.count(search)
        if count == 0:
            failed.append(name + " — SEARCH 블록 미발견")
            print("[FAIL] " + name + " — SEARCH 블록 미발견")
            continue
        if count > 1:
            failed.append(name + " — SEARCH 블록 " + str(count) + "회 중복")
            print("[FAIL] " + name + " — SEARCH 블록 " + str(count) + "회 중복")
            continue
        source = source.replace(search, replace, 1)
        applied += 1
        print("[ OK ] " + name)

    if failed:
        print("-" * 68)
        print("[ABORT] " + str(len(failed)) + "건 실패 → 파일 미기록 (fail-closed)")
        for f in failed:
            print("        · " + f)
        return 1

    with io.open(TARGET, "w", encoding="utf-8", newline="") as f:
        f.write(source)

    print("-" * 68)
    print("  치환 완료: " + str(applied) + "/" + str(len(PATCHES)) + "건")
    print("  파일 크기: " + str(original_len) + " -> " + str(len(source)) + " bytes")
    print("  줄 수    : " + str(len(source.splitlines())) + "줄")
    return 0


if __name__ == "__main__":
    sys.exit(main())
