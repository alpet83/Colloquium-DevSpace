# /app/agent/managers/llm_interactor.py, updated 2025-07-28 09:41 EEST
import datetime
import json
from pathlib import Path
from lib.sandwich_pack import SandwichPack, estimate_tokens
from context_assembler import ContextAssembler
from managers.db import DataTable
import globals
import os

log = globals.get_logger("interactor")

class LLMInteractor(ContextAssembler):
    """Базовый класс для взаимодействия с LLM, кэширования индекса и управления контекстом."""
    def __init__(self):
        """Инициализирует LLMInteractor с загрузкой пре-промпта и настройкой таблицы llm_usage."""
        super().__init__()
        self.pre_prompt = self._load_pre_prompt()
        self.last_sandwich_idx = None
        self.llm_usage_table = DataTable(
            table_name="llm_usage",
            template=[
                "ts INTEGER",
                "model TEXT",
                "sent_tokens INTEGER",
                "used_tokens INTEGER",
                "output_tokens INTEGER",
                "sources_used INTEGER",
                "token_limit INTEGER DEFAULT 131072",
                "token_cost FLOAT",
                "chat_id INTEGER"
            ]
        )

    def _load_pre_prompt(self) -> str:
        """Загружает пре-промпт из файла.

        Returns:
            str: Содержимое пре-промпта.

        Raises:
            FileNotFoundError: Если файл пре-промпта не найден.
        """
        try:
            with open(globals.PRE_PROMPT_PATH, 'r', encoding='utf-8-sig') as f:
                pre_prompt = f.read()
            log.debug("Загружен пре-промпт из %s", globals.PRE_PROMPT_PATH)
            return pre_prompt
        except FileNotFoundError as e:
            globals.handle_exception("Файл пре-промпта %s не найден" % globals.PRE_PROMPT_PATH, e=e)
            raise

    def _write_context_stats(self, content_blocks: list, llm_name: str, chat_id: int, index_json: str):
        """Записывает статистику контекста в файл логов.

        Args:
            content_blocks (list): Список блоков контента.
            llm_name (str): Имя LLM-актёра.
            chat_id (int): ID чата.
            index_json (str): JSON-строка индекса.
        """
        stats_file = Path(f"/app/logs/{llm_name}_context.stats")
        stats = []
        pre_prompt_tokens = estimate_tokens(self.pre_prompt)
        stats.append({
            "block_type": ":pre_prompt",
            "block_id": "N/A",
            "file_name": globals.PRE_PROMPT_PATH,
            "tokens": pre_prompt_tokens
        })
        index_tokens = estimate_tokens(index_json)
        stats.append({
            "block_type": ":index",
            "block_id": "N/A",
            "file_name": "JSON index",
            "tokens": index_tokens
        })
        unique_file_names = set()
        for block in content_blocks:
            block_text = block.to_sandwich_block()
            token_count = estimate_tokens(block_text)
            block_id = block.post_id or block.file_id or getattr(block, 'quote_id', None) or "N/A"
            file_name = block.file_name or "N/A"
            if file_name != "N/A" and file_name in unique_file_names:
                log.debug("Пропущен дубликат файла в статистике: file_name=%s, block_id=%s", file_name, block_id)
                continue
            unique_file_names.add(file_name)
            stats.append({
                "block_type": block.content_type,
                "block_id": block_id,
                "file_name": file_name,
                "tokens": token_count
            })
        stats.sort(key=lambda x: x["tokens"], reverse=True)
        accumulated_tokens = 0
        for stat in stats:
            accumulated_tokens += stat["tokens"]
            stat["accumulated"] = accumulated_tokens
        header = f"{'Block Type':<15} {'Block ID':<10} {'File Name':<50} {'Tokens':<10} {'Accumulated':<10}\n"
        separator = "-" * 95 + "\n"
        rows = [
            f"{s['block_type']:<15} {s['block_id']:<10} {s['file_name']:<50} {s['tokens']:<10} {s['accumulated']:<10}\n"
            for s in stats]
        try:
            with open(stats_file, "w", encoding="utf-8") as f:
                f.write(header)
                f.write(separator)
                f.writelines(rows)
            log.info("Статистика контекста записана в %s для chat_id=%d, блоков=%d",
                     str(stats_file), chat_id, len(stats))
        except Exception as e:
            globals.handle_exception(f"Не удалось записать статистику контекста в {stats_file}", e)
            globals.post_manager.add_message(chat_id, 2, f"Не удалось записать статистику контекста для {llm_name}: {str(e)}")

    def _write_chat_index(self, chat_id: int, index_content: str):
        """Сохраняет индекс чата в /app/projects/.chat-meta/{chat_id}-index.json и регистрирует в БД.

        Args:
            chat_id (int): ID чата.
            index_content (str): JSON-строка индекса.
        """
        file_name = f".chat-meta/{chat_id}-index.json"
        try:
            fm = globals.file_manager
            file_id = fm.add_file(file_name, content=index_content, project_id=0)
            if file_id > 0:
                log.debug("Дамп индекса '%s' зарегистрирован как %d", file_name, file_id)
                fm.update_file(file_id, index_content)
            else:
                log.error("Не удалось зарегистрировать %s", file_name)
        except Exception as e:
            log.excpt("Не удалось сохранить индекс для chat_id=%d", chat_id, e=e)
            globals.post_manager.add_message(
                chat_id, 2, f"Не удалось сохранить индекс для {file_name}: {str(e)}"
            )

    async def interact(self, content_blocks: list, users: list, chat_id: int, actor, debug_mode: bool = False, rql: int = 1) -> str:
        """Взаимодействует с LLM, формируя контекст и сохраняя статистику.

        Args:
            content_blocks (list): Список блоков контента.
            users (list): Список пользователей.
            chat_id (int): ID чата.
            actor: Объект ChatActor.
            debug_mode (bool, optional): Режим отладки. Defaults to False.
            rql (int, optional): Уровень рекурсии. Defaults to 1.

        Returns:
            str: Ответ LLM или сообщение об ошибке.
        """
        tokens_limit, tokens_cost = globals.user_manager.get_user_token_limits(actor.user_id)
        content_blocks.sort(key=lambda x: x.relevance if x.relevance else 0, reverse=True)
        total_tokens = estimate_tokens(self.pre_prompt)
        filtered_blocks = []
        for block in content_blocks:
            block_text = block.to_sandwich_block()
            block_tokens = estimate_tokens(block_text)
            if total_tokens + block_tokens <= tokens_limit:
                filtered_blocks.append(block)
                total_tokens += block_tokens
            else:
                log.debug("Пропуск блока post_id=%s, file_id=%s из-за лимита токенов %d",
                          str(block.post_id or 'N/A'), str(block.file_id or 'N/A'), tokens_limit)
        content_blocks = filtered_blocks
        context = ''
        log.debug("Упаковка %d блоков контента для rql=%d", len(content_blocks), rql)
        try:
            proj_man = globals.project_manager
            project_name = proj_man.project_name if proj_man else 'not specified'
            packer = SandwichPack(project_name, max_size=1_000_000, compression=True)
            result = packer.pack(content_blocks, users=users)
            context = f"RQL: {rql}\n{result['index']}\n{''.join(result['sandwiches'])}"
            self.last_sandwich_idx = result['index']  # Кэшируем индекс
            log.debug("Контекст сгенерирован, длина %d символов, индекс кэширован", len(context))
            self._write_context_stats(content_blocks, actor.user_name, chat_id, json.dumps(result['index']))
            self._write_chat_index(chat_id, self.last_sandwich_idx)
        except Exception as e:
            globals.handle_exception("Не удалось упаковать блоки контента для chat_id=%d" % chat_id, e)
        context_file = Path(f"/app/logs/context-{actor.user_name}-{chat_id}.llm")
        try:
            with open(context_file, "w", encoding="utf-8") as f:
                f.write(context)
            log.info("Контекст сохранён в %s для user_id=%d, размер=%d символов",
                     str(context_file), actor.user_id, len(context))
        except Exception as e:
            err = f"Не удалось сохранить контекст для {actor.user_name}: {str(e)}"
            globals.post_manager.add_message(chat_id, 2, err, rql=rql)
            globals.handle_exception(err, e)
        sent_tokens = estimate_tokens(context)
        if sent_tokens > tokens_limit:
            log.error("Контекст превышает лимит токенов для user_id=%d: %d > %d", actor.user_id, sent_tokens, tokens_limit)
            raise ValueError(f"Контекст превышает лимит токенов: {sent_tokens} > {tokens_limit}")
        if debug_mode:
            debug_file = Path(f"/app/logs/debug_{actor.user_name}_"
                              f"{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.txt")
            with open(debug_file, "w", encoding="utf-8") as f:
                f.write(context)
            log.info("Сохранён отладочный контекст в %s для user_id=%d", str(debug_file), actor.user_id)
            return "OK"
        else:
            conn = actor.llm_connection
            conn.pre_prompt = self.pre_prompt
            log.debug("Отправка в LLM для user_id=%d: %d символов, rql %d", actor.user_id, len(context) + len(self.pre_prompt), rql)
            search_params = conn.get_search_params(actor.user_id)
            try:
                conn.make_payload(context)
                if search_params.get("mode", 'off') == "off":
                    log.debug("Поиск отключён для user_id=%d", actor.user_id)
                    response = await conn.call()
                else:
                    conn.add_search_tool(search_params)
                    response = await conn.call()
                if response:
                    usage = response.get('usage', {})
                    used_tokens = usage.get('prompt_tokens', 0)
                    output_tokens = usage.get('completion_tokens', 0)
                    sources_used = usage.get('num_sources_used', 0)
                    total_cost = (used_tokens + output_tokens) * tokens_cost / 1_000_000
                    self.llm_usage_table.insert_into({
                        "ts": int(datetime.datetime.now().timestamp()),
                        "model": conn.model,
                        "sent_tokens": sent_tokens,
                        "used_tokens": used_tokens,
                        "output_tokens": output_tokens,
                        "sources_used": sources_used,
                        "token_limit": tokens_limit,
                        "token_cost": total_cost,
                        "chat_id": chat_id
                    })
                    log.debug("Сохранена статистика LLM для chat_id=%d, user_id=%d: model=%s, sent_tokens=%d, used_tokens=%d, output_tokens=%d, sources_used=%d, token_cost=%f",
                              chat_id, actor.user_id, conn.model, sent_tokens, used_tokens, output_tokens, sources_used, total_cost)
                    text = response.get('text', 'void-response')
                    response_file = Path(f"/app/logs/response-{actor.user_name}-{chat_id}.llm")
                    with open(response_file, "w", encoding="utf-8") as f:
                        f.write(text)
                    return text
                else:
                    log.error("llm_connection вернул %s", str(response))
                    return f"invalid_response: {response}"
            except Exception as e:
                error_msg = f"Ошибка LLM для {actor.user_name}: {str(e)}"
                globals.handle_exception(error_msg, e)
                globals.post_manager.add_message(
                    chat_id, 2, error_msg, rql=rql if rql >= 2 else None
                )
                return f"LLMException {e}"
