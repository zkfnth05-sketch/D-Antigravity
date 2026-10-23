# -*- coding: utf-8 -*-
"""
Antigravity AgentExecutor — 전면 개편판
=========================================================================
개편 핵심 4대 원칙:
  1) _build_context_inventory() 의 'kmarket-marketing-engine' 하드코딩 완전 제거
     → self.pm.active_project 하위 디렉터리를 동적 스캔하여 실제 파이썬 패키지 /
       주요 모듈 폴더를 지능적으로 식별 (마케팅봇·아우라·어떤 앱이든 고유 구조 파악)
  2) execute_user_request / run_autonomous_turn 진입 시 context_file 이 주어지면
     self.pm.auto_bind_project_for_file(context_file) 로 active_project 즉시 rebind
  3) tool_read_file / tool_write_file / tool_list_files 등 모든 파일 작업에서
     self.pm.resolve_path() 우선 정규화
  4) tool_write_file 실행 후 대상이 .py 이면 즉시 py_compile 문법 검증 →
     반환 객체(syntax) + SessionMemory(작업 메모리)에 명확히 기록
=========================================================================
"""
import os
import re
import json
import asyncio
import py_compile
from typing import Dict, Any, List, Callable, Optional

from core.session_memory import SessionMemory


def decode_bytes(b: bytes) -> str:
    if not b:
        return ""
    for enc in ["cp949", "utf-8", "euc-kr"]:
        try:
            return b.decode(enc).strip()
        except Exception:
            pass
    return b.decode("latin1", errors="replace").strip()


# =========================================================================
# DeepSeek 공식 Function Calling Tools 스키마
# =========================================================================
NATIVE_TOOLS_SCHEMA = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "프로젝트 내 파일의 실제 전체 코드를 처음부터 끝까지 온전히 읽어옵니다. 절대로 중간에 자르지 않습니다.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "읽을 파일의 상대 또는 절대 경로"}
                },
                "required": ["path"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "프로젝트 내 파일에 코드를 실제로 작성하거나 수정합니다. 화면 에디터에 즉시 반영됩니다.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "수정/저장할 파일 경로"},
                    "content": {"type": "string", "description": "작성할 전체 코드 내용"}
                },
                "required": ["path", "content"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "run_command",
            "description": "터미널 명령어나 파이썬 스크립트(pytest, python xxx.py 등)를 프로젝트 디렉터리에서 실제로 구동하고 결과를 확인합니다.",
            "parameters": {
                "type": "object",
                "properties": {
                    "cmd": {"type": "string", "description": "실행할 터미널 명령어"}
                },
                "required": ["cmd"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "프로젝트 내 특정 폴더의 파일 및 하위 디렉터리 목록을 조회합니다.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "조회할 폴더 상대 경로 (기본값 빈문자열: 프로젝트 루트)"}
                }
            }
        }
    }
]

# 인벤토리 스캔 시 제외할 노이즈 디렉터리
_SKIP_DIRS = {
    ".git", ".idea", ".vscode", "__pycache__", "node_modules", "venv", "env",
    ".venv", "build", "dist", "site-packages", ".pytest_cache", ".mypy_cache",
    ".next", ".cache", "logs", "log", "tmp", "temp", "backup", "backups",
    ".gradle", "target", "bin", "obj", "assets", "static", "media"
}

# 주요 모듈 폴더로 간주할 이름 힌트 (가중치 부여)
_KEY_DIR_HINTS = (
    "core", "engine", "module", "modules", "app", "apps", "src", "source",
    "backend", "frontend", "server", "api", "service", "services", "agent",
    "agents", "bot", "bots", "crawler", "scraper", "pipeline", "worker",
    "workers", "task", "tasks", "scheduler", "daemon", "utils", "common",
    "lib", "libs", "handler", "handlers", "router", "routes", "model",
    "models", "view", "views", "controller", "controllers", "brands",
    "marketing", "publish", "publisher", "content", "factory", "collector"
)


class AgentExecutor:
    """
    Antigravity DeepSeek 엔터프라이즈급 공식 Function Calling 에이전트
    - 다중 대화 맥락 기억 엔진 (SessionMemory) 완벽 내장
    - 파일 자름 현상(Truncation) 영구 제거 (최대 10만 자 온전한 전수 독해)
    - 이전 대화, 수정 이력, 도구 실행 결과를 100% 기억하여 자율 협업
    - 프로젝트 구조 동적 인벤토리 (하드코딩 제로)
    - context_file 기반 active_project 자동 rebind
    - .py 저장 시 즉시 py_compile 문법 검증
    """

    def __init__(self, deepseek_client, project_manager):
        self.llm = deepseek_client
        self.pm = project_manager
        self.memory = SessionMemory(max_history_turns=24)

    # =====================================================================
    # [3] 경로 정규화 단일 창구 — pm.resolve_path() 우선
    # =====================================================================
    def _resolve_path(self, path: str) -> str:
        """
        모든 파일 작업의 단일 경로 정규화 창구.
        pm.resolve_path() 를 최우선 사용하고, 실패 시에만 절대/상대 폴백.
        """
        if not path:
            return self.pm.active_project
        try:
            resolved = self.pm.resolve_path(path)
            if resolved:
                return resolved
        except Exception:
            pass
        if os.path.isabs(path):
            return os.path.abspath(path)
        return os.path.abspath(os.path.join(self.pm.active_project, path))

    # =====================================================================
    # [2] context_file 기반 active_project 자동 rebind
    # =====================================================================
    def _rebind_project_for_context(self, context_file: str) -> str:
        """
        context_file 이 주어지면 해당 파일이 속한 프로젝트로 active_project 즉시 전환.
        반환: rebind 후의 active_project 경로
        """
        raw = (context_file or "").strip().strip("\"'`").strip()
        if not raw:
            return self.pm.active_project

        try:
            resolved = self._resolve_path(raw)
            bound = self.pm.auto_bind_project_for_file(resolved)
            if bound:
                return bound
        except Exception:
            pass
        return self.pm.active_project

    # =====================================================================
    # [4] 파이썬 문법 검증 (py_compile)
    # =====================================================================
    def _verify_python_syntax(self, full_path: str) -> Dict[str, Any]:
        """
        대상 .py 파일에 대해 py_compile 문법 검증을 즉시 수행.
        반환: {"checked": bool, "valid": bool, "message": str, "error": str}
        """
        result = {
            "checked": False,
            "valid": True,
            "message": "파이썬 파일이 아니므로 문법 검증 생략",
            "error": ""
        }
        if not full_path.lower().endswith(".py"):
            return result

        result["checked"] = True
        try:
            py_compile.compile(full_path, cfile=None, doraise=True, optimize=-1)
            result["valid"] = True
            result["message"] = "✅ py_compile 문법 검증 통과 (오류 0건)"
            return result
        except py_compile.PyCompileError as e:
            result["valid"] = False
            result["error"] = str(e).strip()
            result["message"] = f"❌ py_compile 문법 오류 발견: {result['error'][:400]}"
            return result
        except Exception as e:
            result["valid"] = False
            result["error"] = str(e).strip()
            result["message"] = f"❌ 문법 검증 실행 오류: {result['error'][:400]}"
            return result

    # =====================================================================
    # [1] 프로젝트 구조 동적 인벤토리 (하드코딩 완전 제거)
    # =====================================================================
    def _scan_subdirectory(self, dir_path: str, max_items: int = 18) -> List[str]:
        """단일 하위 디렉터리의 항목 목록 (노이즈 제외)."""
        items: List[str] = []
        try:
            entries = sorted(os.scandir(dir_path), key=lambda e: (not e.is_dir(), e.name.lower()))
            for entry in entries:
                if entry.name.startswith((".", "__pycache__")):
                    continue
                items.append(f"{entry.name}/" if entry.is_dir() else entry.name)
                if len(items) >= max_items:
                    break
        except Exception:
            pass
        return items

    def _is_python_package(self, dir_path: str) -> bool:
        """__init__.py 존재 여부로 실제 파이썬 패키지 판별."""
        try:
            return os.path.isfile(os.path.join(dir_path, "__init__.py"))
        except Exception:
            return False

    def _count_python_files(self, dir_path: str, max_depth: int = 2) -> int:
        """디렉터리 내 .py 파일 개수 (제한 깊이)."""
        count = 0
        base_depth = dir_path.rstrip("\\/").count(os.sep)
        try:
            for current, dirs, files in os.walk(dir_path):
                depth = current.rstrip("\\/").count(os.sep) - base_depth
                if depth >= max_depth:
                    dirs[:] = []
                dirs[:] = [d for d in dirs if d not in _SKIP_DIRS and not d.startswith(".")]
                count += sum(1 for f in files if f.lower().endswith(".py"))
                if count > 500:
                    break
        except Exception:
            pass
        return count

    def _discover_key_directories(self, project_root: str, max_dirs: int = 8) -> List[Dict[str, Any]]:
        """
        active_project 하위 디렉터리를 동적 스캔하여
        '실제 파이썬 패키지' 또는 '주요 모듈 폴더'를 지능적으로 식별.
        점수 기반 정렬 후 상위 max_dirs 개 반환.
        """
        candidates: List[Dict[str, Any]] = []
        try:
            entries = [e for e in os.scandir(project_root) if e.is_dir()]
        except Exception:
            return []

        for entry in entries:
            name = entry.name
            if name.startswith(".") or name.lower() in _SKIP_DIRS:
                continue

            is_pkg = self._is_python_package(entry.path)
            py_count = self._count_python_files(entry.path)
            if py_count == 0 and not is_pkg:
                continue  # 파이썬과 무관한 폴더는 인벤토리에서 제외

            score = 0
            if is_pkg:
                score += 100
            score += min(py_count, 60)
            lname = name.lower()
            if lname in _KEY_DIR_HINTS:
                score += 40
            elif any(h in lname for h in _KEY_DIR_HINTS):
                score += 20

            candidates.append({
                "name": name,
                "path": entry.path,
                "is_package": is_pkg,
                "py_count": py_count,
                "score": score
            })

        candidates.sort(key=lambda c: (-c["score"], c["name"].lower()))
        return candidates[:max_dirs]

    def _build_context_inventory(self) -> str:
        """
        [개편 핵심] 'kmarket-marketing-engine' 하드코딩 완전 제거.
        active_project 내부를 동적으로 스캔하여 어떤 앱이든 고유 구조를 정확히 파악.
        """
        try:
            proj = self.pm.active_project
            if not proj or not os.path.isdir(proj):
                return f"- 현재 작업 프로젝트: (경로 없음) {proj}"

            proj_name = os.path.basename(proj.rstrip("\\/"))

            # 1) 루트 파일 / 루트 디렉터리 분리 수집
            root_files: List[str] = []
            root_dirs: List[str] = []
            try:
                entries = sorted(os.scandir(proj), key=lambda e: (not e.is_dir(), e.name.lower()))
                for entry in entries:
                    if entry.name.startswith((".", "__pycache__")):
                        continue
                    if entry.is_dir():
                        if entry.name.lower() in _SKIP_DIRS:
                            continue
                        root_dirs.append(entry.name)
                    else:
                        root_files.append(entry.name)
            except Exception:
                pass

            # 2) 주요 모듈 폴더 지능 식별 (동적)
            key_dirs = self._discover_key_directories(proj)

            lines: List[str] = []
            lines.append(f"- 현재 작업 프로젝트: {proj_name}")
            lines.append(f"- 프로젝트 절대 경로: {proj}")
            lines.append(f"- 루트 파일 목록 ({len(root_files)}개): {', '.join(root_files[:30]) or '없음'}")
            lines.append(f"- 루트 디렉터리 ({len(root_dirs)}개): {', '.join(root_dirs[:20]) or '없음'}")

            # 3) 주요 모듈 폴더별 상세 인벤토리 (동적)
            if key_dirs:
                lines.append("- [주요 모듈 폴더 동적 인벤토리]:")
                for d in key_dirs:
                    tag = "파이썬 패키지" if d["is_package"] else "모듈 폴더"
                    items = self._scan_subdirectory(d["path"], max_items=18)
                    lines.append(
                        f"  · {d['name']}/ ({tag}, .py {d['py_count']}개): "
                        f"{', '.join(items) or '비어 있음'}"
                    )
            else:
                lines.append("- [주요 모듈 폴더 동적 인벤토리]: 하위 파이썬 모듈 폴더 없음 (단일 파일 프로젝트)")

            # 4) 실행 진입점 후보 안내
            entry_candidates = [
                f for f in root_files
                if f.lower().endswith(".py") and (
                    f.lower() in ("main.py", "app.py", "server.py", "run.py", "manage.py")
                    or f.lower().startswith("run_")
                )
            ]
            if entry_candidates:
                lines.append(f"- 실행 진입점 후보: {', '.join(entry_candidates[:10])}")

            return "\n".join(lines)
        except Exception:
            return ""

    # =====================================================================
    # Tool: read_file
    # =====================================================================
    def tool_read_file(self, path: str) -> Dict[str, Any]:
        full_path = self._resolve_path(path)
        res = self.pm.read_file_content(full_path)
        if res.get("success"):
            content = res.get("content", "")
            lines = content.splitlines()
            # 절대로 중간에 자르지 않고 최대 10만 자까지 온전히 제공
            safe_content = content[:100000]
            return {
                "success": True,
                "path": full_path,
                "filename": os.path.basename(full_path),
                "total_lines": len(lines),
                "content": content,
                "full_text": safe_content
            }
        return {"success": False, "error": f"파일 열기 실패: {res.get('error')}", "path": full_path}

    # =====================================================================
    # Tool: write_file (+ [4] py_compile 즉시 검증)
    # =====================================================================
    def tool_write_file(self, path: str, content: str) -> Dict[str, Any]:
        full_path = self._resolve_path(path)
        res = self.pm.save_file_content(full_path, content)
        lines = len(content.splitlines())
        filename = os.path.basename(full_path)

        if not res.get("success"):
            return {
                "success": False,
                "path": full_path,
                "filename": filename,
                "lines": lines,
                "content": content,
                "syntax": {
                    "checked": False,
                    "valid": False,
                    "message": "저장 실패로 문법 검증 미실행",
                    "error": ""
                },
                "error": res.get("error", "저장 실패")
            }

        # [4] .py 파일이면 즉시 py_compile 문법 검증
        syntax = self._verify_python_syntax(full_path)

        # 작업 메모리에 파일 수정 이력 + 문법 검증 결과 영구 기록
        if syntax.get("checked"):
            if syntax.get("valid"):
                summary = "DeepSeek 에이전트가 직접 코드 작성/수정 반영 완료 · py_compile 문법 검증 통과"
            else:
                summary = (
                    "DeepSeek 에이전트가 직접 코드 작성/수정 반영 완료 · "
                    f"⚠️ py_compile 문법 오류: {syntax.get('error', '')[:200]}"
                )
        else:
            summary = "DeepSeek 에이전트가 직접 코드 작성/수정 반영 완료"

        try:
            self.memory.record_file_modification(filename, full_path, lines, summary)
        except Exception:
            pass

        return {
            "success": True,
            "path": full_path,
            "filename": filename,
            "lines": lines,
            "content": content,
            "syntax": syntax
        }

    # =====================================================================
    # Tool: run_command
    # =====================================================================
    async def tool_run_command(self, cmd: str) -> str:
        try:
            proc = await asyncio.create_subprocess_shell(
                cmd,
                cwd=self.pm.active_project,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            try:
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=45.0)
                out_str = decode_bytes(stdout)
                err_str = decode_bytes(stderr)
                res = f"[종료 코드: {proc.returncode}]\n"
                if out_str:
                    res += f"STDOUT:\n{out_str[:8000]}\n"
                if err_str:
                    res += f"STDERR:\n{err_str[:8000]}\n"
                return res if (out_str or err_str) else f"[정상 종료 (출력 없음, 코드: {proc.returncode})]"
            except asyncio.TimeoutError:
                proc.kill()
                return "[실행 시간 초과 (45초 초과 프로세스 종료)]"
        except Exception as e:
            return f"명령어 구동 오류: {str(e)}"

    # =====================================================================
    # Tool: list_files
    # =====================================================================
    def tool_list_files(self, rel_path: str = "") -> str:
        target_dir = self._resolve_path(rel_path)
        if not os.path.exists(target_dir):
            return f"폴더가 존재하지 않음: {rel_path}"
        try:
            items = []
            for entry in os.scandir(target_dir):
                if entry.name.startswith((".", "__pycache__")):
                    continue
                tag = "[DIR]" if entry.is_dir() else "[FILE]"
                items.append(f"{tag} {entry.name}")
            return "\n".join(sorted(items)[:100])
        except Exception as e:
            return f"폴더 조회 에러: {e}"

    # =====================================================================
    # 시스템 프롬프트 조립
    # =====================================================================
    def _build_system_prompt(self, context_file: str, inventory: str) -> str:
        return f"""당신은 'Antigravity Studio'의 최고 수석 자율 AI 코딩 엔지니어입니다.
작업 디렉터리: {self.pm.active_project}
현재 화면에 열린 파일: {context_file or '없음'}

[프로젝트 사전 인벤토리]:
{inventory}

🚨 [대표님의 절대 행동 수칙 - 반드시 준수]:
🚨 [핵심 행동 강제 - 텍스트 흉내내기 절대 금지, 실제 도구 호출 의무]:
- 코드를 고쳐야 한다면 절대로 채팅창 텍스트로만 "이렇게 고쳤습니다", "변경 내용: ..."라고 말로 때우지 마십시오.
- 변경할 코드가 있다면 반드시 `write_file` 도구를 실제로 호출하여 디스크의 해당 파일에 전체 코드를 기록하십시오!
- 디스크에 `write_file`을 호출하지 않고 채팅창에 코드 블록만 작성하는 것은 가장 치명적인 불이행(허위 보고)입니다.
- 파일을 수정한 후에는 반드시 `run_command`로 `python -m py_compile [파일명]` 또는 테스트를 실행하여 문법 오류가 없는지 직접 검증하십시오!

1. [되묻기 및 지시대기 전면 금지]:
   - "어느 파일을 읽을까요?", "어느 줄을 고칠까요?", "1번, 2번, 3번 중 골라주세요", "지시를 기다립니다"라는 질문을 절대 하지 마십시오.
   - 당신은 100% 완전 자율 안티그래피티 AI입니다. 스스로 원인을 찾고, 코드를 고치고, 터미널로 검증하십시오.
2. [비굴한 반성문 및 패배주의적 고백 금지]:
   - "정직하게 고백합니다. 저는 아무것도 안 했습니다" 같은 무능한 반성문 작성을 일체 금지합니다.
   - 이미 수정한 내역이 있다면 수정 상태와 diff를 기반으로 당당하게 보고하십시오.
3. [대화 맥락 및 수정 이력 100% 인지]:
   - 당신은 이전 대화 맥락과 작업 메모리를 모두 기억하고 있습니다.
   - 사용자가 "뭘 수정했어?", "아까 그거 왜 그랬어?"라고 물으면 이전 대화와 작업 메모리를 근거로 정확하게 답변하십시오.
4. [코드 독해 완결 원칙 - 중간에 끊기 금지]:
   - 파일은 처음부터 끝까지 완전하게 읽고 분석하십시오.
5. [결과 보고는 3줄 요약]:
   - 📌 변경 파일: [파일명] (00줄 ➔ 00줄)
   - 💡 핵심 해결: [무엇을 왜 어떻게 고쳤는지 1문장]
   - 🔍 검증 결과: [문법 검증 완료, 상단 '🔀 변경점(Diff)' 탭에서 실시간 확인 가능]
6. [100% 한국어 엄격 사용]:
   - 모든 생각, 분석, 보고는 100% 품격 있고 명확한 한국어로만 작성하십시오.
"""
