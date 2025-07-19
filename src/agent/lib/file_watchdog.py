# /app/agent/lib/file_watchdog.py, created 2025-07-19 09:55 EEST
import os
import time
import glob
import asyncio
from lib.basic_logger import BasicLogger
import globals

log = globals.get_logger("file_watchdog")

async def watch_files(shutdown_event: asyncio.Event):
    """Следит за изменениями в *.py файлах в /app/agent и вызывает shutdown при изменениях."""
    log.info("Запуск мониторинга изменений в *.py файлах в /app/agent")
    watch_dir = "/app/agent"
    last_modified = {}

    # Инициализация начальных времен изменения
    for file_path in glob.glob(os.path.join(watch_dir, "**/*.py"), recursive=True):
        try:
            last_modified[file_path] = os.path.getmtime(file_path)
        except OSError as e:
            log.error("Ошибка получения времени изменения файла %s: %s", file_path, str(e))

    while not shutdown_event.is_set():
        try:
            for file_path in glob.glob(os.path.join(watch_dir, "**/*.py"), recursive=True):
                try:
                    mtime = os.path.getmtime(file_path)
                    if file_path in last_modified and mtime > last_modified[file_path]:
                        log.info("Обнаружено изменение в файле %s, инициируется shutdown", file_path)
                        from server import shutdown  # Импорт внутри для избежания циклического импорта
                        await shutdown()
                        return
                    last_modified[file_path] = mtime
                except OSError as e:
                    log.error("Ошибка проверки времени изменения файла %s: %s", file_path, str(e))
            # Удаляем записи о файлах, которые больше не существуют
            for file_path in list(last_modified.keys()):
                if not os.path.exists(file_path):
                    del last_modified[file_path]
                    log.debug("Удалена запись о несуществующем файле %s", file_path)
            await asyncio.sleep(5)
        except Exception as e:
            log.excpt("Ошибка в мониторинге файлов: %s", str(e), exc_info=(type(e), e, e.__traceback__))
            await asyncio.sleep(5)