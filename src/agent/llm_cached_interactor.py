import datetime
import os
import time

from llm_interactor import LLMInteractor
from managers.db import DataTable
from lib.context_reference_store import file_rev_ts_map, map_get, post_digest_map
from lib.cache_rollout import cache_rollout_enabled
from lib.session_context import get_session_id
from lib.relevance_window_anchor import set_anchor_on_full
import globals as g

log = g.get_logger("llm_cached_interactor")


class LLMCachedInteractor(LLMInteractor):
    """Промежуточный interactor-слой для кэшированной интеракции.

    В текущем этапе выносит кэш-политику в отдельный уровень наследования:
    LLMInteractor -> LLMCachedInteractor -> ReplicationManager.
    Это сохраняет API и позволяет быстрый rollback через смену базового класса
    в ReplicationManager обратно на LLMInteractor.
    """

    LLM_USAGE_EXTRA_TEMPLATE = [
        "effective_sent_tokens INTEGER DEFAULT 0",
        "cache_mode TEXT DEFAULT ''",
        "cache_reason TEXT DEFAULT ''",
        "cache_session_id TEXT DEFAULT ''",
        "cache_cycle_id TEXT DEFAULT ''",
        "cache_cycle_step INTEGER DEFAULT 0",
        "context_metric_id INTEGER DEFAULT 0",
    ]

    def _init_cache_metrics_table(self) -> None:
        self.context_cache_metrics_table = DataTable(
            table_name="context_cache_metrics",
            template=[
                "metric_id INTEGER PRIMARY KEY AUTOINCREMENT",
                "ts INTEGER",
                "schema_ver INTEGER DEFAULT 1",
                "actor_id INTEGER",
                "chat_id INTEGER",
                "project_id INTEGER",
                "session_id TEXT",
                "model TEXT",
                "rql INTEGER",
                "mode TEXT",
                "reason TEXT",
                "blocks_count INTEGER",
                "last_post_id INTEGER",
                "build_context_ms INTEGER",
                "sent_tokens INTEGER",
                "used_tokens INTEGER",
                "output_tokens INTEGER",
                "provider_error INTEGER DEFAULT 0",
                "error_type TEXT",
                "pre_prompt_hash TEXT",
                "index_hash TEXT",
                "context_upgrade_tokens INTEGER DEFAULT 0",
                "context_full_tokens INTEGER DEFAULT 0",
                "context_upgrade_pct REAL DEFAULT 0",
                "prefix_reuse_on INTEGER DEFAULT 0",
                "prefix_reuse_apply INTEGER DEFAULT 0",
                "prefix_reuse_probe_enabled INTEGER DEFAULT 0",
                "prefix_reuse_probe_candidate INTEGER DEFAULT 0",
                "prefix_reuse_probe_match INTEGER DEFAULT 0",
                "usage_prompt_cache_hit_tokens INTEGER DEFAULT 0",
                "usage_prompt_cache_miss_tokens INTEGER DEFAULT 0",
                "usage_cache_read_input_tokens INTEGER DEFAULT 0",
                "usage_cache_creation_input_tokens INTEGER DEFAULT 0",
                "usage_details_json TEXT DEFAULT ''",
            ]
        )
        # Runtime state для расчёта инкрементального sent_tokens по кэш-циклу.
        # key: actor_id:chat_id:session_id
        self._usage_cycle_state: dict[str, dict] = {}
        # Одноразовый принудительный FULL для пары (actor_id, chat_id).
        self._pending_full_reasons: dict[tuple[int, int], str] = {}
        self.post_retention_refs_table = DataTable(
            table_name="post_retention_refs",
            template=[
                "post_id INTEGER",
                "chat_id INTEGER",
                "actor_id INTEGER",
                "session_id TEXT",
                "expires_at INTEGER",
                "updated_at INTEGER",
                "ref_reason TEXT",
                "PRIMARY KEY (post_id, actor_id, session_id)",
            ],
        )

    def invalidate_context(self, chat_id: int, actor_id: int | None = None, reason: str = "forced_invalidate") -> None:
        """Принудительно инвалидировать контекстный кэш для actor/chat.

        Следующий ход соответствующего актёра пойдёт в FULL с reason=...,
        а снимки Layer A/B будут очищены.
        """
        rid = str(reason or "forced_invalidate")
        cid = int(chat_id)
        if actor_id is None:
            removed = self._context_ref_store.evict_scope(chat_id=cid, actor_id=None)
            # Глобальную метку ставим только для известных LLM-актёров.
            actors = getattr(g.replication_manager, "actors", []) if getattr(g, "replication_manager", None) else []
            for a in actors:
                uid = int(getattr(a, "user_id", 0) or 0)
                if uid > 0 and getattr(a, "llm_connection", None):
                    self._pending_full_reasons[(uid, cid)] = rid
            log.info("ContextInvalidate chat_id=%d actor=ALL removed_keys=%d reason=%s", cid, removed, rid)
            return
        aid = int(actor_id)
        removed = self._context_ref_store.evict_scope(chat_id=cid, actor_id=aid)
        self._pending_full_reasons[(aid, cid)] = rid
        log.info("ContextInvalidate chat_id=%d actor_id=%d removed_keys=%d reason=%s", cid, aid, removed, rid)

    @staticmethod
    def _lease_ttl_sec() -> int:
        try:
            return max(60, int(os.environ.get("CQDS_POST_LEASE_TTL_SEC", "1800")))
        except (TypeError, ValueError):
            return 1800

    @staticmethod
    def _gc_batch_size() -> int:
        try:
            return max(10, int(os.environ.get("CQDS_POST_GC_BATCH", "200")))
        except (TypeError, ValueError):
            return 200

    def _refresh_post_leases(self, ci, filtered_blocks: list) -> None:
        sid = str(get_session_id() or "")
        if not sid:
            return
        uid = int(getattr(ci.actor, "user_id", 0) or 0)
        cid = int(ci.chat_id)
        now_ts = int(time.time())
        exp_ts = now_ts + self._lease_ttl_sec()
        seen: set[int] = set()
        for b in filtered_blocks:
            if getattr(b, "content_type", None) != ":post":
                continue
            pid = int(getattr(b, "post_id", 0) or 0)
            if pid <= 0 or pid in seen:
                continue
            seen.add(pid)
            self.post_retention_refs_table.insert_or_replace(
                {
                    "post_id": pid,
                    "chat_id": cid,
                    "actor_id": uid,
                    "session_id": sid,
                    "expires_at": exp_ts,
                    "updated_at": now_ts,
                    "ref_reason": "context_window",
                }
            )

    def gc_deleted_posts(self, *, chat_id: int | None = None) -> int:
        now_ts = int(time.time())
        params = {"now_ts": now_ts, "batch": self._gc_batch_size()}
        where = "p.deleted_at IS NOT NULL AND p.deleted_at <= :now_ts"
        if chat_id is not None:
            where += " AND p.chat_id = :chat_id"
            params["chat_id"] = int(chat_id)
        rows = self.post_retention_refs_table.db.fetch_all(
            f"""
            SELECT p.id
            FROM posts p
            WHERE {where}
              AND NOT EXISTS (
                SELECT 1 FROM post_retention_refs r
                WHERE r.post_id = p.id AND r.expires_at > :now_ts
              )
            ORDER BY p.id
            LIMIT :batch
            """,
            params,
        )
        removed = 0
        for row in rows:
            pid = int(row[0])
            self.post_retention_refs_table.db.execute(
                "DELETE FROM post_retention_refs WHERE post_id = :pid",
                {"pid": pid},
            )
            self.post_retention_refs_table.db.execute(
                "DELETE FROM posts WHERE id = :pid",
                {"pid": pid},
            )
            removed += 1
        if removed > 0:
            log.info("PostGC removed=%d chat_id=%s", removed, str(chat_id) if chat_id is not None else "ALL")
        return removed

    def _incremental_patches_enabled(self, ci) -> bool:
        if not self._context_ref_store.enabled:
            return False
        return cache_rollout_enabled(get_session_id(), int(getattr(ci.actor, "user_id", 0) or 0))

    def _after_context_fingerprint(self, ci, filtered_blocks: list, fp: dict) -> None:
        _ = fp
        self._refresh_post_leases(ci, filtered_blocks)

    def _usage_cycle_markers(self, *, ci, context_meta: dict, sent_tokens: int) -> dict:
        mode = str((context_meta or {}).get("cache_mode", "FULL") or "FULL")
        reason = str((context_meta or {}).get("cache_reason", "") or "")
        sid = str((context_meta or {}).get("session_id", "") or "")
        ck = self._context_cache_key(ci.actor.user_id, ci.chat_id, sid)
        now = int(datetime.datetime.now().timestamp())
        st = self._usage_cycle_state.get(ck)
        sent = int(sent_tokens or 0)

        if mode == "FULL" or not isinstance(st, dict):
            cycle_id = f"{ck}:{now}"
            step = 0
            eff = sent
            self._usage_cycle_state[ck] = {
                "cycle_id": cycle_id,
                "last_sent": sent,
                "step": 0,
            }
        else:
            cycle_id = str(st.get("cycle_id", "") or f"{ck}:{now}")
            prev_sent = int(st.get("last_sent", 0) or 0)
            step = int(st.get("step", 0) or 0) + 1
            eff = max(sent - prev_sent, 0)
            st["last_sent"] = sent
            st["step"] = step
            st["cycle_id"] = cycle_id
            self._usage_cycle_state[ck] = st

        return {
            "cache_mode": mode,
            "cache_reason": reason,
            "cache_session_id": sid,
            "cache_cycle_id": cycle_id,
            "cache_cycle_step": int(step),
            "effective_sent_tokens": int(eff),
        }

    @staticmethod
    def _prefix_reuse_probe_enabled() -> bool:
        v = (os.environ.get("CQDS_CONTEXT_PREFIX_REUSE_PROBE") or "0").strip().lower()
        on = v in ("1", "true", "on", "yes")
        return on and cache_rollout_enabled(get_session_id(), None)

    @staticmethod
    def _prefix_reuse_on() -> bool:
        v = (os.environ.get("CQDS_CONTEXT_PREFIX_REUSE") or "0").strip().lower()
        on = v in ("1", "true", "on", "yes")
        return on and cache_rollout_enabled(get_session_id(), None)

    @staticmethod
    def _provider_prefix_cache_on() -> bool:
        v = (os.environ.get("CQDS_PROVIDER_PREFIX_CACHE") or "0").strip().lower()
        on = v in ("1", "true", "on", "yes")
        return on and cache_rollout_enabled(get_session_id(), None)

    def _provider_cache_hint(self, *, conn, context: str, context_meta: dict) -> dict:
        if not self._provider_prefix_cache_on():
            return {}
        if getattr(conn, "name", "") != "OpenRouter":
            return {}
        model = str(getattr(conn, "model", "") or "").lower()
        if not model.startswith("anthropic/"):
            return {}
        if str((context_meta or {}).get("cache_mode", "") or "") != "DELTA_SAFE":
            return {}
        txt = str(context or "")
        if len(txt) < 2000:
            return {}
        return {
            "system_cache_prefix_chars": len(str(getattr(conn, "pre_prompt", "") or "")),
            "user_cache_prefix_chars": int(len(txt) * 0.85),
        }

    def _cleanup_mp(self, cache_key: str, prev_fp: dict | None) -> None:
        if isinstance(prev_fp, dict) and str(prev_fp.get("last_committed_mode", "")) == "FULL":
            self._context_ref_store.evict_mp(cache_key)

    def _build_pd_body(
        self,
        *,
        prev_prefix: dict | None,
        prev_fp: dict | None,
        filtered_blocks: list,
    ) -> str:
        if not isinstance(prev_prefix, dict) or not isinstance(prev_fp, dict):
            return ""
        prefix_body = str(prev_prefix.get("context_body", "") or "")
        if not prefix_body:
            return ""
        p_prev = int(prev_fp.get("last_post_id", 0) or 0)
        prefix_body = self._replace_post_blocks(prefix_body, filtered_blocks, p_prev)
        tail_parts: list[str] = []
        for b in filtered_blocks:
            ct = getattr(b, "content_type", None)
            if ct == ":context_patch":
                tail_parts.append(b.to_sandwich_block())
                continue
            if ct != ":post":
                continue
            pid = int(getattr(b, "post_id", 0) or 0)
            if pid > p_prev:
                tail_parts.append(b.to_sandwich_block())
        if not tail_parts:
            return ""
        tail_text = "".join(tail_parts)
        prepend = False
        for b in filtered_blocks:
            if getattr(b, "content_type", None) != ":post":
                continue
            pid = int(getattr(b, "post_id", 0) or 0)
            if pid <= 0:
                continue
            prepend = pid > p_prev
            break
        return (tail_text + prefix_body) if prepend else (prefix_body + tail_text)

    def _build_pd_sand(
        self,
        *,
        prev_prefix: dict | None,
        prev_fp: dict | None,
        filtered_blocks: list,
        packer,
        users: list,
    ) -> str:
        if not isinstance(prev_prefix, dict) or not isinstance(prev_fp, dict):
            return ""
        base = str(prev_prefix.get("sandwich_body", "") or "")
        if not base:
            return ""
        p_prev = int(prev_fp.get("last_post_id", 0) or 0)
        base = self._replace_post_blocks(base, filtered_blocks, p_prev)
        tail_blocks: list = []
        for b in filtered_blocks:
            ct = getattr(b, "content_type", None)
            if ct == ":context_patch":
                tail_blocks.append(b)
                continue
            if ct != ":post":
                continue
            pid = int(getattr(b, "post_id", 0) or 0)
            if pid > p_prev:
                tail_blocks.append(b)
        if not tail_blocks:
            return ""
        tail_result = packer.pack(tail_blocks, users=users)
        tail_text = "".join(tail_result["sandwiches"])
        prepend = False
        for b in filtered_blocks:
            if getattr(b, "content_type", None) != ":post":
                continue
            pid = int(getattr(b, "post_id", 0) or 0)
            if pid <= 0:
                continue
            prepend = pid > p_prev
            break
        return (tail_text + base) if prepend else (base + tail_text)

    def _can_reuse_mp(
        self,
        *,
        prev_prefix: dict | None,
        pre_prompt_hash: str,
        index_hash: str,
        head_posts_sig: str,
        p_prev: int,
    ) -> bool:
        if not isinstance(prev_prefix, dict):
            return False
        if not str(prev_prefix.get("context_body", "") or ""):
            return False
        if str(prev_prefix.get("pre_prompt_hash", "") or "") != str(pre_prompt_hash):
            return False
        if str(prev_prefix.get("index_hash", "") or "") != str(index_hash):
            return False
        if str(prev_prefix.get("head_posts_sig", "") or "") != str(head_posts_sig):
            return False
        if int(prev_prefix.get("last_post_id", 0) or 0) != int(p_prev or 0):
            return False
        return True

    def _store_mp_payload(
        self,
        *,
        ci,
        fp: dict,
        context_body: str,
        deep_index: str = "",
        sandwich_body: str = "",
    ) -> None:
        sid = str(fp.get("session_id", "") or "")
        ck = self._context_cache_key(ci.actor.user_id, ci.chat_id, sid)
        self._context_ref_store.put_mp(
            ck,
            {
                "pre_prompt_hash": str(fp.get("pre_prompt_hash", "") or ""),
                "index_hash": str(fp.get("index_hash", "") or ""),
                "head_posts_sig": str(fp.get("head_posts_sig", "") or ""),
                "last_post_id": int(fp.get("last_post_id", 0) or 0),
                "context_body": context_body,
                "context_body_hash": self._hash_text(context_body),
                "deep_index": deep_index,
                "sandwich_body": sandwich_body,
                "updated_at": int(fp.get("updated_at", 0) or 0),
            },
        )

    def _decide_cache_mode(
        self,
        prev: dict | None,
        *,
        actor_id: int = 0,
        chat_id: int = 0,
        pre_prompt_hash: str,
        index_hash: str,
        filtered_blocks: list,
        last_post_id: int,
        post_digest_current: dict[int, str] | None = None,
        reference_digest_enabled: bool = True,
    ) -> tuple[str, str]:
        forced_reason = self._pending_full_reasons.pop((int(actor_id or 0), int(chat_id or 0)), "")
        if forced_reason:
            return "FULL", str(forced_reason)
        if not cache_rollout_enabled(get_session_id(), int(actor_id or 0)):
            return "FULL", "rollout_disabled"
        if prev is None:
            return "FULL", "no_cache_state"
        if "head_posts_sig" not in prev or "non_post_sig" not in prev:
            return "FULL", "stale_fingerprint_layout"
        if reference_digest_enabled and "post_digest" not in prev:
            return "FULL", "stale_fingerprint_layout"
        if prev.get("pre_prompt_hash") != pre_prompt_hash:
            return "FULL", "pre_prompt_changed"
        if prev.get("index_hash") != index_hash:
            return "FULL", "index_changed"

        p_prev = int(prev.get("last_post_id", 0) or 0)
        p_cur = int(last_post_id or 0)
        head_now_upto_prev = self._head_posts_signature(filtered_blocks, p_prev)
        if head_now_upto_prev != prev.get("head_posts_sig"):
            return "FULL", "head_posts_changed"
        if reference_digest_enabled and post_digest_current is not None:
            pd_prev = prev.get("post_digest") or {}
            if isinstance(pd_prev, dict):
                for pid, cur_d in post_digest_current.items():
                    if int(pid) > p_prev:
                        continue
                    old_d = map_get(pd_prev, int(pid))
                    if old_d is None:
                        continue
                    if old_d == "__PROGRESS__" and cur_d not in ("__PROGRESS__", "__WARN__"):
                        continue
                    if old_d != cur_d:
                        return "FULL", "head_post_content_changed"
        if self._non_post_signature(filtered_blocks) != prev.get("non_post_sig"):
            return "FULL", "attachments_or_spans_changed"
        if p_cur < p_prev:
            return "FULL", "history_rewind_or_edit"
        if p_cur == p_prev:
            return "FULL", "no_tail_append"
        return "DELTA_SAFE", "tail_append_detected"

    def _log_context_fingerprint(
        self,
        ci,
        full_idx: str,
        index_fp_json: str,
        filtered_blocks: list,
        last_post_id: int,
        rql: int,
    ):
        session_id = get_session_id()
        cache_key = self._context_cache_key(ci.actor.user_id, ci.chat_id, session_id)
        pre_prompt_hash = self._hash_text(self.pre_prompt)
        index_hash = self._hash_text(index_fp_json)
        p_cur = int(last_post_id or 0)
        ref = self._context_ref_store
        prev = ref.get(cache_key)
        digest_map = post_digest_map(ci.blocks) if ref.enabled else {}
        ref_on = ref.enabled

        mode, reason = self._decide_cache_mode(
            prev,
            actor_id=int(getattr(ci.actor, "user_id", 0) or 0),
            chat_id=int(ci.chat_id),
            pre_prompt_hash=pre_prompt_hash,
            index_hash=index_hash,
            filtered_blocks=filtered_blocks,
            last_post_id=p_cur,
            post_digest_current=(digest_map if ref_on else None),
            reference_digest_enabled=ref_on,
        )
        if mode == "FULL":
            set_anchor_on_full(ci.chat_id, session_id or "", p_cur)
            ref.evict_mp(cache_key)
        current_fp = {
            "pre_prompt_hash": pre_prompt_hash,
            "index_hash": index_hash,
            "head_posts_sig": self._head_posts_signature(filtered_blocks, p_cur),
            "non_post_sig": self._non_post_signature(filtered_blocks),
            "last_post_id": p_cur,
            "last_committed_mode": mode,
            "blocks_count": len(filtered_blocks),
            "rql": int(rql),
            "session_id": session_id or "",
            "updated_at": int(datetime.datetime.now().timestamp()),
        }
        if ref_on:
            current_fp["post_digest"] = digest_map
            current_fp["file_rev_ts"] = file_rev_ts_map(ci.blocks)
        ref.put(cache_key, current_fp)
        log.info(
            "ContextCacheDecision mode=%s reason=%s actor_id=%d chat_id=%d rql=%d session=%s last_post_id=%d blocks=%d",
            mode, reason, ci.actor.user_id, ci.chat_id, rql,
            (session_id[:8] if session_id else "-"),
            current_fp["last_post_id"], current_fp["blocks_count"],
        )
        return mode, reason, current_fp

