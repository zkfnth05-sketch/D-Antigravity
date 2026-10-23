# -*- coding: utf-8 -*-
"""
agent_executor.py 전면 개편 검증 단위 테스트
- [1] 동적 인벤토리 (kmarket 하드코딩 제거)
- [2] context_file 기반 active_project 자동 rebind
- [3] pm.resolve_path() 우선 경로 정규화
- [4] .py 저장 시 py_compile 즉시 검증 + 메모리 기록
"""
import os
import sys
import asyncio
import tempfile
import shutil

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(BASE, "backend"))

from core.agent_executor import AgentExecutor, NATIVE_TOOLS_SCHEMA  # noqa: E402

PASS = []
FAIL = []


def check(name, cond, detail=""):
    if cond:
        PASS.append(name)
        print(f"  [PASS] {name}")
    else:
        FAIL.append(f"{name} :: {detail}")
        print(f"  [FAIL] {name} :: {detail}")


# ---------------------------------------------------------------------
# 가짜 ProjectManager (실제 resolve_path / auto_bind 동작 재현)
# ---------------------------------------------------------------------
class FakePM:
    def __init__(self, root):
        self.active_project = root
        self.bound_calls = []

    def resolve_path(self, path):
        if not path:
            return self.active_project
        if os.path.isabs(path):
            return os.path.abspath(path)
        return os.path.abspath(os.path.join(self.active_project, path))

    def auto_bind_project_for_file(self, file_path):
        self.bound_calls.append(file_path)
        # 파일이 속한 프로젝트 루트를 찾아 active_project 전환
        p = os.path.abspath(file_path)
        while p and p != os.path.dirname(p):
            if os.path.isdir(os.path.join(p, ".git")) or os.path.isfile(os.path.join(p, "config.json")):
                self.active_project = p
                return p
            p = os.path.dirname(p)
        return self.active_project

    def read_file_content(self, file_path):
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                return {"success": True, "content": f.read()}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def save_file_content(self, file_path, content):
        try:
            os.makedirs(os.path.dirname(file_path), exist_ok=True)
            with open(file_path, "w", encoding="utf-8", newline="\n") as f:
                f.write(content)
            return {"success": True}
        except Exception as e:
            return {"success": False, "error": str(e)}


class FakeLLM:
    model_chat = "fake-model"

    async def call_llm_with_tools(self, messages, tools, model, temperature):
        return {"content": "테스트 응답", "tool_calls": []}

    async def call_llm(self, messages, model, temperature):
        return "테스트 브리핑"


def build_sandbox():
    """마케팅봇 유사 구조 + 아우라 유사 구조를 가진 임시 프로젝트 생성"""
    root = tempfile.mkdtemp(prefix="ae_test_")
    proj_a = os.path.join(root, "마케팅봇_샌드박스")
    proj_b = os.path.join(root, "Aura_샌드박스")

    # 프로젝트 A: core/ 패키지 + brands/ 모듈폴더 + run_main.py
    os.makedirs(os.path.join(proj_a, "core"))
    open(os.path.join(proj_a, "core", "__init__.py"), "w").close()
    open(os.path.join(proj_a, "core", "engine.py"), "w", encoding="utf-8").write("x = 1\n")
    open(os.path.join(proj_a, "core", "scheduler.py"), "w", encoding="utf-8").write("y = 2\n")
    os.makedirs(os.path.join(proj_a, "brands"))
    open(os.path.join(proj_a, "brands", "aura.py"), "w", encoding="utf-8").write("z = 3\n")
    open(os.path.join(proj_a, "run_main.py"), "w", encoding="utf-8").write("print('a')\n")
    open(os.path.join(proj_a, "config.json"), "w", encoding="utf-8").write("{}")

    # 프로젝트 B: modules/ 패키지 + daemon.py
    os.makedirs(os.path.join(proj_b, "modules"))
    open(os.path.join(proj_b, "modules", "__init__.py"), "w").close()
    open(os.path.join(proj_b, "modules", "worker.py"), "w", encoding="utf-8").write("w = 9\n")
    open(os.path.join(proj_b, "daemon.py"), "w", encoding="utf-8").write("print('b')\n")
    open(os.path.join(proj_b, "config.json"), "w", encoding="utf-8").write("{}")

    return root, proj_a, proj_b


def main():
    root, proj_a, proj_b = build_sandbox()
    try:
        print("\n=== [1] 동적 인벤토리 (kmarket 하드코딩 제거) ===")
        pm = FakePM(proj_a)
        ex = AgentExecutor(FakeLLM(), pm)
        inv_a = ex._build_context_inventory()
        print(inv_a)
        check("인벤토리에 프로젝트명 표기", "마케팅봇_샌드박스" in inv_a, inv_a[:200])
        check("core/ 패키지 동적 탐지", "core/" in inv_a and "파이썬 패키지" in inv_a, inv_a[:400])
        check("brands/ 모듈폴더 동적 탐지", "brands/" in inv_a, inv_a[:400])
        check("진입점 후보 탐지", "run_main.py" in inv_a, inv_a[:400])
        check("kmarket 문자열 미포함", "kmarket" not in inv_a, inv_a[:200])

        # 프로젝트 B로 전환 → 완전히 다른 구조를 스캔해야 함
        pm.active_project = proj_b
        inv_b = ex._build_context_inventory()
        print("\n--- 프로젝트 B 인벤토리 ---")
        print(inv_b)
        check("B 프로젝트 modules/ 탐지", "modules/" in inv_b, inv_b[:400])
        check("B 프로젝트에 core/ 미등장", "core/" not in inv_b, inv_b[:400])
        check("B 프로젝트 daemon.py 진입점", "daemon.py" in inv_b, inv_b[:400])

        print("\n=== [2] context_file 기반 active_project 자동 rebind ===")
        pm2 = FakePM(proj_a)
        ex2 = AgentExecutor(FakeLLM(), pm2)
        target = os.path.join(proj_b, "modules", "worker.py")
        bound = ex2._rebind_project_for_context(target)
        check("rebind 후 active_project = 프로젝트 B", os.path.abspath(bound) == os.path.abspath(proj_b), bound)
        check("auto_bind_project_for_file 실제 호출됨", len(pm2.bound_calls) == 1, str(pm2.bound_calls))

        # execute_user_request 진입 시 rebind 되는지
        pm3 = FakePM(proj_a)
        ex3 = AgentExecutor(FakeLLM(), pm3)
        asyncio.get_event_loop().run_until_complete(
            ex3.execute_user_request("테스트", context_file=target)
        )
        check("execute_user_request 진입 시 rebind", os.path.abspath(pm3.active_project) == os.path.abspath(proj_b), pm3.active_project)

        # run_autonomous_turn 진입 시 rebind 되는지
        pm4 = FakePM(proj_a)
        ex4 = AgentExecutor(FakeLLM(), pm4)

        async def drain():
            async for _ in ex4.run_autonomous_turn("테스트", context_file=target):
                pass

        asyncio.get_event_loop().run_until_complete(drain())
        check("run_autonomous_turn 진입 시 rebind", os.path.abspath(pm4.active_project) == os.path.abspath(proj_b), pm4.active_project)

        print("\n=== [3] pm.resolve_path() 우선 경로 정규화 ===")
        pm5 = FakePM(proj_a)
        ex5 = AgentExecutor(FakeLLM(), pm5)
        rel = ex5._resolve_path("core/engine.py")
        check("상대경로 → 절대경로 정규화", os.path.isabs(rel) and rel.endswith("engine.py"), rel)
        check("정규화 결과가 active_project 하위", os.path.abspath(rel).startswith(os.path.abspath(proj_a)), rel)
        abs_p = os.path.join(proj_b, "daemon.py")
        check("절대경로 그대로 유지", ex5._resolve_path(abs_p) == os.path.abspath(abs_p), ex5._resolve_path(abs_p))
        check("빈 경로 → active_project", ex5._resolve_path("") == pm5.active_project, ex5._resolve_path(""))

        print("\n=== [4] .py 저장 시 py_compile 즉시 검증 ===")
        pm6 = FakePM(proj_a)
        ex6 = AgentExecutor(FakeLLM(), pm6)

        # 4-1. 정상 파이썬
        ok = ex6.tool_write_file("core/good.py", "def hello():\n    return 1\n")
        check("정상 .py 저장 성공", ok.get("success") is True, str(ok)[:200])
        check("syntax.checked True", ok["syntax"]["checked"] is True, str(ok["syntax"]))
        check("syntax.valid True", ok["syntax"]["valid"] is True, str(ok["syntax"]))
        check("검증 통과 메시지", "통과" in ok["syntax"]["message"], ok["syntax"]["message"])

        # 4-2. 문법 오류 파이썬
        bad = ex6.tool_write_file("core/bad.py", "def broken(:\n    pass\n")
        check("문법오류 파일도 저장은 성공", bad.get("success") is True, str(bad)[:200])
        check("syntax.valid False", bad["syntax"]["valid"] is False, str(bad["syntax"]))
        check("오류 메시지 포함", "문법 오류" in bad["syntax"]["message"], bad["syntax"]["message"])

        # 4-3. 비파이썬 파일은 검증 생략
        txt = ex6.tool_write_file("core/note.txt", "hello")
        check("비파이썬 검증 생략", txt["syntax"]["checked"] is False, str(txt["syntax"]))

        # 4-4. 메모리 기록 확인
        mem_dump = str(ex6.memory.__dict__)
        check("메모리에 문법검증 통과 기록", "py_compile 문법 검증 통과" in mem_dump, mem_dump[:300])
        check("메모리에 문법오류 기록", "py_compile 문법 오류" in mem_dump, mem_dump[:300])

        print("\n=== [5] 도구 스키마 무결성 ===")
        names = [t["function"]["name"] for t in NATIVE_TOOLS_SCHEMA]
        check("4대 도구 정의", names == ["read_file", "write_file", "run_command", "list_files"], str(names))

        print("\n=== [6] read_file / list_files 정규화 동작 ===")
        rd = ex6.tool_read_file("core/good.py")
        check("read_file 성공", rd.get("success") is True, str(rd)[:200])
        check("read_file 절대경로 반환", os.path.isabs(rd["path"]), rd["path"])
        ls = ex6.tool_list_files("core")
        check("list_files 정상 조회", "good.py" in ls, ls[:200])

    finally:
        shutil.rmtree(root, ignore_errors=True)

    print("\n" + "=" * 60)
    print(f"총 {len(PASS)}개 통과 / {len(FAIL)}개 실패")
    if FAIL:
        for f in FAIL:
            print("  ✗", f)
        sys.exit(1)
    print("✅ 모든 단위 테스트 통과 — 무결점 확인")
    print("=" * 60)


if __name__ == "__main__":
    main()
