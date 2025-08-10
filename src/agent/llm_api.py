# /app/agent/lib/llm_api.py, updated 2025-07-26 21:15 EEST
import aiohttp
import json
import re
from typing import Dict, Optional
from managers.db import Database
import globals
import datetime

log = globals.get_logger("llm_api")

class LLMConnection:
    def __init__(self, config: Dict):
        self.api_key = config.get("api_key")
        self.model = config.get("model", "default")

    async def call(self, prompt: str, search_parameters: Optional[Dict] = None) -> Dict:
        log.warn("Базовый LLMConnection вызван для model=%s, не реализован", self.model)
        return {}

    def set_search_params(self, user_id: int) -> Dict:
        """Заглушка для настройки параметров поиска."""
        log.debug("Заглушка set_search_params вызвана для user_id=%d", user_id)
        return {}

class XAIConnection(LLMConnection):
    def __init__(self, config: Dict):
        super().__init__(config)
        self.base_url = "https://api.x.ai/v1"
        self.db = Database.get_database()

    async def call(self, prompt: str, search_parameters: Optional[Dict] = None) -> Dict:
        log.debug("XAIConnection вызов: model=%s, prompt_length=%d, search_parameters=~C95%s~C00",
                  self.model, len(prompt), str(search_parameters))
        try:
            async with aiohttp.ClientSession() as session:
                payload = {
                    "model": self.model,
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": 4096,
                    "temperature": 0.7  # Для большей предсказуемости
                }
                if search_parameters:
                    payload["search_parameters"] = search_parameters
                async with session.post(
                    f"{self.base_url}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json"
                    },
                    json=payload
                ) as response:
                    if response.status != 200:
                        log.error("Ошибка XAI API: status=%d, response=%s", response.status, await response.text())
                        return {}
                    result = await response.json()
                    log.debug("Ответ XAI API: ~C95%s~C00", json.dumps(result, indent=2, ensure_ascii=False))
                    text = result.get("choices", [{}])[0].get("message", {}).get("content", "")
                    # Постобработка: удаление нежелательного '?' в конце или после '. ?'
                    last_15 = text[-15:] if len(text) >= 15 else text
                    if '?' in last_15:
                        original_text = text
                        # Удаляем ' ?' или '?' перед тегами или в конце
                        text = re.sub(r'\s*\?\s*(?=(@post#\d+)?$)', '', text)
                        if text != original_text:
                            log.debug("Удалён нежелательный '?' из ответа Grok3-mini: %s", original_text[-50:])
                    return {
                        "text": text,
                        "usage": result.get("usage", {})
                    }
        except Exception as e:
            log.excpt("Ошибка XAIConnection: ", e=e)
            return {}

    def set_search_params(self, user_id: int) -> Dict:
        """Настраивает параметры поиска для xAI API."""
        log.debug("Настройка search_parameters для user_id=%d", user_id)
        settings = self.db.fetch_one(
            'SELECT search_mode, search_sources, max_search_results, from_date, to_date FROM user_settings '
            'WHERE user_id = :user_id',
            {'user_id': user_id}
        )
        if settings:
            try:
                sources = json.loads(settings[1]) if settings[1] else ['web', 'x', 'news']
                sources = [{"type": src} for src in sources if src in ['web', 'x', 'news']]
            except json.JSONDecodeError:
                sources = [{"type": "web"}, {"type": "x"}, {"type": "news"}]
            return {
                "mode": settings[0] or "off",
                "sources": sources,
                "max_search_results": settings[2] or 20,
                "from_date": settings[3],
                "to_date": settings[4]
            }
        return {
            "mode": "off",
            "sources": [{"type": "web"}, {"type": "x"}, {"type": "news"}],
            "max_search_results": 20,
            "from_date": datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%d"),
            "to_date": datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%d")
        }

class OpenAIConnection(LLMConnection):
    def __init__(self, config: Dict):
        super().__init__(config)
        self.base_url = "https://api.openai.com/v1"

    async def call(self, prompt: str, search_parameters: Optional[Dict] = None) -> Dict:
        log.debug("OpenAIConnection вызов: model=%s, prompt_length=%d, search_parameters=~C95%s~C00",
                  self.model, len(prompt), str(search_parameters))
        try:
            async with aiohttp.ClientSession() as session:
                payload = {
                    "model": self.model,
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": 4096
                }
                if search_parameters:
                    payload["tools"] = [{
                        "type": "web_search",
                        "web_search": {
                            "query": search_parameters.get("query", prompt),
                            "max_results": search_parameters.get("max_results", 5),
                            "search_depth": search_parameters.get("search_depth", "basic")
                        }
                    }]
                    payload["tool_choice"] = "web_search"
                async with session.post(
                    f"{self.base_url}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json"
                    },
                    json=payload
                ) as response:
                    if response.status != 200:
                        log.error("Ошибка OpenAI API: status=%d, response=%s", response.status, await response.text())
                        return {}
                    result = await response.json()
                    log.debug("Ответ OpenAI API: ~C95%s~C00", json.dumps(result, indent=2))
                    text = result.get("choices", [{}])[0].get("message", {}).get("content", "")
                    search_results = []
                    if "tools" in payload:
                        tool_calls = result.get("choices", [{}])[0].get("message", {}).get("tool_calls", [])
                        for call in tool_calls:
                            if call.get("function", {}).get("name") == "web_search":
                                search_results = json.loads(call.get("function", {}).get("arguments", "{}")).get("results", [])
                    return {
                        "text": text,
                        "search_results": search_results,
                        "usage": result.get("usage", {})
                    }
        except Exception as e:
            log.excpt("Ошибка OpenAIConnection: ", e=e)
            return {}