# -*- coding: utf-8 -*-
"""
_patch_agent_bind.py
=========================================================================
목적: backend/core/agent_executor.py 의 _rebind_project_for_context 메서드를
      정밀 패치하여, active_project 전환 직후
      self.memory.bind_project(bound or self.pm.active_project)
      1줄을 배선한다.

효과:
  - 어떤 앱의 파일이 열리든 해당 앱의 대화 기억/수정 이력이 디스크에서
    즉시 자동 로드된다.
  - 이전 앱의 기억은 flush 후 초기화되어 100% 격리된다.

안전장치:
  1) 원본 백업 (.bak_agent_bind)
  2) 정확한 메서드 블록만 치환 (정규식 앵커 + 개수 검증)
  3) 이미 배선되어 있으면 중복 패치 없이 스킵 (멱등성)
  4) 원자적 쓰기 (temp → os.replace)
  5) py_compile 즉시 검증
=========================================================================
"""
import io
import os
import re
import sys
import shutil
import py_compile

TARGET = os.path.join("backend", "core", "agent_executor.py")
BACKUP = TARGET + ".bak_agent_bind"

BIND_LINE = "self.memory.bind_project(bound or self.pm.active_project)"

NEW_METHOD = '''    # =====================================================================
    # [2][5] context_file 기반 active_project 자동 rebind + 기억 격리/복원
    # =====================================================================
    def _rebind_project_for_context(self, context_file: str) -> str:
        """
        context_file 이 주어지면 해당 파일이 속한 프로젝트로 active_project 즉시 전환.
        전환 직후 SessionMemory 를 새 프로젝트에 바인딩하여
          - 이전 프로젝트의 대화/수정 이력은 디스크로 flush(보존)
          - 새 프로젝트의 대화/수정 이력은 디스크에서 자동 로드(복원)
        → 프로젝트 간 기억 100% 격리.
        반환: rebind 후의 active_project 경로
        """
        raw = (context_file or "").strip().strip("\\"'`").strip()
        if not raw:
            # context_file 이 없어도 현재 프로젝트 기억은 항상 보장
            try:
                self.memory.bind_project(self.pm.active_project)
            except Exception:
                pass
            return self.pm.active_project

        bound = ""
        try:
            resolved = self._resolve_path(raw)
            bound = self.pm.auto_bind_project_for_file(resolved)
        except Exception:
            bound = ""

        # [핵심 배선] active_project 전환 후 세션 메모리를 해당 프로젝트로 재바인딩
        # → 이전 앱 기억 flush + 새 앱 기억 디스크 로드 (완전 격리)
        try:
            self.memory.bind_project(bound or self.pm.active_project)
        except Exception:
            pass

        return bound or self.pm.active_project
'''


def read_text(path: str) -> str:
    with io.open(path, "r", encoding="utf-8") as f:
        return f.read()


def write_text_atomic(path: str, text: str) -> None:
    tmp = path + ".tmp_patch"
    with io.open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    os.replace(tmp, path)


def main() -> int:
    print("=" * 70)
    print("[1] 대상 파일 확인")
    print("=" * 70)
    if not os.path.isfile(TARGET):
        print(f"❌ 대상 파일 없음: {TARGET}")
        return 1
    src = read_text(TARGET)
    before_lines = len(src.splitlines())
    print(f"  · 경로: {os.path.abspath(TARGET)}")
    print(f"  · 현재 줄 수: {before_lines}")

    print()
    print("=" * 70)
    print("[2] 멱등성 검사 (이미 배선되었는지)")
    print("=" * 70)
    if BIND_LINE in src:
        print(f"  · 이미 배선됨 → 중복 패치 스킵: {BIND_LINE}")
        already = True
    else:
        print("  · 미배선 상태 → 패치 진행")
        already = False

    if not already:
        print()
        print("=" * 70)
        print("[3] _rebind_project_for_context 메서드 블록 탐색")
        print("=" * 70)
        # 메서드 시작 주석부터 다음 메서드 주석([4] 파이썬 문법 검증) 직전까지
        pattern = re.compile(
            r"    # =+\n"
            r"    # \[2\] context_file 기반 active_project 자동 rebind\n"
            r"    # =+\n"
            r"    def _rebind_project_for_context\(self, context_file: str\) -> str:\n"
            r"(?:.*?\n)*?"
            r"        return self\.pm\.active_project\n",
            re.MULTILINE
        )
        matches = list(pattern.finditer(src))
        print(f"  · 매칭된 메서드 블록 수: {len(matches)}")
        if len(matches) != 1:
            print("❌ 메서드 블록을 정확히 1개 특정하지 못했습니다. 패치 중단.")
            return 2

        m = matches[0]
        old_block = m.group(0)
        old_start_line = src[:m.start()].count("\n") + 1
        old_end_line = src[:m.end()].count("\n") + 1
        print(f"  · 치환 범위: {old_start_line}줄 ~ {old_end_line}줄 ({old_end_line - old_start_line + 1}줄)")
        print(f"  · 기존 블록 내 bind_project 존재: {BIND_LINE in old_block}")

        print()
        print("=" * 70)
        print("[4] 백업 생성")
        print("=" * 70)
        shutil.copy2(TARGET, BACKUP)
        print(f"  · 백업 완료: {BACKUP}")

        print()
        print("=" * 70)
        print("[5] 정밀 치환 및 원자적 저장")
        print("=" * 70)
        patched = src[:m.start()] + NEW_METHOD + src[m.end():]
        write_text_atomic(TARGET, patched)
        after_lines = len(patched.splitlines())
        print(f"  · 저장 완료: {before_lines}줄 → {after_lines}줄 (Δ{after_lines - before_lines:+d})")

    print()
    print("=" * 70)
    print("[6] py_compile 문법 검증")
    print("=" * 70)
    try:
        py_compile.compile(TARGET, cfile=None, doraise=True, optimize=-1)
        print("  · ✅ COMPILE_OK — 문법 오류 0건")
        compile_ok = True
    except py_compile.PyCompileError as e:
        print(f"  · ❌ COMPILE_FAIL: {e}")
        compile_ok = False

    print()
    print("=" * 70)
    print("[7] has_bind 실측 검증")
    print("=" * 70)
    final_src = read_text(TARGET)
    has_bind = BIND_LINE in final_src
    bind_count = final_src.count("self.memory.bind_project(")
    print(f"  · has_bind            : {has_bind}")
    print(f"  · bind_project 호출 수 : {bind_count}")

    # 메서드 내부에 실제로 존재하는지 위치 확인
    idx = final_src.find("def _rebind_project_for_context")
    if idx >= 0:
        seg = final_src[idx: idx + 2000]
        in_method = BIND_LINE in seg
        print(f"  · 메서드 내부 배선 확인 : {in_method}")
    else:
        in_method = False
        print("  · ❌ 메서드 미발견")

    print()
    print("=" * 70)
    print("[8] 최종 결과")
    print("=" * 70)
    ok = compile_ok and has_bind and in_method
    if ok:
        print("  ✅ 패치 성공 — py_compile 통과 + bind_project 배선 완료")
        print(f"  · 최종 줄 수: {len(final_src.splitlines())}")
        return 0
    print("  ❌ 패치 실패 — 위 로그 확인")
    return 3


if __name__ == "__main__":
    sys.exit(main())
