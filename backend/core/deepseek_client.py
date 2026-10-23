# -*- coding: utf-8 -*-
import json
import asyncio
import aiohttp
from typing import AsyncGenerator, Dict, Any, List, Optional

class DeepSeekClient:
    def __init__(self, api_key: str = "", model_reasoner: str = "deepseek-reasoner", model_chat: str = "deepseek-chat"):
        self.api_key = api_key.strip()
        self.model_reasoner = model_reasoner
        self.model_chat = model_chat
        self.api_url = "https://api.deepseek.com/chat/completions"

    def update_key(self, api_key: str):
        self.api_key = api_key.strip()

    def has_key(self) -> bool:
        return bool(self.api_key and len(self.api_key) > 5)

    async def call_llm(self, messages: List[Dict[str, Any]], model: str = "deepseek-chat", temperature: float = 0.2) -> str:
        if not self.has_key():
            return await self._simulate_response(messages, model)

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json"
        }
        payload = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "stream": False,
            "max_tokens": 8192
        }

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(self.api_url, headers=headers, json=payload, timeout=aiohttp.ClientTimeout(total=60)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        msg = data["choices"][0]["message"]
                        content = msg.get("content", "")
                        reasoning = msg.get("reasoning_content", "")
                        if reasoning and not content:
                            return reasoning
                        elif reasoning and content:
                            return f"[추론 분석]\n{reasoning}\n\n[설계 브리핑]\n{content}"
                        return content
                    else:
                        err_text = await resp.text()
                        return f"[DeepSeek API 에러 {resp.status}]: {err_text}"
        except Exception as e:
            return f"[DeepSeek 연결 오류]: {str(e)}"

    async def call_llm_with_tools(self, messages: List[Dict[str, Any]], tools: List[Dict[str, Any]], model: str = "deepseek-chat", temperature: float = 0.2) -> Dict[str, Any]:
        """
        DeepSeek 공식 네이티브 Function Calling API 호출
        """
        if not self.has_key():
            return {"content": "DeepSeek API 키가 등록되지 않았습니다.", "tool_calls": []}

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json"
        }
        payload = {
            "model": model,
            "messages": messages,
            "tools": tools,
            "tool_choice": "auto",
            "temperature": temperature,
            "stream": False,
            "max_tokens": 8192
        }

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(self.api_url, headers=headers, json=payload, timeout=aiohttp.ClientTimeout(total=90)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        choice = data["choices"][0]["message"]
                        return {
                            "content": choice.get("content") or "",
                            "tool_calls": choice.get("tool_calls") or []
                        }
                    else:
                        err_text = await resp.text()
                        return {"content": f"[DeepSeek API 오류 {resp.status}]: {err_text}", "tool_calls": []}
        except Exception as e:
            return {"content": f"[DeepSeek 통신 장애]: {str(e)}", "tool_calls": []}

    async def stream_llm(self, messages: List[Dict[str, Any]], model: str = "deepseek-chat", temperature: float = 0.3) -> AsyncGenerator[str, None]:
        if not self.has_key():
            async for chunk in self._simulate_stream(messages, model):
                yield chunk
            return

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json"
        }
        payload = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "stream": True,
            "max_tokens": 8192
        }

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(self.api_url, headers=headers, json=payload, timeout=aiohttp.ClientTimeout(total=120)) as resp:
                    if resp.status == 200:
                        async for line in resp.content:
                            line_str = line.decode("utf-8").strip()
                            if not line_str or line_str == "data: [DONE]":
                                continue
                            if line_str.startswith("data: "):
                                raw_json = line_str[6:]
                                try:
                                    chunk = json.loads(raw_json)
                                    delta = chunk["choices"][0].get("delta", {})
                                    reasoning = delta.get("reasoning_content", "")
                                    content = delta.get("content", "")
                                    if reasoning:
                                        yield reasoning
                                    elif content:
                                        yield content
                                except Exception:
                                    continue
                    else:
                        err_text = await resp.text()
                        yield f"\n[DeepSeek API 에러 {resp.status}]: {err_text}"
        except Exception as e:
            yield f"\n[DeepSeek 통신 오류]: {str(e)}"

    async def _simulate_response(self, messages: List[Dict[str, Any]], model: str) -> str:
        return "[가상 모드] DeepSeek API 키가 필요합니다."

    async def _simulate_stream(self, messages: List[Dict[str, Any]], model: str) -> AsyncGenerator[str, None]:
        yield "[가상 모드 작동 중]"
