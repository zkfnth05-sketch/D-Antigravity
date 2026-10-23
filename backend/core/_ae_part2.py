    # =====================================================================
    # 메인 진입점 1: execute_user_request
    # =====================================================================
    async def execute_user_request(
        self,
        user_prompt: str,
        context_file: str = "",
        broadcast_fn: Optional[Callable] = None
    ) -> str:
        # [2] context_file 이 주어지면 active_project 즉시 rebind
        self._rebind_project_for_context(context_file)

        inventory = self._build_context_inventory()
        system_prompt = self._build_system_prompt(context_file, inventory)

        # 1. 사용자 프롬프트를 영구 세션 메모리에 기록
        self.memory.add_user_message(user_prompt)

        # 2. [시스템 프롬프트 + 작업 메모리(수정파일목록) + 이전 대화 맥락] 자동 결합
        messages = self.memory.get_messages_for_llm(system_prompt)

        # Multi-turn Native Function Calling Loop (Up to 6 turns)
        final_answer = ""
        for turn in range(6):
            ai_resp = await self.llm.call_llm_with_tools(
                messages=messages,
                tools=NATIVE_TOOLS_SCHEMA,
                model=self.llm.model_chat,
                temperature=0.2
            )

            content = ai_resp.get("content") or ""
            tool_calls = ai_resp.get("tool_calls") or []

            # Stream thought / interim message to UI
            if content and broadcast_fn:
                await broadcast_fn({
                    "type": "agent_action",
                    "data": {
                        "speaker": "🤖 DeepSeek Agent",
                        "role": "assistant",
                        "message": content
                    }
                })

            # If no tools called, check if the model proposed code blocks for a file and auto-apply!
            if not tool_calls:
                code_blocks = re.findall(r'```python\s*([\s\S]*?)```', content)
                is_mod_req = any(
                    kw in user_prompt
                    for kw in ["수정", "고쳐", "에러", "해결", "자가치유", "반영", "바꿔", "패치"]
                )

                if code_blocks and is_mod_req and context_file:
                    target_p = self._resolve_path(context_file)
                    code_to_write = code_blocks[-1].strip()
                    if len(code_to_write.splitlines()) > 5:
                        write_res = self.tool_write_file(target_p, code_to_write)
                        if write_res.get("success"):
                            if broadcast_fn:
                                await broadcast_fn({
                                    "type": "file_modified",
                                    "data": {
                                        "path": write_res["path"],
                                        "filename": write_res["filename"],
                                        "content": write_res["content"],
                                        "lines": write_res["lines"]
                                    }
                                })
                                await broadcast_fn({
                                    "type": "agent_action",
                                    "data": {
                                        "speaker": "🛠️ DeepSeek [코드 디스크 자동 반영]",
                                        "role": "engineer",
                                        "message": f"작성된 최신 코드를 파일에 자동 저장 완료: `{write_res['filename']}` ({write_res['lines']}줄)"
                                    }
                                })
                            # [4] py_compile 결과 브로드캐스트
                            syntax = write_res.get("syntax", {})
                            if syntax.get("checked") and broadcast_fn:
                                await broadcast_fn({
                                    "type": "agent_action",
                                    "data": {
                                        "speaker": "🧪 파이썬 문법 검증 (py_compile)",
                                        "role": "tester",
                                        "message": syntax.get("message", "")
                                    }
                                })

            # If no tools called, we are finished!
            if not tool_calls:
                final_answer = content or "요청하신 작업을 성공적으로 완결하였습니다."
                self.memory.add_assistant_message(final_answer)
                return final_answer

            # Append assistant message with tool_calls
            assistant_msg = {"role": "assistant", "content": content}
            if tool_calls:
                assistant_msg["tool_calls"] = tool_calls
            messages.append(assistant_msg)
            self.memory.add_assistant_message(content, tool_calls)

            # Execute each official tool call
            for tc in tool_calls:
                func = tc.get("function", {})
                name = func.get("name")
                raw_args = func.get("arguments", "{}")
                tc_id = tc.get("id", f"call_{turn}")

                try:
                    args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
                except Exception:
                    args = {}

                tool_result_str = ""

                # 1. READ FILE (온전한 전체 독해)
                if name == "read_file":
                    p = args.get("path", "")
                    res = self.tool_read_file(p)
                    if res.get("success"):
                        tool_result_str = (
                            f"[파일 읽기 성공: {res['filename']} "
                            f"(총 {res['total_lines']}줄 온전히 독해 완료)]\n{res['full_text']}"
                        )
                        if broadcast_fn:
                            await broadcast_fn({
                                "type": "file_opened",
                                "data": {
                                    "path": res["path"],
                                    "filename": res["filename"],
                                    "content": res["content"]
                                }
                            })
                            await broadcast_fn({
                                "type": "agent_action",
                                "data": {
                                    "speaker": "📖 DeepSeek [파일 전수 독해 완료]",
                                    "role": "tester",
                                    "message": f"파일 전체 ({res['total_lines']}줄) 온전히 독해 완료: `{res['filename']}`"
                                }
                            })
                    else:
                        tool_result_str = f"[오류]: {res.get('error')}"

                # 2. WRITE FILE (+ py_compile 즉시 검증)
                elif name == "write_file":
                    p = args.get("path", "")
                    c = args.get("content", "")
                    write_res = self.tool_write_file(p, c)
                    if write_res.get("success"):
                        syntax = write_res.get("syntax", {})
                        tool_result_str = (
                            f"[파일 수정 완료: {write_res['filename']}] "
                            f"({write_res['lines']}줄 저장됨)\n"
                            f"[py_compile 문법 검증]: {syntax.get('message', '검증 미실행')}"
                        )
                        if broadcast_fn:
                            await broadcast_fn({
                                "type": "file_modified",
                                "data": {
                                    "path": write_res["path"],
                                    "filename": write_res["filename"],
                                    "content": write_res["content"],
                                    "lines": write_res["lines"]
                                }
                            })
                            await broadcast_fn({
                                "type": "agent_action",
                                "data": {
                                    "speaker": "🛠️ DeepSeek [코드 직접 수정 & 에디터 동기화]",
                                    "role": "engineer",
                                    "message": f"파일 및 화면 에디터에 코드 반영 완료: `{write_res['filename']}` ({write_res['lines']}줄)"
                                }
                            })
                            if syntax.get("checked"):
                                await broadcast_fn({
                                    "type": "agent_action",
                                    "data": {
                                        "speaker": "🧪 파이썬 문법 검증 (py_compile)",
                                        "role": "tester",
                                        "message": syntax.get("message", "")
                                    }
                                })
                    else:
                        tool_result_str = f"[저장 실패]: {p} — {write_res.get('error', '')}"

                # 3. RUN COMMAND
                elif name == "run_command":
                    cmd = args.get("cmd", "")
                    if broadcast_fn:
                        await broadcast_fn({
                            "type": "agent_action",
                            "data": {
                                "speaker": "⚡ DeepSeek [터미널 실제 구동]",
                                "role": "deployer",
                                "message": f"터미널 실행 중: `{cmd}`"
                            }
                        })
                    cmd_res = await self.tool_run_command(cmd)
                    tool_result_str = cmd_res
                    if broadcast_fn:
                        await broadcast_fn({
                            "type": "agent_action",
                            "data": {
                                "speaker": "📋 터미널 출력 결과",
                                "role": "tester",
                                "message": f"```text\n{cmd_res}\n```"
                            }
                        })

                # 4. LIST FILES
                elif name == "list_files":
                    p = args.get("path", "")
                    tool_result_str = self.tool_list_files(p)

                # Append tool result to messages and session memory
                tool_msg = {
                    "role": "tool",
                    "tool_call_id": tc_id,
                    "content": tool_result_str
                }
                messages.append(tool_msg)
                self.memory.add_tool_result(tc_id, name, tool_result_str)

        # If no explicit summary was given, request a concise final briefing from DeepSeek
        if not final_answer or final_answer == "요청하신 작업을 성공적으로 완결하였습니다.":
            try:
                summary_prompt = messages + [{
                    "role": "user",
                    "content": (
                        "지금까지 도구들을 통해 실제로 코드를 수정하고 터미널에서 검증한 결과를 바탕으로, "
                        "대표님께 무엇을 왜 어떻게 고쳤는지 3대 핵심 요약 보고서를 작성하십시오."
                    )
                }]
                briefing = await self.llm.call_llm(
                    summary_prompt,
                    model=self.llm.model_chat,
                    temperature=0.2
                )
                if briefing:
                    final_answer = briefing
                    self.memory.add_assistant_message(final_answer)
                    if broadcast_fn:
                        await broadcast_fn({
                            "type": "agent_action",
                            "data": {
                                "speaker": "📊 DeepSeek 최종 작업 보고",
                                "role": "assistant",
                                "message": final_answer
                            }
                        })
            except Exception:
                pass

        return final_answer or "모든 코드 수정 및 터미널 검증 작업이 완결되었습니다."

    # =====================================================================
    # 메인 진입점 2: run_autonomous_turn (server.py ws_agent_handler 호환)
    # =====================================================================
    async def run_autonomous_turn(
        self,
        prompt: str,
        context_file: str = "",
        target_file: str = "",
        broadcast_fn: Optional[Callable] = None
    ):
        """
        server.py 의 ws_agent_handler 가 사용하는 비동기 제너레이터 진입점.
        [2] context_file / target_file 이 주어지면 active_project 즉시 rebind.
        이벤트를 dict 형태로 yield 하여 WebSocket 으로 그대로 전송 가능.
        """
        ctx = (context_file or target_file or "").strip()

        # [2] 진입 즉시 프로젝트 rebind
        self._rebind_project_for_context(ctx)

        inventory = self._build_context_inventory()
        system_prompt = self._build_system_prompt(ctx, inventory)

        self.memory.add_user_message(prompt)
        messages = self.memory.get_messages_for_llm(system_prompt)

        final_answer = ""
        for turn in range(6):
            ai_resp = await self.llm.call_llm_with_tools(
                messages=messages,
                tools=NATIVE_TOOLS_SCHEMA,
                model=self.llm.model_chat,
                temperature=0.2
            )

            content = ai_resp.get("content") or ""
            tool_calls = ai_resp.get("tool_calls") or []

            if content:
                yield {
                    "type": "agent_action",
                    "data": {
                        "speaker": "🤖 DeepSeek Agent",
                        "role": "assistant",
                        "message": content
                    }
                }
                if broadcast_fn:
                    await broadcast_fn({
                        "type": "agent_action",
                        "data": {
                            "speaker": "🤖 DeepSeek Agent",
                            "role": "assistant",
                            "message": content
                        }
                    })

            if not tool_calls:
                final_answer = content or "요청하신 작업을 성공적으로 완결하였습니다."
                self.memory.add_assistant_message(final_answer)
                yield {
                    "type": "agent_final",
                    "data": {"message": final_answer}
                }
                return

            assistant_msg = {"role": "assistant", "content": content}
            assistant_msg["tool_calls"] = tool_calls
            messages.append(assistant_msg)
            self.memory.add_assistant_message(content, tool_calls)

            for tc in tool_calls:
                func = tc.get("function", {})
                name = func.get("name")
                raw_args = func.get("arguments", "{}")
                tc_id = tc.get("id", f"call_{turn}")

                try:
                    args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
                except Exception:
                    args = {}

                tool_result_str = ""

                # 1. READ FILE
                if name == "read_file":
                    res = self.tool_read_file(args.get("path", ""))
                    if res.get("success"):
                        tool_result_str = (
                            f"[파일 읽기 성공: {res['filename']} "
                            f"(총 {res['total_lines']}줄 온전히 독해 완료)]\n{res['full_text']}"
                        )
                        yield {
                            "type": "file_opened",
                            "data": {
                                "path": res["path"],
                                "filename": res["filename"],
                                "content": res["content"]
                            }
                        }
                    else:
                        tool_result_str = f"[오류]: {res.get('error')}"

                # 2. WRITE FILE (+ py_compile 즉시 검증)
                elif name == "write_file":
                    write_res = self.tool_write_file(
                        args.get("path", ""),
                        args.get("content", "")
                    )
                    if write_res.get("success"):
                        syntax = write_res.get("syntax", {})
                        tool_result_str = (
                            f"[파일 수정 완료: {write_res['filename']}] "
                            f"({write_res['lines']}줄 저장됨)\n"
                            f"[py_compile 문법 검증]: {syntax.get('message', '검증 미실행')}"
                        )
                        yield {
                            "type": "file_modified",
                            "data": {
                                "path": write_res["path"],
                                "filename": write_res["filename"],
                                "content": write_res["content"],
                                "lines": write_res["lines"]
                            }
                        }
                        if syntax.get("checked"):
                            yield {
                                "type": "agent_action",
                                "data": {
                                    "speaker": "🧪 파이썬 문법 검증 (py_compile)",
                                    "role": "tester",
                                    "message": syntax.get("message", "")
                                }
                            }
                    else:
                        tool_result_str = f"[저장 실패]: {args.get('path', '')} — {write_res.get('error', '')}"

                # 3. RUN COMMAND
                elif name == "run_command":
                    cmd = args.get("cmd", "")
                    yield {
                        "type": "agent_action",
                        "data": {
                            "speaker": "⚡ DeepSeek [터미널 실제 구동]",
                            "role": "deployer",
                            "message": f"터미널 실행 중: `{cmd}`"
                        }
                    }
                    cmd_res = await self.tool_run_command(cmd)
                    tool_result_str = cmd_res
                    yield {
                        "type": "agent_action",
                        "data": {
                            "speaker": "📋 터미널 출력 결과",
                            "role": "tester",
                            "message": f"```text\n{cmd_res}\n```"
                        }
                    }

                # 4. LIST FILES
                elif name == "list_files":
                    tool_result_str = self.tool_list_files(args.get("path", ""))

                messages.append({
                    "role": "tool",
                    "tool_call_id": tc_id,
                    "content": tool_result_str
                })
                self.memory.add_tool_result(tc_id, name, tool_result_str)

        # 루프 소진 시 최종 브리핑
        if not final_answer:
            try:
                summary_prompt = messages + [{
                    "role": "user",
                    "content": (
                        "지금까지 도구들을 통해 실제로 코드를 수정하고 터미널에서 검증한 결과를 바탕으로, "
                        "대표님께 무엇을 왜 어떻게 고쳤는지 3대 핵심 요약 보고서를 작성하십시오."
                    )
                }]
                briefing = await self.llm.call_llm(
                    summary_prompt,
                    model=self.llm.model_chat,
                    temperature=0.2
                )
                if briefing:
                    final_answer = briefing
                    self.memory.add_assistant_message(final_answer)
            except Exception:
                pass

        yield {
            "type": "agent_final",
            "data": {"message": final_answer or "모든 코드 수정 및 터미널 검증 작업이 완결되었습니다."}
        }
