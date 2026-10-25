# -*- coding: utf-8 -*-
"""
core/write_guard.py
파괴적 덮어쓰기 방지 가드 (독립 모듈 / 단일 책임)

배경:
  LLM 이 대용량 파일의 일부만 보고 '완성본'을 써서 원본(예: 1,379줄)을
  수백 줄 / 0바이트로 덮어써 버리는 사고를 물리적으로 차단한다.
  대용량 파일의 부분 수정은 반드시 apply_search_replace 로 수행해야 한다.
"""
from typing import Tuple

# 이 줄 수 미만의 소형 파일은 전체 재작성을 허용한다.
GUARD_MIN_ORIGINAL_LINES = 100
# 신규 내용이 원본 줄 수의 이 비율 미만이면 파괴적 덮어쓰기로 판정한다.
GUARD_MIN_KEEP_RATIO = 0.7


def check_overwrite(original: str, new: str) -> Tuple[bool, str]:
    """
    원본 대비 신규 내용이 과도하게 축소되었는지 검사한다.

    반환: (허용 여부, 차단 사유). 허용이면 사유는 빈 문자열.
    차단 시 반드시 호출자가 실패로 처리하고 로그를 남겨야 한다(조용한 통과 금지).
    """
    if not original:
        return True, ""
    orig_lines = len(original.splitlines())
    if orig_lines < GUARD_MIN_ORIGINAL_LINES:
        return True, ""
    new_lines = len((new or "").splitlines())
    if new_lines == 0 or new_lines < orig_lines * GUARD_MIN_KEEP_RATIO:
        return False, (
            "파괴적 덮어쓰기 차단: 원본 %d줄 -> 신규 %d줄 (허용 하한 %d줄). "
            "대용량 파일은 전체를 다시 쓰지 말고 apply_search_replace 로 "
            "고장 난 부분만 정밀 치환하십시오."
            % (orig_lines, new_lines, int(orig_lines * GUARD_MIN_KEEP_RATIO))
        )
    return True, ""
