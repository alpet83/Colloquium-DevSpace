# /agent/managers/post_processor.py, updated 2025-07-19 09:42 EEST
import re
import datetime
import globals
from managers.db import DataTable
from managers.posts import PostManager
from llm_hands import process_message
from lib.basic_logger import BasicLogger

log = globals.get_logger("postproc")


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
        log.debug("Инициализирован PostProcessor с таблицей quotes")

    def process_response(self, chat_id: int, user_id: int, response: str, post_id: int = None) -> dict:
        """Обрабатывает ответ LLM, извлекая цитаты, команды редактирования, файлы и патчи, вызывая llm_hands."""
        log.debug("Обработка ответа для chat_id=%d, user_id=%d, post_id=%s, response_type=%s, response=%s",
                  chat_id, user_id, str(post_id) if post_id is not None else "None", type(response), response[:50])

        # Декодируем response, если он байтовый
        if isinstance(response, bytes):
            response = response.decode('utf-8', errors='replace')
            log.warn("Response был байтовым, декодирован в строку: %s", response[:50])
        elif not isinstance(response, str):
            log.error("Неверный тип ответа: %s", type(response))
            return {"handled_cmds": 0, "failed_cmds": 1, "processed_msg": response,
                    "agent_reply": "Error: Invalid response type"}

        # Проверяем команды для llm_hands
        user_name = globals.user_manager.get_user_name(user_id)
        hands_response = process_message(response, int(datetime.datetime.now(datetime.UTC).timestamp()), user_name)
        if hands_response and (hands_response["handled_cmds"] > 0 or hands_response["failed_cmds"] > 0):
            log.debug("llm_hands response: handled_cmds=%d, failed_cmds=%d, processed_msg=%s, agent_reply=%s",
                      hands_response["handled_cmds"], hands_response["failed_cmds"],
                      hands_response["processed_msg"][:50],
                      hands_response["agent_reply"][:50] if hands_response["agent_reply"] else None)
            return hands_response

        # Извлечение и сохранение цитат
        def save_quote(match):
            quote_content = match.group(1)
            quote_id = self._save_quote(chat_id, user_id, quote_content)
            log.debug("Сохранена цитата quote_id=%d для chat_id=%d: %s", quote_id, chat_id, quote_content[:50])
            return "@quote#%d" % quote_id

        processed_response = re.sub(r'<quote>(.*?)</quote>', save_quote, response, flags=re.DOTALL)

        # Обработка <edit_post id="X">
        def handle_edit_post(match):
            post_id = int(match.group(1))
            new_content = match.group(2)
            result = globals.post_manager.edit_post(post_id, new_content, user_id)
            if result.get("error"):
                log.warn("Не удалось отредактировать post_id=%d для user_id=%d: %s", post_id, user_id, result['error'])
                return {"handled_cmds": 0, "failed_cmds": 1, "processed_msg": processed_response,
                        "agent_reply": f"Error: {result['error']} ❌"}
            log.debug("Отредактирован post_id=%d с новым содержимым=%s", post_id, new_content[:50])
            return {"handled_cmds": 1, "failed_cmds": 0,
                    "processed_msg": processed_response.replace(match.group(0), f"Edited post_id={post_id} ✅"),
                    "agent_reply": f"Edited post_id={post_id} ✅"}

        matches = list(re.finditer(r'<edit_post id="(\d+)">([\s\S]*?)</edit_post>', processed_response,
                                   flags=re.DOTALL))
        agent_reply = []
        handled_cmds = 0
        failed_cmds = 0
        for match in matches:
            result = handle_edit_post(match)
            processed_response = result["processed_msg"]
            if result["agent_reply"]:
                agent_reply.append(result["agent_reply"])
            handled_cmds += result["handled_cmds"]
            failed_cmds += result["failed_cmds"]

        # Замена @quote#id
        def replace_quote_ref(match):
            quote_id = int(match.group(1))
            quote = self.quotes_table.select_from(
                conditions={'quote_id': quote_id, 'chat_id': chat_id},
                limit=1
            )
            if quote:
                return "[@quote#%d](%s...)" % (quote_id, quote[0][3][:50])
            return match.group(0)

        processed_response = re.sub(r'@quote#(\d+)', replace_quote_ref, processed_response)
        agent_reply_text = "\n".join(agent_reply) if agent_reply else None
        log.debug(
            "Обработанный ответ для chat_id=%d: handled_cmds=%d, failed_cmds=%d, processed_msg=%s, agent_reply=%s",
            chat_id, handled_cmds, failed_cmds, processed_response[:50],
            agent_reply_text[:50] if agent_reply_text else None)
        return {"handled_cmds": handled_cmds, "failed_cmds": failed_cmds, "processed_msg": processed_response,
                "agent_reply": agent_reply_text}

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
            log.excpt("Ошибка сохранения цитаты для chat_id=%d, user_id=%d: %s",
                      chat_id, user_id, str(e), exc_info=(type(e), e, e.__traceback__))
            return 0