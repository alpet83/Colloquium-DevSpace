# llm_hands.py
# Предложено: 2025-07-14 11:47 EEST

import logging
import re
import requests
from llm_api import LLMConnection

MCP_URL = "http://mcp-sandbox:8084"

def process_message(text, timestamp, user_name):
    if text.startswith('@agent'):
        logging.info(f"#INFO: #post_{timestamp}: Обработка команды от {user_name}: {text}")
        refs = re.findall(r'#ref_(\d+)', text)
        context = ''
        for ref in refs:
            context += f"#ref_{ref}: [загружено из кэша]\n"
        command = text.split()[1].lower() if len(text.split()) > 1 else None
        response = None
        if command == 'ping':
            response = f'@{user_name} pong'
        elif command == 'apply_patch':
            data = {'project_name': 'default', 'file_path': 'file.rs', 'content': 'patch code'}
            resp = requests.post(f"{MCP_URL}/apply_patch", json=data)
            response = resp.text if resp.status_code == 200 else f"Ошибка: {resp.status_code}"
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
    return None
