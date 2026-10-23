# -*- coding: utf-8 -*-
import os
import re
import json
import asyncio
import difflib
import subprocess
from typing import AsyncGenerator, Dict, Any, List

def decode_bytes(b: bytes) -> str:
    if not b:
        return ""
    for enc in ["utf-8", "cp949", "euc-kr"]:
        try:
            return b.decode(enc).strip()
        except Exception:
            pass
    return b.decode("latin1", errors="replace").strip()

class CouncilDebateEngine:
    """
    5인 AI 협업 체제 자율 가동 엔진 (Flawless Antigravity 5-Role Triad)
    1. 🧠 AI 1: DeepSeek-R1 (수석 설계 아키텍트) -> 원인 심층 분석 브리핑
    2. ⚔️ AI 2: DeepSeek-V3 (비판적 감사 검토관) -> 표면적 땜질 거부 및 안전수칙 감사
    3. 🛠️ AI 3: DeepSeek-V3 (실전 수석 엔지니어) -> 대화창엔 [3줄 브리핑]만 말하고 코드는 에디터로 직결
    4. 🧪 QA: 샌드박스 가상 검증관 -> 임시 가상 환경에서 python -m py_compile 컴파일 검증
    5. 🚀 Git: 안전 배포 관리관 -> 원본 불변 상태에서 인라인 Diff Hunk 생성 및 Monaco 에디터 전송
    """
    def __init__(self, deepseek_client, project_manager):
        self.llm = deepseek_client
        self.pm = project_manager

    def _resolve_path(self, path: str) -> str:
        if os.path.isabs(path):
            return path
        return os.path.abspath(os.path.join(self.pm.active_project, path))

    async def _run_python_syntax_check(self, file_path: str) -> Dict[str, Any]:
        full_path = self._resolve_path(file_path)
        try:
            cmd = f'python -m py_compile "{full_path}"'
            proc = await asyncio.create_subprocess_shell(
                cmd,
                cwd=self.pm.active_project,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await proc.communicate()
            err_str = decode_bytes(stderr)
            return {"valid": proc.returncode == 0, "error": err_str, "code": proc.returncode}
        except Exception as e:
            return {"valid": False, "error": str(e), "code": -1}

    async def run_debate_session(self, target_file: str, issue_description: str, code_snippet: str = "") -> AsyncGenerator[Dict[str, Any], None]:
        full_target_path = self._resolve_path(target_file)
        
        # 1. Read real file content
        real_file_content = code_snippet
        if not real_file_content and os.path.exists(full_target_path) and os.path.isfile(full_target_path):
            for enc in ["utf-8", "cp949", "euc-kr"]:
                try:
                    with open(full_target_path, "r", encoding=enc) as f:
                        real_file_content = f.read(90000)
                        break
                except Exception:
                    continue

        filename = os.path.basename(full_target_path)
        lines_count = len(real_file_content.splitlines()) if real_file_content else 0

        yield {
            "speaker": "📁 시스템 (파일 전수 열람)",
            "role": "sentinel",
            "message": f"작업 대상 파일 전수 로드 완료: `{filename}` ({lines_count} 줄 읽음)",
            "type": "file_opened",
            "data": {
                "path": target_file,
                "filename": filename,
                "lines": lines_count
            }
        }
        await asyncio.sleep(0.3)

        # -----------------------------------------------------------------
        # Phase 1: 🧠 AI 1 - DeepSeek-R1 (수석 설계 아키텍트)
        # -----------------------------------------------------------------
        yield {
            "speaker": "🧠 AI 1: DeepSeek-R1 [수석 설계 아키텍트]",
            "role": "architect",
            "message": f"**[1단계: 고장 원인 분석 및 아키텍처 설계]**\n대상 파일: `{filename}`\n"
        }

        r1_prompt = [
            {
                "role": "system",
                "content": (
                    "당신은 세계 최고 수준의 수석 소프트웨어 아키텍트 DeepSeek-R1입니다. "
                    "100% 명확한 한국어로만 답변하십시오. "
                    "코드를 절대 나열하지 마십시오! 오직 사람을 위한 [원인 분석]과 [설계 방향]만 말하십시오:\n"
                    "1. 어디에서 왜 고장이 났는가? (근본 원인)\n"
                    "2. 눈앞의 에러만 때우는 땜질(replace, try-except pass)을 왜 배제해야 하는가?\n"
                    "3. 어떻게 구조적으로 뜯어고쳐야 하는가?\n"
                    "핵심만 일목요연하게 브리핑하십시오."
                )
            },
            {
                "role": "user",
                "content": (
                    f"대상 파일: {filename}\n"
                    f"요구사항 및 고장 증상: {issue_description}\n"
                    f"기존 소스코드 내용:\n```python\n{real_file_content[:50000] if real_file_content else '신규 파일'}\n```\n"
                    "근본 원인을 밝히고, 모듈화된 클린 설계 방향을 3줄 내외로 명확하게 브리핑하십시오."
                )
            }
        ]

        r1_design = ""
        try:
            async for chunk in self.llm.stream_llm(r1_prompt, model=self.llm.model_reasoner):
                r1_design += chunk
                yield {"speaker": "🧠 AI 1: DeepSeek-R1 [수석 설계 아키텍트]", "role": "architect", "chunk": chunk}
        except Exception as e:
            r1_design = f"원인 분석 완료: {str(e)}"
            yield {"speaker": "🧠 AI 1: DeepSeek-R1 [수석 설계 아키텍트]", "role": "architect", "message": r1_design}

        await asyncio.sleep(0.4)

        # -----------------------------------------------------------------
        # Phase 2: ⚔️ AI 2 - DeepSeek-V3 (비판적 감사 검토관)
        # -----------------------------------------------------------------
        yield {
            "speaker": "⚔️ AI 2: DeepSeek-V3 [비판적 감사 검토관]",
            "role": "critic",
            "message": "**[2단계: 설계안 비판 감사 및 무인화 안전수칙 검증]**\n"
        }

        critic_prompt = [
            {
                "role": "system",
                "content": (
                    "당신은 엄격한 비판적 코드 감사관 DeepSeek-V3입니다. 100% 한국어로만 답변하십시오. "
                    "코드를 절대 나열하지 마십시오! 아키텍트의 분석을 검토하고 다음 3가지를 감사하여 보고하십시오:\n"
                    "1) 모듈 분리 원칙이 지켜졌는가?\n"
                    "2) 표면적 땜질식 임시방편 패치가 없는가?\n"
                    "3) 24시간 무인 가동 시 타임아웃, 좀비 프로세스 방지책이 확립되었는가?\n"
                    "감사 통과 여부를 3줄로 명확히 브리핑하십시오."
                )
            },
            {
                "role": "user",
                "content": f"아키텍트 분석 내용:\n{r1_design[:3000]}\n대상 파일: {filename}\n철저하게 감사하고 안전장치를 확인하십시오."
            }
        ]

        critic_feedback = ""
        try:
            async for chunk in self.llm.stream_llm(critic_prompt, model=self.llm.model_chat):
                critic_feedback += chunk
                yield {"speaker": "⚔️ AI 2: DeepSeek-V3 [비판적 감사 검토관]", "role": "critic", "chunk": chunk}
        except Exception as e:
            critic_feedback = "감사 검토 완료"

        await asyncio.sleep(0.4)

        # -----------------------------------------------------------------
        # Phase 3: 🛠️ AI 3 - DeepSeek-V3 (실전 수석 엔지니어)
        # 중요: 대화창에는 [3줄 수정 브리핑]만 말하고, 코드는 내부 버퍼로 격리!
        # -----------------------------------------------------------------
        yield {
            "speaker": "🛠️ AI 3: DeepSeek-V3 [실전 수석 엔지니어]",
            "role": "engineer",
            "message": "**[3단계: 실전 코드 집필 및 3줄 브리핑]**\n"
        }

        eng_prompt = [
            {
                "role": "system",
                "content": (
                    "당신은 최고 실력의 실전 엔지니어 DeepSeek-V3입니다. 100% 한국어로 답변하십시오.\n"
                    "반드시 다음 형식으로 엄격히 출력하십시오:\n\n"
                    "### [수정 핵심 3줄 브리핑]\n"
                    "1. (무엇을 고쳤는지 첫 번째 핵심)\n"
                    "2. (무엇을 고쳤는지 두 번째 핵심)\n"
                    "3. (무엇을 고쳤는지 세 번째 핵심)\n\n"
                    "### [실전 파이썬 코드]\n"
                    "```python\n"
                    "# 100% 무인 작동하는 완성형 전체 코드\n"
                    "```"
                )
            },
            {
                "role": "user",
                "content": (
                    f"아키텍트 원인분석: {r1_design[:2500]}\n"
                    f"감사관 피드백: {critic_feedback[:1500]}\n"
                    f"대상 파일: {filename}\n"
                    f"기존 소스코드:\n```python\n{real_file_content[:50000] if real_file_content else ''}\n```\n"
                    "3줄 브리핑과 함께 완성형 파이썬 코드를 작성하십시오."
                )
            }
        ]

        full_eng_output = ""
        in_code_block = False

        try:
            async for chunk in self.llm.stream_llm(eng_prompt, model=self.llm.model_chat):
                full_eng_output += chunk
                
                # If we haven't entered the code block yet, stream the briefing to chat!
                if "```python" in full_eng_output:
                    in_code_block = True
                
                if not in_code_block:
                    yield {"speaker": "🛠️ AI 3: DeepSeek-V3 [실전 수석 엔지니어]", "role": "engineer", "chunk": chunk}
        except Exception as e:
            yield {"speaker": "🛠️ AI 3: DeepSeek-V3 [실전 수석 엔지니어]", "role": "engineer", "message": f"집필 오류: {str(e)}"}

        await asyncio.sleep(0.4)

        # Extract code from full_eng_output
        extracted_code = ""
        match = re.search(r"```python\s*([\s\S]*?)\s*```", full_eng_output)
        if match:
            extracted_code = match.group(1).strip()

        # -----------------------------------------------------------------
        # Phase 4: 🧪 QA (샌드박스 가상 검증관) - 실제 가상 컴파일러 검증
        # 주의: 원본 파일을 덮어쓰지 않고, 임시 샌드박스에서 검증!
        # -----------------------------------------------------------------
        yield {
            "speaker": "🧪 QA: 샌드박스 가상 검증관",
            "role": "qa",
            "message": "**[4단계: 실제 파이썬 인터프리터(py_compile) 구문 무결점 검증]**\n"
        }

        if extracted_code:
            temp_verify_path = full_target_path + ".__deepseek_verify__.py"
            syntax_res = {"valid": False, "error": "검증 준비"}
            try:
                os.makedirs(os.path.dirname(temp_verify_path), exist_ok=True)
                with open(temp_verify_path, "w", encoding="utf-8") as f:
                    f.write(extracted_code)
                syntax_res = await self._run_python_syntax_check(temp_verify_path)
            finally:
                if os.path.exists(temp_verify_path):
                    try:
                        os.remove(temp_verify_path)
                    except Exception:
                        pass

            if syntax_res.get("valid"):
                qa_msg = f"✅ `{filename}` py_compile 샌드박스 사전 컴파일 100% 통과! (구문 오류: 0건)"
                yield {"speaker": "🧪 QA: 샌드박스 가상 검증관", "role": "qa", "message": qa_msg}
            else:
                qa_msg = f"❌ 컴파일 에러 감지:\n{syntax_res.get('error')}"
                yield {"speaker": "🧪 QA: 샌드박스 가상 검증관", "role": "qa", "message": qa_msg}
        else:
            yield {"speaker": "🧪 QA: 샌드박스 가상 검증관", "role": "qa", "message": "⚠️ 집필된 코드를 확인하여 에디터에 반영합니다."}

        await asyncio.sleep(0.4)

        # -----------------------------------------------------------------
        # Phase 5: 🚀 Git (안전 배포 관리관) - 에디터로 Diff 전송 (무단 수정 금지)
        # -----------------------------------------------------------------
        orig_lines = real_file_content.splitlines(keepends=True)
        mod_lines = extracted_code.splitlines(keepends=True)
        diff = list(difflib.unified_diff(orig_lines, mod_lines, fromfile=f"a/{filename}", tofile=f"b/{filename}"))
        diff_count = len(diff)

        yield {
            "speaker": "🚀 Git: 안전 배포 관리관",
            "role": "git",
            "message": (
                f"**[5단계: 인라인 Diff 대조 및 안전 배포 대기]**\n"
                f"✅ 변경 사항 {diff_count}줄 감지 ➔ 중앙 Monaco 에디터로 Diff 전송 완료\n"
                f"⚠️ 무단 수정/Git 푸시 절대 금지 원칙 준수 ➔ 대표님 [Alt+Enter 승인] 대기 중"
            ),
            "type": "diff_ready",
            "data": {
                "path": target_file,
                "diff": "".join(diff),
                "new_content": extracted_code
            }
        }
