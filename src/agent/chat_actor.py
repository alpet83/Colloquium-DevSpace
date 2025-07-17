# /agent/chat_actor.py, created 2025-07-17 10:43 EEST
import logging
from llm_api import LLMConnection, XAIConnection, OpenAIConnection

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
                logging.debug(f"Initialized ChatActor for user_id={user_id}, user_name={user_name} with XAIConnection")
            elif model == 'chatgpt':
                self.llm_connection = OpenAIConnection(config)
                logging.debug(f"Initialized ChatActor for user_id={user_id}, user_name={user_name} with OpenAIConnection")
            else:
                self.llm_connection = LLMConnection(config)
                logging.debug(f"Initialized ChatActor for user_id={user_id}, user_name={user_name} with LLMConnection")