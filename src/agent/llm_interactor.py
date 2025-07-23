# /app/agent/managers/llm_interactor.py, updated 2025-07-20 23:59 EEST
import datetime
import json
from pathlib import Path
from lib.sandwich_pack import SandwichPack, estimate_tokens
from context_assembler import ContextAssembler
from managers.db import DataTable
import globals

log = globals.get_logger("interactor")

class LLMInteractor(ContextAssembler):
    def __init__(self):
        super().__init__()
        self.pre_prompt = self._load_pre_prompt()
        self.llm_usage_table = DataTable(
            table_name="llm_usage",
            template=[
                "ts INTEGER",
                "model TEXT",
                "sent_tokens INTEGER",
                "used_tokens INTEGER",
                "sources_used INTEGER",
                "chat_id INTEGER"
            ]
        )

    def _load_pre_prompt(self):
        try:
            with open(globals.PRE_PROMPT_PATH, 'r', encoding='utf-8-sig') as f:
                pre_prompt = f.read()
            log.debug("Загружен пре-промпт из %s", globals.PRE_PROMPT_PATH)
            return pre_prompt
        except FileNotFoundError as e:
            globals.handle_exception("Файл пре-промпта %s не найден", globals.PRE_PROMPT_PATH, e)
            raise
    def _write_context_stats(self, content_blocks: list, llm_name: str, chat_id: int, index_json: str):
        """Записывает статистику по блокам сэндвича в файл /app/logs/{$llm_name}_context.stats."""
        stats_file = Path(f"/app/logs/{llm_name}_context.stats")
        stats = []

        pre_prompt_tokens = len(self.pre_prompt) // 4 if self.pre_prompt else 0
        stats.append({
            "block_type": ":pre_prompt",
            "block_id": "N/A",
            "file_name": globals.PRE_PROMPT_PATH,
            "tokens": pre_prompt_tokens
        })

        index_tokens = len(index_json) // 4 if index_json else 0
        stats.append({
            "block_type": ":index",
            "block_id": "N/A",
            "file_name": "JSON index",
            "tokens": index_tokens
        })

        unique_file_names = set()
        for block in content_blocks:
            block_text = block.to_sandwich_block()
            token_count = len(block_text) // 4 if block_text else 0
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


    async def interact(self, content_blocks: list, users: list, chat_id: int, actor, debug_mode: bool = False, rql: int = 1) -> str:
        max_tokens = 131072
        content_blocks.sort(key=lambda x: x.relevance if x.relevance else 0, reverse=True)
        total_tokens = len(self.pre_prompt) // 4
        filtered_blocks = []
        for block in content_blocks:
            block_text = block.to_sandwich_block()
            block_tokens = estimate_tokens(block_text)
            if total_tokens + block_tokens <= max_tokens:
                filtered_blocks.append(block)
                total_tokens += block_tokens
            else:
                log.debug("Пропуск блока post_id=%s, file_id=%s из-за лимита токенов",
                          str(block.post_id or 'N/A'), str(block.file_id or 'N/A'))
        content_blocks = filtered_blocks
        context = ''
        log.debug("Упаковка %d блоков контента для rql=%d", len(content_blocks), rql)
        try:
            proj_man = globals.project_manager
            project_name = proj_man.project_name if proj_man else 'not specified'
            packer = SandwichPack(project_name, max_size=1_000_000, system_prompt=self.pre_prompt)
            result = packer.pack(content_blocks, users=users)
            context = f"{self.pre_prompt}\nRQL: {rql}\n{result['index']}\n{''.join(result['sandwiches'])}"
            log.debug("Контекст сгенерирован, длина %d символов", len(context))
            index = result['index']
            self._write_context_stats(content_blocks, actor.user_name, chat_id, json.dumps(index))

        except Exception as e:
            globals.handle_exception("Не удалось упаковать блоки контента для chat_id=%d" % chat_id, e)

        context_file = Path(f"/app/logs/context-{actor.user_name}-{chat_id}.log")
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
        if sent_tokens > max_tokens:
            raise ValueError(f"Контекст превышает лимит токенов: {sent_tokens} > {max_tokens}")

        if debug_mode:
            debug_file = Path(f"/app/logs/debug_{actor.user_name}_"
                              f"{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.txt")
            with open(debug_file, "w", encoding="utf-8") as f:
                f.write(context)
            log.info("Сохранён отладочный контекст в %s для user_id=%d", str(debug_file), actor.user_id)
            return "OK"
        else:
            conn = actor.llm_connection
            log.debug("Отправка в LLM для user_id=%d: %d символов, rql=%d", actor.user_id, len(context), rql)
            search_parameters = conn.set_search_params(actor.user_id)
            try:
                if search_parameters.get("mode") == "off":
                    log.debug("Поиск отключён для user_id=%d", actor.user_id)
                    response = await conn.call(context)
                else:
                    response = await conn.call(context, search_parameters=search_parameters)
                if response:
                    usage = response.get('usage', {})
                    used_tokens = usage.get('prompt_tokens', 0)
                    sources = usage.get('num_sources_used', 0)
                    self.llm_usage_table.insert_into({
                        "ts": int(datetime.datetime.now(datetime.UTC).timestamp()),
                        "model": conn.model,
                        "sent_tokens": sent_tokens,
                        "used_tokens": used_tokens,
                        "chat_id": chat_id
                    })
                    log.debug("Сохранена статистика LLM для chat_id=%d, user_id=%d: model=%s, sent_tokens=%d, used_tokens=%d, num_sources_used=%d",
                              chat_id, actor.user_id, conn.model, sent_tokens, used_tokens, sources)
                    text = response.get('text', 'void-response')
                    response_file = Path(f"/app/logs/response-{actor.user_name}-{chat_id}.log")
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
