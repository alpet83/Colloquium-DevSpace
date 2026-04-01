# /app/agent/managers/llm_interactor.py, updated 2025-07-28 09:41 EEST
import asyncio
import datetime
import hashlib
import json
from pathlib import Path
from lib.sandwich_pack import SandwichPack, estimate_tokens
from context_assembler import ContextAssembler
from managers.db import DataTable
from chat_actor import ChatActor
from lib.session_context import get_session_id
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


def _normalize_user_name(user_name: str) -> str:
    if not user_name:
        return "user"

    # Handle common visually-confusable characters in nicknames.
    alias = user_name.replace("с", "c").replace("С", "c")
    alias = alias.lower().strip()
    normalized = "".join(ch if (ch.isalnum() or ch in "._-") else "_" for ch in alias)
    normalized = normalized.strip("._-")
    return normalized or "user"


def _resolve_pre_prompt_path(user_name: str = None) -> str:
    if user_name:
        normalized = _normalize_user_name(user_name)
        user_path = f"/app/docs/llm_pre_prompt-{normalized}.md"
        if os.path.exists(user_path):
            return user_path
    return g.PRE_PROMPT_PATH


def _load_pre_prompt(user_name: str = None) -> tuple[str, str]:
    """Загружает пре-промпт из файла.

    Returns:
        str: Содержимое пре-промпта.

    Raises:
        FileNotFoundError: Если файл пре-промпта не найден.
    """
    try:
        pre_prompt_path = _resolve_pre_prompt_path(user_name)
        with open(pre_prompt_path, 'r', encoding='utf-8-sig') as f:
            pre_prompt = f.read()
        log.debug("Загружен пре-промпт из %s", pre_prompt_path)
        return pre_prompt, pre_prompt_path
    except FileNotFoundError as e:
        missing = _resolve_pre_prompt_path(user_name)
        g.handle_exception("Файл пре-промпта %s не найден" % missing, e=e)
        raise


class LLMInteractor(ContextAssembler):
    """Базовый класс для взаимодействия с LLM, кэширования индекса и управления контекстом."""
    def __init__(self, debug_mode: bool = False):
        """Инициализирует LLMInteractor с загрузкой пре-промпта и настройкой таблицы llm_usage."""
        super().__init__()
        self.debug_mode = debug_mode
        self.pre_prompt, self.pre_prompt_path = _load_pre_prompt()
        self.last_sandwich_idx = None
        self.entities_idx = {}   # index per chat
        self.context_fp_cache = {}
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
                "input_token_cost FLOAT",
                "output_token_cost FLOAT",
                "chat_id INTEGER"
            ]
        )

    @staticmethod
    def _hash_text(text: str) -> str:
        payload = (text or "").encode("utf-8", errors="replace")
        return hashlib.sha256(payload).hexdigest()

    @staticmethod
    def _build_blocks_signature(blocks: list) -> str:
        rows = []
        for block in blocks:
            rows.append(
                f"{block.content_type}|{block.post_id or 0}|{block.file_id or 0}|{block.file_name or ''}"
            )
        return LLMInteractor._hash_text("\n".join(rows))

    def _context_cache_key(self, actor_id: int, chat_id: int, session_id: str | None) -> str:
        sid = str(session_id or "")
        return f"{actor_id}:{chat_id}:{sid}"

    def _decide_cache_mode(self, cache_key: str, current_fp: dict) -> tuple[str, str]:
        prev = self.context_fp_cache.get(cache_key)
        if prev is None:
            return "FULL", "no_cache_state"

        if prev.get("pre_prompt_hash") != current_fp.get("pre_prompt_hash"):
            return "FULL", "pre_prompt_changed"
        if prev.get("index_hash") != current_fp.get("index_hash"):
            return "FULL", "index_changed"
        if prev.get("blocks_signature") != current_fp.get("blocks_signature"):
            return "FULL", "block_layout_changed"

        prev_last_post = int(prev.get("last_post_id", 0) or 0)
        cur_last_post = int(current_fp.get("last_post_id", 0) or 0)
        if cur_last_post < prev_last_post:
            return "FULL", "history_rewind_or_edit"
        if cur_last_post == prev_last_post:
            return "FULL", "no_tail_append"

        return "DELTA_SAFE", "tail_append_detected"

    def _log_context_fingerprint(self, ci: ContextInput, full_idx: str, filtered_blocks: list, last_post_id: int, rql: int):
        session_id = get_session_id()
        cache_key = self._context_cache_key(ci.actor.user_id, ci.chat_id, session_id)
        current_fp = {
            "pre_prompt_hash": self._hash_text(self.pre_prompt),
            "index_hash": self._hash_text(full_idx),
            "blocks_signature": self._build_blocks_signature(filtered_blocks),
            "last_post_id": int(last_post_id or 0),
            "blocks_count": len(filtered_blocks),
            "rql": int(rql),
            "session_id": session_id or "",
            "updated_at": int(datetime.datetime.now().timestamp()),
        }
        mode, reason = self._decide_cache_mode(cache_key, current_fp)
        self.context_fp_cache[cache_key] = current_fp
        log.info(
            "ContextCacheDecision mode=%s reason=%s actor_id=%d chat_id=%d rql=%d session=%s last_post_id=%d blocks=%d",
            mode,
            reason,
            ci.actor.user_id,
            ci.chat_id,
            rql,
            (session_id[:8] if session_id else "-"),
            current_fp["last_post_id"],
            current_fp["blocks_count"],
        )

    @staticmethod
    def _atomic_write_text(path: Path, text: str):
        lock = g.get_named_lock(f"path:{str(path)}")
        with lock:
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)

    @staticmethod
    def _stats_lock_key(llm_name: str) -> str:
        return f"llm-stats:{llm_name}"

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
            "file_name": self.pre_prompt_path,
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
            # Stats file is shared by actor name across chats; serialize writes explicitly.
            lock = g.get_named_lock(self._stats_lock_key(llm_name))
            with lock:
                with open(stats_file, "w", encoding="utf-8") as f:
                    f.write(header)
                    f.write(separator)
                    f.writelines(rows)
            log.info("Статистика контекста записана в %s для chat_id=%d, блоков=%d",
                     str(stats_file), chat_id, len(stats))
        except Exception as e:
            g.handle_exception(f"Не удалось записать статистику контекста в {stats_file}", e)
            g.post_manager.add_post(chat_id, g.AGENT_UID, f"Не удалось записать статистику контекста для {llm_name}: {str(e)}")

    def _write_chat_index(self, chat_id: int, index_content: str):
        """Сохраняет индекс чата в /app/projects/.chat-meta/{chat_id}-index.json и регистрирует в БД.

        Args:
            chat_id (int): ID чата.
        """
        file_name = f".chat-meta/{chat_id}-index.json"
        try:
            fm = g.file_manager
            file_id = fm.add_file(file_name, content=index_content, project_id=0)
            if not file_id:
                file_id = fm.find(file_name, project_id=0)
            if file_id and file_id > 0:
                log.debug("Дамп индекса '%s' зарегистрирован как %d", file_name, file_id)
                fm.update_file(file_id, index_content, project_id=0)
            else:
                log.error("Не удалось зарегистрировать %s", file_name)
        except Exception as e:
            log.excpt("Не удалось сохранить индекс для chat_id=%d", chat_id, e=e)
            g.post_manager.add_post(
                chat_id, g.AGENT_UID, f"Не удалось сохранить индекс для {file_name}: {str(e)}"
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
            proj_man = g.current_project_manager.get() or g.project_manager  # TODO(pre-release): remove g.project_manager fallback after ContextVar adoption is verified
            project_name = proj_man.project_name
            packer = SandwichPack(project_name, max_size=1_000_000, compression=True)
            result = packer.pack(ci.blocks, users=ci.users)
            self.entities_idx[ci.chat_id] = list(packer.entities).copy()
            self.last_sandwich_idx = full_idx = result['index']  # Запомнить индекс, это уже JSON в строке
            self._write_chat_index(ci.chat_id, full_idx)  # сохранение отдельного файла с индексом, для перекрестного взаимодействия в разных чатах

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
            self._log_context_fingerprint(ci, full_idx, filtered_blocks, last_post_id, rql)
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
        self.pre_prompt, self.pre_prompt_path = _load_pre_prompt(actor.user_name)
        tokens_limit, input_tokens_cost, output_tokens_cost = g.user_manager.get_user_token_limits(actor.user_id)
        # build_context is CPU/IO heavy and can block event loop; move it to a worker thread.
        context = await asyncio.to_thread(self.build_context, ci, rql)
        if not context:
            return "ERROR: failed build_context"

        context_file = Path(f"/app/logs/context-{actor.user_name}-{ci.chat_id}.llm")
        try:
            await asyncio.to_thread(self._atomic_write_text, context_file, context)
            log.info("Контекст сохранён в %s для user_id=%d, размер=%d символов",
                     str(context_file), actor.user_id, len(context))
        except Exception as e:
            err = f"Не удалось сохранить контекст для {actor.user_name}: {str(e)}"
            g.post_manager.add_post(ci.chat_id, g.AGENT_UID, err, rql=rql)
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
                if not isinstance(usage, dict):
                    usage = {}
                used_tokens = usage.get('prompt_tokens', 0)
                output_tokens = usage.get('completion_tokens', 0)
                sources_used = usage.get('num_sources_used', 0)
                input_cost = used_tokens * input_tokens_cost / 1_000_000
                output_cost = output_tokens * output_tokens_cost / 1_000_000
                total_cost = input_cost + output_cost
                self.llm_usage_table.insert_into({
                    "ts": int(datetime.datetime.now().timestamp()),
                    "model": conn.model,
                    "sent_tokens": sent_tokens,
                    "used_tokens": used_tokens,
                    "output_tokens": output_tokens,
                    "sources_used": sources_used,
                    "token_limit": tokens_limit,
                    "input_token_cost": input_cost,
                    "output_token_cost": output_cost,
                    "chat_id": ci.chat_id
                })
                log.debug("Сохранена статистика LLM для chat_id=%d, user_id=%d: model=%s, sent_tokens=%d, used_tokens=%d, output_tokens=%d, sources_used=%d, input_cost=%f, output_cost=%f, total=%f",
                          ci.chat_id, actor.user_id, conn.model, sent_tokens, used_tokens, output_tokens, sources_used, input_cost, output_cost, total_cost)
                text = response.get('text', 'void-response')
                response_file = Path(f"/app/logs/response-{actor.user_name}-{ci.chat_id}.llm")
                await asyncio.to_thread(self._atomic_write_text, response_file, text)
                return text
            else:
                log.error("llm_connection вернул %s", str(response))
                return f"invalid_response: {response}"
        except Exception as e:
            error_msg = f"Ошибка LLM для {actor.user_name}: {str(e)}"
            g.handle_exception(error_msg, e)
            g.post_manager.add_post(
                ci.chat_id, 2, error_msg, rql=rql if rql >= 2 else None
            )
            return f"LLMException {e}"
