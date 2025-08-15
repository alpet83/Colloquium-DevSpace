# /app/agent/managers/llm_interactor.py, updated 2025-07-28 09:41 EEST
import datetime
import json
from pathlib import Path
from lib.sandwich_pack import SandwichPack, estimate_tokens
from context_assembler import ContextAssembler
from managers.db import DataTable
from chat_actor import ChatActor
import globals as g
import os

log = g.get_logger("interactor")


class ContextInput:
    def __init__(self, content_blocks: list, users: list, chat_id: int, actor: ChatActor, exclude_source_id: int = None):
        self.blocks = content_blocks
        self.users = users
        self.chat_id = chat_id
        self.actor = actor
        self.exclude_id = exclude_source_id
        self.debug_mode = False


def _load_pre_prompt() -> str:
    """Загружает пре-промпт из файла.

    Returns:
        str: Содержимое пре-промпта.

    Raises:
        FileNotFoundError: Если файл пре-промпта не найден.
    """
    try:
        with open(g.PRE_PROMPT_PATH, 'r', encoding='utf-8-sig') as f:
            pre_prompt = f.read()
        log.debug("Загружен пре-промпт из %s", g.PRE_PROMPT_PATH)
        return pre_prompt
    except FileNotFoundError as e:
        g.handle_exception("Файл пре-промпта %s не найден" % g.PRE_PROMPT_PATH, e=e)
        raise


class LLMInteractor(ContextAssembler):
    """Базовый класс для взаимодействия с LLM, кэширования индекса и управления контекстом."""
    def __init__(self, debug_mode: bool = False):
        """Инициализирует LLMInteractor с загрузкой пре-промпта и настройкой таблицы llm_usage."""
        super().__init__()
        self.debug_mode = debug_mode
        self.pre_prompt = _load_pre_prompt()
        self.last_sandwich_idx = None
        self.entities_idx = {}   # index per chat
        self.tokens_limit = 131072
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
            "file_name": g.PRE_PROMPT_PATH,
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
            g.handle_exception(f"Не удалось записать статистику контекста в {stats_file}", e)
            g.post_manager.add_message(chat_id, 2, f"Не удалось записать статистику контекста для {llm_name}: {str(e)}")

    def _write_chat_index(self, chat_id: int):
        """Сохраняет индекс чата в /app/projects/.chat-meta/{chat_id}-index.json и регистрирует в БД.

        Args:
            chat_id (int): ID чата.
        """
        index_content = self.last_sandwich_idx
        file_name = f".chat-meta/{chat_id}-index.json"
        try:
            fm = g.file_manager
            file_id = fm.add_file(file_name, content=index_content, project_id=0)
            if file_id > 0:
                log.debug("Дамп индекса '%s' зарегистрирован как %d", file_name, file_id)
                fm.update_file(file_id, index_content)
            else:
                log.error("Не удалось зарегистрировать %s", file_name)
        except Exception as e:
            log.excpt("Не удалось сохранить индекс для chat_id=%d", chat_id, e=e)
            g.post_manager.add_message(
                chat_id, 2, f"Не удалось сохранить индекс для {file_name}: {str(e)}"
            )

    def entity_index(self, chat_id: int):
        return self.entities_idx.get(chat_id, None)

    def build_context(self, ci: ContextInput, rql: int=1):
        """ TODO: need extract from interact """
        filtered_blocks = []
        actor = ci.actor
        context = ''
        total_tokens = estimate_tokens(self.pre_prompt)
        last_post_id = 0
        log.debug("Упаковка %d блоков контента для rql=%d", len(ci.blocks), rql)
        try:
            proj_man = g.project_manager
            project_name = proj_man.project_name
            packer = SandwichPack(project_name, max_size=1_000_000, compression=True)
            result = packer.pack(ci.blocks, users=ci.users)
            self.entities_idx[ci.chat_id] = list(packer.entities).copy()
            self.last_sandwich_idx = full_idx = result['index']  # Запомнить индекс, это уже JSON в строке
            self._write_chat_index(ci.chat_id)  # сохранение отдельного файла с индексом, для перекрестного взаимодействия в разных чатах

            context = f"RQL: {rql}\n{full_idx}\n"  # От полного списка блоков забираются только индексы, содержащий описание файлов и сущностей. Детальный deep_index сохранять сейчас нельзя, поскольку привязывается к блокам

            files_passed = []
            # пере-сборка блоков, с контентом актуальных файлов, до наступления переполнения контекста
            for block in ci.blocks:
                block_text = block.to_sandwich_block()
                block_tokens = estimate_tokens(block_text)
                if total_tokens + block_tokens >= self.tokens_limit:
                    log.debug("Пропуск блока post_id=%s, file_id=%s из-за лимита токенов %d",
                              str(block.post_id or 'N/A'), str(block.file_id or 'N/A'), self.tokens_limit)
                    break
                if block.content_type == ":post":
                    last_post_id = max(block.post_id, last_post_id)
                elif block.file_id and block.file_id in self.fresh_files:
                    files_passed.append(block.file_id)
                else:
                    continue
                filtered_blocks.append(block)

            log.debug("Отфильтровано %d блоков из %d, добавлено %d файлов из %s ", len(filtered_blocks), len(ci.blocks), len(files_passed), str(self.fresh_files))
            # Второй вызов: компактный сэндвич с ограниченным детализированным индексом
            result = packer.pack(filtered_blocks, users=ci.users)
            context += result['deep_index'] + "\n" + ''.join(result['sandwiches'])
            focus = {"last_post_id": last_post_id, "attached_files": files_passed}
            context += f"\n<focus>\n{focus}\n</focus>"  # подстраховка для моделей, что ценят больше последние символы контекста

            log.debug("Контекст сгенерирован, длина %d символов, индекс кэширован", len(context))
            self._write_context_stats(filtered_blocks, actor.user_name, ci.chat_id, result['index'])
            return context
        except Exception as e:
            g.handle_exception("Не удалось упаковать блоки контента для chat_id=%d" % ci.chat_id, e)
            raise e

    async def interact(self, ci: ContextInput, rql: int = 1) -> str:
        """Взаимодействует с LLM, формируя контекст и сохраняя статистику.

        Args:
            ci (ContextInput): Набор входных данных в объекте
            rql (int, optional): Уровень рекурсии. Defaults to 1.

        Returns:
            str: Ответ LLM или сообщение об ошибке.
        """
        actor = ci.actor
        tokens_limit, tokens_cost = g.user_manager.get_user_token_limits(actor.user_id)
        context = self.build_context(ci, rql)
        if not context:
            return "ERROR: failed build_context"

        context_file = Path(f"/app/logs/context-{actor.user_name}-{ci.chat_id}.llm")
        try:
            with open(context_file, "w", encoding="utf-8") as f:
                f.write(context)
            log.info("Контекст сохранён в %s для user_id=%d, размер=%d символов",
                     str(context_file), actor.user_id, len(context))
        except Exception as e:
            err = f"Не удалось сохранить контекст для {actor.user_name}: {str(e)}"
            g.post_manager.add_message(ci.chat_id, 2, err, rql=rql)
            g.handle_exception(err, e)
        sent_tokens = estimate_tokens(context)
        if sent_tokens > tokens_limit:
            log.error("Контекст превышает лимит токенов для user_id=%d: %d > %d", actor.user_id, sent_tokens, tokens_limit)
            raise ValueError(f"Контекст превышает лимит токенов: {sent_tokens} > {tokens_limit}")

        if ci.debug_mode:
            log.info("Отладочный режим, контекст не отправлен LLM %s", actor.user_name)
            return f"OK: debug_mode, context size = {len(context)}"

        conn = actor.llm_connection
        conn.pre_prompt = f"You are @{actor.user_name}, participant of software developers chat\n" + self.pre_prompt
        log.debug("Отправка в LLM для user_id=%d: %d символов, rql %d", actor.user_id, len(context) + len(self.pre_prompt), rql)
        search_params = conn.get_search_params(actor.user_id)
        try:
            conn.make_payload(prompt=context)
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
                    "chat_id": ci.chat_id
                })
                log.debug("Сохранена статистика LLM для chat_id=%d, user_id=%d: model=%s, sent_tokens=%d, used_tokens=%d, output_tokens=%d, sources_used=%d, token_cost=%f",
                          ci.chat_id, actor.user_id, conn.model, sent_tokens, used_tokens, output_tokens, sources_used, total_cost)
                text = response.get('text', 'void-response')
                response_file = Path(f"/app/logs/response-{actor.user_name}-{ci.chat_id}.llm")
                with open(response_file, "w", encoding="utf-8") as f:
                    f.write(text)
                return text
            else:
                log.error("llm_connection вернул %s", str(response))
                return f"invalid_response: {response}"
        except Exception as e:
            error_msg = f"Ошибка LLM для {actor.user_name}: {str(e)}"
            g.handle_exception(error_msg, e)
            g.post_manager.add_message(
                ci.chat_id, 2, error_msg, rql=rql if rql >= 2 else None
            )
            return f"LLMException {e}"
