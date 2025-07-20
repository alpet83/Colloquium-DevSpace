# /app/agent/managers/llm_interactor.py, updated 2025-07-20 23:59 EEST
import datetime
from pathlib import Path
from lib.sandwich_pack import SandwichPack
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

    async def interact(self, content_blocks: list, users: list, chat_id: int, actor, debug_mode: bool = False, rql: int = 1) -> dict:
        max_tokens = 131072
        content_blocks.sort(key=lambda x: x.relevance if x.relevance else 0, reverse=True)
        total_tokens = len(self.pre_prompt) // 4
        filtered_blocks = []
        for block in content_blocks:
            block_text = block.to_sandwich_block()
            block_tokens = len(block_text) // 4 if block_text else 0
            if total_tokens + block_tokens <= max_tokens:
                filtered_blocks.append(block)
                total_tokens += block_tokens
            else:
                log.debug("Пропуск блока post_id=%s, file_id=%s из-за лимита токенов",
                          str(block.post_id or 'N/A'), str(block.file_id or 'N/A'))
        content_blocks = filtered_blocks

        log.debug("Упаковка %d блоков контента для rql=%d", len(content_blocks), rql)
        try:
            packer = SandwichPack(max_size=1_000_000, system_prompt=self.pre_prompt)
            result = packer.pack(content_blocks, users=users)
            context = f"{self.pre_prompt}\nRecursion Level: {rql}\n{result['index']}\n{''.join(result['sandwiches'])}"
            sent_tokens = len(context) // 4
            log.debug("Контекст сгенерирован, длина=%d символов, оценено токенов=%d", len(context), sent_tokens)
            if sent_tokens > max_tokens:
                raise ValueError(f"Контекст превышает лимит токенов: {sent_tokens} > {max_tokens}")
        except Exception as e:
            globals.handle_exception("Не удалось упаковать блоки контента для chat_id=%d", chat_id, e)
            raise

        context_file = Path(f"/app/logs/context-{actor.user_name}.log")
        try:
            with open(context_file, "w", encoding="utf-8") as f:
                f.write(context)
            log.info("Контекст сохранён в %s для user_id=%d, размер=%d символов",
                     str(context_file), actor.user_id, len(context))
        except Exception as e:
            globals.handle_exception("Не удалось сохранить контекст в %s", str(context_file), e)
            globals.post_manager.add_message(chat_id, 2, f"Не удалось сохранить контекст для {actor.user_name}: {str(e)}",
                                            rql=rql if rql >= 2 else None)

        if debug_mode:
            debug_file = Path(f"/app/logs/debug_{actor.user_name}_"
                              f"{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.txt")
            with open(debug_file, "w", encoding="utf-8") as f:
                f.write(context)
            log.info("Сохранён отладочный контекст в %s для user_id=%d", str(debug_file), actor.user_id)
            return {"text": "", "usage": {}}
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
                    return response
                else:
                    log.error("llm_connection вернул %s", str(response))
                    return {}
            except Exception as e:
                error_msg = f"Ошибка LLM для {actor.user_name}: {str(e)}"
                globals.handle_exception(error_msg, e)
                globals.post_manager.add_message(
                    chat_id, 2, error_msg, rql=rql if rql >= 2 else None
                )
                return {}