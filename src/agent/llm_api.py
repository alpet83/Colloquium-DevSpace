# /app/agent/lib/llm_api.py, updated 2025-08-11 12:02 EEST
# Formatted with proper line breaks and indentation for project compliance.

import aiohttp
import json
import re
import codecs
# from typing import dict, Optional  PROHIBITED OBSOLETE CODE, NEVER USE!
from managers.db import Database
import globals
import datetime

Optional = Dict = None
log = globals.get_logger("llm_api")
class LLMConnection:
    def __init__(self, config: dict):
        self.api_key = config.get("api_key")
        self.name = "Local"
        self.model = config.get("model", "default")
        self.flood = [r'\.\s+\?']
        self.last_result = {}
        self.base_url = 'http://localhost'  # dummy local model
        self.search_sources = ["web"]
        self.db = Database.get_database()
        self.pre_prompt = ''
        self.payload = {}
        self.timeout = aiohttp.ClientTimeout(total=600, connect=45)  # default for OpenAI

    async def call(self) -> dict:
        try:
            messages = self.payload.get("messages", [])
            if messages:
                log.debug(f"{self.name} API вызов: model=%s, макро-сообщений %d", self.model, len(messages))
            else:
                log.error(" Нет сообщений для запроса к API")
                return {}
            async with aiohttp.ClientSession() as session:
                async with session.post(
                        f"{self.base_url}/chat/completions",
                        headers={
                            "Authorization": f"Bearer {self.api_key}",
                            "Content-Type": "application/json; charset=utf-8",
                            "Accept": "application/json; charset=UTF-8",
                            "Accept-Charset": "UTF-8"
                        },
                        json=self.payload,
                        timeout=self.timeout
                ) as response:
                    text = await response.text()
                    self.last_result = result = json.loads(text)
                    sr = []
                    if response.status != 200:
                        log.error(f"Ошибка {self.name} API: status=%d, response=%s", response.status, text)
                    else:
                        log.debug(f"Ответ {self.name} API {response.charset} : %s", json.dumps(result, indent=2))
                        sr = self.get_search_results()

                    return {
                        "text": self.get_text(),
                        "search_results": sr,
                        "usage": result.get("usage", {})
                    }
        except Exception as e:
            log.excpt(f"Ошибка {self.name} API: ", e=e)
            return {}

    def make_payload(self, prompt: str, max_tokens=27000, max_completion_tokens=13500, reasoning_effort="low", temp=0.3, extra=None) -> dict:
        # TODO: нужно считывать параметры role, max_tokens, max_completion_tokens, reasoning_effort из БД
        messages = []
        if self.pre_prompt:
            messages.append({"role": "system", "content": self.pre_prompt})
        messages.append({"role": "user", "content": prompt})
        self.payload = {
            "model": self.model,
            "messages": messages,
            "max_tokens": max_tokens,
            "max_completion_tokens": max_completion_tokens,
            "reasoning_effort": reasoning_effort,
            "temperature": temp  # Для большей предсказуемости
        }
        if extra is not None:
            self.payload.update(extra)
        return self.payload

    def clean_response(self, text: str) -> str:
        for token in self.flood:
            text = re.sub(token, '', text)  # response artefact removing
        return text

    def get_text(self) -> str:
        """Extracts text from API response, formatting errors in <llm_error> tags if present.

        Returns:
            str: Extracted text or formatted error message.
        """
        response = self.last_result
        choices = response.get("choices", [{}])
        if not choices:
            log.error("No choices in response: %s", json.dumps(response, indent=2))
            return "<llm_error>No choices in response</llm_error>"

        choice = choices[0]
        if "error" in choice:
            error = choice["error"]
            message = error.get("message", "Unknown error")
            code = error.get("code", "Unknown")
            provider = response.get("provider", "Unknown")
            log.error("API error: %s, code=%s, provider=%s", message, code, provider)
            return f"<llm_error>\n{message}\nError code: {code}\nProvider: {provider}\n</llm_error>"

        msg = choice.get("message", {})
        text = msg.get("content", "") + msg.get("text", "")


        text = text.strip()
        if not text:
            reason = choice.get("native_finish_reason", choice.get("finish_reason", "unknown reason"))
            text = f"<void_response>Due {reason}</void_response>"
        return self.clean_response(text)

    def add_search_tool(self, params: dict) -> dict:
        if 'off' == params.get('mode', 'off'):
            return self.payload
        tools = self.payload.get('tools', [])
        tools.append({
            "type": "web_search",
            "web_search": {
                "query": {"type": "string"},
                "max_results": params.get("max_search_results", 5),
                "search_depth": params.get("search_depth", "basic")
            }
        })
        self.payload['tools'] = tools
        return self.payload

    def get_search_params(self, user_id: int) -> dict:
        """Настраивает параметры поиска web_search."""
        log.debug("Настройка search_params для user_id=%d", user_id)
        settings = self.db.fetch_one(
            'SELECT search_mode, search_sources, max_search_results, from_date, to_date FROM user_settings '
            'WHERE user_id = :user_id',
            {'user_id': user_id}
        )
        today = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%d")
        def_sources = self.search_sources
        if settings:
            try:
                sources = json.loads(settings[1]) if settings[1] else def_sources
                sources = [{"type": src} for src in sources if src in def_sources]
            except json.JSONDecodeError:
                sources = []
            return {
                "mode": settings[0] or "off",
                "sources": sources,
                "max_search_results": settings[2] or 20,
                "from_date": settings[3].replace("today", today),
                "to_date": settings[4].replace("today", today)
            }
        return {
            "mode": "off",
            "sources": [],
            "max_search_results": 20,
            "from_date": today,
            "to_date": today
        }

    def get_search_results(self) -> list:
        tool_calls = self.last_result.get("choices", [{}])[0].get("message", {}).get("tool_calls", [])
        for call in tool_calls:
            if call.get("function", {}).get("name") == "web_search":
                return call.get("function", {}).get("arguments", "{}").get("results", [])
        return []


class XAIConnection(LLMConnection):
    def __init__(self, config: dict):
        super().__init__(config)
        self.base_url = "https://api.x.ai/v1"
        self.search_sources = ["web", "x", "news"]


class OpenAIConnection(LLMConnection):
    def __init__(self, config: dict):
        super().__init__(config)
        self.name = "OpenAI"
        self.base_url = "https://api.openai.com/v1"


class OpenRouterConnection(LLMConnection):
    def __init__(self, config: dict):
        super().__init__(config)
        self.model = self.model.split(':')[-1]  # MATTER: llm_class have prefix 'openrouter:', need ignore it
        self.name = "OpenRouter"
        self.base_url = "https://openrouter.ai/api/v1"

