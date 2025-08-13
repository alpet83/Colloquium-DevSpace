# /mcp_server.py, updated 2025-07-19 13:15 EEST
from flask import Flask, request, Response
import os
import json
import re
import hashlib
import time
import toml
import socket
import git
import atexit
import lib.globals as g
from lib.execute_commands import execute
from lib.basic_logger import BasicLogger


app = Flask(__name__)

PROJECTS_DIR = "/app/projects"
CONFIG_PATH = "/app/data/mcp_config.toml"
SECRET_TOKEN = g.MCP_AUTH_TOKEN
LOG_FILE = "/app/logs/mcp_errors.log"
log = BasicLogger("mcp_server", "mcp-server")

def server_init():
    log_dir = "/app/logs"
    if not os.path.exists(log_dir):
        os.makedirs(log_dir)
    if not os.path.exists(LOG_FILE):
        open(LOG_FILE, 'w').close()

    global admin_ips, admin_subnet
    config = toml.load(CONFIG_PATH)
    admin_ips = config.get('security', {}).get('admin_ips', [])
    admin_subnet = config.get('security', {}).get('admin_subnet', '')

    local_ip = socket.gethostbyname(socket.gethostname())
    log.info(f"Сервер запущен на IP {local_ip}:8084")

    os.system("chown agent -R /app/projects")
    log.info("Установлены права для пользователя agent на /app/projects")

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

@app.route('/exec_commands', methods=['POST'])
def exec_commands():
    if not is_admin_ip() and request.headers.get('Authorization') != f"Bearer {SECRET_TOKEN}":
        log.error("Неавторизованный доступ: неверный или отсутствует токен")
        return Response("Unauthorized: Invalid or missing token", status=401, mimetype='text/plain')

    data = request.get_json()
    if not data or 'command' not in data or 'user_inputs' not in data or 'project_name' not in data:
        log.error("Отсутствуют command, user_inputs или project_name")
        return Response("Missing command, user_inputs or project_name", status=400, mimetype='text/plain')

    command = data['command']
    user_inputs = data['user_inputs']
    project_name = data['project_name']
    timeout = data.get('timeout', 300)

    if not project_name or not isinstance(project_name, str):
        log.error("Некорректное project_name: %s", str(project_name))
        return Response("Invalid or missing project_name", status=400, mimetype='text/plain')

    project_dir = os.path.join(PROJECTS_DIR, project_name)
    try:
        os.makedirs(project_dir, exist_ok=True)
    except Exception as e:
        log.excpt(f"Ошибка создания директории {project_dir}: {e}", exc_info=(type(e), e, e.__traceback__))
        return Response(f"Failed to create project directory: {e}", status=500, mimetype='text/plain')

    result = execute(command, user_inputs, "mcp_server", cwd=project_dir, timeout=timeout)

    timestamp = int(time.time())
    status = "Success" if result["status"] == "success" else "Failed"
    log.info(f"Команда {command} для {project_name}: {status}")

    headers = {"X-Timestamp": str(timestamp), "X-Status": status}
    return Response(f"#post_{timestamp}: Результат команды: {result['message']}", headers=headers,
                    mimetype='text/plain')

@app.route('/run_test', methods=['GET'])
def run_test():
    if not is_admin_ip() and request.headers.get('Authorization') != f"Bearer {SECRET_TOKEN}":
        log.error("Неавторизованный доступ: неверный или отсутствует токен")
        return Response("Unauthorized: Invalid or missing token", status=401, mimetype='text/plain')

    project_name = request.args.get('project_name')
    test_name = request.args.get('test_name')
    if not project_name or not test_name:
        log.error("Отсутствуют project_name или test_name")
        return Response("Missing project_name or test_name", status=400, mimetype='text/plain')

    project_dir = os.path.join(PROJECTS_DIR, project_name)
    cmd = f"cargo test --test {test_name}"
    result = execute(cmd, [], "mcp_server", cwd=project_dir)

    timestamp = int(time.time())
    status = "Success" if result["status"] == "success" else "Failed"
    log.info(f"Тест {test_name} для {project_name}: {status}")

    headers = {"X-Timestamp": str(timestamp), "X-Status": status}
    return Response(f"#post_{timestamp}: Результат теста: {result['message']}", headers=headers,
                    mimetype='text/plain')

@app.route('/commit', methods=['POST'])
def commit():
    if not is_admin_ip() and request.headers.get('Authorization') != f"Bearer {SECRET_TOKEN}":
        log.error("Неавторизованный доступ: неверный или отсутствует токен")
        return Response("Unauthorized: Invalid or missing token", status=401, mimetype='text/plain')

    data = request.get_json()
    if not data or 'project_name' not in data or 'msg' not in data:
        log.error("Отсутствуют project_name или msg")
        return Response("Missing project_name or msg", status=400, mimetype='text/plain')

    project_name = data['project_name']
    msg = data['msg']
    project_dir = os.path.join(PROJECTS_DIR, project_name)

    repo = git.Repo.init(project_dir)
    repo.index.add(['*'])
    repo.index.commit(msg)
    os.system(f"chown agent -R {project_dir}")
    log.info(f"Коммит для {project_name}: {msg}, права обновлены для agent")

    timestamp = int(time.time())
    headers = {"X-Timestamp": str(timestamp)}
    return Response(f"#post_{timestamp}: Коммит выполнен: {msg}", headers=headers, status=200, mimetype='text/plain')

@app.route('/ping')
def ping():
    return "pong"

def shutdown():
    log.info("Сервер остановлен")

atexit.register(shutdown)

if __name__ == "__main__":
    server_init()
    app.run(host="0.0.0.0", port=8084)
