# /agent/llm_hands.py, updated 2025-07-17 18:48 EEST
import logging
import re
import requests
import time
import traceback
from datetime import datetime
from llm_api import LLMConnection
from managers.files import FileManager
import globals

MCP_URL = "http://mcp-sandbox:8084"

def process_message(text, timestamp, user_name):
    logging.debug(f"Processing message: text='{text}', timestamp={timestamp}, user_name={user_name}")
    file_manager = FileManager()

    # Обработка тега <code_file>
    def handle_code_file(match):
        file_name = match.group(1)  # file_name как путь (например, trade_report/example.rs)
        source_code = match.group(2)
        logging.debug(f"Found code_file tag: file_name={file_name}, content_length={len(source_code)}")

        # Получаем текущий проект из globals.project_manager
        project_manager = globals.project_manager
        project_id = project_manager.project_id if project_manager and project_manager.project_id else None
        if not project_id:
            logging.error(f"No active project selected for code_file processing")
            return f"Error: No active project selected"

        # Проверяем существование файла
        file_id = file_manager.exists(file_name, project_id)
        action = "Обновлен"
        if file_id:
            try:
                # Создаём бэкап
                backup_path = file_manager.backup_file(file_id)
                if not backup_path:
                    logging.error(f"Failed to create backup for file_id={file_id}")
                    return f"Error: Failed to backup file {file_name}"
                # Обновляем существующий файл
                file_manager.update_file(
                    file_id=file_id,
                    content=b'',  # Пустой контент, данные хранятся на диске
                    file_name=file_name,
                    timestamp=int(time.time()),
                    project_id=project_id
                )
            except Exception as e:
                logging.error("Exception {e}")
                traceback.print_exc()
        else:
            action = "Создан"
            # Создаём новый файл
            file_id = file_manager.add_file(
                content=b'',  # Пустой контент, данные хранятся на диске
                file_name=file_name,
                timestamp=int(time.time()),
                project_id=project_id
            )
        # Сохраняем файл на диске
        project_manager.write_file(file_name, source_code)
        logging.debug(f"Saved file to disk: {file_name}, project_id={project_id}")
        return f"{action} файл: {file_name} (@attached_file#{file_id}, {datetime.fromtimestamp(timestamp).strftime('%m/%d/%Y, %I:%M:%S %p')})"

    # Заменяем теги <code_file> и убираем @agent
    processed_text = re.sub(
        r'<code_file name="([^"]+)">\s*(.*?)\s*</code_file>',
        handle_code_file,
        text,
        flags=re.DOTALL
    )
    if processed_text.startswith('@agent'):
        processed_text = processed_text.replace('@agent', '', 1).strip()

    # Обработка команд @agent
    if processed_text.startswith('@agent'):
        logging.info(f"#INFO: #post_{timestamp}: Обработка команды от {user_name}: {processed_text}")
        refs = re.findall(r'#ref_(\d+)', processed_text)
        context = ''
        for ref in refs:
            context += f"#ref_{ref}: [загружено из кэша]\n"
        command = processed_text.split()[1].lower() if len(processed_text.split()) > 1 else None
        response = None
        if command == 'ping':
            response = f'@{user_name} pong'
        elif command == 'run_test':
            params = {'project_name': 'default', 'test_name': 'test'}
            resp = requests.get(f"{MCP_URL}/run_test", params=params)
            response = resp.text if resp.status_code == 200 else f"Ошибка: {resp.status_code}"
        elif command == 'commit':
            data = {'project_name': 'default', 'msg': 'commit msg'}
            resp = requests.post(f"{MCP_URL}/commit", json=data)
            response = resp.text if resp.status_code == 200 else f"Ошибка: {resp.status_code}"
        if response:
            logging.info(f"#INFO: #post_{timestamp + 1}: Ответ: {response}")
            return response
    return processed_text if processed_text != text else None