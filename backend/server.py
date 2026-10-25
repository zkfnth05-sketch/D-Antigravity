# -*- coding: utf-8 -*-
"""
DeepSeek-Antigravity IDE Backend Server (aiohttp)
Independent Desktop IDE Backend Engine with 5-Role Antigravity Triad
Port: 8890

전면 개편 사항:
  1) REST 신설: GET /api/workspace/projects, POST /api/workspace/set_project
  2) OPTIONS 라우터에 신규 2개 엔드포인트 CORS 등록
  3) ws_debate_handler / ws_agent_handler 진입 시
     - target_file → pm.auto_bind_project_for_file() 자동 바인딩
     - target_file 미지정 시 pm.active_project 내 유효 파이썬 파일 지능 탐색
  4) save_file / generate_diff / read_file 등 모든 파일 작업에 pm.resolve_path() 적용
  5) [Turn 0 선제적 전수 독해 연결]
     ws_agent_handler → executor.run_autonomous_turn(prompt, context_file=target_file,
     target_file=target_file) 로 target_file 을 반드시 전달하여,
     AgentExecutor 가 진입 즉시 대상 파일 전체를 읽어 시스템 프롬프트에 주입하도록 함
"""
import os
import sys
import json
import asyncio
import difflib
from datetime import datetime
from aiohttp import web, WSMsgType

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from core.deepseek_client import DeepSeekClient
from core.project_manager import ProjectManager
from core.session_memory import SessionMemory
from core.council_debate import CouncilDebateEngine
from core.agent_executor import AgentExecutor
from core.watchdog_sentinel import WatchdogSentinel
from core.cloudcode_bridge import CloudCodeBridge

CONFIG_PATH = os.path.join(BASE_DIR, "config.json")


def load_config():
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {
        "api_key": "",
        "base_url": "https://api.deepseek.com",
        "default_project": r"C:\Users\zkfnt\Desktop\한국 마케팅봇_Sandbox",
        "models": {
            "chat": "deepseek-chat",
            "reasoner": "deepseek-reasoner"
        }
    }


config = load_config()
pm = ProjectManager(CONFIG_PATH)
llm = DeepSeekClient(
    api_key=pm.config.get("deepseek_api_key", "") or pm.config.get("api_key", ""),
    model_reasoner=pm.config.get("model_reasoner", "deepseek-reasoner"),
    model_chat=pm.config.get("model_chat", "deepseek-chat")
)
memory = SessionMemory(max_history_turns=24)
council = CouncilDebateEngine(llm, pm)
executor = AgentExecutor(llm, pm)
watchdog = WatchdogSentinel(pm)
bridge = CloudCodeBridge(llm, council, pm)


# =========================================================================
# 공통 유틸리티
# =========================================================================

def decode_bytes(b: bytes) -> str:
    if not b:
        return ""
    for enc in ["utf-8", "cp949", "euc-kr"]:
        try:
            return b.decode(enc).strip()
        except Exception:
            pass
    return b.decode("latin1", errors="replace").strip()


def json_cors_response(data, status=200):
    return web.json_response(data, status=status, headers={
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "*",
        "Access-Control-Allow-Headers": "*"
    })


async def options_handler(request):
    return web.Response(headers={
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "*",
        "Access-Control-Allow-Headers": "*"
    })


def safe_resolve(path: str) -> str:
    """
    모든 파일 작업의 단일 경로 정규화 창구.
    pm.resolve_path() 를 우선 사용하고, 실패 시 절대경로 폴백.
    """
    if not path:
        return pm.active_project
    try:
        return pm.resolve_path(path)
    except Exception:
        if os.path.isabs(path):
            return os.path.abspath(path)
        return os.path.abspath(os.path.join(pm.active_project, path))


def read_text_safe(full_path: str, limit=None):
    """인코딩 자동 감지 파일 읽기. (content, encoding) 반환."""
    content = ""
    encoding_used = "utf-8"
    for enc in ["utf-8", "cp949", "euc-kr", "utf-16"]:
        try:
            with open(full_path, "r", encoding=enc) as f:
                content = f.read(limit)
                encoding_used = enc
                break
        except Exception:
            continue
    return content, encoding_used


# 기본 파이썬 파일 지능 탐색 우선순위
_PRIORITY_NAMES = [
    "main.py", "app.py", "server.py", "run.py", "manage.py",
    "start.py", "index.py", "bot.py", "daemon.py"
]
_SKIP_DIRS = {
    ".git", "__pycache__", "node_modules", "venv", "env", ".venv",
    "build", "dist", ".idea", ".vscode", "site-packages", "assets", "data"
}


def discover_default_python_file(project_root: str = None) -> str:
    """
    active_project 내에서 유효한 첫 번째 파이썬 파일을 지능적으로 탐색.
    탐색 우선순위:
      1) main.py / app.py / server.py 등 표준 진입점
      2) run_*.py (실행 스크립트)
      3) 루트의 그 외 .py
      4) 하위 디렉터리 재귀 탐색
    반환: 프로젝트 루트 기준 상대 경로 (실패 시 빈 문자열)
    """
    root = project_root or pm.active_project
    if not root or not os.path.isdir(root):
        return ""

    try:
        entries = [
            e for e in os.scandir(root)
            if e.is_file() and e.name.lower().endswith(".py")
            and not e.name.startswith("_")
        ]
    except Exception:
        return ""

    names = {e.name.lower(): e.name for e in entries}

    # 1) 표준 진입점 우선
    for candidate in _PRIORITY_NAMES:
        if candidate in names:
            return names[candidate]

    # 2) run_*.py 실행 스크립트
    run_scripts = sorted(
        [e.name for e in entries if e.name.lower().startswith("run_")],
        key=lambda n: len(n)
    )
    if run_scripts:
        return run_scripts[0]

    # 3) 루트의 그 외 .py (짧은 이름 우선)
    others = sorted([e.name for e in entries], key=lambda n: (len(n), n.lower()))
    if others:
        return others[0]

    # 4) 하위 디렉터리 재귀 탐색
    for current, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS and not d.startswith(".")]
        py_files = sorted([f for f in files if f.lower().endswith(".py") and not f.startswith("_")])
        if py_files:
            rel = os.path.relpath(os.path.join(current, py_files[0]), root)
            return rel.replace("\\", "/")

    return ""


def bind_and_resolve_target(target_file: str) -> str:
    """
    WS 핸들러 공통 진입 로직.
      1) target_file 이 유효하면 해당 파일 기준으로 active_project 자동 바인딩
      2) target_file 이 비었거나 기본값이면 active_project 내 파이썬 파일 지능 탐색
    반환: 최종 target_file (프로젝트 루트 기준 상대 경로 또는 절대 경로)
    """
    raw = (target_file or "").strip().strip("\"'`").strip()

    # 기본값/플레이스홀더 판별
    placeholder = raw.lower() in ("", "run_stock.py", "none", "null", "undefined", "default")

    if not placeholder:
        try:
            resolved = safe_resolve(raw)
            pm.auto_bind_project_for_file(resolved)
            return raw
        except Exception:
            pass

    # 지능 탐색으로 기본 파일 결정
    discovered = discover_default_python_file(pm.active_project)
    if discovered:
        try:
            pm.auto_bind_project_for_file(safe_resolve(discovered))
        except Exception:
            pass
        return discovered

    return ""


# =========================================================================
# REST Endpoints
# =========================================================================

async def get_status(request):
    return json_cors_response({
        "status": "ready",
        "name": "DeepSeek-Antigravity IDE",
        "version": "1.1.0",
        "active_project": pm.active_project,
        "api_configured": bool(config.get("api_key") or config.get("deepseek_api_key")),
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    })


async def get_workspace_projects(request):
    """GET /api/workspace/projects — 데스크톱 프로젝트 목록 + 활성 프로젝트 정보."""
    try:
        projects = pm.list_available_projects()
    except Exception as e:
        return json_cors_response({
            "status": "error",
            "detail": f"프로젝트 목록 조회 실패: {e}",
            "projects": [],
            "active_project": pm.active_project
        }, status=500)

    active = pm.active_project
    active_info = None
    for p in projects:
        if p.get("is_active"):
            active_info = p
            break

    return json_cors_response({
        "status": "ok",
        "active_project": active,
        "active_project_name": os.path.basename(active.rstrip("\\/")) if active else "",
        "active_project_info": active_info,
        "count": len(projects),
        "projects": projects
    })


async def set_workspace_project(request):
    """POST /api/workspace/set_project — active_project 즉시 전환 + config.json 영구 반영."""
    try:
        data = await request.json()
    except Exception:
        data = {}

    raw_path = (data.get("path") or data.get("project_path") or "").strip().strip("\"'`").strip()
    if not raw_path:
        return json_cors_response({
            "status": "error",
            "detail": "path 인자가 비어 있습니다.",
            "active_project": pm.active_project
        }, status=400)

    previous = pm.active_project
    target = safe_resolve(raw_path)

    ok = False
    try:
        ok = pm.set_active_project(target)
    except Exception as e:
        return json_cors_response({
            "status": "error",
            "detail": f"프로젝트 전환 예외: {e}",
            "active_project": previous
        }, status=500)

    if not ok:
        return json_cors_response({
            "status": "error",
            "detail": f"유효한 프로젝트 디렉터리가 아닙니다: {target}",
            "requested_path": target,
            "active_project": previous
        }, status=400)

    # config.json 영구 반영 (ProjectManager 저장 + 서버 config 이중 보장)
    saved = False
    try:
        saved = bool(pm.save_config())
    except Exception:
        saved = False

    if not saved:
        try:
            config["active_project"] = pm.active_project
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(config, f, indent=2, ensure_ascii=False)
            saved = True
        except Exception:
            saved = False

    return json_cors_response({
        "status": "ok",
        "message": "프로젝트가 전환되었습니다.",
        "previous_project": previous,
        "active_project": pm.active_project,
        "active_project_name": os.path.basename(pm.active_project.rstrip("\\/")),
        "config_saved": saved,
        "config_path": CONFIG_PATH
    })


async def get_workspace_tree(request):
    root_path = pm.active_project
    if not os.path.exists(root_path):
        return json_cors_response({"error": f"Workspace directory not found: {root_path}", "tree": []})

    def build_tree(current_dir: str, depth: int = 0, max_depth: int = 4):
        if depth > max_depth:
            return []
        items = []
        try:
            entries = sorted(os.scandir(current_dir), key=lambda e: (not e.is_dir(), e.name.lower()))
            for entry in entries:
                if entry.name.startswith((".", "__pycache__", "node_modules", ".git", "venv", "env")):
                    continue
                rel_path = os.path.relpath(entry.path, root_path).replace("\\", "/")
                if entry.is_dir():
                    children = build_tree(entry.path, depth + 1, max_depth)
                    items.append({
                        "name": entry.name,
                        "path": rel_path,
                        "type": "directory",
                        "children": children
                    })
                else:
                    size = entry.stat().st_size
                    items.append({
                        "name": entry.name,
                        "path": rel_path,
                        "type": "file",
                        "size": size
                    })
        except Exception:
            pass
        return items

    tree = build_tree(root_path)
    return json_cors_response({"workspace": root_path, "tree": tree})


async def read_file(request):
    data = await request.json()
    file_path = data.get("path", "")

    # 4) pm.resolve_path() 기반 안전 정규화
    full_path = safe_resolve(file_path)

    if not os.path.exists(full_path):
        return json_cors_response({"detail": f"File not found: {file_path}"}, status=404)

    try:
        content, encoding_used = read_text_safe(full_path)
        stat = os.stat(full_path)
        return json_cors_response({
            "path": file_path,
            "full_path": full_path,
            "content": content,
            "encoding": encoding_used,
            "size": stat.st_size,
            "lines": len(content.splitlines())
        })
    except Exception as e:
        return json_cors_response({"detail": str(e)}, status=500)


async def save_file(request):
    data = await request.json()
    file_path = data.get("path", "")
    content = data.get("content", "")

    # 4) pm.resolve_path() 기반 안전 정규화
    full_path = safe_resolve(file_path)

    try:
        parent = os.path.dirname(full_path)
        if parent:
            os.makedirs(parent, exist_ok=True)

        with open(full_path, "w", encoding="utf-8") as f:
            f.write(content)

        syntax_res = {"valid": True}
        if full_path.lower().endswith(".py"):
            syntax_res = await council._run_python_syntax_check(full_path)

        return json_cors_response({
            "status": "saved",
            "path": file_path,
            "full_path": full_path,
            "active_project": pm.active_project,
            "syntax": syntax_res
        })
    except Exception as e:
        return json_cors_response({"detail": str(e)}, status=500)


async def generate_diff(request):
    data = await request.json()
    file_path = data.get("path", "")
    mod_content = data.get("modified_content", "")

    # 4) pm.resolve_path() 기반 안전 정규화
    full_path = safe_resolve(file_path)

    orig_content = ""
    if os.path.exists(full_path):
        orig_content, _ = read_text_safe(full_path)

    orig_lines = orig_content.splitlines(keepends=True)
    mod_lines = mod_content.splitlines(keepends=True)

    diff = list(difflib.unified_diff(
        orig_lines, mod_lines,
        fromfile=f"a/{file_path}",
        tofile=f"b/{file_path}"
    ))
    diff_text = "".join(diff)
    return json_cors_response({
        "path": file_path,
        "full_path": full_path,
        "active_project": pm.active_project,
        "has_changes": bool(diff_text.strip()),
        "diff": diff_text,
        "orig_lines": len(orig_lines),
        "new_lines": len(mod_lines)
    })


async def run_terminal_command(request):
    data = await request.json()
    cmd = data.get("command", "")
    try:
        proc = await asyncio.create_subprocess_shell(
            cmd,
            cwd=pm.active_project,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await proc.communicate()
        out_str = decode_bytes(stdout)
        err_str = decode_bytes(stderr)
        return json_cors_response({
            "command": cmd,
            "cwd": pm.active_project,
            "exit_code": proc.returncode,
            "stdout": out_str,
            "stderr": err_str
        })
    except Exception as e:
        return json_cors_response({
            "command": cmd,
            "cwd": pm.active_project,
            "exit_code": -1,
            "stdout": "",
            "stderr": str(e)
        })


# =========================================================================
# WebSockets: 5-Role Collaborative Debate Pipeline
# =========================================================================

async def ws_debate_handler(request):
    ws = web.WebSocketResponse()
    await ws.prepare(request)

    try:
        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                data = json.loads(msg.data)
                task = data.get("task", "")
                raw_target = data.get("target_file", "")

                # 3) 자동 바인딩 + 지능적 기본 파일 탐색
                target_file = bind_and_resolve_target(raw_target)

                await ws.send_json({
                    "type": "session_init",
                    "task": task,
                    "target_file": target_file,
                    "active_project": pm.active_project
                })

                async for step in council.run_debate_session(
                    target_file=target_file,
                    issue_description=task
                ):
                    await ws.send_json(step)

                await ws.send_json({"type": "session_complete"})
            elif msg.type == WSMsgType.ERROR:
                break
    except Exception as e:
        try:
            await ws.send_json({"type": "error", "message": str(e)})
        except Exception:
            pass

    return ws


async def ws_agent_handler(request):
    ws = web.WebSocketResponse()
    await ws.prepare(request)

    try:
        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                data = json.loads(msg.data)
                action = data.get("action", "")

                if action == "reset_session":
                    memory.clear()
                    await ws.send_json({"type": "session_reset", "message": "새 세션이 시작되었습니다."})
                    continue

                prompt = data.get("prompt", "")
                raw_target = data.get("target_file", "") or data.get("context_file", "")

                # 3) 자동 바인딩 + 지능적 기본 파일 탐색
                target_file = bind_and_resolve_target(raw_target)

                await ws.send_json({
                    "type": "turn_start",
                    "prompt": prompt,
                    "target_file": target_file,
                    "active_project": pm.active_project
                })

                # ------------------------------------------------------------------
                # [Turn 0 선제적 전수 독해 연결]
                # 프런트엔드가 웹소켓으로 보낸 target_file 을 executor 로 반드시 전달.
                # context_file / target_file 두 인자 모두에 넘겨, AgentExecutor 가
                # 진입 즉시 대상 파일 전체를 읽어 시스템 프롬프트에 주입하도록 한다.
                # (이 인자가 빠지면 Turn 0 전수 독해가 발동하지 않는다)
                # ------------------------------------------------------------------
                async for event in executor.run_autonomous_turn(
                    prompt,
                    context_file=target_file,
                    target_file=target_file
                ):
                    await ws.send_json(event)

                await ws.send_json({"type": "turn_end"})
            elif msg.type == WSMsgType.ERROR:
                break
    except Exception as e:
        try:
            await ws.send_json({"type": "error", "message": str(e)})
        except Exception:
            pass

    return ws


# =========================================================================
# App Initialization & Routing
# =========================================================================

app = web.Application()

# 2) CORS OPTIONS 라우터 — 신규 2개 엔드포인트 포함 전량 등록
CORS_ROUTES = [
    "/api/status",
    "/api/workspace/tree",
    "/api/workspace/read",
    "/api/workspace/save",
    "/api/workspace/diff",
    "/api/workspace/projects",
    "/api/workspace/set_project",
    "/api/terminal/run",
]
for route in CORS_ROUTES:
    app.router.add_route("OPTIONS", route, options_handler)

app.router.add_get("/api/status", get_status)
app.router.add_get("/api/workspace/tree", get_workspace_tree)
app.router.add_post("/api/workspace/read", read_file)
app.router.add_post("/api/workspace/save", save_file)
app.router.add_post("/api/workspace/diff", generate_diff)
app.router.add_post("/api/terminal/run", run_terminal_command)

# 1) 신규 워크스페이스 프로젝트 관리 엔드포인트
app.router.add_get("/api/workspace/projects", get_workspace_projects)
app.router.add_post("/api/workspace/set_project", set_workspace_project)

app.router.add_get("/ws/debate", ws_debate_handler)
app.router.add_get("/ws/agent", ws_agent_handler)

# Antigravity IDE Internal CloudCode Bridge Routes
app.router.add_post("/v1internal:loadCodeAssist", bridge.handle_load_code_assist)
app.router.add_post("/v1internal:fetchAvailableModels", bridge.handle_fetch_available_models)
app.router.add_post("/v1internal:fetchUserInfo", bridge.handle_user_info)
app.router.add_post("/v1internal:onboardUser", bridge.handle_onboard_user)
app.router.add_post("/v1internal:retrieveUserQuotaSummary", bridge.handle_user_quota)
app.router.add_post("/v1internal:retrieveUserQuota", bridge.handle_user_quota)
app.router.add_post("/v1internal:listExperiments", bridge.handle_experiments)
app.router.add_get("/v1internal/cascadeNuxes", bridge.handle_cascade_nuxes)
app.router.add_post("/v1internal/cascadeNuxes", bridge.handle_cascade_nuxes)
app.router.add_post("/v1internal:streamGenerateContent", bridge.handle_stream_generate_content)
app.router.add_post("/v1internal:generateContent", bridge.handle_stream_generate_content)
app.router.add_route("*", "/{tail:.*}", bridge.fallback_handler)

if __name__ == "__main__":
    print("[DeepSeek-Antigravity IDE Backend] Serving on http://127.0.0.1:8890")
    print(f"[Active Project] {pm.active_project}")
    web.run_app(app, host="127.0.0.1", port=8890)
