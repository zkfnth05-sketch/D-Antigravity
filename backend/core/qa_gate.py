# -*- coding: utf-8 -*-
"""backend/core/qa_gate.py

Antigravity IDE 백엔드 코어 - QA 게이트 브릿지 모듈.

.agents/scripts/council_runtime.py 의 QAGate / QAReport / QA_PASSED / QA_FAILED 를
그대로 재사용하여, IDE 백엔드 어디서든 `from backend.core.qa_gate import ...`
한 줄로 3단계 실동작 QA 게이트를 쓸 수 있게 한다.

추가 제공:
    verify_code_snippet(code, filename, project_root) -        디스크에 없는 코드 문자열을 임시 파일로 승격시켜
        syntax - - 3단계로 사전 검열한 뒤
        임시 파일/디렉터리를 반드시 자동 삭제한다.

사용 예:
    from backend.core.qa_gate import QAGate, verify_code_snippet

    report = verify_code_snippet("def f(:\n    pass\n", "candidate.py")
    print(report.code)   # QA_FAILED
"""

import io
import os
import sys
import tempfile

__all__ = [
    "QAGate",
    "QAReport",
    "QA_PASSED",
    "QA_FAILED",
    "verify_code_snippet",
    "SCRIPTS_DIR",
    "ROOT",
]

# ============================================================
# 0. council_runtime 브릿지
# ============================================================

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
SCRIPTS_DIR = os.path.join(ROOT, ".agents", "scripts")

if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

from council_runtime import QAGate, QAReport, QA_PASSED, QA_FAILED  # noqa: E402


# ============================================================
# 1. 코드 스니펫 사전 검열 (디스크 미저장 코드 대상)
# ============================================================

def verify_code_snippet(code: str, filename: str = "temp_verify.py", project_root: str = None) -> QAReport:
    """메모리상의 코드 문자열을 임시 파일로 승격시켜 QA 게이트를 통과시킨다.

    Args:
        code: 검열할 파이썬 소스 코드 문자열.
        filename: 임시 파일명 (확장자 .py 권장).
        project_root: QA 게이트 기준 루트. 미지정 시 이 모듈 기준 ROOT.

    Returns:
        QAReport: code 가 QA_PASSED 또는 QA_FAILED 인 리포트 객체.

    Note:
        임시 디렉터리와 임시 파일은 finally 블록에서 반드시 정리된다.
        run_tests=False 로 호출하여 pytest 전량 실행은 생략한다.
    """
    root = project_root or ROOT
    gate = QAGate(project_root=root)
    temp_dir = tempfile.mkdtemp(prefix="qa_snippet_")
    path = os.path.join(temp_dir, filename)
    try:
        with io.open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(code)
        return gate.run([path], run_tests=False)
    finally:
        try:
            if os.path.isfile(path):
                os.remove(path)
            if os.path.isdir(temp_dir):
                os.rmdir(temp_dir)
        except OSError:
            pass
