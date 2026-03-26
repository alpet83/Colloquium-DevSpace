# LLM Interactive Upgrade — Документация потока и проблем

> Создано: 2026-03-26 | Автор: анализ codebase

---

## 1. Полный поток сообщения: от HTTP до ответа LLM

```
[Браузер] POST /chat/post {chat_id, message}
  │
  ▼ chat_routes.py:149
  post_manager.add_post(chat_id, user_id, message, rql=0)
    └── INSERT в posts table → post_id
  │
  ▼ chat_routes.py:154
  await post_manager.process_post(post, allow_rep=True)
  │
  ├── [1] РАННИЙ ПРОГРЕСС-ПОСТ (posts.py:154-167)
  │     if allow_rep and has_replication_trigger(message):
  │       Найти triggered LLM-актёра по @упоминанию
  │       add_post(chat_id, _p_uid, "⏳ @{name} LLM request accepted...")
  │       → replication_progress_post_id
  │
  ├── [2] ПОСТ-ПРОЦЕССОР — MCP команды (posts.py:181-206)
  │     if user_id != g.AGENT_UID:
  │       await pp.process_response(chat_id, user_id, message, post_id)
  │       → agent_message (ответ на MCP-команды, если были)
  │
  ├── [3] ОТВЕТ АГЕНТА (posts.py:207-213) — если были MCP-команды
  │     add_post(chat_id, g.AGENT_UID, agent_message, rql, post_id, elapsed)
  │
  ├── [4] ЗАПУСК РЕПЛИКАЦИИ (posts.py:216-222)
  │     sr = check_start_replication(chat_id, post_id, user_id, message, rql)
  │     if NOT sr: delete_post(replication_progress_post_id)  ← очистка
  │                 (если sr=True — пост #1 ОСТАЁТСЯ висеть! ← БАГИ)
  │
  └── Возврат ответа браузеру (HTTP response)
      (репликация продолжается в фоне как asyncio.Task)

[asyncio Task: replicate_to_llm(chat_id)]
  │
  ▼ replication.py:408-420
  replicate_to_llm()
    └── _broadcast(users, chat_id, exclude_id, session_id)
  │
  ▼ replication.py:286+
  _broadcast()
    Читает latest_post из БД (ВНИМАНИЕ: может прочитать не тот пост если race)
    Находит @упоминания в тексте
    llm_actors = [a for a if a.llm_connection and a.user_id > g.AGENT_UID]
    Для каждого совпадающего актёра:
      await _recursive_replicate(ci, rql+1, session_id)
  │
  ▼ replication.py:176+
  _recursive_replicate(ci, rql)
    │
    ├── [5] ВТОРОЙ ПРОГРЕСС-ПОСТ НА АКТЁРА (replication.py:218-226)
    │     post_man.add_post(ci.chat_id, actor.user_id,
    │                       "⏳ @{actor.user_name} preparing response...")
    │     → progress_post_id  ← локальная переменная в _recursive_replicate
    │
    ├── [6] ЗАПРОС К LLM API (replication.py:232)
    │     task = asyncio.create_task(self.interact(ci, rql=rql))
    │     ChatLocker: блокирует параллельные взаимодействия для чата
    │
    ├── [7] HEARTBEAT ОБНОВЛЕНИЯ (replication.py:233-240)
    │     while not task.done():
    │       edit_post(progress_post_id,
    │                 "⏳ @{name} response in progress... {N}s")
    │       await asyncio.sleep(interval_s)  # 0.3-5с из session options
    │
    ├── [8] ПОЛУЧЕНИЕ ОТВЕТА LLM
    │     original_response = await task  → строка с текстом ответа
    │     Парсинг @quote#{id}, @post#{id} для reply_to
    │     Обрезка @agent-преамбул
    │
    ├── [9] ЗАМЕНА/СОЗДАНИЕ ПОСТА С ОТВЕТОМ (replication.py:263-272)
    │     if progress_post_id:
    │       post_man.edit_post(progress_post_id, response, actor.user_id)
    │     else:
    │       post_man.add_post(chat_id, actor.user_id, response, rql, reply_to)
    │
    └── [10] ПОСТОБРАБОТКА ОТВЕТА (replication.py:274)
          response_result = await post_man.process_post(post, allow_rep=False)
          (MCP-команды в ответе LLM; allow_rep=False → нет нового прогресс-поста)
```

---

## 2. Два прогресс-поста — корень проблемы

| | Пост #1 (ранний) | Пост #2 (per-LLM) |
|---|---|---|
| **Создаётся в** | `posts.py:154` (`process_post`) | `replication.py:218` (`_recursive_replicate`) |
| **user_id** | `_triggered.user_id` или `g.AGENT_UID` | `actor.user_id` (конкретный LLM) |
| **Текст** | `"⏳ @{name} LLM request accepted, preparing response..."` | `"⏳ @{actor.user_name} preparing response..."` |
| **reply_to** | `post_id` пользователя | `None` |
| **Хранится в** | `replication_progress_post_id` (local в process_post) | `progress_post_id` (local в _recursive_replicate) |
| **Heartbeat** | ❌ нет | ✅ каждые 0.3-5с |
| **Финальный ответ** | ❌ НИКОГДА не обновляется | ✅ edit_post() заменяет текстом LLM |
| **Удаляется** | Только если `sr=False` | Нет (заменяется через edit) |
| **Проблема** | Если `sr=True` → висит вечно | Работает корректно |

### Итог:
Пост #1 создаётся как «быстрая обратная связь», но при успешном запуске репликации (`sr=True`) он **не удаляется и не передаётся** в `_recursive_replicate`. В результате:
- Пользователь видит ДВА сообщения "preparing response"
- Пост #1 остаётся навсегда в состоянии "LLM request accepted..."
- Пост #2 корректно обновляется до финального ответа

---

## 3. Все хардкоды user_id = 2 (должно быть g.AGENT_UID)

| Файл | Строка | Контекст |
|------|--------|----------|
| `llm_interactor.py` | 167 | Ошибка записи статистики контекста |
| `llm_interactor.py` | 268 | Ошибка сохранения контекста |
| `managers/posts.py` | 207 | Ответ агента на MCP-команды ← уже исправлено как g.AGENT_UID |
| `managers/replication.py` | 129 | Ошибка запуска репликации |

---

## 4. План исправлений

### 4.1 Исправить orphaned прогресс-пост (ГЛАВНЫЙ БАГ)

**Вариант A (рекомендован): передать post_id в задачу репликации**

В `posts.py` после запуска задачи передавать `replication_progress_post_id`
в `replicate_to_llm()` как параметр, и в `_broadcast` → `_recursive_replicate`
использовать его вместо создания нового поста #2:

```python
# posts.py — изменить вызов
sr = g.replication_manager.check_start_replication(
    chat_id, post_id, user_id, message, rql,
    session_id=session_id,
    progress_post_id=replication_progress_post_id   # ← НОВЫЙ ПАРАМЕТР
)
```

```python
# replication.py — check_start_replication принимает progress_post_id
# передаёт в replicate_to_llm → _broadcast → _recursive_replicate
# _recursive_replicate использует переданный post_id вместо создания нового
```

**Вариант B (простой паллиатив): удалять пост #1 при запуске репликации**

```python
# posts.py — сразу после sr = True
if sr and replication_progress_post_id:
    self.delete_post(replication_progress_post_id, g.AGENT_UID)
```
Минус: пользователь не увидит никакой обратной связи пока идёт первый цикл репликации.

### 4.2 Убрать все `add_post(chat_id, 2, ...)` → `add_post(chat_id, g.AGENT_UID, ...)`

Файлы: `llm_interactor.py` строки 167, 268; `replication.py` строка 129.

### 4.3 Сохранить проверенный баг chat_id != g.AGENT_UID

`posts.py:181` — исправлено `chat_id != 2` → `user_id != g.AGENT_UID`. ✅

---

## 5. Архитектура interact() и LLM-подключений

### ContextInput (llm_interactor.py)
```python
class ContextInput:
    blocks: list          # ContentBlock — собранный контекст чата
    users: list           # [{user_id, username, role}]
    chat_id: int
    actor: ChatActor      # LLM-актёр: user_id, user_name, llm_class, llm_token
    exclude_source_id: int
    debug_mode: bool
```

### ChatActor → LLMConnection mapping (chat_actor.py)
```
llm_class="grok"          → XAIConnection
llm_class="chatgpt"       → OpenAIConnection
llm_class="openrouter:.*" → OpenRouterConnection
llm_class="/.*"           → OpenRouterConnection
default                   → LLMConnection (базовый)
```

### interact() поток (llm_interactor.py)
```
1. build_context(ci, rql)   — asyncio.to_thread (CPU-bound)
   └── assemble_posts, assemble_files, assemble_spans → prompt string
2. conn.make_payload(prompt) — формирует тело запроса
3. conn.add_search_tool(params) — если search mode != "off"
4. response = await conn.call() — HTTP к API LLM
5. Парсинг response → text (строка с ответом)
6. return text
```

### Обновление прогресс-поста
Обновление происходит **не внутри** `interact()`, а в heartbeat-цикле `_recursive_replicate`:
```python
while not task.done():                    # task = asyncio.create_task(interact())
    edit_post(progress_post_id, heartbeat) # "⏳ ... {elapsed}s"
    await asyncio.sleep(interval_s)        # интервал из session option llm_update_interval_ms
```
После завершения task: `edit_post(progress_post_id, final_response)`.

---

## 6. Найденные баги и исправления (2026-03-26)

### БАГ #1 — КРИТИЧЕСКИЙ: LLM-ответ вообще не приходил 

**Симптом:** прогресс-пост висит вечно: `"@grok4f LLM request accepted, preparing response..."`

**Цепочка причин:**
1. `check_start_replication(post_id=11)` создаёт asyncio task `replicate_to_llm(chat_id)` — **post_id не передаётся**
2. `replicate_to_llm` вызывает `_broadcast(users, chat_id, ...)` — **post_id не передаётся (default=-1)**
3. `_broadcast` с `post_id=-1`: `cond = {'chat_id': chat_id}` (без фильтра по id)
4. `latest_post({'chat_id': 3})` → возвращает ПОСЛЕДНИЙ пост в чате = **post #1 (прогресс-пост, user_id=grok4f)**
5. `_broadcast` видит `user_id=4:grok4f` как автора → `Пропуск диалога для автора поста 4:grok4f`
6. Все LLM-актёры пропущены → **LLM API вообще не вызывается**

**Исправлено:**
- `check_start_replication`: `replicate_to_llm(chat_id, post_id=post_id, ...)` — передаёт post_id
- `replicate_to_llm`: добавлен параметр `post_id: int = -1`; передаётся в `_broadcast(... post_id=post_id)`
- Теперь `_broadcast` получает `cond = {'chat_id': 3, 'id': 11}` → читает правильный пост пользователя

### БАГ #2: Самоссылка в тексте прогресс-поста

**Симптом:** UI показывает `grok4f: @grok4f LLM request accepted...` — двойное упоминание имени.

**Причина:** пост атрибутирован `user_id=grok4f`, и текст содержал `f"⏳ @{_p_name} LLM request..."`.

**Исправлено:** убрано `@{_p_name}` из текста → `f"⏳ LLM request accepted, preparing response..."`

### БАГ #3 (ранее): Неправильное поле в условии пост-процессора

**Исправлено ранее:** `chat_id != 2` → `user_id != g.AGENT_UID` (posts.py:181)

### Хардкоды user_id=2 → g.AGENT_UID (исправлены ранее)

- `llm_interactor.py:167,268` ✅
- `managers/posts.py:207` ✅  
- `managers/replication.py:129` ✅

---

## 7. Следующие шаги

- [ ] Тест: убедиться что LLM теперь отвечает (grok4f реагирует на @grok4f)
- [ ] Рассмотреть Вариант A для пост #1: передавать `replication_progress_post_id` в `_recursive_replicate`, чтобы reuse вместо создания поста #2
- [ ] Рассмотреть streaming в LLM-подключениях для более плавного heartbeat
