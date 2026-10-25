# -*- coding: utf-8 -*-
"""
core/work_order.py
=================================================================
작업지시서(WorkOrder) 계약 모듈 (독립 모듈 / 단일 책임)

문제: 제미나이가 "어디를 고치라"는 위치를 안 넘기면 딥시크는 코드가 없다며 헤맨다.
해결: 기존 파일을 수정하는 요청은 반드시 '범위'를 명시해야 한다.
      - target_symbol : 함수/클래스/메서드 이름 (쉼표로 여러 개, 예: "Foo.bar, helper")
      - line_range    : "시작-끝" 줄 범위 (예: "120-180")
      - whole_file    : 전체 파일 대상임을 명시적으로 승인
      범위가 없으면 거부하고, 사용 가능한 심볼 목록을 알려준다.
      범위가 있으면 서버가 디스크에서 해당 원문을 직접 추출해 프롬프트 블록으로 만든다
      (제미나이의 요약/누락과 무관하게 딥시크가 정확한 원문을 본다).

신규 파일 생성(대상 파일이 아직 없음)과 대상 미지정 요청은 범위 요구에서 제외한다.
=================================================================
"""
import os
import re
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from core.code_tools.code_eye import CodeEye

_RANGE_RE = re.compile(r"^\s*(\d+)\s*[-:~]\s*(\d+)\s*$")
_MAX_LISTED_SYMBOLS = 60

HOW_TO = (
    "다음 중 하나를 지정하십시오:\n"
    "  - target_symbol: 수정할 함수/클래스/메서드 이름 (예: \"Foo.bar\" 또는 \"a, b\")\n"
    "  - line_range: 수정할 줄 범위 (예: \"120-180\")\n"
    "  - whole_file=true: 파일 전체가 대상임을 명시 (비권장)"
)


@dataclass
class WorkOrder:
    ok: bool
    mode: str = ""                      # no_target | new_file | snippet | whole_file | symbol | line_range
    error: str = ""
    file: str = ""
    symbols: List[str] = field(default_factory=list)   # 완료검증 게이트에 넘길 요청 심볼
    line_range: Optional[Tuple[int, int]] = None
    prompt_block: str = ""


def _fail(file: str, message: str) -> WorkOrder:
    return WorkOrder(ok=False, mode="rejected", file=file, error="❌ [작업지시서 거부] " + message)


def _section(lines: List[str], start: int, end: int) -> str:
    return "\n".join(lines[max(0, start - 1):min(len(lines), end)])


def _split_names(raw: str) -> List[str]:
    return [p.strip() for p in re.split(r"[,\n]", raw or "") if p.strip()]


def _find_symbol(name: str, symbols):
    low = name.lower()
    exact = [s for s in symbols if s.name.lower() == low]
    if exact:
        return exact
    return [s for s in symbols if s.name.lower().endswith("." + low)]


def build(full_path: str, target_symbol: str = "", line_range: str = "",
          whole_file: bool = False, has_snippet: bool = False) -> WorkOrder:
    """수정 범위를 검증하고 서버가 디스크에서 추출한 원문 블록을 만든다."""
    if not full_path:
        return WorkOrder(ok=True, mode="no_target")
    if not os.path.isfile(full_path):
        return WorkOrder(ok=True, mode="new_file", file=full_path,
                         prompt_block="\n\n[작업지시서] 신규 파일 생성 요청: `%s` (기존 파일 없음)\n" % full_path)

    has_scope = bool((target_symbol or "").strip() or (line_range or "").strip())
    if whole_file and not has_scope:
        return WorkOrder(ok=True, mode="whole_file", file=full_path,
                         prompt_block="\n\n[작업지시서] 전체 파일 대상 (제미나이가 whole_file 로 명시 승인): `%s`\n" % full_path)
    if not has_scope:
        if has_snippet:
            return WorkOrder(ok=True, mode="snippet", file=full_path)
        return _fail(full_path, "수정 범위가 지정되지 않았습니다. 대상: `%s`\n%s" % (full_path, HOW_TO))

    try:
        eye = CodeEye()
        content, _enc = eye.read_file(full_path)
        outline = eye.outline(full_path)
    except (OSError, FileNotFoundError) as exc:
        return _fail(full_path, "대상 파일을 읽을 수 없습니다: %s" % exc)
    lines = content.splitlines()

    spans: List[Tuple[str, int, int]] = []
    symbols: List[str] = []
    parsed_range: Optional[Tuple[int, int]] = None

    for name in _split_names(target_symbol):
        hits = _find_symbol(name, outline.symbols)
        if not hits:
            avail = ", ".join(s.name for s in outline.symbols[:_MAX_LISTED_SYMBOLS])
            more = " ... (총 %d개)" % len(outline.symbols) if len(outline.symbols) > _MAX_LISTED_SYMBOLS else ""
            return _fail(full_path, "심볼 `%s` 을(를) 파일에서 찾을 수 없습니다.\n사용 가능한 심볼: %s%s"
                         % (name, avail or "(없음)", more))
        for s in hits:
            if s.name not in symbols:
                symbols.append(s.name)
                spans.append((s.name, s.line_start, s.line_end))

    if (line_range or "").strip():
        m = _RANGE_RE.match(line_range)
        if not m:
            return _fail(full_path, "line_range 형식 오류: `%s` (예: \"120-180\")" % line_range)
        start, end = int(m.group(1)), int(m.group(2))
        if start < 1 or end < start or end > len(lines):
            return _fail(full_path, "line_range `%s` 이(가) 파일 범위(1-%d줄)를 벗어났습니다." % (line_range, len(lines)))
        parsed_range = (start, end)
        spans.append(("줄 %d-%d" % (start, end), start, end))

    blocks = []
    for label, s, e in spans:
        blocks.append("### %s [L%d-L%d]\n```\n%s\n```" % (label, s, e, _section(lines, s, e)))

    prompt = (
        "\n\n[작업지시서 — 서버가 디스크에서 직접 추출한 수정 대상 원문]\n"
        "- 파일: `%s` (총 %d줄)\n"
        "- 수정 범위: %s\n"
        "- 규칙: 위 범위 밖 코드는 수정하지 마십시오. SEARCH 블록은 아래 원문과 글자 그대로 일치해야 합니다. "
        "(줄 번호는 코드가 아니므로 포함 금지)\n\n"
        "[파일 구조 개요]\n%s\n\n[대상 원문]\n%s\n"
        % (full_path, len(lines), ", ".join("%s(L%d-%d)" % sp for sp in spans),
           outline.summary_text(), "\n\n".join(blocks))
    )
    return WorkOrder(ok=True, mode="symbol" if symbols else "line_range", file=full_path,
                     symbols=symbols, line_range=parsed_range, prompt_block=prompt)
