# -*- coding: utf-8 -*-
"""
DeepSeek Multi-Agent Studio - Antigravity MCP Server
Model Context Protocol (JSON-RPC 2.0 stdio) Bridge for Google Antigravity IDE

Enables authentic DeepSeek-R1 (Architect) + DeepSeek-V3 (Critic and Engineer)
+ QA Sandbox + Git Diff Manager + Project Context Auto-Binding.

핵심 업그레이드:
  1) CONFIG_PATH 표준화 + ProjectManager 신규 계약 완전 연동
  2) 모든 도구 핸들러 진입 시 auto_bind_project_for_file() 로 active_project 자동 전환
  3) deepseek_list_projects / deepseek_switch_project 도구 신설
  4) 도구명·서버명 따옴표/공백 자동 정규화로 알 수 없는 도구 에러 원천 차단
  5) codegen / council_debate 결과에 py_compile 검증 결과 필수 포함
  6) [NEW] 핫 리로드 — codegen 진입 시 core.agent_executor / core.project_manager
     디스크 최신 변경분을 importlib.reload 로 즉시 메모리 동기화
  7) [NEW] 물리적 디스크 검증 게이트 — 기록 전/후 SHA-256 + 파일 크기 대조로
     '거짓 보고(미기록)' 를 원천 차단하고 결과 배너를 강제 주입
"""
import os
import re
import sys
import json
import asyncio
import hashlib
import traceback
from typing import Dict, Any, List, Optional

# Ensure UTF-8 stdio on Windows
if sys.platform == "win32":
    try:
        sys.stdin.reconfigure(encoding="utf-8")
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

# Ensure backend directory is in sys.path
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
BACKEND_DIR = os.path.dirname(CURRENT_DIR)
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

from core.deepseek_client import DeepSeekClient
from core.project_manager import ProjectManager
from core.council_debate import CouncilDebateEngine
from core.agent_executor import AgentExecutor
from core.conversation_context import build_task_with_context
from core.code_tools.search_code import search_code, format_search_result
from core.code_tools.code_eye import CodeEye
from core import completion_gate
from core import work_order

_code_eye = CodeEye()

PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "deepseek-council-studio"
SERVER_VERSION = "1.2.0"

# 표준 설정 파일 경로 (backend/config.json)
CONFIG_PATH = os.path.join(BACKEND_DIR, "config.json")

# 도구명 정규화 시 제거할 문자 집합 (따옴표/공백/제로폭 문자)
_STRIP_CHARS = "\"'` \t\r\n\u200b\u200c\u200d\ufeff"

# [방법 2] 코드블록 추출 정규식 — ```python / ```py / ``` 등 허용
_CODE_BLOCK_RE = re.compile(r"```(?:python|py)?\s*\n?([\s\S]*?)```", re.IGNORECASE)


def normalize_name(value: Any) -> str:
    """
    도구 이름 / 서버 이름에 섞여 들어올 수 있는 따옴표, 공백, 제로폭 문자를
    자동 정규화하여 '알 수 없는 도구' 에러를 원천 방지한다.
    """
    if value is None:
        return ""
    if not isinstance(value, str):
        value = str(value)
    return value.strip(_STRIP_CHARS)


def sha256_of_file(full_path: str) -> Dict[str, Any]:
    """
    파일의 SHA-256 해시와 바이트 크기를 실측한다.
    파일이 없거나 읽기 실패 시 exists=False 를 반환한다(예외 미전파).
    """
    info = {"exists": False, "hash": "", "size": 0}
    try:
        if not full_path or not os.path.isfile(full_path):
            return info
        with open(full_path, "rb") as f:
            data = f.read()
        info["exists"] = True
        info["hash"] = hashlib.sha256(data).hexdigest()
        info["size"] = len(data)
    except Exception:
        pass
    return info


def extract_largest_code_block(text: str) -> str:
    """
    [방법 2] LLM 응답 텍스트에서 가장 긴 코드블록을 추출한다.
    - ```python / ```py / ``` 등 언어 태그 허용
    - 여러 블록이 있으면 '가장 긴 블록'을 완성 코드로 간주
    - 3줄 미만의 사소한 블록은 무시 (빈 응답/설명 블록 오탐 방지)
    """
    if not text or not isinstance(text, str):
        return ""
    try:
        blocks = _CODE_BLOCK_RE.findall(text)
    except Exception:
        return ""
    if not blocks:
        return ""

    cleaned = [b.strip() for b in blocks if b and b.strip()]
    if not cleaned:
        return ""

    candidate = max(cleaned, key=lambda b: len(b))
    if len(candidate.splitlines()) < 3:
        return ""
    return candidate


class DeepSeekMcpServer:
    def __init__(self, config_path: Optional[str] = None):
        # 1) CONFIG_PATH 표준화
        self.config_path = config_path or CONFIG_PATH
        self.load_config()

        # 2) ProjectManager 신규 계약 연동 — config.json 파일 경로를 그대로 전달
        #    (ProjectManager 가 파일/디렉터리 양쪽을 모두 수용하도록 개편됨)
        self.pm = ProjectManager(self.config_path)

        # config.json 의 active_project 가 유효하면 ProjectManager 쪽으로 재확정
        cfg_active = self.config.get("active_project")
        if cfg_active and os.path.isdir(cfg_active):
            try:
                self.pm.set_active_project(cfg_active)
            except Exception:
                pass

        self.llm = DeepSeekClient(
            api_key=self.config.get("api_key", ""),
            model_reasoner=self.config.get("model_reasoner", "deepseek-reasoner"),
            model_chat=self.config.get("model_chat", "deepseek-chat")
        )
        self.council = CouncilDebateEngine(self.llm, self.pm)
        self.executor = AgentExecutor(self.llm, self.pm)

        self.log(f"[MCP] 서버 기동 — config={self.config_path}")
        self.log(f"[MCP] 활성 프로젝트 = {self.pm.active_project}")

    # ------------------------------------------------------------------
    # 로깅 / 설정
    # ------------------------------------------------------------------
    @staticmethod
    def log(message: str) -> None:
        """stdout 은 JSON-RPC 전용이므로 로그는 반드시 stderr 로 출력."""
        try:
            sys.stderr.write(str(message) + "\n")
            sys.stderr.flush()
        except Exception:
            pass

    def load_config(self) -> None:
        try:
            with open(self.config_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            self.config = data if isinstance(data, dict) else {}
        except Exception:
            self.config = {
                "api_key": os.environ.get("DEEPSEEK_API_KEY", ""),
                "active_project": os.getcwd(),
                "model_reasoner": "deepseek-reasoner",
                "model_chat": "deepseek-chat"
            }

    def save_config(self, updates: Dict[str, Any]) -> bool:
        """config.json 병합 저장 (active_project 영구 반영용)."""
        self.config.update(updates or {})
        try:
            parent = os.path.dirname(self.config_path)
            if parent and not os.path.isdir(parent):
                os.makedirs(parent, exist_ok=True)
            with open(self.config_path, "w", encoding="utf-8") as f:
                json.dump(self.config, f, indent=2, ensure_ascii=False)
            return True
        except Exception as e:
            self.log(f"[MCP] config 저장 실패: {e}")
            return False

    # ------------------------------------------------------------------
    # 경로 해석 / 자동 바인딩 헬퍼
    # ------------------------------------------------------------------
    def resolve_path(self, path: str) -> str:
        """ProjectManager.resolve_path 위임 (절대/상대 경로 통합 해석)."""
        if not path:
            return self.pm.active_project
        try:
            return self.pm.resolve_path(path)
        except Exception:
            return os.path.abspath(path)

    def bind_project_for(self, *candidate_paths: str) -> str:
        """
        도구 핸들러 진입 시 호출.
        전달된 파일 경로들 중 유효한 첫 경로로 active_project 를 자동 전환(rebind).
        반환값: 최종 active_project 경로.
        """
        for raw in candidate_paths:
            if not raw or not isinstance(raw, str):
                continue
            try:
                resolved = self.resolve_path(raw)
                before = self.pm.active_project
                after = self.pm.auto_bind_project_for_file(resolved)
                if after and os.path.abspath(after) != os.path.abspath(before):
                    self.log(f"[MCP] 프로젝트 자동 전환: {before} → {after} (트리거: {raw})")
                return self.pm.active_project
            except Exception as e:
                self.log(f"[MCP] 자동 바인딩 실패({raw}): {e}")
        return self.pm.active_project

    # ------------------------------------------------------------------
    # [NEW] 핫 리로드 헬퍼
    # ------------------------------------------------------------------
    def hot_reload_core(self) -> str:
        """
        core.project_manager / core.agent_executor 의 디스크 최신 변경분을
        importlib.reload 로 즉시 메모리에 동기화하고 executor 를 재생성한다.
        실패해도 서버는 계속 동작해야 하므로 예외를 삼키고 로그만 남긴다.
        반환값: 사람이 읽을 수 있는 상태 문자열.
        """
        try:
            import importlib
            import core.project_manager as _pm_mod
            import core.agent_executor as _ae_mod

            importlib.reload(_pm_mod)
            importlib.reload(_ae_mod)

            # ProjectManager 는 설정 경로를 유지한 채 재생성 (active_project 보존)
            try:
                self.pm = _pm_mod.ProjectManager(self.config_path)
                cfg_active = self.config.get("active_project")
                if cfg_active and os.path.isdir(cfg_active):
                    try:
                        self.pm.set_active_project(cfg_active)
                    except Exception:
                        pass
            except Exception as e:
                self.log(f"[MCP] ProjectManager 재생성 실패(기존 인스턴스 유지): {e}")

            # Council / Executor 를 최신 모듈 기준으로 재결선
            try:
                self.council = CouncilDebateEngine(self.llm, self.pm)
            except Exception as e:
                self.log(f"[MCP] CouncilDebateEngine 재결선 실패: {e}")

            self.executor = _ae_mod.AgentExecutor(self.llm, self.pm)
            self.log("[MCP] 핫리로드 완료 — project_manager / agent_executor 메모리 동기화")
            return "✅ 핫리로드 성공 (project_manager · agent_executor 디스크 최신본 반영)"
        except Exception as e:
            self.log(f"[MCP] 핫리로드 예외: {e}")
            return f"⚠️ 핫리로드 예외 (기존 모듈로 계속 동작): {e}"

    # ------------------------------------------------------------------
    # py_compile 검증 헬퍼
    # ------------------------------------------------------------------
    async def run_syntax_check(self, file_path: str) -> Dict[str, Any]:
        """py_compile 기반 문법 검증. 실패해도 예외를 던지지 않고 dict 반환."""
        if not file_path:
            return {"valid": False, "error": "검증할 파일 경로가 비어 있습니다."}

        full_path = self.resolve_path(file_path)
        if not os.path.exists(full_path):
            return {"valid": False, "error": f"파일이 존재하지 않습니다: {full_path}"}
        if not full_path.lower().endswith(".py"):
            return {"valid": True, "skipped": True,
                    "message": f"파이썬 파일이 아니므로 py_compile 검증을 생략했습니다: {os.path.basename(full_path)}"}

        try:
            res = await self.council._run_python_syntax_check(full_path)
            if isinstance(res, dict):
                res.setdefault("path", full_path)
                return res
            return {"valid": False, "error": f"검증기 응답 형식 오류: {res}", "path": full_path}
        except Exception as e:
            return {"valid": False, "error": f"{e}\n{traceback.format_exc()}", "path": full_path}

    @staticmethod
    def format_syntax_result(res: Dict[str, Any], label: str = "") -> str:
        """py_compile 결과를 사람이 읽기 좋은 블록으로 변환."""
        target = os.path.basename(res.get("path", "")) or label or "대상 파일"
        if res.get("skipped"):
            return f"⚪ [QA 샌드박스] {target}: {res.get('message', '검증 생략')}"
        if res.get("valid"):
            return f"✅ [QA 샌드박스] {target}: py_compile 문법 검증 100% 통과 (SyntaxError 없음)"
        return f"❌ [QA 샌드박스] {target}: 문법 오류 감지\n```\n{res.get('error', '알 수 없는 오류')}\n```"

    # ------------------------------------------------------------------
    # [방법 2] 강제 직접 저장 헬퍼 — 코드블록 → 디스크 동기화
    # ------------------------------------------------------------------
    def force_sync_code_block(self, full_p: str, output: str, before_hash: str,
                              tag: str = "MCP 강제 동기화") -> Dict[str, Any]:
        """
        [방법 2] 도구 호출 실패/미호출 시 LLM 응답 내 코드블록을 디스크에 직접 저장.

        조건:
          - full_p 가 유효한 파일 경로일 것
          - 디스크 기록 전/후 해시가 동일하거나 파일이 존재하지 않을 것 (미기록 상태)
          - 응답 텍스트에 3줄 이상의 코드블록이 존재할 것

        반환: {"synced": bool, "lines": int, "path": str, "message": str}
        """
        result = {"synced": False, "lines": 0, "path": full_p, "message": ""}

        if not full_p:
            result["message"] = "대상 파일 경로가 없어 강제 동기화를 생략했습니다."
            return result

        # 미기록 상태 재확인 (이미 도구가 정상 기록했다면 개입 금지)
        after_check = sha256_of_file(full_p)
        if after_check["exists"] and before_hash and after_check["hash"] != before_hash:
            result["message"] = "이미 디스크에 정상 기록되어 강제 동기화가 불필요합니다."
            return result

        candidate_code = extract_largest_code_block(output or "")
        if not candidate_code:
            result["message"] = "응답에 유효한 코드블록(3줄 이상)이 없어 강제 동기화를 생략했습니다."
            return result

        try:
            save_res = self.pm.save_file_content(full_p, candidate_code)
        except Exception as e:
            result["message"] = "강제 동기화 예외: %s" % e
            self.log("[%s] 저장 예외: %s" % (tag, e))
            return result

        if save_res.get("success"):
            lines = len(candidate_code.splitlines())
            result["synced"] = True
            result["lines"] = lines
            result["message"] = "%s (%d줄 직접 저장 완료)" % (full_p, lines)
            self.log("[%s] write_file 도구 미호출/실패 보완 -> %s (%d줄 직접 저장 완료)"
                     % (tag, full_p, lines))
        else:
            result["message"] = "강제 동기화 저장 실패: %s" % save_res.get("error")
            self.log("[%s] 저장 실패: %s" % (tag, save_res.get("error")))

        return result

    # ------------------------------------------------------------------
    # 도구 목록
    # ------------------------------------------------------------------
    def get_tools_list(self) -> List[Dict[str, Any]]:
        return [
            {
                "name": "deepseek_council_debate",
                "description": "DeepSeek 5인 협업체(R1 수석설계 아키텍트 원인분석 + V3 비판적 감사검토 + V3 선임엔지니어 구현 + QA 샌드박스 py_compile 검증 + Git Diff 안전관리)를 가동하여 작업을 무결하게 검토하고 코드를 생성합니다. 대상 파일이 속한 프로젝트로 컨텍스트가 자동 전환됩니다.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "task": {
                            "type": "string",
                            "description": "수행할 작업 내용, 버그 수정 요구사항 또는 신규 기능 개발 지시사항"
                        },
                        "target_file": {
                            "type": "string",
                            "description": "검토 및 수정 대상 소스코드 파일 경로 (상대 경로 또는 절대 경로)"
                        },
                        "code_snippet": {
                            "type": "string",
                            "description": "특정 검토 대상 코드 조각 (지정하면 범위 지정 없이도 허용됨)"
                        },
                        "target_symbol": {
                            "type": "string",
                            "description": "수정할 함수/클래스/메서드 이름. 쉼표로 여러 개 가능 (예: \"Foo.bar, helper\"). 기존 파일 수정 시 target_symbol / line_range / whole_file 중 하나 필수."
                        },
                        "line_range": {
                            "type": "string",
                            "description": "수정할 줄 범위 \"시작-끝\" (예: \"120-180\")"
                        },
                        "whole_file": {
                            "type": "boolean",
                            "description": "파일 전체가 대상임을 명시 (비권장)",
                            "default": False
                        }
                    },
                    "required": ["task"]
                }
            },
            {
                "name": "deepseek_autonomous_codegen",
                "description": "DeepSeek-V3/R1 기반으로 100% 무인 자율 파이프라인 코드를 작성하고 py_compile 구문 무결점 검증 결과를 반드시 함께 반환합니다. 진입 시 core 모듈 핫리로드 + 디스크 SHA-256 물리적 대조 검증이 강제 수행됩니다.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "instructions": {
                            "type": "string",
                            "description": "구현할 작업 및 코드 작성 지시사항"
                        },
                        "file_path": {
                            "type": "string",
                            "description": "수정 또는 생성할 파일 경로"
                        },
                        "target_symbol": {
                            "type": "string",
                            "description": "수정할 함수/클래스/메서드 이름. 쉼표로 여러 개 가능 (예: \"Foo.bar, helper\"). 기존 파일 수정 시 target_symbol / line_range / whole_file 중 하나 필수."
                        },
                        "line_range": {
                            "type": "string",
                            "description": "수정할 줄 범위 \"시작-끝\" (예: \"120-180\")"
                        },
                        "whole_file": {
                            "type": "boolean",
                            "description": "파일 전체가 대상임을 명시 (비권장)",
                            "default": False
                        }
                    },
                    "required": ["instructions"]
                }
            },
            {
                "name": "deepseek_read_file_full",
                "description": "중간 자름(Truncation) 없이 대상 소스코드 파일 전체를 100% 온전하게 전수 독해합니다. 대상 파일이 속한 프로젝트로 컨텍스트가 자동 전환됩니다.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "file_path": {
                            "type": "string",
                            "description": "읽어올 대상 파일 경로"
                        }
                    },
                    "required": ["file_path"]
                }
            },
            {
                "name": "deepseek_verify_python_syntax",
                "description": "파이썬 소스코드의 문법 오류(SyntaxError)를 실제 인터프리터 py_compile로 샌드박스 검증합니다. 대상 파일이 속한 프로젝트로 컨텍스트가 자동 전환됩니다.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "file_path": {
                            "type": "string",
                            "description": "검증할 파이썬 파일 경로"
                        }
                    },
                    "required": ["file_path"]
                }
            },
            {
                "name": "deepseek_list_projects",
                "description": "현재 데스크톱에 존재하는 모든 유효 프로젝트 디렉터리 목록과 현재 활성 프로젝트(active_project)를 반환합니다.",
                "inputSchema": {
                    "type": "object",
                    "properties": {},
                    "required": []
                }
            },
            {
                "name": "deepseek_switch_project",
                "description": "active_project를 지정한 경로로 즉시 전환하고 config.json에 영구 반영합니다. 이후 모든 도구가 해당 프로젝트 기준으로 동작합니다.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "project_path": {
                            "type": "string",
                            "description": "전환할 프로젝트 루트 디렉터리 경로 (절대 경로 권장)"
                        }
                    },
                    "required": ["project_path"]
                }
            },
            {
                "name": "deepseek_view_timeline",
                "description": "시간별 스냅샷 타임라인(timeline.jsonl 원장)을 조회합니다. file_path를 지정하면 해당 파일의 시간대별 변경 이력과 각 스냅샷의 unified diff를, 생략하면 활성 프로젝트 전체 변경 이력을 최신순으로 반환합니다.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "file_path": {
                            "type": "string",
                            "description": "조회할 대상 파일 경로 (생략 시 활성 프로젝트 전체 이력 조회)"
                        },
                        "limit": {
                            "type": "integer",
                            "description": "최대 반환 건수 (기본 10, 최신순)",
                            "default": 10
                        }
                    },
                    "required": []
                }
            },
            {
                "name": "deepseek_verify_receipt",
                "description": "딥시크 도구가 반환한 완료검증 영수증(receipt_id)을 현재 디스크 상태와 독립 재대조합니다. 제미나이는 '수정 완료' 보고 전에 반드시 이 도구로 MATCH 확인을 받아야 합니다.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "receipt_id": {"type": "string", "description": "응답 첫 줄의 receipt=... 값 (예: rc_20261025113000_ab12cd34)"}
                    },
                    "required": ["receipt_id"]
                }
            },
            {
                "name": "deepseek_search_code",
                "description": "활성 프로젝트 전체 파일을 절단 없이 전수 검색하여 파일:라인 단위 매칭 결과를 반환합니다(정규식/대소문자/확장자/컨텍스트 지원).",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "검색어 또는 정규식"},
                        "is_regex": {"type": "boolean", "default": False},
                        "case_sensitive": {"type": "boolean", "default": False},
                        "extension": {"type": "string", "description": "확장자 필터 (예: py)"},
                        "context_lines": {"type": "integer", "default": 0, "description": "전후 문맥 줄 수 (최대 10)"},
                        "max_results": {"type": "integer", "default": 200},
                        "project_path": {"type": "string", "description": "검색 루트 (생략 시 활성 프로젝트)"}
                    },
                    "required": ["query"]
                }
            },
            {
                "name": "deepseek_rollback_file",
                "description": "지정한 특정 시간대(target_time) 또는 특정 스냅샷(snapshot_id) 또는 직전 상태(previous)로 파일을 1초 만에 안전하게 롤백합니다. 롤백 직전 상태도 자동 보존되어 '롤백의 롤백'이 가능합니다.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "file_path": {
                            "type": "string",
                            "description": "롤백할 대상 파일 경로 (필수)"
                        },
                        "target_time": {
                            "type": "string",
                            "description": "복원 기준 시각 (ISO 8601 문자열 또는 epoch 초). 지정 시각 이전 상태로 복원합니다."
                        },
                        "snapshot_id": {
                            "type": "string",
                            "description": "복원할 특정 스냅샷 ID (timeline 조회로 확인 가능)"
                        },
                        "previous": {
                            "type": "boolean",
                            "description": "True 이면 직전 스냅샷 상태로 복원합니다.",
                            "default": False
                        }
                    },
                    "required": ["file_path"]
                }
            },
            {
                "name": "deepseek_code_outline",
                "description": "대상 소스코드 파일의 전체 구조 개요(클래스/함수/메서드/상수 심볼 테이블)를 100K 절단 없이 반환합니다. 파일 전체를 읽기 전 구조 파악에 사용하십시오.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "file_path": {
                            "type": "string",
                            "description": "개요를 볼 파일 경로 (절대/상대 경로)"
                        }
                    },
                    "required": ["file_path"]
                }
            },
            {
                "name": "deepseek_view_section",
                "description": "소스코드 파일에서 특정 함수/클래스/메서드 구간만 정밀 조회합니다. 100K 절단 없이 해당 심볼의 전체 코드를 반환합니다. 수정 전 반드시 이 도구로 원문을 먼저 확인하십시오.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "file_path": {
                            "type": "string",
                            "description": "파일 경로 (절대/상대 경로)"
                        },
                        "symbol_name": {
                            "type": "string",
                            "description": "조회할 함수/클래스/메서드 이름 (예: MyClass, MyClass.my_method)"
                        },
                        "line_range": {
                            "type": "string",
                            "description": "직접 줄 범위 지정 (예: 120-180). symbol_name 대신 사용 가능."
                        }
                    },
                    "required": ["file_path"]
                }
            }
        ]

    # ------------------------------------------------------------------
    # 도구 실행 디스패처
    # ------------------------------------------------------------------
    async def handle_call_tool(self, name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        # 4) 도구명 정규화 — 따옴표/공백/제로폭 문자 제거
        tool_name = normalize_name(name)
        args = arguments if isinstance(arguments, dict) else {}

        # 인자 값 자체에도 따옴표가 섞여 들어오는 경우 정규화
        def arg_str(key: str) -> str:
            val = args.get(key, "")
            if val is None:
                return ""
            if not isinstance(val, str):
                val = str(val)
            return val.strip().strip("\"'`").strip()

        if tool_name == "deepseek_council_debate":
            return await self._tool_council_debate(arg_str("task"),
                                                   arg_str("target_file"),
                                                   args.get("code_snippet", "") or "",
                                                   arg_str("target_symbol"),
                                                   arg_str("line_range"),
                                                   bool(args.get("whole_file", False)))

        elif tool_name == "deepseek_autonomous_codegen":
            return await self._tool_autonomous_codegen(arg_str("instructions"),
                                                       arg_str("file_path"),
                                                       arg_str("target_symbol"),
                                                       arg_str("line_range"),
                                                       bool(args.get("whole_file", False)))

        elif tool_name == "deepseek_read_file_full":
            return await self._tool_read_file_full(arg_str("file_path"))

        elif tool_name == "deepseek_verify_python_syntax":
            return await self._tool_verify_python_syntax(arg_str("file_path"))

        elif tool_name == "deepseek_list_projects":
            return await self._tool_list_projects()

        elif tool_name == "deepseek_switch_project":
            return await self._tool_switch_project(arg_str("project_path"))

        elif tool_name == "deepseek_view_timeline":
            return await self._tool_view_timeline(arg_str("file_path"),
                                                  args.get("limit", 10))

        elif tool_name == "deepseek_verify_receipt":
            return await self._tool_verify_receipt(arg_str("receipt_id"))

        elif tool_name == "deepseek_search_code":
            return await self._tool_search_code(args, arg_str)

        elif tool_name == "deepseek_code_outline":
            return await self._tool_code_outline(arg_str("file_path"))

        elif tool_name == "deepseek_view_section":
            return await self._tool_view_section(arg_str("file_path"),
                                                 arg_str("symbol_name"),
                                                 arg_str("line_range"))

        elif tool_name == "deepseek_rollback_file":
            return await self._tool_rollback_file(arg_str("file_path"),
                                                  arg_str("target_time"),
                                                  arg_str("snapshot_id"),
                                                  args.get("previous", False))

        else:
            available = ", ".join(t["name"] for t in self.get_tools_list())
            return {
                "content": [{
                    "type": "text",
                    "text": f"❌ 알 수 없는 MCP 도구: '{tool_name}'\n\n사용 가능한 도구: {available}"
                }],
                "isError": True
            }

    # ------------------------------------------------------------------
    # 도구 1: 5인 협업체 심의 (+ py_compile 필수 포함)
    # ------------------------------------------------------------------
    async def _tool_council_debate(self, task: str, target_file: str, code_snippet: str,
                                   target_symbol: str = "", line_range: str = "",
                                   whole_file: bool = False) -> Dict[str, Any]:
        # 2) 진입 즉시 프로젝트 자동 전환
        active = self.bind_project_for(target_file)
        task = build_task_with_context(task)
        gate_before = completion_gate.capture_state(self.resolve_path(target_file)) if target_file else None

        # [작업지시서 계약] 기존 파일 수정 요청은 범위 필수 — 서버가 디스크에서 원문을 직접 추출해 주입
        wo = work_order.build(self.resolve_path(target_file) if target_file else "",
                              target_symbol, line_range, whole_file, has_snippet=bool(code_snippet))
        if not wo.ok:
            return {"content": [{"type": "text", "text": wo.error}], "isError": True}
        task = task + wo.prompt_block
        self.log("[WORK_ORDER] council_debate mode=%s symbols=%s file=%s" % (wo.mode, wo.symbols, wo.file))

        sections = {
            "architect": "",
            "critic": "",
            "engineer_briefing": "",
            "qa": "",
            "git": "",
            "code": "",
            "patch": ""
        }

        async for step in self.council.run_debate_session(target_file, task, code_snippet):
            role = step.get("role", "")
            msg = step.get("message", "")
            chunk = step.get("chunk", "")
            stype = step.get("type", "")

            if role == "architect":
                if chunk:
                    sections["architect"] += chunk
                elif msg:
                    sections["architect"] += msg + "\n"
            elif role == "critic":
                if chunk:
                    sections["critic"] += chunk
                elif msg:
                    sections["critic"] += msg + "\n"
            elif role == "engineer":
                if chunk:
                    sections["engineer_briefing"] += chunk
                elif msg:
                    sections["engineer_briefing"] += msg + "\n"
            elif role == "qa":
                if msg:
                    sections["qa"] += msg + "\n"
            elif role == "git":
                if msg:
                    sections["git"] += msg + "\n"
                if stype == "diff_ready":
                    data = step.get("data", {}) or {}
                    sections["code"] = data.get("new_content", "")
                    sections["patch"] = data.get("patch_text", "")

        # ------------------------------------------------------------------
        # [방법 2] sections["code"] + target_file 존재 시 완성 코드를 디스크에 직접 저장
        # ------------------------------------------------------------------
        force_save_banner = ""
        if sections["patch"] and target_file:
            full_t = self.resolve_path(target_file)
            try:
                asr_res = self.pm.apply_search_replace(full_t, sections["patch"])
            except Exception as e:
                asr_res = {"success": False, "applied": 0, "total": 0,
                           "saved": False, "message": "apply_search_replace 예외: %s" % e}

            if asr_res.get("applied", 0) > 0 and asr_res.get("saved"):
                self.log("[MCP SEARCH/REPLACE] council_debate 정밀 패치 적용 -> %s (%d/%d 블록)"
                         % (full_t, asr_res.get("applied", 0), asr_res.get("total", 0)))
                force_save_banner = (
                    "\n\n🎯 [SEARCH/REPLACE 정밀 패치 적용]\n"
                    "`%s` 에 블록 %d/%d 건을 국소 치환하여 저장했습니다. (파일 전체 재작성 없음)"
                    % (full_t, asr_res.get("applied", 0), asr_res.get("total", 0))
                )
            else:
                self.log("[MCP SEARCH/REPLACE] council_debate 패치 미적용 -> %s (%s)"
                         % (full_t, asr_res.get("message", "")))
                force_save_banner = (
                    "\n\n⚠️ [SEARCH/REPLACE 패치 미적용 - 파일은 변경되지 않았습니다]\n"
                    "`%s` - %s (적용 %d/%d 블록)"
                    % (full_t, asr_res.get("message", "알 수 없는 사유"),
                       asr_res.get("applied", 0), asr_res.get("total", 0))
                )
        elif sections["code"] and target_file:
            full_t = self.resolve_path(target_file)
            try:
                save_res = self.pm.save_file_content(full_t, sections["code"])
            except Exception as e:
                save_res = {"success": False, "error": str(e)}

            if save_res.get("success"):
                saved_lines = len(sections["code"].splitlines())
                self.log("[MCP 강제 동기화] council_debate 완성 코드 직접 저장 -> %s (%d줄)"
                         % (full_t, saved_lines))
                force_save_banner = (
                    "\n\n💾 [MCP 강제 동기화: 협업체 완성 코드 직접 저장]\n"
                    "`%s` 에 완성 코드 %d줄을 디스크에 직접 기록했습니다." % (full_t, saved_lines)
                )
            else:
                self.log("[MCP 강제 동기화] council_debate 저장 실패: %s" % save_res.get("error"))
                force_save_banner = (
                    "\n\n⚠️ [MCP 강제 동기화: 저장 실패]\n"
                    "`%s` 저장 실패 — %s" % (full_t, save_res.get("error"))
                )

        # 5) py_compile 검증 결과 필수 산출
        syntax_block = ""
        if target_file:
            syntax_res = await self.run_syntax_check(target_file)
            syntax_block = self.format_syntax_result(syntax_res, target_file)
        else:
            syntax_block = "⚪ [QA 샌드박스] 대상 파일(target_file)이 지정되지 않아 py_compile 검증을 수행하지 않았습니다."

        report = []
        report.append("## 🏛️ [DeepSeek 5인 협업체 합동 심의 보고서]")
        report.append(f"**🎯 활성 프로젝트**: `{active}`")
        if sections["architect"]:
            report.append(f"### 🧠 AI 1: DeepSeek-R1 [수석 설계 아키텍트]\n{sections['architect'].strip()}")
        if sections["critic"]:
            report.append(f"### ⚔️ AI 2: DeepSeek-V3 [비판적 감사 검토관]\n{sections['critic'].strip()}")
        if sections["engineer_briefing"]:
            report.append(f"### 🛠️ AI 3: DeepSeek-V3 [선임 구현 엔지니어 3줄 브리핑]\n{sections['engineer_briefing'].strip()}")
        if sections["qa"]:
            report.append(f"### 🧪 QA: 샌드박스 가상 검증관 (py_compile)\n{sections['qa'].strip()}")
        if sections["git"]:
            report.append(f"### 🚀 Git: 안전 배포 관리관\n{sections['git'].strip()}")
        if sections["patch"]:
            report.append("### 🩹 [적용 시도된 SEARCH/REPLACE 패치]\n```\n%s\n```" % sections["patch"])
        if sections["code"]:
            report.append(f"### 💻 [검증 완료된 완성형 코드]\n```python\n{sections['code']}\n```")

        # py_compile 최종 검증 블록은 항상 마지막에 강제 삽입
        report.append(f"### 🔬 [최종 py_compile 검증 결과]\n{syntax_block}")

        text = "\n\n".join(report)
        if force_save_banner:
            text += force_save_banner

        # [완료검증 게이트] 코드/패치가 산출된 수정 요청은 디스크 실측 판정을 응답 맨 앞에 고정
        is_err = False
        if gate_before is not None and (sections["patch"] or sections["code"]):
            gate = completion_gate.evaluate(self.resolve_path(target_file), gate_before,
                                            wo.symbols, tag="council_debate")
            text = completion_gate.format_header(gate) + text
            is_err = completion_gate.is_failure(gate)
        elif gate_before is not None:
            text = "⚪ **[완료검증 없음]** 이번 심의에서 코드/패치가 산출되지 않았습니다(분석 전용). 파일은 변경되지 않았습니다.\n\n" + text

        return {"content": [{"type": "text", "text": text}], "isError": is_err}

    # ------------------------------------------------------------------
    # 도구 2: 자율 코드 생성 (+ 핫리로드 + 디스크 물리 검증 + py_compile)
    # ------------------------------------------------------------------
    async def _tool_autonomous_codegen(self, instructions: str, file_path: str,
                                       target_symbol: str = "", line_range: str = "",
                                       whole_file: bool = False) -> Dict[str, Any]:
        # 0) 핫 리로드 (agent_executor / project_manager 디스크 최신 변경분 즉시 메모리 동기화)
        reload_status = self.hot_reload_core()

        # 1) 디스크 기록 전 스냅샷 해시 측정 (거짓 보고 방지)
        full_p = self.resolve_path(file_path) if file_path else ""
        before = sha256_of_file(full_p)
        gate_before = completion_gate.capture_state(full_p)
        before_hash = before["hash"]
        before_size = before["size"]

        # 2) 진입 즉시 프로젝트 자동 전환
        active = self.bind_project_for(file_path)

        instructions = build_task_with_context(instructions)

        # [작업지시서 계약] 기존 파일 수정 요청은 범위 필수 — 서버가 디스크에서 원문을 직접 추출해 주입
        wo = work_order.build(full_p, target_symbol, line_range, whole_file)
        if not wo.ok:
            return {"content": [{"type": "text", "text": wo.error}], "isError": True}
        instructions = instructions + wo.prompt_block
        self.log("[WORK_ORDER] autonomous_codegen mode=%s symbols=%s file=%s" % (wo.mode, wo.symbols, wo.file))
        output = await self.executor.execute_user_request(
            user_prompt=instructions,
            context_file=file_path
        )

        # ------------------------------------------------------------------
        # [방법 2 강제 직접 저장 게이트]
        # write_file 도구가 호출되지 않았거나 실패하여 디스크가 그대로인 경우,
        # LLM 응답 내 최대 코드블록을 추출해 디스크에 직접 저장한다.
        # ------------------------------------------------------------------
        force_sync_banner = ""
        after_check = sha256_of_file(full_p)

        # ------------------------------------------------------------------
        # [최우선] SEARCH/REPLACE 정밀 부분 패치 게이트
        # 응답에 '<<<<<<< SEARCH' 블록이 있으면 대용량 파일 전체 재작성 대신
        # apply_search_replace 로 국소 구간만 정밀 치환한다. (토큰 절감 + 회귀 방지)
        # ------------------------------------------------------------------
        if full_p and output and "<<<<<<< SEARCH" in output:
            try:
                asr_res = self.pm.apply_search_replace(full_p, output)
            except Exception as e:
                asr_res = {"success": False, "applied": 0, "total": 0,
                           "saved": False, "message": "apply_search_replace 예외: %s" % e}

            applied_n = asr_res.get("applied", 0)
            total_n = asr_res.get("total", 0)
            saved_ok = asr_res.get("saved", False)

            if applied_n > 0 and saved_ok:
                self.log("[MCP SEARCH/REPLACE] 정밀 부분 패치 완료 -> %s (%d/%d 블록)"
                         % (full_p, applied_n, total_n))
                force_sync_banner = (
                    "\n\n🎯 [MCP SEARCH/REPLACE 정밀 부분 패치]\n"
                    "`%s` 에 SEARCH/REPLACE 블록 %d/%d 건을 국소 정밀 치환하여 저장했습니다. "
                    "(대용량 파일 전체 재작성 회피)" % (full_p, applied_n, total_n)
                )
                if asr_res.get("failed"):
                    force_sync_banner += "\n- 실패 블록 %d건: %s" % (
                        len(asr_res["failed"]),
                        "; ".join(f.get("reason", "") for f in asr_res["failed"][:3])
                    )
            else:
                self.log("[MCP SEARCH/REPLACE] 정밀 패치 미적용 -> %s (%s)"
                         % (full_p, asr_res.get("message", "")))
                force_sync_banner = (
                    "\n\n⚠️ [MCP SEARCH/REPLACE 정밀 부분 패치 실패]\n"
                    "`%s` — %s (적용 %d/%d 블록)"
                    % (full_p, asr_res.get("message", "알 수 없는 사유"), applied_n, total_n)
                )

            # 정밀 패치를 시도했으므로 코드블록 전체 덮어쓰기 게이트는 건너뛴다.
            after_check = sha256_of_file(full_p)

        elif full_p and (not after_check["exists"] or after_check["hash"] == before_hash):
            code_blocks = re.findall(r'```(?:python)?\s*([\s\S]*?)```', output or "")
            if code_blocks:
                candidate_code = max(code_blocks, key=lambda b: len(b.strip())).strip()
                if len(candidate_code.splitlines()) >= 3:
                    save_res = self.pm.save_file_content(full_p, candidate_code)
                    if save_res.get("success"):
                        saved_lines = len(candidate_code.splitlines())
                        self.log("[MCP 강제 동기화] write_file 도구 미호출/실패 보완 -> %s (%d줄 직접 저장 완료)"
                                 % (full_p, saved_lines))
                        force_sync_banner = (
                            "\n\n💾 [MCP 강제 동기화: 코드블록 직접 저장]\n"
                            "write_file 도구가 호출되지 않았거나 실패하여, 응답 내 최대 코드블록을 "
                            "`%s` 에 직접 저장했습니다. (%d줄)" % (full_p, saved_lines)
                        )
                    else:
                        self.log("[MCP 강제 동기화] 저장 실패: %s" % save_res.get("error"))
                        force_sync_banner = (
                            "\n\n⚠️ [MCP 강제 동기화: 저장 실패]\n"
                            "`%s` 직접 저장 실패 — %s" % (full_p, save_res.get("error"))
                        )

        # 5) py_compile 검증 결과 필수 산출
        if file_path:
            syntax_res = await self.run_syntax_check(file_path)
            syntax_info = "\n\n### 🔬 [최종 py_compile 검증 결과]\n" + self.format_syntax_result(syntax_res, file_path)
        else:
            syntax_info = ("\n\n### 🔬 [최종 py_compile 검증 결과]\n"
                           "⚪ 대상 파일(file_path)이 지정되지 않아 py_compile 검증을 수행하지 않았습니다.")

        # 6) 실제 디스크 변경 여부 물리적 대조 검증 (거짓 보고 원천 차단)
        verify_banner = ""
        after = sha256_of_file(full_p)
        if full_p and after["exists"]:
            after_hash = after["hash"]
            after_size = after["size"]
            if before_hash and before_hash == after_hash:
                verify_banner = (
                    f"\n\n🚨 [디스크 물리적 대조 검증: 미기록 경고]\n"
                    f"파일이 실제 디스크에 반영되지 않았습니다! "
                    f"(수정 전후 SHA-256 동일: {after_hash[:12]}, 크기: {after_size}B)\n"
                )
            elif before_hash and before_hash != after_hash:
                verify_banner = (
                    f"\n\n✅ [디스크 물리적 대조 검증: 실제 기록 확인]\n"
                    f"파일이 디스크에 성공적으로 수정 반영되었습니다! "
                    f"(크기: {before_size}B ➔ {after_size}B, 신규 해시: {after_hash[:12]})\n"
                )
            else:
                # 기록 전 스냅샷이 없던 신규 파일 생성 케이스
                verify_banner = (
                    f"\n\n✅ [디스크 물리적 대조 검증: 신규 파일 생성 확인]\n"
                    f"디스크에 새 파일이 생성되었습니다! (크기: {after_size}B, 해시: {after_hash[:12]})\n"
                )
        elif full_p:
            verify_banner = (
                f"\n\n🚨 [디스크 물리적 대조 검증: 파일 부재]\n"
                f"지정 경로에 파일이 존재하지 않습니다: `{full_p}`\n"
            )

        # ------------------------------------------------------------------
        # [SNAPSHOT-ENGINE] 시간별 스냅샷 생성 시각 + diff 정보 배너
        #   - 저장 직후 원장에서 최신 스냅샷을 조회하여 생성 시각/ID/변경량을 표시
        #   - 조회 실패는 결과 보고를 막지 않는다 (예외 격리)
        # ------------------------------------------------------------------
        snapshot_banner = ""
        if full_p:
            try:
                tl = self.pm.get_timeline(full_p, limit=1)
                if tl.get("success") and tl.get("timeline"):
                    rec = tl["timeline"][0]
                    sid = rec.get("snapshot_id", "")
                    created = rec.get("created_at", "")
                    note = rec.get("note", "") or "-"
                    size = rec.get("size", 0)

                    diff_line = "- **변경량**: 변경 없음 (최초 스냅샷 또는 동일 내용)"
                    try:
                        d = self.pm.get_diff(sid, full_p)
                        if d.get("success") and d.get("changed"):
                            diff_line = (
                                "- **변경량**: +%d / -%d (직전 스냅샷 `%s` 대비)"
                                % (d.get("added_lines", 0), d.get("removed_lines", 0),
                                   d.get("from_snapshot_id", "") or "-")
                            )
                    except Exception as e:
                        self.log(f"[MCP] 스냅샷 diff 조회 실패: {e}")

                    snapshot_banner = (
                        "\n\n### 🕰️ [시간별 스냅샷 기록]\n"
                        "- **스냅샷 ID**: `%s`\n"
                        "- **생성 시각**: %s\n"
                        "- **메모**: %s\n"
                        "- **크기**: %sB\n"
                        "%s\n"
                        "- **타임라인 전체**: %s건 (원장: timeline.jsonl)"
                        % (sid, created, note, size, diff_line, tl.get("total", 0))
                    )
                else:
                    snapshot_banner = (
                        "\n\n### 🕰️ [시간별 스냅샷 기록]\n"
                        "⚪ 스냅샷 엔진이 비활성 상태이거나 기록된 이력이 없습니다."
                    )
            except Exception as e:
                self.log(f"[MCP] 스냅샷 배너 생성 실패: {e}")
                snapshot_banner = (
                    "\n\n### 🕰️ [시간별 스냅샷 기록]\n"
                    "⚠️ 스냅샷 정보 조회 실패: %s" % e
                )

        header = f"**🎯 활성 프로젝트**: `{active}`\n**♻️ 핫리로드**: {reload_status}\n\n"
        gate = completion_gate.evaluate(full_p, gate_before, wo.symbols, tag="autonomous_codegen")
        gate_header = completion_gate.format_header(gate)
        return {"content": [{"type": "text", "text": gate_header + header + (output or "") + force_sync_banner + verify_banner + snapshot_banner + syntax_info}],
                "isError": completion_gate.is_failure(gate)}

    # ------------------------------------------------------------------
    # 도구 3: 파일 전수 독해
    # ------------------------------------------------------------------
    async def _tool_read_file_full(self, file_path: str) -> Dict[str, Any]:
        # 2) 진입 즉시 프로젝트 자동 전환
        active = self.bind_project_for(file_path)

        res = self.executor.tool_read_file(file_path)
        if res.get("success"):
            text = (f"**🎯 활성 프로젝트**: `{active}`\n\n"
                    f"### [{res.get('filename')}] (총 {res.get('total_lines')} 줄 전수 독해 완료)\n\n"
                    f"```python\n{res.get('content')}\n```")
        else:
            text = f"❌ 파일 읽기 실패: {res.get('error')}\n\n**🎯 활성 프로젝트**: `{active}`"
        return {"content": [{"type": "text", "text": text}]}

    # ------------------------------------------------------------------
    # 도구: 완료검증 영수증 독립 재대조 (core.completion_gate 위임)
    # ------------------------------------------------------------------
    async def _tool_verify_receipt(self, receipt_id: str) -> Dict[str, Any]:
        res = await asyncio.to_thread(completion_gate.verify_receipt, receipt_id)
        rec = res.get("receipt") or {}
        icon = "🟢" if res.get("ok") else "🔴"
        lines = ["%s **[영수증 재검증 %s]** receipt=`%s`" % (icon, res.get("status"), receipt_id),
                 "- %s" % res.get("detail", "")]
        if rec:
            lines.append("- 파일: `%s`" % rec.get("path"))
            lines.append("- 당시 판정: %s (%s)" % (rec.get("verdict"), rec.get("created_at")))
            for c in rec.get("checks", []):
                lines.append("  - %s %s: %s" % ("✅" if c["ok"] else "❌", c["name"], c["detail"]))
        if res.get("status") == "MATCH" and res.get("ok"):
            lines.append("\n→ 제미나이는 이 영수증을 근거로 '완료' 보고할 수 있습니다.")
        else:
            lines.append("\n→ 확인 실패: '완료'라고 보고하면 안 됩니다. 사실그대로 보고하십시오.")
        return {"content": [{"type": "text", "text": "\n".join(lines)}], "isError": not res.get("ok")}

    # ------------------------------------------------------------------
    # 도구: 프로젝트 전수 코드 검색 (code_tools.search_code 위임)
    # ------------------------------------------------------------------
    async def _tool_search_code(self, args: Dict[str, Any], arg_str) -> Dict[str, Any]:
        project_path = arg_str("project_path")
        root = self.resolve_path(project_path) if project_path else self.pm.active_project
        try:
            max_results = int(args.get("max_results", 200) or 200)
            context_lines = int(args.get("context_lines", 0) or 0)
        except (TypeError, ValueError):
            return {"content": [{"type": "text", "text": "❌ max_results/context_lines 는 정수여야 합니다."}], "isError": True}
        res = await asyncio.to_thread(
            search_code, root, arg_str("query"),
            bool(args.get("is_regex", False)), bool(args.get("case_sensitive", False)),
            arg_str("extension"), max_results, context_lines)
        text = format_search_result(root, arg_str("query"), res)
        return {"content": [{"type": "text", "text": text}], "isError": not res.get("success")}

    # ------------------------------------------------------------------
    # 도구: 파일 구조 개요 (code_tools.code_eye 위임)
    # ------------------------------------------------------------------
    async def _tool_code_outline(self, file_path: str) -> Dict[str, Any]:
        if not file_path:
            return {"content": [{"type": "text", "text": "❌ file_path 인자가 비어 있습니다."}], "isError": True}
        active = self.bind_project_for(file_path)
        full_p = self.resolve_path(file_path)
        try:
            outline = await asyncio.to_thread(_code_eye.outline, full_p)
        except Exception as e:
            return {"content": [{"type": "text", "text": f"❌ code_outline 실패: {e}"}], "isError": True}

        # outline 은 FileOutline dataclass 객체
        symbols = outline.symbols  # List[Symbol]
        lang = outline.language
        total = outline.total_lines
        size = outline.total_bytes

        lines = [
            f"## 🔭 [코드 개요] `{file_path}`",
            f"**🎯 활성 프로젝트**: `{active}`",
            f"- 언어: `{lang}` | 총 줄: {total} | 크기: {size:,}B",
            "",
            "| # | 종류 | 심볼명 | 줄 범위 | 서명 |",
            "|---|------|--------|---------|------|",
        ]
        for i, sym in enumerate(symbols, 1):
            lines.append(
                f"| {i} | `{sym.kind}` | `{sym.name}` "
                f"| L{sym.line_start}–{sym.line_end} "
                f"| {sym.signature[:60]} |"
            )
        lines.append(f"\n총 **{len(symbols)}개** 심볼. `deepseek_view_section` 으로 원하는 심볼 정밀 조회 가능.")
        return {"content": [{"type": "text", "text": "\n".join(lines)}]}

    # ------------------------------------------------------------------
    # 도구: 특정 심볼/줄 범위 정밀 조회 (code_tools.code_eye 위임)
    # ------------------------------------------------------------------
    async def _tool_view_section(self, file_path: str, symbol_name: str, line_range: str) -> Dict[str, Any]:
        if not file_path:
            return {"content": [{"type": "text", "text": "❌ file_path 인자가 비어 있습니다."}], "isError": True}
        if not symbol_name and not line_range:
            return {"content": [{"type": "text", "text": "❌ symbol_name 또는 line_range 중 하나는 반드시 지정해야 합니다."}], "isError": True}

        active = self.bind_project_for(file_path)
        full_p = self.resolve_path(file_path)

        try:
            if symbol_name:
                # view_section 은 문자열을 반환함
                text_result = await asyncio.to_thread(_code_eye.view_section, full_p, symbol_name)
                label = symbol_name
                location = ""
            else:
                # line_range "120-180" 파싱
                parts = line_range.replace(":", "-").split("-")
                if len(parts) != 2:
                    return {"content": [{"type": "text", "text": f"❌ line_range 형식 오류: '{line_range}' (예: 120-180)"}], "isError": True}
                start_l, end_l = int(parts[0].strip()), int(parts[1].strip())
                text_result = await asyncio.to_thread(_code_eye.view_lines, full_p, start_l, end_l)
                label = f"L{start_l}–{end_l}"
                location = f" | 줄 범위: {label}"
        except Exception as e:
            return {"content": [{"type": "text", "text": f"❌ view_section 실패: {e}"}], "isError": True}

        if text_result.startswith("❌"):
            return {"content": [{"type": "text", "text": text_result}], "isError": True}

        text = (
            f"## 🔬 [정밀 조회] `{file_path}` → `{label}`\n"
            f"**🎯 활성 프로젝트**: `{active}`{location}\n\n"
            f"{text_result}"
        )
        return {"content": [{"type": "text", "text": text}]}

    # ------------------------------------------------------------------
    # 도구 4: py_compile 문법 검증
    # ------------------------------------------------------------------
    async def _tool_verify_python_syntax(self, file_path: str) -> Dict[str, Any]:
        # 2) 진입 즉시 프로젝트 자동 전환
        active = self.bind_project_for(file_path)

        res = await self.run_syntax_check(file_path)
        if res.get("valid"):
            text = (f"✅ `{file_path}` py_compile 문법 샌드박스 검증 100% 통과 (SyntaxError 없음)\n\n"
                    f"**🎯 활성 프로젝트**: `{active}`")
        else:
            text = (f"❌ `{file_path}` 문법 오류 감지:\n```\n{res.get('error')}\n```\n\n"
                    f"**🎯 활성 프로젝트**: `{active}`")
        return {"content": [{"type": "text", "text": text}]}

    # ------------------------------------------------------------------
    # 도구 5: 프로젝트 목록 조회 (신설)
    # ------------------------------------------------------------------
    async def _tool_list_projects(self) -> Dict[str, Any]:
        try:
            projects = self.pm.list_available_projects()
        except Exception as e:
            return {"content": [{"type": "text", "text": f"❌ 프로젝트 목록 조회 실패: {e}"}], "isError": True}

        active = self.pm.active_project
        lines = ["## 📁 [데스크톱 프로젝트 목록]", f"**🎯 현재 활성 프로젝트**: `{active}`", ""]

        if not projects:
            lines.append("_유효한 프로젝트 디렉터리를 찾지 못했습니다._")
        else:
            lines.append(f"총 **{len(projects)}개** 프로젝트 발견\n")
            for idx, p in enumerate(projects, 1):
                mark = "✅ **활성**" if p.get("is_active") else "▫️"
                cfg = " · config.json 있음" if p.get("has_config") else ""
                lines.append(f"{idx}. {mark} `{p.get('name')}`{cfg}\n   - 경로: `{p.get('path')}`")

        return {"content": [{"type": "text", "text": "\n".join(lines)}]}

    # ------------------------------------------------------------------
    # 도구 6: 프로젝트 전환 (신설)
    # ------------------------------------------------------------------
    async def _tool_switch_project(self, project_path: str) -> Dict[str, Any]:
        if not project_path:
            return {"content": [{"type": "text", "text": "❌ project_path 인자가 비어 있습니다."}], "isError": True}

        target = self.resolve_path(project_path)
        previous = self.pm.active_project

        ok = False
        try:
            ok = self.pm.set_active_project(target)
        except Exception as e:
            self.log(f"[MCP] 프로젝트 전환 예외: {e}")

        if not ok:
            return {
                "content": [{
                    "type": "text",
                    "text": f"❌ 프로젝트 전환 실패: 유효한 디렉터리가 아닙니다.\n- 요청 경로: `{target}`\n- 현재 활성: `{previous}`"
                }],
                "isError": True
            }

        # config.json 영구 반영 (ProjectManager + 서버 설정 이중 보장)
        saved = self.save_config({"active_project": self.pm.active_project})

        text = (
            f"✅ 프로젝트 전환 완료\n\n"
            f"- 이전 활성: `{previous}`\n"
            f"- 현재 활성: `{self.pm.active_project}`\n"
            f"- config.json 영구 저장: {'성공' if saved else '실패'}\n"
            f"- 설정 파일: `{self.config_path}`"
        )
        return {"content": [{"type": "text", "text": text}]}

    # ------------------------------------------------------------------
    # 도구 7: 시간별 타임라인 조회 (신설)
    # ------------------------------------------------------------------
    async def _tool_view_timeline(self, file_path: str, limit: Any = 10) -> Dict[str, Any]:
        """
        시간별 스냅샷 타임라인을 조회한다.

        Args:
            file_path : 대상 파일 (빈 문자열이면 활성 프로젝트 전체 이력)
            limit     : 최대 반환 건수 (기본 10)

        Returns:
            MCP content 응답 dict
        """
        # 진입 즉시 프로젝트 자동 전환 (파일이 지정된 경우)
        active = self.bind_project_for(file_path) if file_path else self.pm.active_project

        # limit 정규화 (문자열/None/비정상 값 방어)
        try:
            cap = int(limit)
        except (TypeError, ValueError):
            cap = 10
        if cap <= 0:
            cap = 10

        try:
            tl = self.pm.get_timeline(file_path or None, limit=cap)
        except Exception as e:
            self.log(f"[MCP] 타임라인 조회 예외: {e}")
            return {
                "content": [{"type": "text",
                             "text": f"❌ 타임라인 조회 실패: {e}"}],
                "isError": True
            }

        if not tl.get("success"):
            return {
                "content": [{
                    "type": "text",
                    "text": (f"❌ 타임라인 조회 실패\n\n"
                             f"- 사유: {tl.get('message', '알 수 없는 오류')}\n"
                             f"- 활성 프로젝트: `{active}`")
                }],
                "isError": True
            }

        scope = f"`{tl.get('path')}`" if tl.get("path") else "활성 프로젝트 전체"
        lines = [
            "## 🕰️ [시간별 스냅샷 타임라인]",
            f"**🎯 활성 프로젝트**: `{active}`",
            f"**📂 조회 범위**: {scope}",
            f"**📊 집계**: 표시 {tl.get('count', 0)}건 / 전체 {tl.get('total', 0)}건 (최신순)",
            ""
        ]

        timeline = tl.get("timeline") or []
        if not timeline:
            lines.append("_기록된 스냅샷이 없습니다. 파일을 저장하면 자동으로 이력이 쌓입니다._")
            return {"content": [{"type": "text", "text": "\n".join(lines)}]}

        for idx, rec in enumerate(timeline, 1):
            sid = rec.get("snapshot_id", "")
            created = rec.get("created_at", "")
            note = rec.get("note", "") or "-"
            size = rec.get("size", 0)
            target = os.path.basename(rec.get("target_path", "") or "")
            pre_rb = " · 🔙 롤백직전보존" if rec.get("is_pre_rollback") else ""

            lines.append(f"### {idx}. `{sid}`{pre_rb}")
            lines.append(f"- **파일**: `{target}`")
            lines.append(f"- **생성 시각**: {created}")
            lines.append(f"- **메모**: {note}")
            lines.append(f"- **크기**: {size}B")

            # 각 스냅샷의 직전 대비 diff 조회
            try:
                d = self.pm.get_diff(sid, rec.get("target_path", "") or None)
            except Exception as e:
                d = {"success": False, "message": str(e)}

            if d.get("success") and d.get("changed"):
                lines.append(
                    "- **변경량**: +%d / -%d (직전 스냅샷 `%s` 대비)"
                    % (d.get("added_lines", 0), d.get("removed_lines", 0),
                       d.get("from_snapshot_id", "") or "-")
                )
                diff_text = (d.get("diff") or "").strip()
                if diff_text:
                    # 대화 폭주 방지 — 최대 40줄까지만 표시
                    diff_lines = diff_text.splitlines()
                    if len(diff_lines) > 40:
                        diff_lines = diff_lines[:40] + ["... (이하 생략)"]
                    lines.append("```diff")
                    lines.extend(diff_lines)
                    lines.append("```")
            elif d.get("success"):
                lines.append("- **변경량**: 변경 없음 (최초 스냅샷 또는 동일 내용)")
            else:
                lines.append(f"- **변경량**: diff 조회 불가 ({d.get('message', '')})")
            lines.append("")

        lines.append("💡 특정 시점으로 되돌리려면 `deepseek_rollback_file` 에 "
                     "`snapshot_id` 또는 `target_time` 을 지정하십시오.")
        return {"content": [{"type": "text", "text": "\n".join(lines)}]}

    # ------------------------------------------------------------------
    # 도구 8: 시간별/스냅샷 롤백 (신설)
    # ------------------------------------------------------------------
    async def _tool_rollback_file(self, file_path: str, target_time: str = "",
                                  snapshot_id: str = "", previous: Any = False) -> Dict[str, Any]:
        """
        파일을 특정 스냅샷 / 특정 시각 / 직전 상태로 안전하게 롤백한다.

        Args:
            file_path   : 롤백 대상 파일 (필수)
            target_time : 복원 기준 시각 (ISO 문자열 또는 epoch)
            snapshot_id : 복원할 스냅샷 ID
            previous    : True 면 직전 스냅샷으로 복원

        Returns:
            MCP content 응답 dict
        """
        if not file_path:
            return {
                "content": [{"type": "text",
                             "text": "❌ file_path 인자가 비어 있습니다. 롤백할 파일 경로를 지정하십시오."}],
                "isError": True
            }

        # previous 정규화 (bool / "true" / "1" 등 수용)
        if isinstance(previous, str):
            prev_flag = previous.strip().lower() in ("1", "true", "yes", "y", "on")
        else:
            prev_flag = bool(previous)

        # 복원 방식 미지정 방어
        if not snapshot_id and not target_time and not prev_flag:
            return {
                "content": [{
                    "type": "text",
                    "text": ("❌ 복원 방식을 지정해야 합니다.\n\n"
                             "- `snapshot_id`: 특정 스냅샷으로 복원\n"
                             "- `target_time`: 지정 시각 이전 상태로 복원\n"
                             "- `previous`: 직전 스냅샷으로 복원\n\n"
                             "💡 먼저 `deepseek_view_timeline` 으로 이력을 확인하십시오.")
                }],
                "isError": True
            }

        # 진입 즉시 프로젝트 자동 전환
        active = self.bind_project_for(file_path)

        # target_time 문자열 → datetime 변환 (snapshot_engine.parse_when 위임)
        parsed_time = None
        if target_time:
            try:
                from core.snapshot_engine import parse_when as _parse_when
                parsed_time = _parse_when(target_time)
            except Exception as e:
                self.log(f"[MCP] target_time 파싱 실패: {e}")
                parsed_time = None

            if parsed_time is None:
                return {
                    "content": [{
                        "type": "text",
                        "text": (f"❌ target_time 해석 실패: `{target_time}`\n\n"
                                 "ISO 8601 문자열(예: 2026-10-23T23:48:29) 또는 "
                                 "epoch 초 단위 숫자를 사용하십시오.")
                    }],
                    "isError": True
                }

        try:
            res = self.pm.rollback_file(
                file_path,
                snapshot_id=snapshot_id or None,
                target_time=parsed_time,
                previous=prev_flag,
            )
        except Exception as e:
            self.log(f"[MCP] 롤백 예외: {e}")
            return {
                "content": [{"type": "text",
                             "text": f"❌ 롤백 실행 중 예외 발생: {e}\n\n{traceback.format_exc()}"}],
                "isError": True
            }

        if not res.get("success"):
            return {
                "content": [{
                    "type": "text",
                    "text": (f"❌ 롤백 실패\n\n"
                             f"- 대상: `{res.get('path') or file_path}`\n"
                             f"- 사유: {res.get('message', '알 수 없는 오류')}\n"
                             f"- 활성 프로젝트: `{active}`")
                }],
                "isError": True
            }

        stats = res.get("stats", {}) or {}
        lines = [
            "## ⏪ [스냅샷 롤백 완료]",
            f"**🎯 활성 프로젝트**: `{active}`",
            f"**📂 대상 파일**: `{res.get('path')}`",
            f"**🕰️ 복원 스냅샷**: `{res.get('snapshot_id') or '-'}`",
            f"**🔐 롤백 직전 보존 스냅샷**: `{res.get('backup_path') or '-'}`",
            f"**📊 변경량**: +{stats.get('added', 0)} / -{stats.get('removed', 0)}",
            ""
        ]

        diff_text = (res.get("diff") or "").strip()
        if diff_text:
            diff_lines = diff_text.splitlines()
            if len(diff_lines) > 60:
                diff_lines = diff_lines[:60] + ["... (이하 생략)"]
            lines.append("### 🔀 롤백 diff (롤백 전 → 롤백 후)")
            lines.append("```diff")
            lines.extend(diff_lines)
            lines.append("```")
        else:
            lines.append("_내용 변경이 없었습니다 (이미 해당 상태와 동일)._")

        lines.append("")
        lines.append("💡 롤백 직전 상태도 스냅샷으로 보존되었으므로, "
                     "필요 시 `deepseek_rollback_file` 로 되돌릴 수 있습니다.")

        return {"content": [{"type": "text", "text": "\n".join(lines)}]}

    # ------------------------------------------------------------------
    # JSON-RPC stdio 루프
    # ------------------------------------------------------------------
    def run_stdio(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        for raw_line in sys.stdin:
            line_str = raw_line.strip()
            if not line_str:
                continue

            try:
                req = json.loads(line_str)
            except Exception:
                continue

            if not isinstance(req, dict):
                continue

            req_id = req.get("id")
            method = normalize_name(req.get("method", ""))
            params = req.get("params", {}) or {}

            if method == "initialize":
                resp = {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "protocolVersion": PROTOCOL_VERSION,
                        "capabilities": {"tools": {}},
                        "serverInfo": {
                            "name": SERVER_NAME,
                            "version": SERVER_VERSION
                        }
                    }
                }
                sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
                sys.stdout.flush()

            elif method == "notifications/initialized":
                pass

            elif method == "ping":
                resp = {"jsonrpc": "2.0", "id": req_id, "result": {}}
                sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
                sys.stdout.flush()

            elif method == "tools/list":
                resp = {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {"tools": self.get_tools_list()}
                }
                sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
                sys.stdout.flush()

            elif method == "tools/call":
                # 4) 도구명 정규화 — 따옴표/공백 제거
                tool_name = normalize_name(params.get("name", ""))
                tool_args = params.get("arguments", {}) or {}
                if not isinstance(tool_args, dict):
                    tool_args = {}

                try:
                    result = loop.run_until_complete(self.handle_call_tool(tool_name, tool_args))
                    resp = {"jsonrpc": "2.0", "id": req_id, "result": result}
                except Exception as e:
                    resp = {
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "result": {
                            "content": [{
                                "type": "text",
                                "text": f"Error executing {tool_name}: {str(e)}\n{traceback.format_exc()}"
                            }],
                            "isError": True
                        }
                    }
                sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
                sys.stdout.flush()

            else:
                if req_id is not None:
                    resp = {
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "error": {
                            "code": -32601,
                            "message": f"Method not found: {method}"
                        }
                    }
                    sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
                    sys.stdout.flush()


def main():
    server = DeepSeekMcpServer()
    server.run_stdio()


if __name__ == "__main__":
    main()
