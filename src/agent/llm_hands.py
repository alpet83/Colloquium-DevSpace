# /agent/llm_hands.py, updated 2025-07-18 22:04 EEST
import re
import requests
import time
import difflib
from datetime import datetime
from llm_api import LLMConnection
import globals
from lib.basic_logger import BasicLogger
from pathlib import Path

MCP_URL = "http://mcp-sandbox:8084"
log = globals.get_logger("llm_hands")

def process_message(text, timestamp, user_name):
    log.debug("Обработка сообщения: text=%s, timestamp=%d, user_name=%s", text[:50], timestamp, user_name or "None")
    # Декодируем входной текст
    if isinstance(text, bytes):
        text = text.decode('utf-8', errors='replace')
        log.warn("Входной текст был байтовым, декодирован: %s", text[:50])
    elif not isinstance(text, str):
        log.error("Неверный тип входного текста: %s", type(text))
        return {"status": "error", "processed_msg": text, "agent_reply": "Error: Invalid input type"}

    file_manager = globals.file_manager
    patch_count = 0
    original_text = text
    processed_msg = original_text
    agent_reply = []

    # Обработка команд @agent
    if processed_msg.strip().startswith('@agent') and processed_msg.strip() != '@agent':
        command_text = processed_msg.replace('@agent', '', 1).strip()
        log.info("#post_%d: Обработка команды от %s: %s", timestamp, user_name or "None", command_text[:50])
        refs = re.findall(r'#ref_(\d+)', command_text)
        context = ''.join(f"#ref_{ref}: [загружено из кэша]\n" for ref in refs)
        command = command_text.split()[0].lower() if command_text.split() else None
        response = None
        if command == 'ping':
            response = f'@{user_name or "Unknown"} pong'
        elif command == 'run_test':
            params = {'project_name': 'default', 'test_name': 'test'}
            resp = requests.get(f"{MCP_URL}/run_test", params=params)
            response = resp.text if resp.status_code == 200 else f"Ошибка: {resp.status_code}"
        elif command == 'commit':
            data = {'project_name': 'default', 'msg': 'commit msg'}
            resp = requests.post(f"{MCP_URL}/commit", json=data)
            response = resp.text if resp.status_code == 200 else f"Ошибка: {resp.status_code}"
        if response:
            log.info("#post_%d: Ответ: %s", timestamp + 1, response[:50])
            return {"status": "success", "processed_msg": command_text, "agent_reply": response}

    # Обработка тега <code_file>
    def handle_code_file(match):
        nonlocal patch_count
        patch_count += 1
        file_name = match.group(1)
        source_code = match.group(2)
        log.debug("Найден тег code_file: file_name=%s, content_length=%d", file_name, len(source_code))

        project_manager = globals.project_manager
        project_id = project_manager.project_id if project_manager and hasattr(project_manager, 'project_id') else None
        if project_id is None:
            log.error("Нет активного проекта для обработки code_file")
            return {"status": "error", "message": "Error: No active project selected", "patch_number": patch_count}

        # Проверка безопасности пути
        try:
            safe_path = (Path('/app/projects') / file_name).resolve()
            if not str(safe_path).startswith('/app/projects'):
                log.error("Недопустимый путь файла: %s", file_name)
                return {"status": "error", "message": "Error: File path outside /app/projects", "patch_number": patch_count}
        except Exception as e:
            log.excpt("Ошибка проверки пути файла %s: %s", file_name, str(e), exc_info=(type(e), e, e.__traceback__))
            return {"status": "error", "message": "Error: Invalid file path", "patch_number": patch_count}

        content_bytes = source_code.encode('utf-8')
        file_id = file_manager.exists(file_name, project_id)
        action = "сохранён" if file_id else "создан"
        if file_id:
            try:
                file_manager.update_file(
                    file_id=file_id,
                    content=content_bytes,
                    file_name=file_name,
                    timestamp=int(time.time()),
                    project_id=project_id
                )
            except Exception as e:
                log.excpt("Ошибка обновления файла file_id=%d: %s", file_id, str(e), exc_info=(type(e), e, e.__traceback__))
                return {"status": "error", "message": "Error: Failed to update file %s" % file_name, "patch_number": patch_count}
        else:
            file_id = file_manager.add_file(
                content=content_bytes,
                file_name=file_name,
                timestamp=int(time.time()),
                project_id=project_id
            )
        return {
            "status": "success",
            "processed_message": f"@attach#{file_id}",
            "agent_message": f"Файл {file_id} успешно {action}",
            "patch_number": patch_count
        }

    # Обработка тега <code_patch>
    def handle_code_patch(match):
        nonlocal patch_count
        patch_count += 1
        file_id = int(match.group(1))
        patch_content = match.group(2)
        if isinstance(patch_content, bytes):
            patch_content = patch_content.decode('utf-8', errors='replace')
            log.warn("patch_content был байтовым, декодирован: %s", patch_content[:50])
        elif not isinstance(patch_content, str):
            log.error("Неверный тип patch_content для file_id=%d: %s", file_id, type(patch_content))
            return {"status": "error", "message": "Error: Invalid patch content type", "patch_number": patch_count}
        log.debug("Найден тег code_patch: file_id=%d, patch_content=~C95%s~C00, type=%s", file_id, patch_content[:50], type(patch_content))

        project_manager = globals.project_manager
        project_id = project_manager.project_id if project_manager and hasattr(project_manager, 'project_id') else None
        if project_id is None:
            log.error("Нет активного проекта для обработки code_patch")
            return {"status": "error", "message": "Error: No active project selected", "patch_number": patch_count}

        file_data = file_manager.get_file(file_id)
        if not file_data:
            log.error("Файл file_id=%d не найден", file_id)
            return {"status": "error", "message": "Error: File %d not found" % file_id, "patch_number": patch_count}

        file_name = file_data['file_name']
        log.debug("Данные файла: ~C95%s~C00", str(file_data))
        current_content = file_data['content']
        if isinstance(current_content, bytes):
            if not current_content:
                log.warn("Файл file_id=%d не содержит контента, попытка загрузки с диска", file_id)
                current_content = project_manager.read_project_file(file_name)
                if not current_content:
                    log.error("Не удалось загрузить файл %s с диска", file_name)
                    return {"status": "error", "message": "Error: Failed to read file %s" % file_name, "patch_number": patch_count}
            current_content = current_content.decode('utf-8', errors='replace')

        try:
            current_lines = current_content.splitlines(keepends=True)
            patch_lines = patch_content.splitlines(keepends=True)
        except Exception as e:
            log.excpt("Ошибка разбиения строк для file_id=%d: %s", file_id, str(e), exc_info=(type(e), e, e.__traceback__))
            return {"status": "error", "message": "Error: Failed to process patch content", "patch_number": patch_count}

        if not any(line.startswith('@@') for line in patch_lines):
            log.error("Невалидный формат патча для file_id=%d", file_id)
            return {"status": "error", "message": "Error: Invalid patch format", "patch_number": patch_count}

        old_lines = [line[1:] for line in patch_lines if line.startswith('-') and not line.startswith('---')]
        old_text = ''.join(old_lines)
        if old_text and old_text not in ''.join(current_lines):
            log.error("Патч не соответствует содержимому файла file_id=%d: удаляемые строки=~C95%s~C00", file_id, old_text)
            return {"status": "error", "message": "Error: Patch does not match file content", "patch_number": patch_count}

        try:
            diff = ['--- {}\n'.format(file_name), '+++ {}\n'.format(file_name)] + patch_lines
            result = []
            for line in patch_lines:
                if line.startswith('@@'):
                    continue
                elif line.startswith('-'):
                    continue
                elif line.startswith('+'):
                    result.append(line[1:])
                else:
                    result.append(line)
            new_content = ''.join(result)
            if new_content == current_content:
                log.debug("Патч для file_id=%d не вносит изменений", file_id)
                return {"status": "success", "message": "Файл %d не изменён" % file_id, "patch_number": patch_count}

            content_bytes = new_content.encode('utf-8')
            file_manager.update_file(
                file_id=file_id,
                content=content_bytes,
                file_name=file_name,
                timestamp=int(time.time()),
                project_id=project_id
            )
            log.debug("Применён патч для file_id=%d, file_name=%s, project_id=%s", file_id, file_name, str(project_id) if project_id is not None else "None")
            return {"status": "success", "message": "Файл %d успешно сохранён" % file_id, "patch_number": patch_count}
        except Exception as e:
            log.excpt("Ошибка применения патча для file_id=%d: %s", file_id, str(e), exc_info=(type(e), e, e.__traceback__))
            return {"status": "error", "message": "Error: Failed to apply patch to %s" % file_name, "patch_number": patch_count}

    # Обрабатываем теги <code_file> и <code_patch>
    has_code_file = False
    for pattern, handler in [
        (r'<code_file name="([^"]+)">\s*(.*?)\s*</code_file>', handle_code_file),
        (r'<code_patch file_id="(\d+)">\s*(.*?)\s*</code_patch>', handle_code_patch)
    ]:
        matches = list(re.finditer(pattern, processed_msg, flags=re.DOTALL))
        for match in matches:
            result = handler(match)
            if result["status"] == "error":
                agent_reply.append(f"@{user_name} {result['message']}")
            else:
                if pattern.startswith('<code_file'):
                    has_code_file = True
                    processed_msg = processed_msg.replace(match.group(0), result["processed_message"], 1).replace('@agent', '', 1).strip()
                    agent_reply.append(result["agent_message"])
                else:
                    agent_reply.append(result["message"])

    status = "error" if agent_reply and all(r["status"] == "error" for r in [handle_code_file(m) for m in re.finditer(r'<code_file name="([^"]+)">\s*(.*?)\s*</code_file>', original_text, flags=re.DOTALL)] +
                    [handle_code_patch(m) for m in re.finditer(r'<code_patch file_id="(\d+)">\s*(.*?)\s*</code_patch>', original_text, flags=re.DOTALL)]) else "success"
    agent_reply_text = "\n".join(agent_reply) if agent_reply else None
    return {"status": status, "processed_msg": processed_msg, "agent_reply": agent_reply_text, "has_code_file": has_code_file}