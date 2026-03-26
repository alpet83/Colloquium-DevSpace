# Performance Issues And TODO

## Summary

Наблюдаемая проблема: сервер Colloquium-DevSpace ведет себя как почти последовательный, хотя маршруты объявлены как `async`.

Ключевой вывод: проблема не в одном "глобальном mutex", а в сочетании нескольких архитектурных факторов:

- Один процесс Uvicorn без worker pool на уровне процессов.
- Внутри `async`-маршрутов выполняется много синхронной работы: SQLAlchemy, файловые операции, чтение логов, рекурсивные сканы проекта.
- На частых read-path вызываются тяжелые действия, включая повторный `scan_project_files()`.
- Из-за этого event loop блокируется, и запросы разных пользователей начинают ждать друг друга.

## Confirmed Findings

- `src/agent/server.py` запускает один Uvicorn server process.
- `src/docker-compose.yml` не добавляет дополнительных workers или process manager.
- `src/agent/managers/db.py` полностью синхронный.
- `src/agent/routes/file_routes.py` содержит `async`-маршруты, которые вызывают синхронный `FileManager`.
- `src/agent/routes/chat_routes.py` содержит тяжелые синхронные участки, включая чтение целого log file в `/chat/logs`.
- `src/agent/managers/posts.py` и `src/agent/llm_interactor.py` выполняют тяжелую синхронную подготовку контекста до фактического LLM-вызова.
- `src/agent/managers/project.py` вызывает `scan_project_files()` из `load()`.
- `ProjectManager.get(project_id)` может создавать новый инстанс, который снова триггерит полный scan проекта.
- `src/agent/context_assembler.py` содержит еще один явный вызов `pm.scan_project_files()` для `@attach_dir#...`.
- `src/agent/routes/project_routes.py:/project/select` меняет project only in session, но не обновляет глобальный `g.project_manager`.

## Main Bottleneck Hypothesis

Самый вероятный первый bottleneck сейчас такой:

- UI или chat flow часто вызывает `chat/list_files`.
- `FileManager.list_files()` для каждого файла идет через `_qfn()`.
- `_qfn()` может зайти в `ProjectManager.get(project_id)`.
- `ProjectManager.get(project_id)` при несовпадении с глобальным singleton создает новый manager.
- Новый manager в `load()` снова делает `scan_project_files()`.
- Полный scan файловой системы блокирует event loop и тормозит все остальные запросы.

Это хорошо объясняет наблюдение пользователя, что запросы разных сессий начинают идти как будто по очереди.

## Assessment Of The Idea: Global Function project_manager(project_id)

Идея в целом хорошая, но только в ограниченном и дисциплинированном варианте.

Что в ней полезно:

- Убирает ложное предположение, что в системе всегда должен существовать только один активный `ProjectManager`.
- Позволяет кэшировать инстансы по `project_id` и перестать пересоздавать manager на каждом обходном read-path.
- Хорошо ложится на текущую реальную модель, где система одновременно работает с несколькими проектами.
- Это может быстро убрать часть лишних rescans даже без полной async-перестройки.

Что важно понимать:

- Само по себе это не решит проблему блокирующих sync-операций.
- Если оставить `load()` с обязательным `scan_project_files()`, словарь инстансов только уменьшит частоту проблемы, но не устранит корень.
- Если в объекте `ProjectManager` есть mutable runtime-state, кэш инстансов надо делать аккуратно, иначе появятся stale-state и трудноуловимые побочные эффекты.
- Если позже появятся несколько worker-процессов, такой словарь будет локальным на процесс и не станет общей точкой истины.
- Нужна явная стратегия invalidation: когда обновлять или сбрасывать кэш manager после `create/update/select/reindex`.

## Recommendation On This Idea

Идею стоит принимать, но не как "замену глобальной переменной", а как небольшой registry/factory слой.

Рекомендованный вариант:

- Не использовать имя `project_manager(project_id)` из-за конфликта смыслов с текущим `g.project_manager`.
- Ввести что-то вроде `get_project_manager(project_id)` или `ProjectRegistry.get(project_id)`.
- Хранить словарь `project_id -> ProjectManager`.
- Делать lazy creation.
- Убрать implicit full scan из обычного `load()`.
- Отдельно оставить явный метод типа `refresh_files()` или `scan_project_files(force=True)` для тех мест, где scan действительно нужен.

Итого по оценке:

- Как первый инфраструктурный шаг: да, идея разумная.
- Как самостоятельное решение проблемы последовательной обработки: нет, недостаточно.
- Как часть первой волны правок вместе с удалением implicit rescans: да, это сильный вариант.

## Recommended Fix Order

1. Убрать implicit `scan_project_files()` из обычных read-path.
2. Ввести registry/factory для `ProjectManager` по `project_id`.
3. Перевести самые тяжелые маршруты с blocking sync-работой из event loop в threadpool или в обычные sync handlers.
4. Упростить `chat/list_files`, чтобы он использовал легкий индекс вместо дорогого path-resolution там, где это возможно.
5. Отдельно разобрать `chat/logs`, чтобы не читать весь файл целиком для выдачи последних строк.
6. Только после этого решать, нужны ли дополнительные Uvicorn workers.

## Proposed First Patch Set

Если идти минимальным безопасным шагом, первый набор правок должен быть таким:

- Вынести получение manager в отдельный registry helper.
- Запретить автоматический full scan внутри `ProjectManager.load()`.
- Явно вызывать scan только там, где без него реально нельзя.
- Перепроверить `FileManager._qfn()` и `FileManager.list_files()`, чтобы они не инициировали тяжелую загрузку проекта на каждый файл.

## Risks To Track

- Устаревший file cache после добавления или удаления файлов.
- Рост памяти, если registry никогда не очищается.
- Неожиданные зависимости на side-effect от старого `load()`.
- Разное поведение между текущим single-process режимом и будущим multi-worker режимом.

## Decision Point

Рациональное решение на текущем этапе:

- Принять идею registry/factory для `ProjectManager`.
- Делать ее только вместе с устранением implicit rescans.
- Не начинать с workers, пока не сняты главные blocking path и повторные project scans.

## Note: all_projects Usage Check

Проверка текущего кода показала, что полноценный режим обзорного `all_projects` (для `project_name = None`) сейчас сервисами фактически не используется как обязательный рабочий путь.

Практический вывод:

- Специальный manager для `all_projects` можно отложить до отдельного шага.
- На первом этапе достаточно стабилизировать работу менеджеров для реальных `project_id` и убрать hidden scans из read-path.