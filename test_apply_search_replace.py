# -*- coding: utf-8 -*-
"""
test_apply_search_replace.py

project_manager.apply_search_replace 실동작 실측 테스트.

케이스:
  A) 정확 일치(Exact)          — 원본 그대로 SEARCH 블록 존재
  B) 줄바꿈 정규화 일치(CRLF)   — 원본은 CRLF, SEARCH 는 LF
  C) 공백 제거 일치(Indent)     — 들여쓰기/공백 변형
  D) SEARCH 미발견              — 파일 무변경 + success=False 확인
  E) 다중 블록(2건)             — 순차 치환 정확성
  F) 블록 없음                 — 파일 무변경 확인

각 케이스는 임시 디렉터리에 실제 파일을 만들고, 실제 디스크 내용을 재독해 검증한다.
"""

import os
import io
import sys
import shutil
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "backend"))

from core.project_manager import ProjectManager

PASS = 0
FAIL = 0


def check(label, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("    ✅ PASS | %s %s" % (label, detail))
    else:
        FAIL += 1
        print("    ❌ FAIL | %s %s" % (label, detail))


def read(p):
    with io.open(p, "r", encoding="utf-8", newline="") as f:
        return f.read()


def main():
    tmpdir = tempfile.mkdtemp(prefix="asr_test_")
    print("=" * 74)
    print("[테스트 샌드박스]", tmpdir)

    pm = ProjectManager(os.path.join(tmpdir, "config.json"))
    try:
        pm.set_active_project(tmpdir)
    except Exception:
        pass

    # ---------------------------------------------------------------
    # 케이스 A: 정확 일치
    # ---------------------------------------------------------------
    print("\n[A] 정확 일치(Exact Match)")
    fa = os.path.join(tmpdir, "case_a.py")
    orig_a = (
        "def alpha():\n"
        "    return 1\n"
        "\n"
        "def beta():\n"
        "    return 2\n"
    )
    with io.open(fa, "w", encoding="utf-8", newline="\n") as f:
        f.write(orig_a)

    patch_a = (
        "<<<<<<< SEARCH\n"
        "def beta():\n"
        "    return 2\n"
        "=======\n"
        "def beta():\n"
        "    return 200\n"
        ">>>>>>> REPLACE\n"
    )
    r = pm.apply_search_replace(fa, patch_a)
    print("    반환:", {k: r[k] for k in ("success", "applied", "total", "saved")})
    check("success", r["success"] is True)
    check("applied==1", r["applied"] == 1, "(실측 %s)" % r["applied"])
    check("total==1", r["total"] == 1, "(실측 %s)" % r["total"])
    check("saved", r["saved"] is True)
    disk_a = read(fa)
    check("디스크 반영 'return 200'", "return 200" in disk_a)
    check("원본 보존 'return 1'", "return 1" in disk_a)
    check("SEARCH 마커 미잔존", "<<<<<<< SEARCH" not in disk_a)

    # ---------------------------------------------------------------
    # 케이스 B: 줄바꿈 정규화 (원본 CRLF, SEARCH LF)
    # ---------------------------------------------------------------
    print("\n[B] 줄바꿈 정규화 일치 (원본 CRLF / SEARCH LF)")
    fb = os.path.join(tmpdir, "case_b.py")
    orig_b = "line1\r\nline2\r\nline3\r\n"
    with io.open(fb, "w", encoding="utf-8", newline="") as f:
        f.write(orig_b)
    check("사전조건: 원본에 CRLF 존재", "\r\n" in read(fb))

    patch_b = (
        "<<<<<<< SEARCH\n"
        "line2\n"
        "=======\n"
        "line2_patched\n"
        ">>>>>>> REPLACE\n"
    )
    r = pm.apply_search_replace(fb, patch_b)
    print("    반환:", {k: r[k] for k in ("success", "applied", "total", "saved")})
    check("success", r["success"] is True)
    check("applied==1", r["applied"] == 1, "(실측 %s)" % r["applied"])
    disk_b = read(fb)
    check("디스크 반영 'line2_patched'", "line2_patched" in disk_b)
    check("'line1' 보존", "line1" in disk_b)
    check("'line3' 보존", "line3" in disk_b)

    # ---------------------------------------------------------------
    # 케이스 C: 공백 제거 일치 (들여쓰기 변형)
    # ---------------------------------------------------------------
    print("\n[C] 공백 제거 일치 (들여쓰기 변형)")
    fc = os.path.join(tmpdir, "case_c.py")
    orig_c = (
        "class Demo:\n"
        "    def method(self):\n"
        "        value = compute( 1, 2 )\n"
        "        return value\n"
    )
    with io.open(fc, "w", encoding="utf-8", newline="\n") as f:
        f.write(orig_c)

    # SEARCH 는 들여쓰기를 8칸이 아닌 4칸으로, 내부 공백도 다르게 작성
    patch_c = (
        "<<<<<<< SEARCH\n"
        "    def method(self):\n"
        "        value = compute(1,2)\n"
        "=======\n"
        "    def method(self):\n"
        "        value = compute(10, 20)\n"
        ">>>>>>> REPLACE\n"
    )
    r = pm.apply_search_replace(fc, patch_c)
    print("    반환:", {k: r[k] for k in ("success", "applied", "total", "saved")})
    check("success", r["success"] is True)
    check("applied==1", r["applied"] == 1, "(실측 %s)" % r["applied"])
    disk_c = read(fc)
    check("디스크 반영 'compute(10, 20)'", "compute(10, 20)" in disk_c)
    check("'class Demo' 보존", "class Demo" in disk_c)

    # ---------------------------------------------------------------
    # 케이스 D: SEARCH 미발견 → 파일 무변경
    # ---------------------------------------------------------------
    print("\n[D] SEARCH 미발견 → 파일 무변경")
    fd = os.path.join(tmpdir, "case_d.py")
    orig_d = "def only():\n    return 0\n"
    with io.open(fd, "w", encoding="utf-8", newline="\n") as f:
        f.write(orig_d)
    before_d = read(fd)

    patch_d = (
        "<<<<<<< SEARCH\n"
        "def nonexistent_xyz():\n"
        "    return 999\n"
        "=======\n"
        "def replaced():\n"
        "    return 1\n"
        ">>>>>>> REPLACE\n"
    )
    r = pm.apply_search_replace(fd, patch_d)
    print("    반환:", {k: r[k] for k in ("success", "applied", "total", "saved")})
    check("success==False", r["success"] is False)
    check("applied==0", r["applied"] == 0, "(실측 %s)" % r["applied"])
    check("saved==False (디스크 미기록)", r["saved"] is False)
    check("failed 1건 기록", len(r["failed"]) == 1, "(실측 %d)" % len(r["failed"]))
    check("파일 내용 완전 불변", read(fd) == before_d)

    # ---------------------------------------------------------------
    # 케이스 E: 다중 블록 2건 순차 치환
    # ---------------------------------------------------------------
    print("\n[E] 다중 블록 2건 순차 치환")
    fe = os.path.join(tmpdir, "case_e.py")
    orig_e = (
        "A = 1\n"
        "B = 2\n"
        "C = 3\n"
    )
    with io.open(fe, "w", encoding="utf-8", newline="\n") as f:
        f.write(orig_e)

    patch_e = (
        "<<<<<<< SEARCH\n"
        "A = 1\n"
        "=======\n"
        "A = 100\n"
        ">>>>>>> REPLACE\n"
        "\n"
        "<<<<<<< SEARCH\n"
        "C = 3\n"
        "=======\n"
        "C = 300\n"
        ">>>>>>> REPLACE\n"
    )
    r = pm.apply_search_replace(fe, patch_e)
    print("    반환:", {k: r[k] for k in ("success", "applied", "total", "saved")})
    check("success", r["success"] is True)
    check("total==2", r["total"] == 2, "(실측 %s)" % r["total"])
    check("applied==2", r["applied"] == 2, "(실측 %s)" % r["applied"])
    disk_e = read(fe)
    check("'A = 100' 반영", "A = 100" in disk_e)
    check("'C = 300' 반영", "C = 300" in disk_e)
    check("'B = 2' 보존", "B = 2" in disk_e)

    # ---------------------------------------------------------------
    # 케이스 F: 블록 없음 → 파일 무변경
    # ---------------------------------------------------------------
    print("\n[F] SEARCH/REPLACE 블록 없음 → 파일 무변경")
    ff = os.path.join(tmpdir, "case_f.py")
    orig_f = "X = 1\n"
    with io.open(ff, "w", encoding="utf-8", newline="\n") as f:
        f.write(orig_f)
    before_f = read(ff)

    r = pm.apply_search_replace(ff, "그냥 평범한 텍스트입니다. 블록이 없습니다.")
    print("    반환:", {k: r[k] for k in ("success", "applied", "total", "saved")})
    check("success==False", r["success"] is False)
    check("total==0", r["total"] == 0, "(실측 %s)" % r["total"])
    check("파일 내용 완전 불변", read(ff) == before_f)

    # ---------------------------------------------------------------
    # 결과 집계
    # ---------------------------------------------------------------
    print("\n" + "=" * 74)
    print("최종 집계: PASS=%d / FAIL=%d" % (PASS, FAIL))
    print("=" * 74)

    shutil.rmtree(tmpdir, ignore_errors=True)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
