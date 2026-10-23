# -*- coding: utf-8 -*-
"""
test_backup_diff_rollback.py

'원본 대조 Diff 및 즉시 롤백/백업 엔진' 실측 테스트.

검증 항목:
  T1. save_file_content — 백업 생성 + unified diff 반환
  T2. _generate_unified_diff — 5000줄 파일에서 변경 몇 줄만 diff 에 등장
  T3. rollback_file — 최근 백업으로 즉시 원상 복구
  T4. 백업 10개 상한 — 12회 저장 시 오래된 것 자동 회수
  T5. apply_search_replace — diff/backup_path 반환 승계
  T6. 신규 파일 — 백업 생략(backup_path == "")
  T7. 변경 없음 — diff == ""
"""

import os
import sys
import shutil
import tempfile

sys.path.insert(0, os.path.abspath("backend"))
from core.project_manager import ProjectManager  # noqa: E402

PASS = 0
FAIL = 0


def check(label, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  [PASS] %s %s" % (label, detail))
    else:
        FAIL += 1
        print("  [FAIL] %s %s" % (label, detail))


def main():
    global PASS, FAIL

    # ---------------------------------------------------------------
    # 임시 프로젝트 생성
    # ---------------------------------------------------------------
    tmp_root = tempfile.mkdtemp(prefix="pm_backup_test_")
    proj = os.path.join(tmp_root, "TestProject")
    os.makedirs(proj, exist_ok=True)

    pm = ProjectManager(proj)
    pm.active_project = os.path.abspath(proj)
    print("=" * 72)
    print("테스트 프로젝트:", pm.active_project)
    print("백업 디렉터리 :", pm._backup_dir())
    print("=" * 72)

    # ===============================================================
    print("\n[T1] save_file_content — 백업 + unified diff")
    # ===============================================================
    f1 = os.path.join(proj, "sample.py")
    with open(f1, "w", encoding="utf-8", newline="") as f:
        f.write("def hello():\n    return 1\n")

    r1 = pm.save_file_content(f1, "def hello():\n    return 2\n")
    check("success", r1.get("success") is True)
    check("backup_path 생성됨", bool(r1.get("backup_path")), "→ %s" % os.path.basename(r1.get("backup_path", "")))
    check("backup 파일 실존", os.path.isfile(r1.get("backup_path", "")))
    check("diff 비어있지 않음", bool(r1.get("diff")))
    check("stats removed==1", r1.get("stats", {}).get("removed") == 1, "→ %s" % r1.get("stats"))
    check("stats added==1", r1.get("stats", {}).get("added") == 1, "→ %s" % r1.get("stats"))
    print("  --- diff ---")
    for line in (r1.get("diff") or "").splitlines():
        print("  | " + line)

    # 백업 내용이 '수정 전' 원본인지 확인
    with open(r1["backup_path"], "r", encoding="utf-8", newline="") as f:
        bak_content = f.read()
    check("백업=수정 전 원본", bak_content == "def hello():\n    return 1\n",
          "→ %r" % bak_content)

    # ===============================================================
    print("\n[T2] _generate_unified_diff — 5000줄 파일 국소 변경")
    # ===============================================================
    big_orig = "".join("line_%04d = %d\n" % (i, i) for i in range(1, 5001))
    big_lines = big_orig.splitlines(keepends=True)
    big_lines[2499] = "line_2500 = 999999  # CHANGED\n"
    big_new = "".join(big_lines)

    d = pm._generate_unified_diff(big_orig, big_new, "big.py")
    dlines = d.splitlines()
    check("diff 생성됨", bool(d))
    check("헤더 --- a/big.py", any(l.startswith("--- a/big.py") for l in dlines))
    check("헤더 +++ b/big.py", any(l.startswith("+++ b/big.py") for l in dlines))
    check("hunk 헤더 @@ 존재", any(l.startswith("@@") for l in dlines))
    check("변경 라인 + 포함", any("CHANGED" in l and l.startswith("+") for l in dlines))
    check("원본 라인 - 포함", any("line_2500 = 2500" in l and l.startswith("-") for l in dlines))
    check("diff 라인 수 < 20 (국소성)", len(dlines) < 20, "→ %d줄" % len(dlines))
    print("  --- 5000줄 파일 diff (총 %d줄) ---" % len(dlines))
    for line in dlines:
        print("  | " + line)

    # ===============================================================
    print("\n[T3] rollback_file — 즉시 원상 복구")
    # ===============================================================
    f3 = os.path.join(proj, "rollback_me.py")
    with open(f3, "w", encoding="utf-8", newline="") as f:
        f.write("ORIGINAL_CONTENT = True\n")

    pm.save_file_content(f3, "BROKEN_CONTENT = True\n")
    with open(f3, "r", encoding="utf-8", newline="") as f:
        broken = f.read()
    check("수정 반영됨", broken == "BROKEN_CONTENT = True\n", "→ %r" % broken)

    rb = pm.rollback_file(f3)
    check("rollback success", rb.get("success") is True)
    check("backup_path 반환", bool(rb.get("backup_path")))
    check("rollback diff 반환", bool(rb.get("diff")))
    with open(f3, "r", encoding="utf-8", newline="") as f:
        restored = f.read()
    check("원본 복원 완료", restored == "ORIGINAL_CONTENT = True\n", "→ %r" % restored)
    print("  --- rollback diff ---")
    for line in (rb.get("diff") or "").splitlines():
        print("  | " + line)

    # 백업 없는 파일 롤백 → 안전 실패
    f3b = os.path.join(proj, "no_backup.py")
    with open(f3b, "w", encoding="utf-8", newline="") as f:
        f.write("x = 1\n")
    rb2 = pm.rollback_file(f3b)
    check("백업 없으면 success=False", rb2.get("success") is False)
    check("백업 없으면 메시지 반환", bool(rb2.get("message")))

    # ===============================================================
    print("\n[T4] 백업 10개 상한 — 오래된 것 자동 회수")
    # ===============================================================
    f4 = os.path.join(proj, "prune_test.py")
    with open(f4, "w", encoding="utf-8", newline="") as f:
        f.write("v = 0\n")
    for i in range(1, 13):  # 12회 저장
        pm.save_file_content(f4, "v = %d\n" % i)

    backups = pm.list_backups(f4)
    check("백업 개수 <= 10", len(backups) <= 10, "→ %d개" % len(backups))
    check("백업 개수 == 10", len(backups) == 10, "→ %d개" % len(backups))
    # 최신 백업이 가장 최근 내용(v = 11)인지
    with open(backups[0]["path"], "r", encoding="utf-8", newline="") as f:
        newest = f.read()
    check("최신 백업 = 직전 내용", newest == "v = 11\n", "→ %r" % newest)

    # ===============================================================
    print("\n[T5] apply_search_replace — diff/backup_path 승계")
    # ===============================================================
    f5 = os.path.join(proj, "asr_test.py")
    with open(f5, "w", encoding="utf-8", newline="") as f:
        f.write("def old_function():\n    return 1\n\n\ndef keep():\n    return 0\n")

    patch = (
        "<<<<<<< SEARCH\n"
        "def old_function():\n"
        "    return 1\n"
        "=======\n"
        "def old_function():\n"
        "    return 2\n"
        ">>>>>>> REPLACE\n"
    )
    r5 = pm.apply_search_replace(f5, patch)
    check("success", r5.get("success") is True)
    check("applied==1", r5.get("applied") == 1)
    check("diff 반환됨", bool(r5.get("diff")))
    check("backup_path 반환됨", bool(r5.get("backup_path")))
    check("stats 존재", "added" in r5.get("stats", {}))
    with open(f5, "r", encoding="utf-8", newline="") as f:
        asr_content = f.read()
    check("치환 결과 정확", "return 2" in asr_content and "return 1" not in asr_content)
    print("  --- asr diff ---")
    for line in (r5.get("diff") or "").splitlines():
        print("  | " + line)

    # ===============================================================
    print("\n[T6] 신규 파일 — 백업 생략")
    # ===============================================================
    f6 = os.path.join(proj, "brand_new.py")
    r6 = pm.save_file_content(f6, "NEW = True\n")
    check("success", r6.get("success") is True)
    check("backup_path == ''", r6.get("backup_path") == "", "→ %r" % r6.get("backup_path"))
    check("diff == ''", r6.get("diff") == "", "→ %r" % r6.get("diff"))

    # ===============================================================
    print("\n[T7] 변경 없음 — diff == ''")
    # ===============================================================
    f7 = os.path.join(proj, "same.py")
    with open(f7, "w", encoding="utf-8", newline="") as f:
        f.write("SAME = 1\n")
    r7 = pm.save_file_content(f7, "SAME = 1\n")
    check("success", r7.get("success") is True)
    check("diff == ''", r7.get("diff") == "", "→ %r" % r7.get("diff"))
    check("백업은 생성됨", bool(r7.get("backup_path")))

    # ===============================================================
    print("\n" + "=" * 72)
    total = PASS + FAIL
    print("최종 결과: %d/%d PASS, %d FAIL" % (PASS, total, FAIL))
    print("=" * 72)

    shutil.rmtree(tmp_root, ignore_errors=True)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
