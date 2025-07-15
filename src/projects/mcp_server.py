# mcp_server.py
# Предложено: 2025-07-13 15:25 EEST

from flask import Flask, request, Response
import subprocess
import os
import logging
import json
import re
import hashlib
import time
import toml  # Для загрузки конфига
import socket  # Для получения IP
import sandwich_loader  # Импорт модуля для загрузки сэндвичей
import atexit

app = Flask(__name__)

PROJECTS_DIR = "/app/projects"
CONFIG_PATH = "/app/data/mcp_config.toml"
SECRET_TOKEN = "Grok-xAI-Agent-The-Best"
LOG_FILE = "/app/logs/mcp_errors.log"

def server_init():
    # Настройка журналов
    logging.basicConfig(level=logging.INFO, format='[%(asctime)s] #%(levelname)s: %(message)s')
    log_dir = "/app/logs"
    if not os.path.exists(log_dir):
        os.makedirs(log_dir)

    # Создание файла для ошибок
    if not os.path.exists(LOG_FILE):
        open(LOG_FILE, 'w').close()

    # Загрузка конфига
    global admin_ips, admin_subnet
    config = toml.load(CONFIG_PATH)
    admin_ips = config.get('security', {}).get('admin_ips', [])
    admin_subnet = config.get('security', {}).get('admin_subnet', '')

    # Получение локального IP
    local_ip = socket.gethostbyname(socket.gethostname())
    logging.info(f"#INFO: Сервер запущен на IP {local_ip}:8084")

def calculate_md5(content):
    return hashlib.md5(content.encode('utf-8')).hexdigest()

def is_admin_ip():
    client_ip = request.remote_addr
    if client_ip in admin_ips:
        return True
    if admin_subnet:
        try:
            from ipaddress import ip_address, ip_network
            return ip_address(client_ip) in ip_network(admin_subnet)
        except ValueError:
            return False
    return False

@app.route('/apply_patch', methods=['POST'])
def apply_patch():
    if not is_admin_ip() and request.headers.get('Authorization') != f"Bearer {SECRET_TOKEN}":
        return Response("Unauthorized: Invalid or missing token", status=401, mimetype='text/plain')

    data = request.get_json()
    if not data or 'project_name' not in data or 'file_path' not in data or 'content' not in data:
        return Response("Missing project_name, file_path or content", status=400, mimetype='text/plain')

    project_name = data['project_name']
    file_path = data['file_path']
    content = data['content']

    project_dir = os.path.join(PROJECTS_DIR, project_name)
    full_path = os.path.join(project_dir, file_path)

    if 'config.toml' in file_path:
        return Response("Access to config.toml is restricted", status=403, mimetype='text/plain')

    # Загрузка сэндвичей для актуальности
    try:
        sandwich_loader.load_and_unpack_sandwiches(project_name)
        logging.info(f"#INFO: Сэндвичи перезагружены для {project_name}")
    except Exception as e:
        logging.error(f"#ERROR: Ошибка перезагрузки сэндвичей: {e}")
        return Response(f"Failed to reload sandwiches: {e}", status=500, mimetype='text/plain')

    # Запись патча
    os.makedirs(os.path.dirname(full_path), exist_ok=True)
    with open(full_path, 'w') as f:
        f.write(content)
    actual_md5 = calculate_md5(content)
    logging.info(f"#INFO: Патч применён к {file_path}, MD5: {actual_md5}")

    timestamp = int(time.time())
    headers = {"X-Timestamp": str(timestamp), "X-MD5": actual_md5}
    return Response(f"#post_{timestamp}: Патч применён успешно", headers=headers, status=200, mimetype='text/plain')

@app.route('/run_test', methods=['GET'])
def run_test():
    if not is_admin_ip() and request.headers.get('Authorization') != f"Bearer {SECRET_TOKEN}":
        return Response("Unauthorized: Invalid or missing token", status=401, mimetype='text/plain')

    project_name = request.args.get('project_name')
    test_name = request.args.get('test_name')
    if not project_name or not test_name:
        return Response("Missing project_name or test_name", status=400, mimetype='text/plain')

    project_dir = os.path.join(PROJECTS_DIR, project_name)
    cmd = ["cargo", "test", "--test", test_name]
    result = subprocess.run(cmd, cwd=project_dir, capture_output=True, text=True)

    timestamp = int(time.time())
    status = "Success" if result.returncode == 0 else "Failed"
    logging.info(f"#INFO: Тест {test_name} для {project_name}: {status}")

    # Суммаризация логов
    errors = re.findall(r'error.*', result.stderr + result.stdout, re.IGNORECASE)
    summary = '\n'.join(errors[:5]) + ('\n... (сокращено)' if len(errors) > 5 else '')

    headers = {"X-Timestamp": str(timestamp), "X-Status": status}
    return Response(f"#post_{timestamp}: Результат теста: {summary}", headers=headers, mimetype='text/plain')

@app.route('/commit', methods=['POST'])
def commit():
    if not is_admin_ip() and request.headers.get('Authorization') != f"Bearer {SECRET_TOKEN}":
        return Response("Unauthorized: Invalid or missing token", status=401, mimetype='text/plain')

    data = request.get_json()
    if not data or 'project_name' not in data or 'msg' not in data:
        return Response("Missing project_name or msg", status=400, mimetype='text/plain')

    project_name = data['project_name']
    msg = data['msg']
    project_dir = os.path.join(PROJECTS_DIR, project_name)

    repo = git.Repo.init(project_dir)
    repo.index.add(['*'])
    repo.index.commit(msg)
    logging.info(f"#INFO: Коммит для {project_name}: {msg}")

    timestamp = int(time.time())
    headers = {"X-Timestamp": str(timestamp)}
    return Response(f"#post_{timestamp}: Коммит выполнен: {msg}", headers=headers, status=200, mimetype='text/plain')

def shutdown():
    logging.info("#INFO: Сервер остановлен")

atexit.register(shutdown)

if __name__ == "__main__":
    server_init()
    app.run(host="0.0.0.0", port=8084)
