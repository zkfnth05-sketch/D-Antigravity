# -*- coding: utf-8 -*-
"""
.agents/scripts/context_bridge.py
=================================================================
[딥시크 컨텍스트 브리지] PreInvocation 훅

문제: 딥시크는 Antigravity 대화창을 볼 수 없고, 제미나이가 도구를 부를 때 넘겨주는
      텍스트만 본다. 제미나이가 요약/누락하면 딥시크는 맥락 없이 코드를 고친다.
해결: 제미나이가 생각하기 전에(PreInvocation) 이 훅이 대화 기록(transcript.jsonl)에서
      최근 20개 메시지를 '원문 그대로' 공유 파일에 물리적으로 복사한다.
      딥시크 MCP 서버(backend/core/conversation_context.py)가 매 호출마다 이 파일을
      자동 주입한다. -> 제미나이의 협조와 무관하게 딥시크가 대화 맥락을 본다.
실패 시: 조용히 넘기지 않는다. 로그 파일 + 제미나이에게 주입되는 경고 메시지로 드러낸다.
=================================================================
"""
import io
import json
import os
import re
import sys
import time
import traceback

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
AGENTS_DIR = os.path.dirname(BASE_DIR)
SHARED_DIR = os.path.join(AGENTS_DIR, "shared")
TAIL_PATH = os.path.join(SHARED_DIR, "conversation_tail.json")
LOG_PATH = os.path.join(SHARED_DIR, "context_bridge.log")
BRAIN_ROOT = os.path.join(os.path.expanduser("~"), ".gemini", "deepseek-antigravity-ide", "brain")

MAX_MESSAGES = 20
MAX_USER_CHARS = 6000
MAX_ASSISTANT_CHARS = 2500
MAX_LATEST_REQUEST_CHARS = 8000
READ_TAIL_BYTES = 4 * 1024 * 1024
MAX_LOG_BYTES = 1024 * 1024


def log(message):
    line = "%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), message)
    try:
        os.makedirs(SHARED_DIR, exist_ok=True)
        if os.path.isfile(LOG_PATH) and os.path.getsize(LOG_PATH) > MAX_LOG_BYTES:
            os.replace(LOG_PATH, LOG_PATH + ".old")
        with io.open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line)
    except OSError as exc:
        sys.stderr.write("[context_bridge] 로그 기록 실패: %s | %s" % (exc, line))


def find_transcript(payload):
    path = payload.get("transcriptPath") or payload.get("transcript_path") or ""
    if path and os.path.isfile(path):
        return path
    cid = payload.get("conversationId") or payload.get("conversation_id") or ""
    if cid:
        cand = os.path.join(BRAIN_ROOT, cid, ".system_generated", "logs", "transcript.jsonl")
        if os.path.isfile(cand):
            return cand
    raise FileNotFoundError("transcript 경로를 찾을 수 없음: payload keys=%s" % sorted(payload.keys()))


def read_tail_lines(path):
    size = os.path.getsize(path)
    offset = max(0, size - READ_TAIL_BYTES)
    with open(path, "rb") as f:
        f.seek(offset)
        data = f.read()
    lines = data.decode("utf-8", errors="replace").splitlines()
    if offset > 0 and lines:
        lines = lines[1:]  # 잘린 첫 줄 버림
    return lines


def extract_user_text(content):
    m = re.search(r"<USER_REQUEST>\s*(.*?)\s*</USER_REQUEST>", content or "", re.DOTALL)
    return (m.group(1) if m else (content or "")).strip()


def collect_messages(lines):
    msgs = []
    bad = 0
    for raw in lines:
        raw = raw.strip()
        if not raw:
            continue
        try:
            step = json.loads(raw)
        except ValueError:
            bad += 1
            continue
        stype = step.get("type")
        content = step.get("content") or ""
        if stype == "USER_INPUT":
            role, text = "user", extract_user_text(content)
        elif stype == "PLANNER_RESPONSE" and content.strip():
            role, text = "assistant", content.strip()
        else:
            continue
        if text:
            msgs.append({"role": role, "step_index": step.get("step_index"), "text": text})
    return msgs, bad


def cap(text, limit):
    if len(text) <= limit:
        return text, len(text)
    omitted = len(text) - limit
    return text[:limit] + "\n[... 원문 %d자 중 %d자 생략]" % (len(text), omitted), len(text)


def write_json_atomic(path, doc):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with io.open(tmp, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def emit(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=True) + "\n")
    sys.stdout.flush()


def build_message(n_saved, latest_user):
    return (
        "[딥시크 컨텍스트 브리지] 최근 대화 %d개가 딥시크 공유 파일에 자동 복사되었습니다. "
        "이번 턴에서 반드시 deepseek-council 도구(deepseek_council_debate 또는 deepseek_autonomous_codegen)를 호출하십시오. "
            "task/instructions 에는 아래 사용자 최신 요청 원문을 요약·가공 없이 그대로 넣으십시오. "
        "소스코드 수정은 딥시크만 수행합니다.\n[사용자 최신 요청 원문]\n%s" % (n_saved, latest_user)
    )


def main():
    payload = {}
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
        if not isinstance(payload, dict):
            payload = {}
    except ValueError as exc:
        log("payload JSON 파싱 실패: %s" % exc)

    try:
        transcript = find_transcript(payload)
        msgs, bad = collect_messages(read_tail_lines(transcript))
        tail = msgs[-MAX_MESSAGES:]
        out_msgs = []
        for m in tail:
            limit = MAX_USER_CHARS if m["role"] == "user" else MAX_ASSISTANT_CHARS
            text, original = cap(m["text"], limit)
            out_msgs.append({"role": m["role"], "step_index": m["step_index"],
                             "text": text, "chars_original": original})
        latest_user = ""
        for m in reversed(msgs):
            if m["role"] == "user":
                latest_user, _ = cap(m["text"], MAX_LATEST_REQUEST_CHARS)
                break
        doc = {
            "conversation_id": payload.get("conversationId", ""),
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "transcript": transcript,
            "bad_lines": bad,
            "latest_user_request": latest_user,
            "messages": out_msgs,
        }
        write_json_atomic(TAIL_PATH, doc)
        log("OK 메시지 %d개 저장 (bad_lines=%d) conv=%s" % (len(out_msgs), bad, doc["conversation_id"]))
        emit({"injectSteps": [{"ephemeralMessage": build_message(len(out_msgs), latest_user)}]})
    except Exception:
        err = traceback.format_exc()
        log("FAIL " + err.replace("\n", " | "))
        emit({"injectSteps": [{"ephemeralMessage": (
            "[딥시크 컨텍스트 브리지 실패] 대화 복사에 실패했습니다. 딥시크는 지금 대화를 볼 수 없습니다. "
            "이 사실을 대표님께 즉시 보고하십시오. 원인: " + err.strip().splitlines()[-1])}]})
    return 0


if __name__ == "__main__":
    sys.exit(main())
