# -*- coding: utf-8 -*-
"""
core/completion_gate.py
=================================================================
완료 검증 게이트 (독립 모듈 / 단일 책임)

목적: "수정했다"는 말이 아니라 '디스크 실측 증거'로 완료를 판정한다.
  1) 수정 전 상태를 capture_state() 로 찍어 두고
  2) 수정 후 evaluate() 가 디스크를 다시 읽어 검사한다:
       - 파일 존재 / 내용 변경(SHA-256) / 구문(compile) / 요청 심볼 실제 변경 / 급격한 축소
  3) 결과를 PASS | PARTIAL | FAIL | UNVERIFIED 로 판정하고
     불변 영수증(receipt)을 .agents/shared/receipts/ 에 저장한다.
  4) verify_receipt() 로 제미나이가 보고 전에 독립 재검증할 수 있다.

원칙: 검증 불가 상황을 PASS 로 둔갑시키지 않는다(UNVERIFIED 로 명시).
      영수증 저장 실패도 숨기지 않고 verdict 에 반영한다.
=================================================================
"""
import ast
import hashlib
import json
import os
import re
import time
import uuid
from typing import Any, Dict, List, Optional

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO_ROOT = os.path.dirname(BACKEND_DIR)
RECEIPT_DIR = os.path.join(REPO_ROOT, ".agents", "shared", "receipts")

SHRINK_WARN_RATIO = 0.5  # 수정 후 크기가 이 비율 미만이면 PARTIAL (잘림/덮어쓰기 사고 의심)
_ENCODINGS = ("utf-8", "utf-8-sig", "cp949", "euc-kr")
_RECEIPT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")

PASS, PARTIAL, FAIL, UNVERIFIED = "PASS", "PARTIAL", "FAIL", "UNVERIFIED"


def _read_text(full_path: str) -> Optional[str]:
    for enc in _ENCODINGS:
        try:
            with open(full_path, "r", encoding=enc) as f:
                return f.read()
        except UnicodeDecodeError:
            continue
        except OSError:
            return None
    return None


def symbol_fingerprints(text: str) -> Dict[str, str]:
    """파이썬 소스의 클래스/함수/메서드별 본문 SHA-256. 파싱 실패 시 빈 dict."""
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return {}
    out: Dict[str, str] = {}

    def add(name: str, node: ast.AST) -> None:
        seg = ast.get_source_segment(text, node) or ""
        out[name] = hashlib.sha256(seg.encode("utf-8")).hexdigest()

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            add(node.name, node)
        elif isinstance(node, ast.ClassDef):
            add(node.name, node)
            for child in node.body:
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    add("%s.%s" % (node.name, child.name), child)
    return out


def capture_state(full_path: str) -> Dict[str, Any]:
    """수정 전 상태 스냅샷(메모리). 파일이 없으면 exists=False."""
    state: Dict[str, Any] = {"path": full_path, "exists": False, "hash": "", "size": 0,
                             "lines": 0, "symbols": {}}
    if not full_path or not os.path.isfile(full_path):
        return state
    with open(full_path, "rb") as f:
        raw = f.read()
    state["exists"] = True
    state["hash"] = hashlib.sha256(raw).hexdigest()
    state["size"] = len(raw)
    text = _read_text(full_path)
    if text is not None:
        state["lines"] = len(text.splitlines())
        if full_path.lower().endswith(".py"):
            state["symbols"] = symbol_fingerprints(text)
    return state


def _match_symbol(requested: str, table: Dict[str, str]) -> Optional[str]:
    if requested in table:
        return requested
    suffix = "." + requested
    for name in table:
        if name.endswith(suffix):
            return name
    return None


def _check(name: str, ok: bool, detail: str) -> Dict[str, Any]:
    return {"name": name, "ok": bool(ok), "detail": detail}


def evaluate(full_path: str, before: Dict[str, Any],
             requested_symbols: Optional[List[str]] = None, tag: str = "") -> Dict[str, Any]:
    """수정 후 디스크를 재읽어 판정하고 영수증을 저장한다."""
    requested_symbols = [s for s in (requested_symbols or []) if s]
    checks: List[Dict[str, Any]] = []
    verdict = PASS

    if not full_path:
        return _finalize({"verdict": UNVERIFIED, "tag": tag, "path": "",
                          "checks": [_check("target", False, "대상 파일 경로 미지정 — 검증 불가")]})

    after = capture_state(full_path)
    existed_before = bool(before.get("exists"))

    if not after["exists"]:
        checks.append(_check("file_exists", False, "수정 후 파일이 디스크에 없음"))
        verdict = FAIL
    else:
        checks.append(_check("file_exists", True, "%dB / %d줄" % (after["size"], after["lines"])))

        changed = (after["hash"] != before.get("hash"))
        checks.append(_check(
            "content_changed", changed,
            ("신규 파일 생성" if not existed_before else
             "변경됨 %dB -> %dB" % (before.get("size", 0), after["size"])) if changed
            else "수정 전후 SHA-256 동일 — 디스크에 반영되지 않음"))
        if not changed:
            verdict = FAIL

        if full_path.lower().endswith(".py"):
            text = _read_text(full_path)
            if text is None:
                checks.append(_check("syntax", False, "디코딩 실패로 구문 검증 불가"))
                verdict = FAIL
            else:
                try:
                    compile(text, full_path, "exec")
                    checks.append(_check("syntax", True, "compile 통과"))
                except SyntaxError as exc:
                    checks.append(_check("syntax", False, "SyntaxError L%s: %s" % (exc.lineno, exc.msg)))
                    verdict = FAIL

        if existed_before and before.get("size", 0) > 0:
            ratio = after["size"] / float(before["size"])
            ok = ratio >= SHRINK_WARN_RATIO
            checks.append(_check("no_truncation", ok,
                                 "크기 비율 %.0f%%" % (ratio * 100) if ok else
                                 "크기가 %.0f%% 로 급감 — 덮어쓰기/잘림 사고 의심" % (ratio * 100)))
            if not ok and verdict == PASS:
                verdict = PARTIAL

        if requested_symbols:
            b_syms, a_syms = before.get("symbols", {}), after["symbols"]
            for req in requested_symbols:
                a_name = _match_symbol(req, a_syms)
                if a_name is None:
                    checks.append(_check("symbol:" + req, False, "수정 후 파일에서 심볼을 찾을 수 없음"))
                    verdict = FAIL if verdict != FAIL else verdict
                    continue
                b_name = _match_symbol(req, b_syms)
                if b_name is None:
                    checks.append(_check("symbol:" + req, True, "신규 심볼 생성 확인"))
                elif a_syms[a_name] != b_syms[b_name]:
                    checks.append(_check("symbol:" + req, True, "본문 변경 확인"))
                else:
                    checks.append(_check("symbol:" + req, False, "요청 심볼 본문이 그대로임(다른 곳만 수정됨)"))
                    if verdict == PASS:
                        verdict = PARTIAL

    return _finalize({
        "verdict": verdict, "tag": tag, "path": full_path,
        "before": {"exists": existed_before, "hash": before.get("hash", ""), "size": before.get("size", 0)},
        "after": {"exists": after["exists"], "hash": after["hash"], "size": after["size"]},
        "requested_symbols": requested_symbols, "checks": checks,
    })


def _finalize(result: Dict[str, Any]) -> Dict[str, Any]:
    """영수증 ID 부여 + 저장. 저장 실패는 숨기지 않고 반영한다."""
    result["receipt_id"] = "rc_%s_%s" % (time.strftime("%Y%m%d%H%M%S"), uuid.uuid4().hex[:8])
    result["created_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    try:
        os.makedirs(RECEIPT_DIR, exist_ok=True)
        with open(os.path.join(RECEIPT_DIR, result["receipt_id"] + ".json"), "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        result["receipt_saved"] = True
    except OSError as exc:
        result["receipt_saved"] = False
        result["receipt_error"] = str(exc)
        if result["verdict"] == PASS:
            result["verdict"] = PARTIAL
    return result


def verify_receipt(receipt_id: str) -> Dict[str, Any]:
    """영수증을 현재 디스크 상태와 재대조한다. (제미나이 보고 전 독립 검증용)"""
    if not _RECEIPT_ID_RE.match(receipt_id or ""):
        return {"ok": False, "status": "INVALID_ID", "detail": "영수증 ID 형식 오류"}
    rpath = os.path.join(RECEIPT_DIR, receipt_id + ".json")
    if not os.path.isfile(rpath):
        return {"ok": False, "status": "NOT_FOUND", "detail": "영수증 없음: %s" % receipt_id}
    try:
        with open(rpath, "r", encoding="utf-8") as f:
            rec = json.load(f)
    except (OSError, ValueError) as exc:
        return {"ok": False, "status": "CORRUPT", "detail": "영수증 읽기 실패: %s" % exc}

    path = rec.get("path", "")
    cur = capture_state(path) if path else {"exists": False, "hash": ""}
    if not cur["exists"]:
        return {"ok": False, "status": "FILE_MISSING", "receipt": rec, "detail": "파일이 현재 디스크에 없음"}
    if cur["hash"] == rec.get("after", {}).get("hash"):
        return {"ok": rec.get("verdict") == PASS, "status": "MATCH", "receipt": rec,
                "detail": "현재 디스크가 영수증(after) 해시와 일치. 판정=%s" % rec.get("verdict")}
    return {"ok": False, "status": "DRIFTED", "receipt": rec,
            "detail": "영수증 이후 파일이 다시 변경됨(현재 해시 %s...)" % cur["hash"][:12]}


_ICON = {PASS: "🟢", PARTIAL: "🟡", FAIL: "🔴", UNVERIFIED: "⚪"}


def format_header(result: Dict[str, Any]) -> str:
    """응답 맨 첫 줄에 고정되는 판정 헤더 + 상세 체크리스트."""
    v = result.get("verdict", UNVERIFIED)
    lines = ["%s **[완료검증 %s]** receipt=`%s` | `%s`" % (
        _ICON.get(v, "⚪"), v, result.get("receipt_id", "-"), result.get("path", "") or "(경로 없음)")]
    for c in result.get("checks", []):
        lines.append("  - %s %s: %s" % ("✅" if c["ok"] else "❌", c["name"], c["detail"]))
    if not result.get("receipt_saved", False):
        lines.append("  - ⚠️ 영수증 저장 실패: %s" % result.get("receipt_error", "알 수 없음"))
    return "\n".join(lines) + "\n\n"


def is_failure(result: Dict[str, Any]) -> bool:
    return result.get("verdict") in (FAIL, UNVERIFIED)
