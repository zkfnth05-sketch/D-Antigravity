# -*- coding: utf-8 -*-
"""
agent_executor.py 패치 스크립트
- Turn 0 선제적 전수 독해 자동 주입
- 5중 인자 폴백
- _current_context_file 등록
- tool_write_file 디렉터리 방어
"""
import io
import os
import re

TARGET = os.path.join("backend", "core", "agent_executor.py")


def read_src():
    with io.open(TARGET, "r", encoding="utf-8") as f:
        return f.read()


def write_src(text):
    with io.open(TARGET, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


def main():
    src = read_src()
    orig_len = len(src)

    # ------------------------------------------------------------------
    # 1) __init__ 에 _current_context_file 등록
    # ------------------------------------------------------------------
    if "_current_context_file" not in src:
        src = src.replace(
            "        self.memory = SessionMemory(max_history_turns=24)\n",
            "        self.memory = SessionMemory(max_history_turns=24)\n"
            "        # 현재 작업 대상 파일 (write_file 디렉터리 오지정 시 자동 전환용)\n"
            "        self._current_context_file: str = \"\"\n",
            1,
        )

    # ------------------------------------------------------------------
    # 2) _preload_target_file / _build_preload_section 헬퍼 삽입
    # ------------------------------------------------------------------
    if "_preload_target_file" not in src:
        helper = '''
    # =====================================================================
    # [신규] 딥시크의 보는 눈 — Turn 0 선제적 전수 독해
    # =====================================================================
    def _preload_target_file(self, context_file: str, target_file: str = "") -> Dict[str, Any]:
        """
        [딥시크의 보는 눈] 진입 즉시 대상 파일을 선제적으로 전수 독해한다.
        context_file 우선, 없으면 target_file 사용.
        """
        raw = (context_file or target_file or "").strip().strip("\\"'`").strip()
        if not raw:
            return {
                "loaded": False, "path": "", "filename": "",
                "total_lines": 0, "content": "", "error": "대상 파일 미지정"
            }

        try:
            res = self.tool_read_file(raw)
        except Exception as e:
            return {
                "loaded": False, "path": raw, "filename": os.path.basename(raw),
                "total_lines": 0, "content": "", "error": "전수 독해 예외: %s" % e
            }

        if not res.get("success"):
            return {
                "loaded": False,
                "path": res.get("path", raw),
                "filename": os.path.basename(res.get("path", raw) or raw),
                "total_lines": 0,
                "content": "",
                "error": res.get("error", "파일 독해 실패")
            }

        content = res.get("content", "") or ""
        if len(content) > _PRELOAD_MAX_CHARS:
            content = content[:_PRELOAD_MAX_CHARS]

        return {
            "loaded": True,
            "path": res.get("path", ""),
            "filename": res.get("filename", os.path.basename(raw)),
            "total_lines": res.get("total_lines", len(content.splitlines())),
            "content": content,
            "error": ""
        }

    def _build_preload_section(self, preload: Dict[str, Any]) -> str:
        """
        [현재 대상 파일 실시간 전수 독해] 섹션 문자열 생성.
        독해 성공 시 ```python 코드 블록으로 전체 코드를 100% 온전히 주입.
        """
        if not preload or not preload.get("loaded"):
            reason = (preload or {}).get("error", "대상 파일 미지정")
            return (
                "[현재 대상 파일 실시간 전수 독해]:\\n"
                "(선제적 전수 독해 미실행 — 사유: %s)\\n"
                "→ 대상 파일이 지정되지 않았거나 아직 존재하지 않습니다. "
                "필요 시 `read_file` 도구로 직접 확인하십시오." % reason
            )

        filename = preload.get("filename", "")
        path = preload.get("path", "")
        total_lines = preload.get("total_lines", 0)
        content = preload.get("content", "")

        return (
            "[현재 대상 파일 실시간 전수 독해] — Turn 0 선제 주입 (딥시크의 보는 눈):\\n"
            "- 파일명: %s\\n"
            "- 절대 경로: %s\\n"
            "- 전체 줄 수: %s줄 (처음부터 끝까지 100%% 온전히 독해 완료)\\n"
            "- 아래 코드는 디스크에 저장된 실제 최신 원문입니다. 수정 시 이 전체 코드를 기준으로 "
            "완결된 전체 코드를 `write_file` 로 기록하십시오.\\n"
            "```python\\n"
            "%s\\n"
            "```\\n" % (filename, path, total_lines, content)
        )

    # =====================================================================
    # [신규] 인자 키 5중 폴백 정규화 헬퍼
    # =====================================================================
    @staticmethod
    def _extract_path_arg(args: Dict[str, Any], context_file: str = "", target_file: str = "") -> str:
        """
        read_file / write_file / list_files 인자에서 경로를 5중 폴백으로 안전 추출.
        """
        if not isinstance(args, dict):
            args = {}
        return (
            args.get("path")
            or args.get("file_path")
            or args.get("filePath")
            or args.get("target_file")
            or args.get("filename")
            or context_file
            or target_file
            or ""
        )

    @staticmethod
    def _extract_content_arg(args: Dict[str, Any]) -> str:
        """
        write_file 인자에서 코드 본문을 5중 폴백으로 안전 추출.
        """
        if not isinstance(args, dict):
            return ""
        return (
            args.get("content")
            or args.get("code")
            or args.get("source")
            or args.get("new_content")
            or ""
        )

'''
        anchor = "    # =====================================================================\n    # [4] 파이썬 문법 검증 (py_compile)\n"
        if anchor in src:
            src = src.replace(anchor, helper + anchor, 1)
        else:
            raise SystemExit("ANCHOR_NOT_FOUND: py_compile section")

    # ------------------------------------------------------------------
    # 3) _PRELOAD_MAX_CHARS 상수 추가
    # ------------------------------------------------------------------
    if "_PRELOAD_MAX_CHARS" not in src:
        src = src.replace(
            "class AgentExecutor:",
            "# Turn 0 선제적 전수 독해 시 주입할 최대 문자 수 (파일 자름 방지 상한)\n"
            "_PRELOAD_MAX_CHARS = 200000\n\n\nclass AgentExecutor:",
            1,
        )

    write_src(src)
    print("PATCH_STAGE1_OK")
    print("orig_len=%d new_len=%d" % (orig_len, len(src)))


if __name__ == "__main__":
    main()
