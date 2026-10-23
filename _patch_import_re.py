# -*- coding: utf-8 -*-
"""
_patch_import_re.py

project_manager.py 에 `import re` 를 추가한다.
apply_search_replace 가 re.compile / re.sub 를 사용하므로 필수.

절차: 멱등성 검사 → 백업 → 정밀 치환 → 원자 저장 → py_compile → 지표 실측
"""

import io
import os
import sys
import shutil
import py_compile

TARGET = os.path.join("backend", "core", "project_manager.py")
BACKUP = TARGET + ".bak_import_re"

OLD = "import os\nimport json\nimport logging\n"
NEW = "import os\nimport re\nimport json\nimport logging\n"


def main() -> int:
    print("=" * 70)
    with io.open(TARGET, "r", encoding="utf-8") as f:
        src = f.read()
    before_lines = len(src.splitlines())
    print("[1] 원본 줄 수:", before_lines)

    print("[2] 멱등성 검사")
    if "\nimport re\n" in src:
        print("    ⚠ 이미 import re 존재 — 중단")
        return 0
    print("    ✓ 미존재 확인")

    print("[3] 앵커 일치 검사")
    cnt = src.count(OLD)
    print("    앵커 등장 횟수:", cnt)
    if cnt != 1:
        print("    ✗ 앵커가 정확히 1회가 아님 — 중단")
        return 1

    print("[4] 백업:", BACKUP)
    shutil.copy2(TARGET, BACKUP)
    print("    ✓ 백업 완료")

    print("[5] import re 삽입")
    new_src = src.replace(OLD, NEW, 1)
    after_lines = len(new_src.splitlines())
    print("    ✓ %d줄 → %d줄 (Δ+%d)" % (before_lines, after_lines, after_lines - before_lines))

    print("[6] 원자적 저장")
    tmp = TARGET + ".tmp_import_re"
    with io.open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(new_src)
    os.replace(tmp, TARGET)
    print("    ✓ 저장 완료")

    print("[7] py_compile 검증")
    try:
        py_compile.compile(TARGET, doraise=True)
        print("    ✓ COMPILE_OK")
    except py_compile.PyCompileError as e:
        print("    ✗ COMPILE_FAIL:", e)
        return 1

    print("[8] 지표 실측")
    with io.open(TARGET, "r", encoding="utf-8") as f:
        final = f.read()
    print("    최종 줄 수      :", len(final.splitlines()))
    print("    import re       :", final.count("\nimport re\n"))
    print("    re.compile 사용 :", final.count("re.compile"))
    print("    re.sub 사용     :", final.count("re.sub"))
    print("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
