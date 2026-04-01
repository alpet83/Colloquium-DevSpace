# /app/agent/routes/chat_routes.py, updated 2025-07-26 15:15 EEST
import math

from fastapi import APIRouter, Request, HTTPException
from typing import Optional
import asyncio
import time
import re
import os
from managers.db import Database
import globals as g
from globals import check_session, handle_exception

router = APIRouter()
log = g.get_logger("chatman")


def _row_get(row, index: int, key: str, default=None):
    try:
        return row[index]
    except Exception:
        pass
    mapping = getattr(row, "_mapping", None)
    if mapping is not None:
        return mapping.get(key, default)
    return default


def _collect_chat_usage_stats(chat_id: int, since_seconds: Optional[int] = None) -> dict:
    """Aggregate token/cost stats for a chat from llm_usage table."""
    conditions = [('chat_id', '=', chat_id)]
    rows = g.replication_manager.llm_usage_table.select_from(
        columns=[
            'model',
            'used_tokens',
            'output_tokens',
            'sources_used',
            'input_token_cost',
            'output_token_cost',
            'ts'
        ],
        conditions=conditions,
        order_by='ts ASC'
    )
    cutoff_ts = int(time.time()) - int(since_seconds) if since_seconds is not None and since_seconds > 0 else None

    total_input_tokens = 0
    total_output_tokens = 0
    total_sources_used = 0
    total_input_cost = 0.0
    total_output_cost = 0.0
    total_cost = 0.0
    models_used: set[str] = set()
    model_breakdown: dict[str, dict] = {}

    for row in rows:
        if cutoff_ts is not None:
            raw_ts = _row_get(row, 6, 'ts', 0)
            ts_value = 0
            try:
                if hasattr(raw_ts, 'timestamp'):
                    ts_value = int(raw_ts.timestamp())
                else:
                    ts_value = int(raw_ts or 0)
            except Exception:
                ts_value = 0
            if ts_value < cutoff_ts:
                continue

        model = row[0] or 'unknown'
        used_tokens = int(row[1] or 0)
        output_tokens = int(row[2] or 0)
        sources_used = int(row[3] or 0)
        input_cost = float(row[4] or 0.0)
        output_cost = float(row[5] or 0.0)
        row_total_cost = input_cost + output_cost

        total_input_tokens += used_tokens
        total_output_tokens += output_tokens
        total_sources_used += sources_used
        total_input_cost += input_cost
        total_output_cost += output_cost
        total_cost += row_total_cost
        models_used.add(model)

        if model not in model_breakdown:
            model_breakdown[model] = {
                'calls': 0,
                'input_tokens': 0,
                'output_tokens': 0,
                'input_cost': 0.0,
                'output_cost': 0.0,
                'total_cost': 0.0,
            }

        model_breakdown[model]['calls'] += 1
        model_breakdown[model]['input_tokens'] += used_tokens
        model_breakdown[model]['output_tokens'] += output_tokens
        model_breakdown[model]['input_cost'] += input_cost
        model_breakdown[model]['output_cost'] += output_cost
        model_breakdown[model]['total_cost'] += row_total_cost

    return {
        'chat_id': chat_id,
        'calls': len(rows),
        'since_seconds': since_seconds,
        'total_input_tokens': total_input_tokens,
        'total_output_tokens': total_output_tokens,
        'num_sources_used': total_sources_used,
        'input_tokens_cost': round(total_input_cost, 8),
        'output_tokens_cost': round(total_output_cost, 8),
        'estimated_cost_usd': round(total_cost, 8),
        'models_used': sorted(models_used),
        'model_breakdown': model_breakdown,
        'status': 'ok',
    }

@router.get("/chat/list")
async def list_chats(request: Request):
    log.debug(g.with_session_tag(request, "Запрос GET /chat/list, IP=%s, Cookies=~%s"), request.client.host, str(request.cookies))
    try:
        user_id = check_session(request)
        chats = g.chat_manager.list_chats(user_id)
        log.debug(g.with_session_tag(request, "Возвращено %d чатов для user_id=%d"), len(chats), user_id)
        return chats
    except Exception as e:
        handle_exception("Ошибка в GET /chat/list", e)
        raise

@router.post("/chat/create")
async def create_chat(request: Request):
    log.debug(g.with_session_tag(request, "Запрос POST /chat/create, IP=%s, Cookies=~%s"), request.client.host, str(request.cookies))
    try:
        user_id = check_session(request)
        data = await request.json()
        description = data.get('description', 'New Chat')
        parent_msg_id = data.get('parent_msg_id')
        chat_id = g.chat_manager.create_chat(description, user_id, parent_msg_id)
        if not isinstance(chat_id, int):
            raise HTTPException(status_code=500, detail="Chat creation failed")
        log.debug(g.with_session_tag(request, "Создан чат chat_id=%d для user_id=%d"), chat_id, user_id)
        return {"chat_id": chat_id}
    except Exception as e:
        handle_exception("Ошибка в POST /chat/create", e)
        raise

@router.post("/chat/delete")
async def delete_chat(request: Request):
    log.debug(g.with_session_tag(request, "Запрос POST /chat/delete, IP=%s, Cookies=~%s"), request.client.host, str(request.cookies))
    try:
        user_id = check_session(request)
        data = await request.json()
        chat_id = data.get('chat_id')
        if not chat_id:
            log.info(g.with_session_tag(request, "Неверный параметр chat_id=%s для IP=%s"), str(chat_id) if chat_id is not None else "None", request.client.host)
            raise HTTPException(status_code=400, detail="Missing chat_id")
        result = g.chat_manager.delete_chat(chat_id, user_id)
        log.debug(g.with_session_tag(request, "Удалён чат chat_id=%d для user_id=%d"), chat_id, user_id)
        return result
    except Exception as e:
        handle_exception("Ошибка в POST /chat/delete", e)
        raise

@router.get("/chat/get")
async def get_chat(request: Request, chat_id: int, wait_changes: int = 0):
    try:
        # NOLOG!: постоянное логирование запрещено из-за флуда
        user_id = check_session(request)
        session_id = request.cookies.get("session_id")
        status = {'status': "nope"}
        if wait_changes:
            max_wait = 15
            _elps = 0
            _loops = 0
            _start = time.time()
            while _elps < max_wait:
                _elps = time.time() - _start
                _loops += 1
                status = g.chat_manager.chat_status(chat_id)
                if status['status'] == 'busy' and max_wait > 1:
                    log.debug(g.with_session_tag(request, " Ожидание сокращено, поскольку чат занят пользователем %s "), status['actor'])
                    max_wait = 1
                active = g.chat_manager.active_chat(user_id, session_id)
                if active is None:
                    active = 0

                if active <= 0 < chat_id:  # автоматическая активация чата, если не выбран.
                    active = chat_id
                    g.chat_manager.select_chat(session_id, user_id, chat_id)

                history = g.post_manager.get_history(chat_id, wait_changes == 1)
                if history != {"chat_history": "no changes"}:
                    quotes = g.post_manager.get_quotes(history)
                    return {"posts": history, "chat_id": chat_id, "quotes": quotes, "status": status}
                if active != chat_id:
                    log.debug(g.with_session_tag(request, "Chat switch detected for user_id=%d, chat_id=%d, active=%d"), user_id or -1, chat_id or -1, active or 0)
                    return {"chat_id": active, "posts": {"chat_history": "chat switch"}, "status": status}
                await asyncio.sleep(0.1)
            return {"chat_id": chat_id, "posts": {"chat_history": "no changes"}, "quotes": {}, "status": status, "wait_loops": _loops, "elapsed": "%.1f" % _elps}
        else:
            log.debug(g.with_session_tag(request, "Статус обработки для user_id=%d, chat_id=%d: %s"), user_id, chat_id, status)
            history = g.post_manager.get_history(chat_id, only_changes=False)
            quotes = g.post_manager.get_quotes(history)
            return {"chat_id": chat_id, "posts": history, "quotes": quotes, "status": status}
    except Exception as e:
        handle_exception("Ошибка в GET /chat/get", e)
        raise

@router.post("/chat/notify_switch")
async def notify_chat_switch(request: Request):
    log.debug(g.with_session_tag(request, "Запрос POST /chat/notify_switch, IP=%s, Cookies=~%s"), request.client.host, str(request.cookies))
    try:
        user_id = check_session(request)
        session_id = request.cookies.get("session_id")
        data = await request.json()
        chat_id = data.get('chat_id')
        if not chat_id:
            log.info(g.with_session_tag(request, "Неверный параметр chat_id=%s для IP=%s"), str(chat_id) if chat_id is not None else "None", request.client.host)
            raise HTTPException(status_code=400, detail="Missing chat_id")

        g.chat_manager.select_chat(session_id, user_id, chat_id)
        changes = g.post_manager.get_changes(chat_id)
        log.debug(g.with_session_tag(request, "Уведомление о смене чата chat_id=%d для user_id=%d, changes=~%s"), chat_id, user_id, str(changes))
        return {"chat_history": "chat switch"}
    except Exception as e:
        handle_exception("Ошибка в POST /chat/notify_switch", e)
        raise


@router.post("/chat/post")
async def post_message(request: Request):
    log.debug(g.with_session_tag(request, "Запрос POST /chat/post, IP=%s, Cookies=~%s"), request.client.host, str(request.cookies))
    try:
        session_id = request.cookies.get('session_id')
        user_id = check_session(request)
        data = await request.json()
        chat_id = data.get('chat_id')
        message = data.get('message')
        interval = data.get('llm_update_interval_ms')
        if not chat_id or not message:
            log.info(g.with_session_tag(request, "Неверные параметры chat_id=%s или message для IP=%s"), str(chat_id) if chat_id is not None else "None", request.client.host)
            raise HTTPException(status_code=400, detail="Missing chat_id or message")
        if interval is not None:
            try:
                iv = int(interval)
                iv = max(300, min(iv, 5000))
                g.set_session_option(session_id, 'llm_update_interval_ms', iv)
                log.debug(g.with_session_tag(request, "Обновлён session-option llm_update_interval_ms=%d"), iv)
            except Exception:
                log.warn(g.with_session_tag(request, "Игнорируется некорректный llm_update_interval_ms=%s"), str(interval))
        post = g.post_manager.add_post(
            chat_id, user_id, message, rql=0, reply_to=None, session_id=session_id
        )
        log.debug(g.with_session_tag(request, "Добавлено сообщение для chat_id=%d, user_id=%d: %s, ожидание обработки"), chat_id, user_id, str(post))
        await asyncio.sleep(0.2)   # дать шанс /get
        return await g.post_manager.process_post(post, True)
    except Exception as e:
        handle_exception("Ошибка в POST /chat/post", e)
        raise


@router.post("/chat/edit_post")
async def edit_post(request: Request):
    log.debug(g.with_session_tag(request, "Запрос POST /chat/edit_post, IP=%s, Cookies=~%s"), request.client.host, str(request.cookies))
    try:
        user_id = check_session(request)
        data = await request.json()
        post_id = data.get('post_id')
        message = data.get('message')
        if not post_id or not message:
            log.info(g.with_session_tag(request, "Неверные параметры post_id=%s или message для IP=%s"), str(post_id) if post_id is not None else "None", request.client.host)
            raise HTTPException(status_code=400, detail="Missing post_id or message")
        result = g.post_manager.edit_post(post_id, message, user_id)
        log.debug(g.with_session_tag(request, "Отредактировано сообщение post_id=%d для user_id=%d"), post_id, user_id)
        return result
    except Exception as e:
        handle_exception("Ошибка в POST /chat/edit_post", e)
        raise


@router.post("/chat/delete_post")
async def delete_post(request: Request):
    log.debug(g.with_session_tag(request, "Запрос POST /chat/delete_post, IP=%s, Cookies=~%s"), request.client.host, str(request.cookies))
    try:
        user_id = check_session(request)
        data = await request.json()
        post_id = data.get('post_id')
        if not post_id:
            log.info(g.with_session_tag(request, "Неверный параметр post_id=%s для IP=%s"), str(post_id) if post_id is not None else "None", request.client.host)
            raise HTTPException(status_code=400, detail="Missing post_id")
        result = g.post_manager.delete_post(post_id, user_id)
        log.debug(g.with_session_tag(request, "Удалено сообщение post_id=%d от user_id=%d"), post_id, user_id)
        return result
    except Exception as e:
        handle_exception("Ошибка в POST /chat/delete_post", e)
        raise

@router.get("/chat/get_stats")
async def get_chat_stats(request: Request, chat_id: int, since_seconds: Optional[int] = None):
    # NOLOG!
    try:
        user_id = check_session(request)
        chat = g.chat_manager.chats_table.select_row(
            conditions=[('chat_id', '=', chat_id)],
            columns=['chat_id']
        )
        if not chat:
            log.info(g.with_session_tag(request, "Чат chat_id=%d не найден для user_id=%d"), chat_id, user_id)
            raise HTTPException(status_code=404, detail="Chat not found")
        aggregated = _collect_chat_usage_stats(chat_id, since_seconds)
        stats = {
            "chat_id": chat_id,
            "tokens": aggregated["total_input_tokens"],
            "output_tokens": aggregated["total_output_tokens"],
            "num_sources_used": aggregated["num_sources_used"],
            "input_tokens_cost": aggregated["input_tokens_cost"],
            "output_tokens_cost": aggregated["output_tokens_cost"],
            "estimated_cost_usd": aggregated["estimated_cost_usd"],
            "models_used": aggregated["models_used"],
            "calls": aggregated["calls"],
            "since_seconds": aggregated["since_seconds"],
            "status": aggregated["status"],
        }
        return stats
    except Exception as e:
        handle_exception("Ошибка в GET /chat/get_stats", e)
        raise

@router.get("/chat/stats")
async def get_chat_stats_new(request: Request, chat_id: int, since_seconds: Optional[int] = None):
    """Get detailed chat usage statistics including input/output token costs.

    Optional query param: since_seconds=<N> — restrict stats to last N seconds.
    Useful for getting per-iteration cost feedback.
    """
    # NOLOG!
    try:
        user_id = check_session(request)

        # Check chat exists
        chat = g.chat_manager.chats_table.select_row(
            conditions=[('chat_id', '=', chat_id)],
            columns=['chat_id', 'chat_description']
        ) if hasattr(g.chat_manager, 'chats_table') else None

        if not chat:
            log.info(g.with_session_tag(request, "Чат chat_id=%d не найден для user_id=%d"), chat_id, user_id)
            raise HTTPException(status_code=404, detail="Chat not found")

        aggregated = _collect_chat_usage_stats(chat_id, since_seconds)
        chat_description = _row_get(chat, 1, 'chat_description')
        stats = {
            "chat_id": chat_id,
            "description": chat_description,
            "calls": aggregated["calls"],
            "since_seconds": aggregated["since_seconds"],
            "total_input_tokens": aggregated["total_input_tokens"],
            "total_output_tokens": aggregated["total_output_tokens"],
            "input_tokens_cost": aggregated["input_tokens_cost"],
            "output_tokens_cost": aggregated["output_tokens_cost"],
            "estimated_cost_usd": aggregated["estimated_cost_usd"],
            "num_sources_used": aggregated["num_sources_used"],
            "models_used": aggregated["models_used"],
            "model_breakdown": aggregated["model_breakdown"],
            "status": aggregated["status"],
        }
        return stats
    except Exception as e:
        handle_exception("Ошибка в GET /chat/stats", e)
        raise

@router.get("/chat/get_parent_msg")
async def get_parent_msg(request: Request, post_id: int):
    log.debug(g.with_session_tag(request, "Запрос GET /chat/get_parent_msg, post_id=%d, IP=%s, Cookies=~%s"), post_id, request.client.host, str(request.cookies))
    try:
        user_id = check_session(request)
        ch_tab = g.chat_manager.chats_table
        ps_tab = g.post_manager.posts_table
        msg = ps_tab.select_row(
            columns=['id', 'chat_id', 'user_id', 'message', 'timestamp'],
            conditions=[('id', '=', post_id)]
        )
        if not msg:
            log.info(g.with_session_tag(request, "Сообщение post_id=%d не найден для user_id=%d"), post_id, user_id)
            affected_chats = ch_tab.select_from(
                conditions=[('parent_msg_id', '=', post_id)],
                columns=['chat_id', 'parent_msg_id']
            )
            for chat_id, parent_msg_id in affected_chats:
                chat = ch_tab.select_row(
                    conditions=[('chat_id', '=', chat_id)],
                    columns=['chat_id']
                )
                if chat:
                    new_parent_msg = ps_tab.select_row(
                        conditions=[('chat_id', '=', chat[0]), ('id', '<', post_id)],
                        columns=['id'],
                        order_by='id DESC'
                    )
                    new_parent_msg_id = new_parent_msg[0] if new_parent_msg else None
                    ch_tab.update(
                        conditions={'chat_id': chat_id},
                        values={'parent_msg_id': new_parent_msg_id}
                    )
                    log.debug(g.with_session_tag(request, "Обновлён parent_msg_id для chat_id=%d: %d -> %s"), chat_id, parent_msg_id,
                              str(new_parent_msg_id) if new_parent_msg_id is not None else "None")
            return None
        result = {
            "id": msg[0],
            "chat_id": msg[1],
            "user_id": msg[2],
            "message": msg[3],
            "timestamp": msg[4]
        }
        log.debug(g.with_session_tag(request, "Возвращено сообщение post_id=%d: ~%s"), post_id, str(result))
        return result
    except Exception as e:
        handle_exception("Ошибка в GET /chat/get_parent_msg", e)
        raise

@router.get("/chat/logs")
async def get_logs(request: Request):
    # NOLOG!
    try:
        user_id = check_session(request)
        log_file = g.LOG_FILE
        logs = []
        if os.path.exists(log_file):
            with open(log_file, "r", encoding="utf-8") as f:
                lines = f.readlines()[-100:]  # Последние 100 строк
                for line in lines:
                    if "#ERROR" in line or "#WARNING" in line:
                        match = re.match(r"[(.+?)] #(\w+): (.+)", line)
                        if match:
                            timestamp, level, message = match.groups()
                            timestamp = timestamp.replace(',', '.')
                            ts = time.strptime(timestamp, g.SQL_TIMESTAMP6)
                            logs.append({
                                "timestamp": int(time.mktime(ts)),
                                "level": level,
                                "message": message
                            })
        return {"logs": logs}
    except Exception as e:
        handle_exception("Ошибка в GET /chat/logs", e)
        raise