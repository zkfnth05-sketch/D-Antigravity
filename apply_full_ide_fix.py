# -*- coding: utf-8 -*-
"""
apply_full_ide_fix.py
=====================================================================
DeepSeek-Antigravity IDE 전면 수리 스크립트 (완성형 / 자율 실행)

수행 작업:
  1) backend/config.json  -> active_project 를 "한국 마케팅봇" 으로 갱신
  2) backend/server.py    -> 정적 파일 서빙(/, /index.html, /css, /js) 라우트 추가
                             + CORS Access-Control-Allow-Private-Network: true 추가
                             + /api/health 헬스체크 추가
  3) frontend/js/app.js   -> run_stock.py 하드코딩 전면 제거
                             + 지능형 초기 파일 자동 탐색/로드
                             + loadFile() Monaco 모델 중복 에러 해결
                             + .workspace-pill 프로젝트 드롭다운 + set_project 전환
                             + btnStartDebate / btnSendAgent / Alt+Enter / Esc 바인딩
  4) [1클릭_실행]_딥시크_안티그래피티_IDE.bat -> 브라우저 오픈 URL 을
                             http://127.0.0.1:8890/ 로 변경
  5) 포트 8890 점유 PID 안전 종료 후 backend/server.py 백그라운드 재기동
  6) py_compile 검증 + 실제 HTTP 200 응답 실측

실행: python apply_full_ide_fix.py
=====================================================================
"""

import os
import re
import sys
import json
import time
import shutil
import socket
import subprocess
import urllib.request
import urllib.error
from datetime import datetime

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.abspath(__file__))
BACKEND_DIR = os.path.join(ROOT, "backend")
FRONTEND_DIR = os.path.join(ROOT, "frontend")
CONFIG_PATH = os.path.join(BACKEND_DIR, "config.json")
SERVER_PATH = os.path.join(BACKEND_DIR, "server.py")
APPJS_PATH = os.path.join(FRONTEND_DIR, "js", "app.js")
BAT_PATH = os.path.join(ROOT, "[1클릭_실행]_딥시크_안티그래피티_IDE.bat")

TARGET_PROJECT = r"C:\Users\zkfnt\Desktop\한국 마케팅봇"
HOST = "127.0.0.1"
PORT = 8890
BASE_URL = "http://%s:%d" % (HOST, PORT)

REPORT = []


def log(step, msg, ok=True):
    mark = "[OK]" if ok else "[FAIL]"
    line = "%s [%s] %s" % (mark, step, msg)
    print(line)
    REPORT.append((ok, step, msg))


def backup(path):
    if not os.path.isfile(path):
        return ""
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    dst = "%s.bak_%s" % (path, stamp)
    try:
        shutil.copy2(path, dst)
        return dst
    except Exception:
        return ""


def read_text(path):
    for enc in ("utf-8", "utf-8-sig", "cp949", "euc-kr"):
        try:
            with open(path, "r", encoding=enc) as f:
                return f.read()
        except Exception:
            continue
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        return f.read()


def write_text(path, content):
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(content)


# =====================================================================
# STEP 1 - backend/config.json 갱신
# =====================================================================
def step1_config():
    print("\n" + "=" * 70)
    print("STEP 1 - backend/config.json active_project 갱신")
    print("=" * 70)

    if not os.path.isdir(TARGET_PROJECT):
        log("1", "대상 프로젝트 디렉터리 부재: %s" % TARGET_PROJECT, ok=False)
        return False

    cfg = {}
    if os.path.isfile(CONFIG_PATH):
        try:
            cfg = json.loads(read_text(CONFIG_PATH))
        except Exception as e:
            log("1", "기존 config.json 파싱 실패(신규 생성): %s" % e, ok=False)
            cfg = {}

    backup(CONFIG_PATH)

    previous = cfg.get("active_project", "")
    cfg["active_project"] = TARGET_PROJECT

    monitored = cfg.get("monitored_projects")
    if not isinstance(monitored, list):
        monitored = []
    if TARGET_PROJECT not in monitored:
        monitored.insert(0, TARGET_PROJECT)
    cfg["monitored_projects"] = monitored

    cfg.setdefault("server_port", PORT)
    cfg.setdefault("watchdog_enabled", True)
    cfg.setdefault("watchdog_interval_seconds", 2)
    cfg.setdefault("theme", "dark")

    write_text(CONFIG_PATH, json.dumps(cfg, indent=2, ensure_ascii=False))

    verify = json.loads(read_text(CONFIG_PATH))
    if verify.get("active_project") == TARGET_PROJECT:
        log("1", "active_project 갱신 완료: %s -> %s" % (previous, TARGET_PROJECT))
        return True
    log("1", "active_project 갱신 검증 실패", ok=False)
    return False
