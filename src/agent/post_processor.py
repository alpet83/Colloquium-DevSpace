# /agent/managers/post_processor.py, updated 2025-07-17 11:53 EEST
import logging
import re
import datetime
import globals
from managers.db import DataTable
from managers.posts import PostManager
from llm_hands import process_message


class PostProcessor:
    def __init__(self):
        self.quotes_table = DataTable(
            table_name="quotes",
            template=[
                "quote_id INTEGER PRIMARY KEY AUTOINCREMENT",
                "chat_id INTEGER",
                "user_id INTEGER",
                "content TEXT NOT NULL",
                "timestamp INTEGER",
                "FOREIGN KEY (chat_id) REFERENCES chats(chat_id)",
                "FOREIGN KEY (user_id) REFERENCES users(user_id)"
            ]
        )
        logging.debug("Initialized PostProcessor with quotes DataTable")

    def process_response(self, chat_id: int, user_id: int, response: str) -> str:
        """Обрабатывает ответ LLM, извлекая цитаты, команды редактирования и вызывая llm_hands."""
        logging.debug(f"Processing response for chat_id={chat_id}, user_id={user_id}, text_length={len(response)}")

        # Проверяем команды для llm_hands
        user_name = globals.user_manager.get_user_name(user_id)
        hands_response = process_message(response, int(datetime.datetime.now(datetime.UTC).timestamp()), user_name)
        if hands_response:
            logging.debug(f"llm_hands response for user_id={user_id}: {hands_response[:50]}...")
            return hands_response

        # Извлечение и сохранение цитат
        def save_quote(match):
            quote_content = match.group(1)
            quote_id = self._save_quote(chat_id, user_id, quote_content)
            logging.debug(f"Saved quote_id={quote_id} for chat_id={chat_id}: {quote_content[:50]}...")
            return f"@quote#{quote_id}"

        processed_response = re.sub(r'<quote>(.*?)</quote>', save_quote, response, flags=re.DOTALL)

        # Обработка <edit_post id="X">
        def handle_edit_post(match):
            post_id = int(match.group(1))
            new_content = match.group(2)
            result = globals.post_manager.edit_post(post_id, new_content, user_id)
            if result.get("error"):
                logging.warning(f"Failed to edit post_id={post_id} for user_id={user_id}: {result['error']}")
                return f"Error: {result['error']} ❌"
            logging.debug(f"Edited post_id={post_id} with new_content={new_content[:50]}...")
            return f"Edited post_id={post_id} ✅"

        processed_response = re.sub(r'<edit_post id="(\d+)">([\s\S]*?)</edit_post>', handle_edit_post,
                                    processed_response, flags=re.DOTALL)

        # Замена @quote#id на отображаемый текст
        def replace_quote_ref(match):
            quote_id = int(match.group(1))
            quote = self.quotes_table.select_from(
                conditions={'quote_id': quote_id, 'chat_id': chat_id},
                limit=1
            )
            if quote:
                return f"[@quote#{quote_id}]({quote[0][3][:50]}...)"
            return match.group(0)

        processed_response = re.sub(r'@quote#(\d+)', replace_quote_ref, processed_response)
        logging.debug(f"Processed response for chat_id={chat_id}: {processed_response[:50]}...")
        return processed_response

    def _save_quote(self, chat_id: int, user_id: int, content: str) -> int:
        """Сохраняет цитату в таблицу quotes и возвращает quote_id."""
        try:
            values = {
                'chat_id': chat_id,
                'user_id': user_id,
                'content': content,
                'timestamp': int(datetime.datetime.now(datetime.UTC).timestamp())
            }
            quote_id = self.quotes_table.insert_into(values)
            return quote_id
        except Exception as e:
            logging.error(f"Error storing quote for chat_id={chat_id}, user_id={user_id}: {str(e)}")
            return 0