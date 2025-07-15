import asyncio
import logging
from llm_api import LLMConnection, XAIConnection, OpenAIConnection

class ReplicationManager:
    def __init__(self, user_manager, chat_manager, post_manager):
        self.user_manager = user_manager
        self.chat_manager = chat_manager
        self.post_manager = post_manager
        self.actors = self._load_actors()

    def _load_actors(self):
        actors = []
        rows = self.user_manager.db.fetch_all('SELECT user_id, user_name, llm_class, llm_token FROM users')
        for row in rows:
            actor = ChatActor(row[0], row[1], row[2], row[3])
            actors.append(actor)
        return actors

    def replicate_to_llm(self, chat_id, exclude_source_id=None):
        hierarchy = self.chat_manager.get_chat_hierarchy(chat_id)
        context = ''
        for cid in hierarchy:
            parent_msg_id = self.chat_manager.db.fetch_one(
                'SELECT parent_msg_id FROM chats WHERE chat_id = :chat_id',
                {'chat_id': cid}
            )[0]
            if parent_msg_id:
                parent_msg = self.post_manager.db.fetch_one(
                    'SELECT chat_id, timestamp, user_id, message FROM posts WHERE id = :parent_msg_id',
                    {'parent_msg_id': parent_msg_id}
                )
                if parent_msg:
                    context += f"#post_{parent_msg[1]} от user_id {parent_msg[2]}: {parent_msg[3]}\n"
            history = self.post_manager.get_history(cid)
            context += '\n'.join([f"#post_{row['timestamp']} от user_id {row['user_id']}: {row['message']}" for row in history]) + '\n'
        llm_actors = [actor for actor in self.actors if actor.llm_connection]
        for actor in llm_actors:
            if exclude_source_id and actor.user_id == exclude_source_id:
                continue
            asyncio.create_task(actor.llm_connection.call(context))

class ChatActor:
    def __init__(self, user_id, user_name, llm_class=None, llm_token=None):
        self.user_id = user_id
        self.user_name = user_name
        self.llm_connection = None
        if llm_class and llm_token:
            config = {"api_key": llm_token, "model": llm_class}
            if llm_class.lower() == 'super_grok':
                self.llm_connection = XAIConnection(config)
            elif llm_class.lower() == 'chatgpt':
                self.llm_connection = OpenAIConnection(config)
            else:
                self.llm_connection = LLMConnection(config)
