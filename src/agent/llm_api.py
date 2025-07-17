# /agent/llm_api.py, updated 2025-07-16 21:42 EEST
import logging
import aiohttp
import json
from typing import Dict, Optional

class LLMConnection:
    def __init__(self, config: Dict):
        self.api_key = config.get("api_key")
        self.model = config.get("model", "default")

    async def call(self, prompt: str, search_parameters: Optional[Dict] = None) -> Dict:
        logging.warning(f"Base LLMConnection called for model={self.model}, not implemented")
        return {}

class XAIConnection(LLMConnection):
    def __init__(self, config: Dict):
        super().__init__(config)
        self.base_url = "https://api.x.ai/v1"

    async def call(self, prompt: str, search_parameters: Optional[Dict] = None) -> Dict:
        logging.debug(f"XAIConnection call: model={self.model}, prompt_length={len(prompt)}, search_parameters={search_parameters}")
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
                        logging.error(f"XAI API error: status={response.status}, response={await response.text()}")
                        return {}
                    result = await response.json()
                    logging.debug(f"XAI API response: {json.dumps(result, indent=2)}")
                    return {
                        "text": result.get("choices", [{}])[0].get("message", {}).get("content", ""),
                        "usage": result.get("usage", {})
                    }
        except Exception as e:
            logging.error(f"XAIConnection error: {str(e)}")
            return {}

class OpenAIConnection(LLMConnection):
    def __init__(self, config: Dict):
        super().__init__(config)
        self.base_url = "https://api.openai.com/v1"

    async def call(self, prompt: str, search_parameters: Optional[Dict] = None) -> Dict:
        logging.debug(f"OpenAIConnection call: model={self.model}, prompt_length={len(prompt)}, search_parameters={search_parameters}")
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
                        logging.error(f"OpenAI API error: status={response.status}, response={await response.text()}")
                        return {}
                    result = await response.json()
                    logging.debug(f"OpenAI API response: {json.dumps(result, indent=2)}")
                    return {
                        "text": result.get("choices", [{}])[0].get("message", {}).get("content", ""),
                        "usage": result.get("usage", {})
                    }
        except Exception as e:
            logging.error(f"OpenAIConnection error: {str(e)}")
            return {}