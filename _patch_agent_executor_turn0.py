# -*- coding: utf-8 -*-
"""
agent_executor.py 패치 스크립트
[방법 1] 딥시크의 보는 눈 — Turn 0 선제적 전수 독해 자동 주입
+ 안전 쓰기 파이프라인 (인자 정규화 / 디렉터리 방어 / py_compile 검증)

패치 항목:
  1) _build_system_prompt(context_file, inventory, preloaded_code="") 시그니처 확장
     → [현재 대상 파일 실시간 전수 독해] 섹션을 프롬프트 최상단에 강제 주입
  2) _preload_context_file(context_file) 헬퍼 신설
     → Turn 0 진입 시 파일 전체를 tool_read_file 로 읽어 프롬프트에 주입
  3) execute_user_request / run_autonomous_turn 진입부에 Turn 0 주입 연결
  4) run_autonomous_turn 인자 정규화 (p / c / _current_context_file)
  5) tool_write_file 디렉터리 방어 강화 (자동 전환 실패 시 저장 거부)
"""
import io
import os
import sys

TARGET = os.path.join("backend", "core", "agent_executor.py")


def read_src():
    with io.open(TARGET, "r", encoding="utf-8") as f:
        return f.read()


def write_src(text):
    with io.open(TARGET, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


def must_replace(src, old, new, label):
    if old not in src:
        raise SystemExit("ANCHOR_NOT_FOUND: %s" % label)
    return src.replace(old, new, 1)


# ---------------------------------------------------------------------------
# 1) _build_system_prompt 확장 — Turn 0 전수 독해 섹션 주입
# ---------------------------------------------------------------------------
def patch_system_prompt(src):
    if "[현재 대상 파일 실시간 전수 독해]" in src:
        return src

    old_sig = (
        "    def _build_system_prompt(self, context_file: str, inventory: str) -> str:\n"
        "        return f\"\"\"당신은 'Antigravity Studio'의 최고 수석 자율 AI 코딩 엔지니어입니다.\n"
        "작업 디렉터리: {self.pm.active_project}\n"
        "현재 화면에 열린 파일: {context_file or '없음'}\n"
        "\n"
        "[프로젝트 사전 인벤토리]:\n"
        "{inventory}\n"
    )

    new_sig = (
        "    def _build_system_prompt(self, context_file: str, inventory: str,\n"
        "                             preloaded_code: str = \"\") -> str:\n"
        "        # ------------------------------------------------------------------\n"
        "        # [방법 1] 딥시크의 보는 눈 — Turn 0 선제적 전수 독해 섹션\n"
        "        # context_file 이 주어지고 preloaded_code 가 준비되면, 시스템 프롬프트\n"
        "        # 최상단에 파일 전체 코드를 그대로 주입하여 DeepSeek 이 시작하자마자\n"
        "        # (Turn 0) 파일 전체를 눈앞에 보면서 작업하도록 강제한다.\n"
        "        # ------------------------------------------------------------------\n"
        "        preload_section = \"\"\n"
        "        if preloaded_code:\n"
        "            preload_section = preloaded_code\n"
        "        elif context_file:\n"
        "            preload_section = (\n"
        "                \"[현재 대상 파일 실시간 전수 독해] (파일 독해 실패)\\n\"\n"
        "                f\"경로: {context_file}\\n\"\n"
        "                \"※ 파일이 존재하지 않거나 읽기에 실패했습니다. \"\n"
        "                \"필요 시 read_file 도구로 직접 확인하십시오.\\n\"\n"
        "            )\n"
        "\n"
        "        return f\"\"\"당신은 'Antigravity Studio'의 최고 수석 자율 AI 코딩 엔지니어입니다.\n"
        "작업 디렉터리: {self.pm.active_project}\n"
        "현재 화면에 열린 파일: {context_file or '없음'}\n"
        "\n"
        "{preload_section}\n"
        "[프로젝트 사전 인벤토리]:\n"
        "{inventory}\n"
    )

    return must_replace(src, old_sig, new_sig, "_build_system_prompt signature")


# ---------------------------------------------------------------------------
# 2) _preload_context_file 헬퍼 신설
# ---------------------------------------------------------------------------
def patch_preload_helper(src):
    if "def _preload_context_file(" in src:
        return src

    helper = '''    # =====================================================================
    # [방법 1] 딥시크의 보는 눈 — Turn 0 선제적 전수 독해 자동 주입
    # =====================================================================
    def _preload_context_file(self, context_file: str) -> str:
        """
        Turn 0 진입 시 대상 파일 전체를 실제로 읽어 시스템 프롬프트 주입용
        텍스트 블록으로 변환한다.

        반환 예시:
            [현재 대상 파일 실시간 전수 독해] (총 977줄 독해 완료)
            파일명: agent_executor.py
            경로: C:\\...\\backend\\core\\agent_executor.py
            ```python
            {content}
            ```

        파일이 없거나 읽기 실패 시 빈 문자열을 반환한다(예외 미전파).
        """
        raw = (context_file or "").strip().strip("\\"'`").strip()
        if not raw:
            return ""

        try:
            full_path = self._resolve_path(raw)
        except Exception:
            return ""

        if not full_path or not os.path.isfile(full_path):
            return ""

        try:
            res = self.tool_read_file(full_path)
        except Exception:
            return ""

        if not res.get("success"):
            return ""

        content = res.get("content", "") or ""
        filename = res.get("filename") or os.path.basename(full_path)
        total_lines = res.get("total_lines", len(content.splitlines()))

        # 확장자에 맞춰 코드펜스 언어 태그 결정
        ext = os.path.splitext(full_path)[1].lower()
        lang = {
            ".py": "python", ".js": "javascript", ".ts": "typescript",
            ".tsx": "tsx", ".jsx": "jsx", ".json": "json", ".html": "html",
            ".css": "css", ".md": "markdown", ".bat": "bat", ".sh": "bash",
            ".yml": "yaml", ".yaml": "yaml", ".sql": "sql"
        }.get(ext, "")

        block = (
            f"[현재 대상 파일 실시간 전수 독해] (총 {total_lines}줄 독해 완료)\\n"
            f"파일명: {filename}\\n"
            f"경로: {full_path}\\n"
            f"```{lang}\\n"
            f"{content}\\n"
            f"```\\n"
        )
        return block

'''
    anchor = "    # =====================================================================\n    # 시스템 프롬프트 조립\n    # =====================================================================\n"
    if anchor not in src:
        raise SystemExit("ANCHOR_NOT_FOUND: 시스템 프롬프트 조립")
    return src.replace(anchor, helper + anchor, 1)


# ---------------------------------------------------------------------------
# 3) execute_user_request 진입부 — Turn 0 주입 연결
# ---------------------------------------------------------------------------
def patch_execute_entry(src):
    if "[Turn 0 선제적 전수 독해]" in src:
        return src

    old = (
        "        # [2] context_file 이 주어지면 active_project 즉시 rebind\n"
        "        self._rebind_project_for_context(context_file)\n"
        "        self._current_context_file = context_file\n"
        "\n"
        "        inventory = self._build_context_inventory()\n"
        "        system_prompt = self._build_system_prompt(context_file, inventory)\n"
    )

    new = (
        "        # [2] context_file 이 주어지면 active_project 즉시 rebind\n"
        "        self._rebind_project_for_context(context_file)\n"
        "        self._current_context_file = context_file\n"
        "\n"
        "        # [방법 1][Turn 0 선제적 전수 독해] 시작 즉시 파일 전체를 눈앞에 주입\n"
        "        preloaded_code = self._preload_context_file(context_file)\n"
        "\n"
        "        inventory = self._build_context_inventory()\n"
        "        system_prompt = self._build_system_prompt(context_file, inventory, preloaded_code)\n"
    )

    return must_replace(src, old, new, "execute_user_request entry")


# ---------------------------------------------------------------------------
# 4) run_autonomous_turn 진입부 — Turn 0 주입 + 인자 정규화
# ---------------------------------------------------------------------------
def patch_autonomous_entry(src):
    if "[Turn 0 선제적 전수 독해]" in src and "_current_context_file = ctx" in src:
        return src

    old = (
        "        ctx = (context_file or target_file or \"\").strip()\n"
        "\n"
        "        # [2] 진입 즉시 프로젝트 rebind\n"
        "        self._rebind_project_for_context(ctx)\n"
        "\n"
        "        inventory = self._build_context_inventory()\n"
        "        system_prompt = self._build_system_prompt(ctx, inventory)\n"
    )

    new = (
        "        ctx = (context_file or target_file or \"\").strip()\n"
        "\n"
        "        # [2] 진입 즉시 프로젝트 rebind\n"
        "        self._rebind_project_for_context(ctx)\n"
        "\n"
        "        # [안전 쓰기 파이프라인] 현재 컨텍스트 파일 등록\n"
        "        # → write_file 도구가 디렉터리 경로를 받았을 때 자동 전환 대상으로 사용\n"
        "        self._current_context_file = ctx\n"
        "\n"
        "        # [방법 1][Turn 0 선제적 전수 독해] 시작 즉시 파일 전체를 눈앞에 주입\n"
        "        preloaded_code = self._preload_context_file(ctx)\n"
        "\n"
        "        inventory = self._build_context_inventory()\n"
        "        system_prompt = self._build_system_prompt(ctx, inventory, preloaded_code)\n"
    )

    return must_replace(src, old, new, "run_autonomous_turn entry")


# ---------------------------------------------------------------------------
# 5) run_autonomous_turn 인자 정규화 (p / c)
# ---------------------------------------------------------------------------
def patch_autonomous_args(src):
    if 'args.get("filePath")' in src and 'args.get("new_content")' in src:
        return src

    # 5-1) read_file 인자 정규화
    old_read = (
        "                # 1. READ FILE\n"
        "                if name == \"read_file\":\n"
        "                    res = self.tool_read_file(args.get(\"path\", \"\"))\n"
    )
    new_read = (
        "                # [안전 쓰기 파이프라인] 인자 정규화 — 모델이 어떤 키로 보내든 흡수\n"
        "                p = (args.get(\"path\") or args.get(\"file_path\") or args.get(\"filePath\")\n"
        "                     or args.get(\"target_file\") or args.get(\"filename\")\n"
        "                     or context_file or target_file or \"\")\n"
        "                c = (args.get(\"content\") or args.get(\"code\") or args.get(\"source\")\n"
        "                     or args.get(\"new_content\") or \"\")\n"
        "\n"
        "                # 1. READ FILE\n"
        "                if name == \"read_file\":\n"
        "                    res = self.tool_read_file(p)\n"
    )
    src = must_replace(src, old_read, new_read, "run_autonomous_turn read_file args")

    # 5-2) write_file 인자 정규화
    old_write = (
        "                elif name == \"write_file\":\n"
        "                    write_res = self.tool_write_file(\n"
        "                        args.get(\"path\", \"\"),\n"
        "                        args.get(\"content\", \"\")\n"
        "                    )\n"
    )
    new_write = (
        "                elif name == \"write_file\":\n"
        "                    write_res = self.tool_write_file(p, c)\n"
    )
    src = must_replace(src, old_write, new_write, "run_autonomous_turn write_file args")

    # 5-3) 저장 실패 메시지의 경로 참조 정규화
    old_fail = (
        "                        tool_result_str = f\"[저장 실패]: {args.get('path', '')} — {write_res.get('error', '')}\"\n"
    )
    new_fail = (
        "                        tool_result_str = f\"[저장 실패]: {p} — {write_res.get('error', '')}\"\n"
    )
    src = must_replace(src, old_fail, new_fail, "run_autonomous_turn write fail msg")

    # 5-4) list_files 인자 정규화
    old_list = (
        "                # 4. LIST FILES\n"
        "                elif name == \"list_files\":\n"
        "                    tool_result_str = self.tool_list_files(args.get(\"path\", \"\"))\n"
    )
    new_list = (
        "                # 4. LIST FILES\n"
        "                elif name == \"list_files\":\n"
        "                    tool_result_str = self.tool_list_files(p)\n"
    )
    src = must_replace(src, old_list, new_list, "run_autonomous_turn list_files args")

    return src


# ---------------------------------------------------------------------------
# 6) tool_write_file 디렉터리 방어 강화
# ---------------------------------------------------------------------------
def patch_write_defense(src):
    if "[안전 쓰기 파이프라인] 디렉터리 방어" in src:
        return src

    old = (
        "    def tool_write_file(self, path: str, content: str) -> Dict[str, Any]:\n"
        "        full_path = self._resolve_path(path)\n"
        "        if os.path.isdir(full_path):\n"
        "            ctx_cand = getattr(self, '_current_context_file', '')\n"
        "            if ctx_cand:\n"
        "                resolved_cand = self._resolve_path(ctx_cand)\n"
        "                if not os.path.isdir(resolved_cand):\n"
        "                    full_path = resolved_cand\n"
        "        res = self.pm.save_file_content(full_path, content)\n"
    )

    new = (
        "    def tool_write_file(self, path: str, content: str) -> Dict[str, Any]:\n"
        "        # ------------------------------------------------------------------\n"
        "        # [안전 쓰기 파이프라인] 디렉터리 방어\n"
        "        # 모델이 path 대신 디렉터리(프로젝트 루트 등)를 넘기는 사고를 원천 차단.\n"
        "        #   1차: _current_context_file 이 유효한 '파일'이면 그쪽으로 자동 전환\n"
        "        #   2차: 그래도 디렉터리면 저장을 명시적으로 거부 (Permission denied 방지)\n"
        "        # ------------------------------------------------------------------\n"
        "        raw_path = (path or \"\").strip().strip(\"\\\"'`\").strip()\n"
        "        full_path = self._resolve_path(raw_path)\n"
        "\n"
        "        if os.path.isdir(full_path):\n"
        "            ctx_cand = (getattr(self, '_current_context_file', '') or \"\").strip()\n"
        "            switched = False\n"
        "            if ctx_cand:\n"
        "                try:\n"
        "                    resolved_cand = self._resolve_path(ctx_cand)\n"
        "                except Exception:\n"
        "                    resolved_cand = \"\"\n"
        "                if resolved_cand and not os.path.isdir(resolved_cand):\n"
        "                    full_path = resolved_cand\n"
        "                    switched = True\n"
        "\n"
        "            if not switched or os.path.isdir(full_path):\n"
        "                return {\n"
        "                    \"success\": False,\n"
        "                    \"path\": full_path,\n"
        "                    \"filename\": os.path.basename(full_path.rstrip(\"\\\\/\")),\n"
        "                    \"lines\": len((content or \"\").splitlines()),\n"
        "                    \"content\": content,\n"
        "                    \"syntax\": {\n"
        "                        \"checked\": False,\n"
        "                        \"valid\": False,\n"
        "                        \"message\": \"디렉터리 경로 거부로 문법 검증 미실행\",\n"
        "                        \"error\": \"\"\n"
        "                    },\n"
        "                    \"error\": (\n"
        "                        f\"저장 거부: '{full_path}' 는 디렉터리입니다. \"\n"
        "                        \"파일 경로(예: backend/core/xxx.py)를 명시하십시오.\"\n"
        "                    )\n"
        "                }\n"
        "\n"
        "        res = self.pm.save_file_content(full_path, content)\n"
    )

    return must_replace(src, old, new, "tool_write_file directory defense")


def main():
    src = read_src()
    orig_len = len(src)

    src = patch_system_prompt(src)
    src = patch_preload_helper(src)
    src = patch_execute_entry(src)
    src = patch_autonomous_entry(src)
    src = patch_autonomous_args(src)
    src = patch_write_defense(src)

    write_src(src)
    print("PATCH_OK")
    print("orig_len=%d new_len=%d delta=%+d" % (orig_len, len(src), len(src) - orig_len))


if __name__ == "__main__":
    main()
