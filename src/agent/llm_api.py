# /agent/llm_api.py, updated 2025-07-18 14:50 EEST
import aiohttp
import json
from typing import Dict, Optional
from lib.basic_logger import BasicLogger
import globals

log = globals.get_logger("llm_api")

class LLMConnection:
    def __init__(self, config: Dict):
        self.api_key = config.get("api_key")
        self.model = config.get("model", "default")

    async def call(self, prompt: str, search_parameters: Optional[Dict] = None) -> Dict:
        log.warn("Базовый LLMConnection вызван для model=%s, не реализован", self.model)
        return {}

class XAIConnection(LLMConnection):
    def __init__(self, config: Dict):
        super().__init__(config)
        self.base_url = "https://api.x.ai/v1"

    async def call(self, prompt: str, search_parameters: Optional[Dict] = None) -> Dict:
        log.debug("XAIConnection вызов: model=%s, prompt_length=%d, search_parameters=~C95%s~C00",
                  self.model, len(prompt), str(search_parameters))
        try:
            async with aiohttp.ClientSession() as session:
                payload = {
                    "model": self.model,
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": 4096
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
                    log.debug("Ответ XAI API: ~C95%s~C00", json.dumps(result, indent=2))
                    return {
                        "text": result.get("choices", [{}])[0].get("message", {}).get("content", ""),
                        "usage": result.get("usage", {})
                    }
        except Exception as e:
            log.excpt("Ошибка XAIConnection: %s", str(e), exc_info=(type(e), e, e.__traceback__))
            return {}

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
                        log.error("Ошибка OpenAI API: status=%d, response=%s", response.status, await response.text())
                        return {}
                    result = await response.json()
                    log.debug("Ответ OpenAI API: ~C95%s~C00", json.dumps(result, indent=2))
                    return {
                        "text": result.get("choices", [{}])[0].get("message", {}).get("content", ""),
                        "usage": result.get("usage", {})
                    }
        except Exception as e:
            log.excpt("Ошибка OpenAIConnection: %s", str(e), exc_info=(type(e), e, e.__traceback__))
            return {}