# /agent/chat_actor.py, created 2025-07-17 10:43 EEST
import logging
from llm_api import *

class ChatActor:
    def __init__(self, user_id, user_name, llm_class=None, llm_token=None, post_manager=None):
        self.user_id = user_id
        self.user_name = user_name
        self.llm_connection = None
        if llm_class and llm_token:
            config = {"api_key": llm_token, "model": llm_class}
            model = llm_class.lower()
            if "grok" in model:
                self.llm_connection = XAIConnection(config)
            elif model == 'chatgpt':
                self.llm_connection = OpenAIConnection(config)
            elif "openrouter:" in model:
                self.llm_connection = OpenRouterConnection(config)
            else:
                self.llm_connection = LLMConnection(config)
