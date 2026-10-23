# -*- coding: utf-8 -*-
"""
_patch_prompt_writefile.py
=========================================================================
목적: backend/core/agent_executor.py 의 _build_system_prompt 내부에서
      'write_file 도구 호출 의무'를 강제하던 구절(629~632줄)을
      '완성형 코드 블록 필수 출력 + MCP 디스크 강제 동기화 게이트' 구절로
      정밀 교체한다.

교체 대상 (4줄):
  🚨 [핵심 행동 강제 - 텍스트 흉내내기 절대 금지, 실제 도구 호출 의무]:
  - 코드를 고쳐야 한다면 절대로 채팅창 텍스트로만 "이렇게 고쳤습니다", ...
  - 변경할 코드가 있다면 반드시 `write_file` 도구를 실제로 호출하여 ...
  - 디스크에 `write_file`을 호출하지 않고 채팅창에 코드 블록만 작성하는 것은 ...

교체 후 (3줄):
  🚨 [핵심 행동 원칙 - 완성형 코드 블록 필수 출력 (시스템 디스크 자동 저장 연동)]:
  - 코드를 작성하거나 수정할 때는 반드시 파일 전체 코드를 온전한 파이썬 코드 블록으로 출력!
  - 출력된 최신 코드 블록은 MCP 서버의 디스크 강제 동기화 게이트가 자동으로 가로채 ...
  - 절대로 중간 생략(...)이나 설명으로 때우지 말고 1줄부터 끝줄까지 100% 동작하는 완성 코드를 ...

안전장치:
  1) 원본 백업 (.bak_prompt_writefile)
  2) 정확한 4줄 블록만 치환 (개수 검증: 정확히 1회 매칭)
  3) 이미 교체된 경우 중복 패치 없이 스킵 (멱등성)
  4) 원자적 쓰기 (temp → os.replace)
  5) py_compile 즉시 검증 + 잔존 구절 실측 검사
=========================================================================
"""
import io
import os
import sys
import shutil
import py_compile

TARGET = os.path.join("backend", "core", "agent_executor.py")
BACKUP = TARGET + ".bak_prompt_writefile"

# ---- 교체 대상 (원본 4줄) ----
OLD_BLOCK = (
    '🚨 [핵심 행동 강제 - 텍스트 흉내내기 절대 금지, 실제 도구 호출 의무]:\n'
    '- 코드를 고쳐야 한다면 절대로 채팅창 텍스트로만 "이렇게 고쳤습니다", "변경 내용: ..."라고 말로 때우지 마십시오.\n'
    '- 변경할 코드가 있다면 반드시 `write_file` 도구를 실제로 호출하여 디스크의 해당 파일에 전체 코드를 기록하십시오!\n'
    '- 디스크에 `write_file`을 호출하지 않고 채팅창에 코드 블록만 작성하는 것은 가장 치명적인 불이행(허위 보고)입니다.\n'
)

# ---- 교체 후 (신규 3줄) ----
NEW_BLOCK = (
    '🚨 [핵심 행동 원칙 - 완성형 코드 블록 필수 출력 (시스템 디스크 자동 저장 연동)]:\n'
    '- 코드를 작성하거나 수정할 때는 반드시 파일 전체 코드를 온전한 파이썬 코드 블록(```python ... ```)으로 출력하십시오!\n'
    '- 출력된 최신 코드 블록은 MCP 서버의 디스크 강제 동기화 게이트가 자동으로 가로채 대상 파일에 100% 원자적으로 안전하게 저장합니다.\n'
    '- 절대로 중간 생략(...)이나 설명으로 때우지 말고 1줄부터 끝줄까지 100% 동작하는 완성 코드를 블록 내에 온전히 작성하십시오.\n'
)

# 잔존 검사용 마커
OLD_MARKER = "핵심 행동 강제 - 텍스트 흉내내기 절대 금지"
NEW_MARKER = "핵심 행동 원칙 - 완성형 코드 블록 필수 출력"


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
    print("[2] 멱등성 검사 (이미 교체되었는지)")
    print("=" * 70)
    old_count = src.count(OLD_MARKER)
    new_count = src.count(NEW_MARKER)
    print(f"  · 구(舊) 구절 잔존 수 : {old_count}")
    print(f"  · 신(新) 구절 존재 수 : {new_count}")

    if old_count == 0 and new_count >= 1:
        print("  · 이미 교체 완료 → 중복 패치 스킵")
        already = True
    elif old_count == 1:
        print("  · 미교체 상태 → 패치 진행")
        already = False
    else:
        print(f"❌ 구절 매칭이 정확히 1회가 아닙니다 (발견 {old_count}회). 패치 중단.")
        return 2

    if not already:
        print()
        print("=" * 70)
        print("[3] 교체 대상 블록 위치 확인")
        print("=" * 70)
        idx = src.find(OLD_BLOCK)
        if idx < 0:
            print("❌ OLD_BLOCK 원문과 정확히 일치하는 블록을 찾지 못했습니다. 패치 중단.")
            return 3
        start_line = src[:idx].count("\n") + 1
        end_line = start_line + OLD_BLOCK.count("\n") - 1
        print(f"  · 치환 범위: {start_line}줄 ~ {end_line}줄 ({OLD_BLOCK.count(chr(10))}줄)")
        print(f"  · 블록 내 write_file 언급 수: {OLD_BLOCK.count('write_file')}")

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
        patched = src.replace(OLD_BLOCK, NEW_BLOCK, 1)
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
    print("[7] 실측 검증 (잔존/신규 구절 + write_file 의무 문구 제거)")
    print("=" * 70)
    final_src = read_text(TARGET)
    old_left = final_src.count(OLD_MARKER)
    new_found = final_src.count(NEW_MARKER)
    writefile_forced = final_src.count("`write_file` 도구를 실제로 호출")
    mcp_gate = final_src.count("디스크 강제 동기화 게이트")
    print(f"  · 구(舊) 구절 잔존 수        : {old_left}  (기대값 0)")
    print(f"  · 신(新) 구절 존재 수        : {new_found}  (기대값 1)")
    print(f"  · 'write_file 도구 강제' 잔존: {writefile_forced}  (기대값 0)")
    print(f"  · '디스크 강제 동기화 게이트': {mcp_gate}  (기대값 1)")

    print()
    print("=" * 70)
    print("[8] 최종 결과")
    print("=" * 70)
    ok = compile_ok and old_left == 0 and new_found == 1 and writefile_forced == 0 and mcp_gate == 1
    if ok:
        print("  ✅ 패치 성공 — py_compile 통과 + 프롬프트 구절 정밀 교체 완료")
        print(f"  · 최종 줄 수: {len(final_src.splitlines())}")
        return 0
    print("  ❌ 패치 실패 — 위 로그 확인")
    return 4


if __name__ == "__main__":
    sys.exit(main())
