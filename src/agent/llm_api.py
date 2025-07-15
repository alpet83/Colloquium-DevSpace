# llm_api.py
# Предложено: 2025-07-13 18:05 EEST

from openai import AsyncOpenAI
import logging

class LLMConnection:
    def __init__(self, config):
        self.config = config
        self.client = AsyncOpenAI(api_key=config["api_key"], base_url=self.base_url)  # base_url в классе
        self.model = config["model"]
        logging.info(f"#INFO: Инициализация {self.model}")

    async def call(self, prompt):
        try:
            response = await self.client.chat.completions.create(
                model=self.model,
                messages=[{"role": "user", "content": prompt}]
            )
            return response.choices[0].message.content
        except Exception as e:
            logging.error(f"#ERROR: Ошибка вызова {self.model}: {e}")
            return None

class XAIConnection(LLMConnection):
    base_url = "https://api.x.ai/v1"

class OpenAIConnection(LLMConnection):
    base_url = "https://api.openai.com/v1"
