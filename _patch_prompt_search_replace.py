# -*- coding: utf-8 -*-
"""
_patch_prompt_search_replace.py

agent_executor.py 의 _build_system_prompt 에
'수천 줄 파일 전체 재작성 금지 + SEARCH/REPLACE 블록 사용' 지침을 명시한다.

삽입 위치: "🚨 [대표님의 절대 행동 수칙 - 반드시 준수]:" 바로 다음,
           기존 "🚨 [핵심 행동 원칙 - 완성형 코드 블록 필수 출력 ...]" 블록 앞.

절차: 멱등성 → 앵커 1회 확인 → 백업 → 원자 저장 → py_compile → 지표 실측
"""

import io
import os
import sys
import shutil
import py_compile

TARGET = os.path.join("backend", "core", "agent_executor.py")
BACKUP = TARGET + ".bak_prompt_search_replace"

ANCHOR = '''🚨 [대표님의 절대 행동 수칙 - 반드시 준수]:
🚨 [핵심 행동 원칙 - 완성형 코드 블록 필수 출력 (시스템 디스크 자동 저장 연동)]:
'''

NEW_BLOCK = '''🚨 [대표님의 절대 행동 수칙 - 반드시 준수]:
🚨 [대용량 파일 패치 원칙 - SEARCH/REPLACE 블록 필수 (최우선 규칙)]:
- 수천 줄 파일 전체를 다시 쓰지 말고, 반드시 변경할 부분만 <<<<<<< SEARCH / ======= / >>>>>>> REPLACE 블록으로 작성하십시오.
- SEARCH 구간에는 원본 파일에 실제로 존재하는 코드를 '한 글자도 다르지 않게' 그대로 복사해 넣으십시오. (추측·요약·생략 절대 금지)
- REPLACE 구간에는 그 구간을 대체할 최종 코드만 작성하십시오.
- 하나의 응답에 여러 개의 SEARCH/REPLACE 블록을 연속으로 나열해도 됩니다. (블록은 위에서부터 순차 적용됩니다)
- 파일 전체 재작성(전체 코드 덮어쓰기)은 신규 파일 생성이거나 파일 구조를 전면 재설계할 때만 허용됩니다.
- 출력 형식 예시:
  <<<<<<< SEARCH
  def old_function():
      return 1
  =======
  def old_function():
      return 2
  >>>>>>> REPLACE
- 위 형식이 감지되면 시스템이 apply_search_replace 로 국소 정밀 치환을 자동 수행합니다.

🚨 [핵심 행동 원칙 - 완성형 코드 블록 필수 출력 (시스템 디스크 자동 저장 연동)]:
'''


def main() -> int:
    print("=" * 72)
    print("[1] 대상:", TARGET)
    if not os.path.isfile(TARGET):
        print("    ✗ 파일 없음")
        return 1

    with io.open(TARGET, "r", encoding="utf-8") as f:
        src = f.read()
    before_lines = len(src.splitlines())
    print("    ✓ 원본 줄 수:", before_lines)

    print("[2] 멱등성 검사")
    if "SEARCH/REPLACE 블록 필수" in src:
        print("    ⚠ 이미 지침 존재 — 중단")
        return 0
    print("    ✓ 미존재 확인")

    print("[3] 앵커 일치 검사")
    cnt = src.count(ANCHOR)
    print("    앵커 등장 횟수:", cnt)
    if cnt != 1:
        print("    ✗ 앵커가 정확히 1회가 아님 — 중단")
        return 1

    print("[4] 백업:", BACKUP)
    shutil.copy2(TARGET, BACKUP)
    print("    ✓ 백업 완료")

    print("[5] SEARCH/REPLACE 지침 삽입")
    new_src = src.replace(ANCHOR, NEW_BLOCK, 1)
    after_lines = len(new_src.splitlines())
    print("    ✓ %d줄 → %d줄 (Δ+%d)" % (before_lines, after_lines, after_lines - before_lines))

    print("[6] 원자적 저장")
    tmp = TARGET + ".tmp_prompt_sr"
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
    print("    최종 줄 수                        :", len(final.splitlines()))
    print("    '수천 줄 파일 전체를 다시 쓰지 말고':", final.count("수천 줄 파일 전체를 다시 쓰지 말고"))
    print("    '<<<<<<< SEARCH' 리터럴           :", final.count("<<<<<<< SEARCH"))
    print("    '>>>>>>> REPLACE' 리터럴          :", final.count(">>>>>>> REPLACE"))
    print("    apply_search_replace 언급         :", final.count("apply_search_replace"))
    print("=" * 72)
    return 0


if __name__ == "__main__":
    sys.exit(main())
