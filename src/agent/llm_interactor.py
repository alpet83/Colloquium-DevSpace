# /app/agent/managers/llm_interactor.py, updated 2025-07-28 09:41 EEST
import asyncio
import datetime
import hashlib
import json
import random
import re
import time
from pathlib import Path
from lib.sandwich_pack import SandwichPack, estimate_tokens
from lib.context_reference_store import (
    ContextReferenceStore,
    append_incremental_patches,
)
from context_assembler import ContextAssembler
from managers.db import DataTable
from chat_actor import ChatActor
from lib.relevance_window_anchor import set_anchor_on_full
from lib.session_context import get_session_id
from lib.cache_rollout import (
    context_cache_metrics_sample_pct,
    context_cache_metrics_write_enabled,
    sent_tokens_warn_threshold,
)
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
        #: Триггерный пост содержит #debug_bypass — не звать провайдера, только build_context + метрики.
        self.debug_bypass = False
        self.context_meta = {}
        #: Только :context_patch (сэндвич-текст) для отладочного дампа .upgrade.llm
        self.context_upgrade_text_for_debug: str = ""


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
    """Базовый interactor: сборка контекста, вызов провайдера и базовая usage-метрика."""
    LLM_USAGE_BASE_TEMPLATE = [
        "ts INTEGER",
        "model TEXT",
        "sent_tokens INTEGER",
        "used_tokens INTEGER",
        "output_tokens INTEGER",
        "sources_used INTEGER",
        "token_limit INTEGER DEFAULT 131072",
        "input_token_cost FLOAT",
        "output_token_cost FLOAT",
        "chat_id INTEGER",
    ]
    LLM_USAGE_EXTRA_TEMPLATE: list[str] = []

    def _llm_usage_template(self) -> list[str]:
        return list(self.LLM_USAGE_BASE_TEMPLATE) + list(self.LLM_USAGE_EXTRA_TEMPLATE)

    def _init_cache_metrics_table(self) -> None:
        """Hook для наследника: создать cache-specific таблицы и runtime state."""
        self.context_cache_metrics_table = None
        self._usage_cycle_state: dict[str, dict] = {}

    @property
    def _llm_usage_has_cache_fields(self) -> bool:
        return bool(self.LLM_USAGE_EXTRA_TEMPLATE)

    def invalidate_context(self, chat_id: int, actor_id: int | None = None, reason: str = "forced_invalidate") -> None:
        """No-op в базовом interactor; кэш-инвалидацию реализует LLMCachedInteractor."""
        _ = (chat_id, actor_id, reason)

    def __init__(
        self,
        debug_mode: bool = False,
        *,
        context_reference_store: ContextReferenceStore | None = None,
    ):
        """Инициализирует LLMInteractor с загрузкой пре-промпта и настройкой таблицы llm_usage.

        Args:
            debug_mode: режим отладки репликации.
            context_reference_store: явный экземпляр хранилища (тесты); иначе создаётся с учётом
                переменной окружения `CQDS_CONTEXT_REFERENCE_STORE`.
        """
        super().__init__()
        self.debug_mode = debug_mode
        self.pre_prompt, self.pre_prompt_path = _load_pre_prompt()
        self.last_sandwich_idx = None
        self.entities_idx = {}   # index per chat
        self._context_ref_store = context_reference_store or ContextReferenceStore()
        self.tokens_limit = 131072
        self.llm_usage_table = DataTable(
            table_name="llm_usage",
            template=self._llm_usage_template()
        )
        self._init_cache_metrics_table()

    @staticmethod
    def _hash_text(text: str) -> str:
        payload = (text or "").encode("utf-8", errors="replace")
        return hashlib.sha256(payload).hexdigest()

    @staticmethod
    def _first_diff_ctx(a: str, b: str, radius: int = 80) -> tuple[int, str, str]:
        """Вернуть позицию первого отличия и короткие фрагменты вокруг неё.

        Returns:
            (idx, a_snippet, b_snippet), где idx=-1 если строки полностью равны.
        """
        sa = str(a or "")
        sb = str(b or "")
        if sa == sb:
            return -1, "", ""
        i = 0
        lim = min(len(sa), len(sb))
        while i < lim and sa[i] == sb[i]:
            i += 1
        start = max(0, i - max(1, int(radius)))
        end_a = min(len(sa), i + max(1, int(radius)))
        end_b = min(len(sb), i + max(1, int(radius)))
        a_sn = sa[start:end_a].replace("\n", "\\n")
        b_sn = sb[start:end_b].replace("\n", "\\n")
        return i, a_sn, b_sn

    @staticmethod
    def _replace_post_blocks(base_text: str, blocks: list, p_prev: int) -> str:
        """Обновить в base_text посты с id<=p_prev на актуальные версии из blocks."""
        text = str(base_text or "")
        if not text:
            return text
        updated = text
        for b in blocks:
            if getattr(b, "content_type", None) != ":post":
                continue
            pid = int(getattr(b, "post_id", 0) or 0)
            if pid <= 0 or pid > int(p_prev or 0):
                continue
            new_block = b.to_sandwich_block()
            pat = rf'<post post_id="{pid}"[^>]*>.*?</post>'
            # Строка замены не должна проходить через синтаксис repl шаблона re
            # (иначе «\x», «\1» из текста поста дают re.error: bad escape).
            updated = re.sub(pat, lambda _m, nb=new_block: nb, updated, count=1, flags=re.DOTALL)
        return updated

    @staticmethod
    def _block_identity_row(block) -> str:
        """Стабильная строка идентичности блока; для :post включает отпечаток текста (редактирование старого поста)."""
        bh = getattr(block, "block_hash", None) or ""
        base = (
            f"{block.content_type}|{int(block.post_id or 0)}|{int(block.file_id or 0)}|"
            f"{block.file_name or ''}|{bh}"
        )
        if getattr(block, "content_type", None) == ":post":
            digest = hashlib.sha256(
                (block.content_text or "").encode("utf-8", errors="replace")
            ).hexdigest()[:24]
            return f"{base}|{digest}"
        return base

    @staticmethod
    def _head_post_identity_row(block) -> str:
        """Идентичность поста для head_posts_sig: только post_id и user_id.

        Текст не входит: иначе каждый цикл «⏳ progress → финальный ответ» под тем же id
        ломает сравнение и DELTA_SAFE никогда не наступает. Редактирование старого поста
        без нового хвоста по-прежнему даёт p_cur == p_prev → FULL (no_tail_append).
        """
        uid = int(getattr(block, "user_id", 0) or 0)
        pid = int(block.post_id or 0)
        return f":post|{pid}|u{uid}"

    @classmethod
    def _head_posts_signature(cls, blocks: list, max_post_id_inclusive: int) -> str:
        """Подпись постов с id <= max_post_id_inclusive (канонический порядок по post_id)."""
        cap = int(max_post_id_inclusive or 0)
        posts = [
            b
            for b in blocks
            if getattr(b, "content_type", None) == ":post" and int(b.post_id or 0) > 0 and int(b.post_id or 0) <= cap
        ]
        posts.sort(key=lambda b: int(b.post_id or 0))
        rows = [cls._head_post_identity_row(b) for b in posts]
        return cls._hash_text("\n".join(rows))

    @classmethod
    def _non_post_signature(cls, blocks: list) -> str:
        """Файлы и spans: стабильный порядок по строке идентичности."""
        np = [
            b
            for b in blocks
            if getattr(b, "content_type", None) not in (":post", ":context_patch")
        ]
        rows = sorted(cls._block_identity_row(b) for b in np)
        return cls._hash_text("\n".join(rows))

    def _context_cache_key(self, actor_id: int, chat_id: int, session_id: str | None) -> str:
        sid = str(session_id or "")
        return f"{actor_id}:{chat_id}:{sid}"

    def _usage_cycle_markers(self, *, ci: ContextInput, context_meta: dict, sent_tokens: int) -> dict:
        """Fallback для базового interactor: без кэш-цикла, каждый вызов как FULL."""
        sid = str((context_meta or {}).get("session_id", "") or "")
        ck = self._context_cache_key(ci.actor.user_id, ci.chat_id, sid)
        now = int(datetime.datetime.now().timestamp())
        return {
            "cache_mode": "FULL",
            "cache_reason": "base_interactor_no_cache",
            "cache_session_id": sid,
            "cache_cycle_id": f"{ck}:{now}",
            "cache_cycle_step": 0,
            "effective_sent_tokens": int(sent_tokens or 0),
        }

    @staticmethod
    def _prefix_reuse_probe_enabled() -> bool:
        return False

    @staticmethod
    def _prefix_reuse_on() -> bool:
        return False

    @staticmethod
    def _provider_prefix_cache_on() -> bool:
        return False

    def _provider_cache_hint(self, *, conn, context: str, context_meta: dict) -> dict:
        return {}

    def _cleanup_mp(self, cache_key: str, prev_fp: dict | None) -> None:
        """Базовый interactor не управляет Layer B."""
        _ = (cache_key, prev_fp)

    def _incremental_patches_enabled(self, ci: ContextInput) -> bool:
        """Инкрементальные :context_patch от предыдущего снимка (Layer A)."""
        _ = ci
        return self._context_ref_store.enabled

    def _handle_context_assembly_cache_error(self, cache_key: str, exc: BaseException) -> None:
        """Сбой склейки с материализованным префиксом (Layer B): сброс B и пересборка FULL."""
        log.warn("Ошибка сборки контекста с кэшем префикса, безопасная пересборка FULL: %s", exc)
        try:
            self._context_ref_store.evict_mp(cache_key)
        except Exception:
            pass

    def _build_pd_body(
        self,
        *,
        prev_prefix: dict | None,
        prev_fp: dict | None,
        filtered_blocks: list,
    ) -> str:
        _ = (prev_prefix, prev_fp, filtered_blocks)
        return ""

    def _build_pd_sand(
        self,
        *,
        prev_prefix: dict | None,
        prev_fp: dict | None,
        filtered_blocks: list,
        packer,
        users: list,
    ) -> str:
        _ = (prev_prefix, prev_fp, filtered_blocks, packer, users)
        return ""

    def _can_reuse_mp(
        self,
        *,
        prev_prefix: dict | None,
        pre_prompt_hash: str,
        index_hash: str,
        head_posts_sig: str,
        p_prev: int,
    ) -> bool:
        _ = (prev_prefix, pre_prompt_hash, index_hash, head_posts_sig, p_prev)
        return False

    def _store_mp_payload(
        self,
        *,
        ci: ContextInput,
        fp: dict,
        context_body: str,
        deep_index: str = "",
        sandwich_body: str = "",
    ) -> None:
        _ = (ci, fp, context_body, deep_index, sandwich_body)

    def _decide_cache_mode(
        self,
        prev: dict | None,
        *,
        pre_prompt_hash: str,
        index_hash: str,
        filtered_blocks: list,
        last_post_id: int,
        post_digest_current: dict[int, str] | None = None,
        reference_digest_enabled: bool = True,
    ) -> tuple[str, str]:
        return "FULL", "base_interactor_no_cache"

    def _log_context_fingerprint(
        self,
        ci: ContextInput,
        full_idx: str,
        index_fp_json: str,
        filtered_blocks: list,
        last_post_id: int,
        rql: int,
    ):
        session_id = get_session_id()
        p_cur = int(last_post_id or 0)
        set_anchor_on_full(ci.chat_id, session_id or "", p_cur)
        fp = {
            "pre_prompt_hash": self._hash_text(self.pre_prompt),
            "index_hash": self._hash_text(index_fp_json),
            "last_post_id": p_cur,
            "last_committed_mode": "FULL",
            "blocks_count": len(filtered_blocks),
            "rql": int(rql),
            "session_id": session_id or "",
            "updated_at": int(datetime.datetime.now().timestamp()),
        }
        return "FULL", "base_interactor_no_cache", fp

    @staticmethod
    def _active_project_id() -> int:
        proj_man = g.current_project_manager.get() or g.project_manager
        return int(getattr(proj_man, "project_id", 0) or 0)

    @staticmethod
    def _safe_int(value, default: int = 0) -> int:
        try:
            return int(value or 0)
        except (TypeError, ValueError):
            return int(default)

    @classmethod
    def _extract_usage_metrics(cls, usage: dict) -> dict:
        """Нормализовать usage разных API в общий набор счётчиков cache-токенов."""
        u = usage if isinstance(usage, dict) else {}
        ptd = u.get("prompt_tokens_details") if isinstance(u.get("prompt_tokens_details"), dict) else {}
        # Anthropic/OpenRouter style.
        cache_read = cls._safe_int(u.get("cache_read_input_tokens"))
        cache_write = cls._safe_int(u.get("cache_creation_input_tokens"))
        # OpenAI style.
        cache_hit = cls._safe_int(ptd.get("cached_tokens"))
        if cache_read <= 0 and cache_hit > 0:
            cache_read = cache_hit
        if cache_write <= 0:
            cache_write = cls._safe_int(ptd.get("cache_write_tokens"))
        # Иногда providers кладут miss как uncached/input_tokens; используем best-effort.
        prompt_tokens = cls._safe_int(u.get("prompt_tokens"))
        cache_miss = cls._safe_int(ptd.get("uncached_tokens"))
        if cache_miss <= 0:
            cache_miss = cls._safe_int(ptd.get("input_tokens"))
        if cache_miss <= 0 and prompt_tokens > 0 and cache_hit >= 0:
            cache_miss = max(prompt_tokens - cache_hit, 0)
        raw_json = ""
        try:
            raw_json = json.dumps(u, ensure_ascii=False, separators=(",", ":"))
        except Exception:
            raw_json = ""
        return {
            "used_tokens": prompt_tokens,
            "output_tokens": cls._safe_int(u.get("completion_tokens")),
            "sources_used": cls._safe_int(u.get("num_sources_used")),
            "usage_prompt_cache_hit_tokens": cache_hit,
            "usage_prompt_cache_miss_tokens": cache_miss,
            "usage_cache_read_input_tokens": cache_read,
            "usage_cache_creation_input_tokens": cache_write,
            "usage_details_json": raw_json,
        }

    def _record_context_cache_metric(
            self,
            ci: ContextInput,
            model: str,
            rql: int,
            context_meta: dict,
            build_context_ms: int,
            sent_tokens: int,
            used_tokens: int = 0,
            output_tokens: int = 0,
            usage_prompt_cache_hit_tokens: int = 0,
            usage_prompt_cache_miss_tokens: int = 0,
            usage_cache_read_input_tokens: int = 0,
            usage_cache_creation_input_tokens: int = 0,
            usage_details_json: str = "",
            provider_error: bool = False,
            error_type: str = "",
    ) -> int | None:
        if self.context_cache_metrics_table is None:
            return None
        if not context_cache_metrics_write_enabled():
            return None
        pct = context_cache_metrics_sample_pct()
        if pct <= 0:
            return None
        if pct < 100 and random.randint(1, 100) > pct:
            return None
        try:
            return self.context_cache_metrics_table.insert_into({
                "ts": int(datetime.datetime.now().timestamp()),
                "schema_ver": 6 if self._context_ref_store.enabled else 5,
                "actor_id": int(ci.actor.user_id),
                "chat_id": int(ci.chat_id),
                "project_id": self._active_project_id(),
                "session_id": str(context_meta.get("session_id", "") or ""),
                "model": str(model or ""),
                "rql": int(rql),
                "mode": str(context_meta.get("cache_mode", "FULL")),
                "reason": str(context_meta.get("cache_reason", "unknown")),
                "blocks_count": int(context_meta.get("blocks_count", 0) or 0),
                "last_post_id": int(context_meta.get("last_post_id", 0) or 0),
                "build_context_ms": int(build_context_ms),
                "sent_tokens": int(sent_tokens),
                "used_tokens": int(used_tokens),
                "output_tokens": int(output_tokens),
                "usage_prompt_cache_hit_tokens": int(usage_prompt_cache_hit_tokens or 0),
                "usage_prompt_cache_miss_tokens": int(usage_prompt_cache_miss_tokens or 0),
                "usage_cache_read_input_tokens": int(usage_cache_read_input_tokens or 0),
                "usage_cache_creation_input_tokens": int(usage_cache_creation_input_tokens or 0),
                "usage_details_json": str(usage_details_json or ""),
                "provider_error": 1 if provider_error else 0,
                "error_type": str(error_type or ""),
                "pre_prompt_hash": str(context_meta.get("pre_prompt_hash", "") or ""),
                "index_hash": str(context_meta.get("index_hash", "") or ""),
                "context_upgrade_tokens": int(context_meta.get("context_upgrade_tokens", 0) or 0),
                "context_full_tokens": int(context_meta.get("context_full_tokens", 0) or 0),
                "context_upgrade_pct": float(context_meta.get("context_upgrade_pct", 0.0) or 0.0),
                "prefix_reuse_on": 1 if bool(context_meta.get("prefix_reuse_on", False)) else 0,
                "prefix_reuse_apply": 1 if bool(context_meta.get("prefix_reuse_apply", False)) else 0,
                "prefix_reuse_probe_enabled": 1 if bool(context_meta.get("prefix_reuse_probe_enabled", False)) else 0,
                "prefix_reuse_probe_candidate": 1 if bool(context_meta.get("prefix_reuse_probe_candidate", False)) else 0,
                "prefix_reuse_probe_match": 1 if bool(context_meta.get("prefix_reuse_probe_match", False)) else 0,
            })
        except Exception as e:
            log.warn("Не удалось записать context_cache_metrics: %s", str(e))
            return None

    def _build_llm_usage_row(
        self,
        *,
        conn,
        ci: ContextInput,
        tokens_limit: int,
        sent_tokens: int,
        used_tokens: int,
        output_tokens: int,
        sources_used: int,
        input_cost: float,
        output_cost: float,
        usage_marks: dict,
        metric_id: int | None,
    ) -> dict:
        row = {
            "ts": int(datetime.datetime.now().timestamp()),
            "model": conn.model,
            "sent_tokens": sent_tokens,
            "used_tokens": used_tokens,
            "output_tokens": output_tokens,
            "sources_used": sources_used,
            "token_limit": tokens_limit,
            "input_token_cost": input_cost,
            "output_token_cost": output_cost,
            "chat_id": ci.chat_id,
        }
        if self._llm_usage_has_cache_fields:
            row.update({
                "effective_sent_tokens": int(usage_marks.get("effective_sent_tokens", 0) or 0),
                "cache_mode": str(usage_marks.get("cache_mode", "")),
                "cache_reason": str(usage_marks.get("cache_reason", "")),
                "cache_session_id": str(usage_marks.get("cache_session_id", "")),
                "cache_cycle_id": str(usage_marks.get("cache_cycle_id", "")),
                "cache_cycle_step": int(usage_marks.get("cache_cycle_step", 0) or 0),
                "context_metric_id": int(metric_id or 0),
            })
        return row

    def _after_context_fingerprint(self, ci: ContextInput, filtered_blocks: list, fp: dict) -> None:
        """Hook для наследника после фиксации fingerprint/режима кэша."""
        _ = (ci, filtered_blocks, fp)

    @staticmethod
    def _atomic_write_text(path: Path, text: str):
        lock = g.get_named_lock(f"path:{str(path)}")
        with lock:
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)

    @staticmethod
    def _stats_lock_key(llm_name: str) -> str:
        return f"llm-stats:{llm_name}"

    def _write_context_stats(
        self,
        content_blocks: list,
        llm_name: str,
        chat_id: int,
        index_json: str,
        *,
        context_upgrade_tokens: int = 0,
        context_full_tokens: int = 0,
        context_upgrade_pct: float = 0.0,
        context_cache_mode: str = "",
        context_cache_reason: str = "",
        debug_context_upgrade: bool = False,
    ):
        """Записывает статистику контекста в файл логов.

        Args:
            content_blocks (list): Список блоков контента.
            llm_name (str): Имя LLM-актёра.
            chat_id (int): ID чата.
            index_json (str): JSON-строка индекса.
            debug_context_upgrade: при True добавить блок про объём context_patch vs полного контекста.
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
                    if debug_context_upgrade:
                        f.write(separator)
                        f.write(
                            "context cache debug (context_patch upgrade vs full body in context *.llm)\n"
                        )
                        f.write(
                            f"chat_id={chat_id} mode={context_cache_mode!s} "
                            f"reason={context_cache_reason!s}\n"
                        )
                        f.write(
                            f"context_full_tokens={context_full_tokens} "
                            f"context_upgrade_tokens={context_upgrade_tokens} "
                            f"upgrade_pct={context_upgrade_pct:.4f}%\n"
                        )
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

            non_post_blocks = [b for b in ci.blocks if getattr(b, "content_type", None) != ":post"]
            if non_post_blocks:
                fp_packer = SandwichPack(project_name, max_size=1_000_000, compression=True)
                index_fp_json = fp_packer.pack(non_post_blocks, users=ci.users)["index"]
            else:
                index_fp_json = "{}"

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
                total_tokens += block_tokens

            _sid = get_session_id()
            _ck = self._context_cache_key(ci.actor.user_id, ci.chat_id, _sid)
            _prev_fp = self._context_ref_store.get(_ck)
            _prev_prefix = self._context_ref_store.get_mp(_ck)
            # Защитная зачистка Layer B при входе: если прошлый commit был FULL,
            # материализованный префикс гарантированно невалиден.
            self._cleanup_mp(_ck, _prev_fp)
            idx_before_patches = len(filtered_blocks)
            total_tokens = append_incremental_patches(
                ci,
                filtered_blocks,
                _prev_fp,
                total_tokens,
                self.tokens_limit,
                reference_enabled=self._incremental_patches_enabled(ci),
            )
            patch_blocks = filtered_blocks[idx_before_patches:]
            upgrade_sandwich_text = "\n".join(
                b.to_sandwich_block() for b in patch_blocks
            )

            log.debug("Отфильтровано %d блоков из %d, добавлено %d файлов из %s ", len(filtered_blocks), len(ci.blocks), len(files_passed), str(self.fresh_files))
            mode, reason, fp = self._log_context_fingerprint(
                ci, full_idx, index_fp_json, filtered_blocks, last_post_id, rql
            )
            self._after_context_fingerprint(ci, filtered_blocks, fp)
            reuse_on = self._prefix_reuse_on()
            reuse_probe_enabled = self._prefix_reuse_probe_enabled()
            reuse_probe_candidate = False
            reuse_probe_match = False
            reuse_apply = False
            context_body = ""
            deep_index_body = ""
            sandwich_body = ""
            stats_index_json = full_idx
            candidate_body = ""
            candidate_sand = ""
            try:
                p_prev = int((_prev_fp or {}).get("last_post_id", 0) or 0)
                head_upto_prev = self._head_posts_signature(filtered_blocks, p_prev)
                mp_ok = self._can_reuse_mp(
                    prev_prefix=_prev_prefix,
                    pre_prompt_hash=str(fp.get("pre_prompt_hash", "") or ""),
                    index_hash=str(fp.get("index_hash", "") or ""),
                    head_posts_sig=head_upto_prev,
                    p_prev=p_prev,
                )
                if mode == "DELTA_SAFE" and (reuse_on or reuse_probe_enabled) and mp_ok:
                    candidate_body = self._build_pd_body(
                        prev_prefix=_prev_prefix,
                        prev_fp=_prev_fp,
                        filtered_blocks=filtered_blocks,
                    )
                    candidate_sand = self._build_pd_sand(
                        prev_prefix=_prev_prefix,
                        prev_fp=_prev_fp,
                        filtered_blocks=filtered_blocks,
                        packer=packer,
                        users=ci.users,
                    )
                    if candidate_body:
                        reuse_probe_candidate = True
                # Быстрый путь экономии CPU: не делаем второй pack, если можно применить candidate.
                # В probe-режиме baseline всё равно строим для сравнения хэшей.
                if mode == "DELTA_SAFE" and reuse_on and candidate_body and not reuse_probe_enabled:
                    context_body = candidate_body
                    deep_index_body = str((_prev_prefix or {}).get("deep_index", "") or "")
                    sandwich_body = candidate_sand
                    reuse_apply = True
                else:
                    # Базовый путь: компактный сэндвич с ограниченным детализированным индексом.
                    result = packer.pack(filtered_blocks, users=ci.users)
                    deep_index_body = result["deep_index"]
                    sandwich_body = "".join(result["sandwiches"])
                    context_body = deep_index_body + "\n" + sandwich_body
                    stats_index_json = result["index"]
                    if mode == "DELTA_SAFE" and candidate_sand:
                        # Сравниваем стабильную sandwich-часть; deep_index может меняться технически.
                        cand_hash = self._hash_text(candidate_sand)
                        base_hash = self._hash_text(sandwich_body)
                        reuse_probe_match = (cand_hash == base_hash)
                        if not reuse_probe_match:
                            diff_at, cand_sn, base_sn = self._first_diff_ctx(candidate_sand, sandwich_body)
                            log.debug(
                                "PREFIX_REUSE_MISMATCH chat_id=%d actor_id=%d cand_len=%d base_len=%d "
                                "cand_hash=%s base_hash=%s diff_at=%d cand_sn=%s base_sn=%s",
                                int(ci.chat_id),
                                int(getattr(ci.actor, "user_id", 0) or 0),
                                len(candidate_sand),
                                len(sandwich_body),
                                cand_hash[:16],
                                base_hash[:16],
                                int(diff_at),
                                cand_sn,
                                base_sn,
                            )
                        if reuse_on and reuse_probe_match:
                            context_body = candidate_body
                            deep_index_body = str((_prev_prefix or {}).get("deep_index", "") or "")
                            sandwich_body = candidate_sand
                            reuse_apply = True
                if not sandwich_body and context_body:
                    # Safety fallback для записи Layer B, если sandwich_body не вычислили явно.
                    sandwich_body = context_body
            except Exception as cache_asm_exc:
                self._handle_context_assembly_cache_error(_ck, cache_asm_exc)
                mode, reason = "FULL", "cache_internal_error"
                fp["last_committed_mode"] = "FULL"
                set_anchor_on_full(ci.chat_id, str(fp.get("session_id", "") or ""), int(fp.get("last_post_id", 0) or 0))
                self._context_ref_store.put(_ck, fp)
                reuse_on = False
                reuse_probe_enabled = False
                reuse_probe_candidate = False
                reuse_probe_match = False
                reuse_apply = False
                result = packer.pack(filtered_blocks, users=ci.users)
                deep_index_body = result["deep_index"]
                sandwich_body = "".join(result["sandwiches"])
                context_body = deep_index_body + "\n" + sandwich_body
                stats_index_json = result["index"]
                if not sandwich_body and context_body:
                    sandwich_body = context_body
            context += context_body
            focus = {"last_post_id": last_post_id, "attached_files": files_passed}
            context += f"\n<focus>\n{focus}\n</focus>"  # подстраховка для моделей, что ценят больше последние символы контекста
            log.debug("Контекст сгенерирован, длина %d символов, индекс кэширован", len(context))
            if mode == "DELTA_SAFE":
                # Layer B payload сохраняем только для DELTA_SAFE-цепочки.
                self._store_mp_payload(
                    ci=ci,
                    fp=fp,
                    context_body=context_body,
                    deep_index=deep_index_body,
                    sandwich_body=sandwich_body,
                )
            full_ctx_tokens = estimate_tokens(context)
            up_tokens = estimate_tokens(upgrade_sandwich_text) if upgrade_sandwich_text else 0
            up_pct = (
                round(100.0 * up_tokens / full_ctx_tokens, 6) if full_ctx_tokens else 0.0
            )
            dbg_ctx = self.debug_mode or getattr(ci, "debug_mode", False)
            ci.context_upgrade_text_for_debug = upgrade_sandwich_text if dbg_ctx else ""
            ci.context_meta = {
                "cache_mode": mode,
                "cache_reason": reason,
                "session_id": str(fp.get("session_id", "") or ""),
                "last_post_id": int(fp.get("last_post_id", 0) or 0),
                "blocks_count": int(fp.get("blocks_count", 0) or 0),
                "pre_prompt_hash": str(fp.get("pre_prompt_hash", "") or ""),
                "index_hash": str(fp.get("index_hash", "") or ""),
                "context_upgrade_tokens": up_tokens if dbg_ctx else 0,
                "context_full_tokens": full_ctx_tokens if dbg_ctx else 0,
                "context_upgrade_pct": up_pct if dbg_ctx else 0.0,
                # Диагностика readiness Layer B: не влияет на транспорт и решение кэша.
                "prefix_reuse_on": reuse_on,
                "prefix_reuse_apply": reuse_apply,
                "prefix_reuse_probe_enabled": reuse_probe_enabled,
                "prefix_reuse_probe_candidate": reuse_probe_candidate,
                "prefix_reuse_probe_match": reuse_probe_match,
            }
            self._write_context_stats(
                filtered_blocks,
                actor.user_name,
                ci.chat_id,
                stats_index_json,
                context_upgrade_tokens=up_tokens if dbg_ctx else 0,
                context_full_tokens=full_ctx_tokens if dbg_ctx else 0,
                context_upgrade_pct=up_pct if dbg_ctx else 0.0,
                context_cache_mode=mode,
                context_cache_reason=reason,
                debug_context_upgrade=dbg_ctx,
            )
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
        build_started = time.monotonic()
        context = await asyncio.to_thread(self.build_context, ci, rql)
        build_context_ms = int((time.monotonic() - build_started) * 1000)
        context_meta = dict(ci.context_meta or {})
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
        dbg_ctx = self.debug_mode or ci.debug_mode
        if dbg_ctx:
            meta_dbg = ci.context_meta or {}
            up_path = Path(f"/app/logs/context-{actor.user_name}-{ci.chat_id}.upgrade.llm")
            up_body = getattr(ci, "context_upgrade_text_for_debug", "") or ""
            up_hdr = (
                "# context upgrade dump (context_patch sandwich blocks only)\n"
                f"# cache_mode={meta_dbg.get('cache_mode')} "
                f"reason={meta_dbg.get('cache_reason')}\n"
                f"# context_full_tokens={meta_dbg.get('context_full_tokens')} "
                f"context_upgrade_tokens={meta_dbg.get('context_upgrade_tokens')} "
                f"upgrade_pct={meta_dbg.get('context_upgrade_pct')}%\n"
                "---\n"
            )
            try:
                await asyncio.to_thread(
                    self._atomic_write_text, up_path, up_hdr + (up_body or "# (no context_patch this turn)\n")
                )
                log.info(
                    "Дамп апгрейда контекста: %s upgrade_tokens=%s pct=%s",
                    str(up_path),
                    meta_dbg.get("context_upgrade_tokens"),
                    meta_dbg.get("context_upgrade_pct"),
                )
            except Exception as e:
                g.handle_exception(f"Не удалось сохранить {up_path}", e)
        sent_tokens = estimate_tokens(context)
        warn_thr = sent_tokens_warn_threshold()
        if warn_thr > 0 and sent_tokens > warn_thr:
            log.warn(
                "sent_tokens=%d превышает CQDS_CONTEXT_SENT_TOKENS_WARN=%d (chat_id=%d user_id=%d)",
                int(sent_tokens),
                int(warn_thr),
                int(ci.chat_id),
                int(actor.user_id),
            )
        if sent_tokens > tokens_limit:
            log.error("Контекст превышает лимит токенов для user_id=%d: %d > %d", actor.user_id, sent_tokens, tokens_limit)
            raise ValueError(f"Контекст превышает лимит токенов: {sent_tokens} > {tokens_limit}")

        if ci.debug_mode:
            bypass = bool(getattr(ci, "debug_bypass", False))
            metric_id = self._record_context_cache_metric(
                ci,
                model=getattr(actor.llm_connection, "model", ""),
                rql=rql,
                context_meta=context_meta,
                build_context_ms=build_context_ms,
                sent_tokens=sent_tokens,
                used_tokens=0,
                output_tokens=0,
                provider_error=False,
                error_type="debug_bypass" if bypass else "debug_mode",
            )
            _ = metric_id
            log.info(
                "Отладочный режим (%s), контекст не отправлен LLM %s",
                "debug_bypass" if bypass else "global_debug",
                actor.user_name,
            )
            return f"OK: debug_mode, context size = {len(context)}"

        conn = actor.llm_connection
        conn.pre_prompt = f"You are @{actor.user_name}, participant of software developers chat\n" + self.pre_prompt
        log.debug("Отправка в LLM для user_id=%d: %d символов, rql %d", actor.user_id, len(context) + len(self.pre_prompt), rql)
        search_params = conn.get_search_params(actor.user_id)
        used_tokens = 0
        output_tokens = 0
        usage_prompt_cache_hit_tokens = 0
        usage_prompt_cache_miss_tokens = 0
        usage_cache_read_input_tokens = 0
        usage_cache_creation_input_tokens = 0
        usage_details_json = ""
        try:
            cache_hint = self._provider_cache_hint(conn=conn, context=context, context_meta=context_meta)
            if cache_hint:
                log.debug(
                    "PROVIDER_PREFIX_CACHE chat_id=%d actor_id=%d model=%s sys_chars=%d user_chars=%d",
                    int(ci.chat_id),
                    int(actor.user_id),
                    str(getattr(conn, "model", "")),
                    int(cache_hint.get("system_cache_prefix_chars", 0) or 0),
                    int(cache_hint.get("user_cache_prefix_chars", 0) or 0),
                )
            conn.make_payload(prompt=context, extra={"cache_hint": cache_hint} if cache_hint else None)
            if search_params.get("mode", 'off') == "off":
                log.debug("Поиск отключён для user_id=%d", actor.user_id)
                response = await conn.call()
            else:
                conn.add_search_tool(search_params)
                response = await conn.call()
            if response:
                usage = response.get('usage', {})
                metrics = self._extract_usage_metrics(usage)
                used_tokens = int(metrics["used_tokens"])
                output_tokens = int(metrics["output_tokens"])
                sources_used = int(metrics["sources_used"])
                usage_prompt_cache_hit_tokens = int(metrics["usage_prompt_cache_hit_tokens"])
                usage_prompt_cache_miss_tokens = int(metrics["usage_prompt_cache_miss_tokens"])
                usage_cache_read_input_tokens = int(metrics["usage_cache_read_input_tokens"])
                usage_cache_creation_input_tokens = int(metrics["usage_cache_creation_input_tokens"])
                usage_details_json = str(metrics["usage_details_json"] or "")
                input_cost = used_tokens * input_tokens_cost / 1_000_000
                output_cost = output_tokens * output_tokens_cost / 1_000_000
                total_cost = input_cost + output_cost
                usage_marks = self._usage_cycle_markers(
                    ci=ci, context_meta=context_meta, sent_tokens=sent_tokens
                )
                metric_id = self._record_context_cache_metric(
                    ci,
                    model=str(conn.model),
                    rql=rql,
                    context_meta=context_meta,
                    build_context_ms=build_context_ms,
                    sent_tokens=sent_tokens,
                    used_tokens=used_tokens,
                    output_tokens=output_tokens,
                    usage_prompt_cache_hit_tokens=usage_prompt_cache_hit_tokens,
                    usage_prompt_cache_miss_tokens=usage_prompt_cache_miss_tokens,
                    usage_cache_read_input_tokens=usage_cache_read_input_tokens,
                    usage_cache_creation_input_tokens=usage_cache_creation_input_tokens,
                    usage_details_json=usage_details_json,
                    provider_error=False,
                    error_type="",
                )
                self.llm_usage_table.insert_into(
                    self._build_llm_usage_row(
                        conn=conn,
                        ci=ci,
                        tokens_limit=tokens_limit,
                        sent_tokens=sent_tokens,
                        used_tokens=used_tokens,
                        output_tokens=output_tokens,
                        sources_used=sources_used,
                        input_cost=input_cost,
                        output_cost=output_cost,
                        usage_marks=usage_marks,
                        metric_id=metric_id,
                    )
                )
                log.debug("Сохранена статистика LLM для chat_id=%d, user_id=%d: model=%s, sent_tokens=%d, used_tokens=%d, output_tokens=%d, sources_used=%d, input_cost=%f, output_cost=%f, total=%f",
                          ci.chat_id, actor.user_id, conn.model, sent_tokens, used_tokens, output_tokens, sources_used, input_cost, output_cost, total_cost)
                text = response.get('text', 'void-response')
                response_file = Path(f"/app/logs/response-{actor.user_name}-{ci.chat_id}.llm")
                await asyncio.to_thread(self._atomic_write_text, response_file, text)
                return text
            else:
                log.error("llm_connection вернул %s", str(response))
                self._record_context_cache_metric(
                    ci,
                    model=str(conn.model),
                    rql=rql,
                    context_meta=context_meta,
                    build_context_ms=build_context_ms,
                    sent_tokens=sent_tokens,
                    used_tokens=used_tokens,
                    output_tokens=output_tokens,
                    usage_prompt_cache_hit_tokens=usage_prompt_cache_hit_tokens,
                    usage_prompt_cache_miss_tokens=usage_prompt_cache_miss_tokens,
                    usage_cache_read_input_tokens=usage_cache_read_input_tokens,
                    usage_cache_creation_input_tokens=usage_cache_creation_input_tokens,
                    usage_details_json=usage_details_json,
                    provider_error=True,
                    error_type="invalid_response",
                )
                return f"invalid_response: {response}"
        except Exception as e:
            error_msg = f"Ошибка LLM для {actor.user_name}: {str(e)}"
            g.handle_exception(error_msg, e)
            g.post_manager.add_post(
                ci.chat_id, 2, error_msg, rql=rql if rql >= 2 else None
            )
            self._record_context_cache_metric(
                ci,
                model=str(getattr(conn, "model", "")),
                rql=rql,
                context_meta=context_meta,
                build_context_ms=build_context_ms,
                sent_tokens=sent_tokens,
                used_tokens=used_tokens,
                output_tokens=output_tokens,
                usage_prompt_cache_hit_tokens=usage_prompt_cache_hit_tokens,
                usage_prompt_cache_miss_tokens=usage_prompt_cache_miss_tokens,
                usage_cache_read_input_tokens=usage_cache_read_input_tokens,
                usage_cache_creation_input_tokens=usage_cache_creation_input_tokens,
                usage_details_json=usage_details_json,
                provider_error=True,
                error_type=type(e).__name__,
            )
            return f"LLMException {e}"
