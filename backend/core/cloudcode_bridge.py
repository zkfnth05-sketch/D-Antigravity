# -*- coding: utf-8 -*-
"""
CloudCode & Language Server Bridge Adapter for Antigravity IDE
Connects IDE internal RPC/REST requests directly to DeepSeek API engine.
"""
import json
import asyncio
from aiohttp import web, ClientConnectionResetError

class CloudCodeBridge:
    def __init__(self, deepseek_client, council_engine, project_manager):
        self.llm = deepseek_client
        self.council = council_engine
        self.pm = project_manager

    def json_response(self, data, status=200):
        return web.json_response(data, status=status, headers={
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "*",
            "Access-Control-Allow-Headers": "*"
        })

    async def handle_load_code_assist(self, request):
        """Responds to IDE loadCodeAssist so Language Server activates the Agent UI."""
        data = {
            "currentTier": {
                "id": "deepseek_pro",
                "name": "DeepSeek Multi-Agent Pro",
                "description": "5-Role DeepSeek Autonomous Council"
            },
            "allowedTiers": [
                {
                    "id": "deepseek_pro",
                    "name": "DeepSeek Pro",
                    "isDefault": True
                }
            ],
            "models": {
                "deepseek-reasoner": {
                    "id": "deepseek-reasoner",
                    "name": "DeepSeek-R1 (수석 아키텍트)",
                    "displayName": "DeepSeek-R1"
                },
                "deepseek-chat": {
                    "id": "deepseek-chat",
                    "name": "DeepSeek-V3 (수석 엔지니어)",
                    "displayName": "DeepSeek-V3"
                }
            },
            "cascadeNuxes": []
        }
        return self.json_response(data)

    async def handle_fetch_available_models(self, request):
        """Populates the IDE model selector with actual DeepSeek models."""
        data = {
            "availableModels": [
                {
                    "modelId": "deepseek-reasoner",
                    "displayName": "DeepSeek-R1 (수석 아키텍트)",
                    "description": "5대 협업팀 심층 원인분석 및 아키텍처 설계"
                },
                {
                    "modelId": "deepseek-chat",
                    "displayName": "DeepSeek-V3 (수석 엔지니어)",
                    "description": "무인 자동화 코드베이스 정밀 구현"
                }
            ],
            "models": {
                "deepseek-reasoner": {
                    "id": "deepseek-reasoner",
                    "name": "DeepSeek-R1 (수석 아키텍트)"
                },
                "deepseek-chat": {
                    "id": "deepseek-chat",
                    "name": "DeepSeek-V3 (수석 엔지니어)"
                }
            }
        }
        return self.json_response(data)

    async def handle_user_info(self, request):
        """Returns authenticated user info so language server activates the user session."""
        return self.json_response({
            "userSettings": {},
            "userTags": ["TRUSTED_TESTER"],
            "regionCode": "KR",
            "email": "deepseek@antigravity.local"
        })

    async def handle_onboard_user(self, request):
        return self.json_response({
            "status": "ok",
            "onboarded": True
        })

    async def handle_user_quota(self, request):
        return self.json_response({"buckets": [], "groups": []})

    async def handle_experiments(self, request):
        return self.json_response({"experiments": []})

    async def handle_cascade_nuxes(self, request):
        return self.json_response({"cascadeNuxes": []})

    async def handle_stream_generate_content(self, request):
        """
        Receives chat/code generation requests from Antigravity IDE and
        streams responses from real DeepSeek API in SSE format.
        """
        try:
            req_data = await request.json()
        except Exception:
            req_data = {}

        # Extract user prompt from Google request structure
        user_prompt = ""
        contents = req_data.get("contents", [])
        if contents:
            for c in contents:
                parts = c.get("parts", [])
                for p in parts:
                    if isinstance(p, dict) and "text" in p:
                        user_prompt += p["text"] + "\n"
        if not user_prompt:
            user_prompt = req_data.get("prompt", "코드 정밀 분석 요청")

        target_model = req_data.get("model", self.llm.model_chat)
        if "reasoner" in str(target_model).lower() or "r1" in str(target_model).lower():
            target_model = self.llm.model_reasoner
        else:
            target_model = self.llm.model_chat

        response = web.StreamResponse(
            status=200,
            headers={
                "Content-Type": "text/event-stream",
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "Access-Control-Allow-Origin": "*"
            }
        )
        await response.prepare(request)

        messages = [
            {"role": "system", "content": "당신은 Antigravity 5대 협업팀의 수석 엔지니어 DeepSeek입니다. 한국어로 정확하게 답변하십시오."},
            {"role": "user", "content": user_prompt.strip()}
        ]

        try:
            async for chunk in self.llm.stream_llm(messages, model=target_model):
                gemini_chunk = {
                    "response": {
                        "candidates": [
                            {
                                "content": {
                                    "parts": [{"text": chunk}],
                                    "role": "model"
                                }
                            }
                        ]
                    },
                    "candidates": [
                        {
                            "content": {
                                "parts": [{"text": chunk}],
                                "role": "model"
                            }
                        }
                    ]
                }
                payload = f"data: {json.dumps(gemini_chunk, ensure_ascii=False)}\n\n"
                await response.write(payload.encode("utf-8"))
        except (ClientConnectionResetError, ConnectionResetError):
            return response
        except Exception as e:
            try:
                err_chunk = {
                    "candidates": [
                        {
                            "content": {
                                "parts": [{"text": f"\n[DeepSeek 오류]: {str(e)}"}],
                                "role": "model"
                            }
                        }
                    ]
                }
                await response.write(f"data: {json.dumps(err_chunk, ensure_ascii=False)}\n\n".encode("utf-8"))
            except Exception:
                pass

        try:
            await response.write(b"data: [DONE]\n\n")
        except Exception:
            pass
        return response

    async def fallback_handler(self, request):
        """Universal fallback for any other internal endpoint."""
        return self.json_response({"status": "ok"})
