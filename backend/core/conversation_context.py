# -*- coding: utf-8 -*-
"""
core/conversation_context.py
=================================================================
딘시크 대화 맥락 수신 모듈 (독립 모듈 / 단일 책임)

.agents/scripts/context_bridge.py (PreInvocation 훅)가 Antigravity 대화 최근 20개를
.agents/shared/conversation_tail.json 에 원문 그대로 복사해 둔다.
이 모듈은 MCP 도구 호출마다 그 파일을 읽어 딘시크 프롬프트 앞에 자동 주입한다.
-> 제미나이가 요약/누락해도 딘시크는 대화 원문을 본다.
맥락 미수신/오래됨은 조용히 넘기지 않고 경고 블록으로 드러낸다.
=================================================================
"""
import json
import os
import re
import time
from typing import Any, Dict, Optional, Tuple

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO_ROOT = os.path.dirname(BACKEND_DIR)
TAIL_PATH = os.path.join(REPO_ROOT, ".agents", "shared", "conversation_tail.json")

CONTEXT_CHAR_BUDGET = 30000
USER_MSG_CAP = 4000
ASSISTANT_MSG_CAP = 1500
STALE_SECONDS = 600

# SEARCH/REPLACE 구분자가 대화 인용에 섞여 들어와 자동 치환 게이트를 오동작시키는 것을 방지
MARKER_RE = re.compile(r"(<{7}|={7}|>{7})")


def defuse_markers(text: str) -> str:
    return MARKER_RE.sub(lambda m: "[" + m.group(1)[0] + "x7]", text)


def cap_text(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "\n[... %d자 생략]" % (len(text) - limit)


def load_tail(path: str = TAIL_PATH) -> Tuple[Optional[Dict[str, Any]], str]:
    if not os.path.isfile(path):
        return None, "공유 파일 없음: %s" % path
    try:
        with open(path, "r", encoding="utf-8") as f:
            doc = json.load(f)
    except (OSError, ValueError) as exc:
        return None, "공유 파일 읽기 실패: %s" % exc
    if not isinstance(doc, dict) or not isinstance(doc.get("messages"), list):
        return None, "공유 파일 형식 오류"
    return doc, ""


def age_seconds(doc: Dict[str, Any]) -> Optional[float]:
    try:
        stamp = time.mktime(time.strptime(str(doc.get("updated_at", "")), "%Y-%m-%dT%H:%M:%S"))
    except ValueError:
        return None
    return max(0.0, time.time() - stamp)


def build_context_block(max_messages: int = 20, path: str = TAIL_PATH) -> str:
    doc, why = load_tail(path)
    if doc is None:
        return (
            "[⚠️ 대화 맥락 미수신] 사유: %s\n"
            "딘시크는 이번 호출에서 사용자 대화 원문을 보지 못했습니다. "
            "아래 지시문에 없는 맥락을 추측하지 말고, 불확실하면 불확실하다고 보고하십시오." % why
        )

    msgs = doc["messages"][-max_messages:]
    picked = []
    used = 0
    for m in reversed(msgs):
        role = m.get("role", "")
        limit = USER_MSG_CAP if role == "user" else ASSISTANT_MSG_CAP
        text = defuse_markers(cap_text(str(m.get("text", "")), limit))
        cost = len(text) + 30
        if picked and used + cost > CONTEXT_CHAR_BUDGET:
            break
        picked.append((role, text))
        used += cost
    picked.reverse()
    omitted = len(msgs) - len(picked)

    age = age_seconds(doc)
    warn = ""
    if age is None:
        warn = " (⚠️ 수신 시각 형식 오류)"
    elif age > STALE_SECONDS:
        warn = " (⚠️ %d분 전 데이터 - 오래될 수 있음)" % int(age // 60)

    lines = [
        "[Antigravity 대화 맥락 - 최근 %d개 메시지 원문 (자동 수신, 제미나이 가공 없음)]" % len(picked),
        "수신 시각: %s%s | 대화 ID: %s" % (doc.get("updated_at", "?"), warn, doc.get("conversation_id", "?")),
    ]
    if omitted > 0:
        lines.append("(토큰 예산 초과로 오래된 메시지 %d개 생략)" % omitted)
    for idx, (role, text) in enumerate(picked, 1):
        label = "사용자(대표님)" if role == "user" else "제미나이(대화창 응답)"
        lines.append("#%d [%s]\n%s" % (idx, label, text))
    lines.append("[대화 맥락 끝]")
    return "\n".join(lines)


def build_task_with_context(task: str, max_messages: int = 20) -> str:
    block = build_context_block(max_messages)
    return block + "\n\n[제미나이가 전달한 지시 원문]\n" + (task or "")
