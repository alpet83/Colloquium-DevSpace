# /mcp_server.py, updated 2025-08-17

import os
import json
import re
import hashlib
import time
import toml
import socket
import git
import atexit
import asyncio
import globals as g
from lib.execute_commands import execute
from lib.basic_logger import BasicLogger
from quart import Quart, request, Response


def tss():
    return time.strftime('[%Y-%m-%d %H:%M:%S]')


app = Quart(__name__)


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
    print(tss() + "Server initialization finished...")


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
async def exec_commands():
    if not is_admin_ip() and request.headers.get('Authorization') != f"Bearer {SECRET_TOKEN}":
        log.error("Неавторизованный доступ: неверный или отсутствует токен")
        return Response("Unauthorized: Invalid or missing token", status=401, mimetype='text/plain')

    data = await request.get_json()
    if not data or ('command' not in data) or ('project_name' not in data):
        params = json.dumps(data)
        log.error("Отсутствуют cmd или project_name в параметрах: %s %s", str(type(data)), params)
        return Response(f"ERROR: Missing cmd or project_name in {params} ", status=400, mimetype='text/plain')

    cmd = data['command']
    project_name = data['project_name']
    user_inputs = data.get('user_inputs', [])
    project_dir = os.path.join(PROJECTS_DIR, project_name)
    print(tss() + f" starting {cmd} on project {project_name}... ")
    result = await execute(cmd, user_inputs, 'mcp_server', cwd=project_dir)

    timestamp = int(time.time())
    status = "Success" if result["status"] == "success" else "Failed"
    log.info(f"Команда {cmd} для {project_name}: {status}")

    headers = {"X-Timestamp": str(timestamp), "X-Status": status}
    return Response(f"#post_{timestamp}: Результат выполнения команды: {result['message']}", headers=headers,
                    mimetype='text/plain')

@app.route('/commit', methods=['POST'])
async def commit():
    if not is_admin_ip() and request.headers.get('Authorization') != f"Bearer {SECRET_TOKEN}":
        log.error("Неавторизованный доступ: неверный или отсутствует токен")
        return Response("Unauthorized: Invalid or missing token", status=401, mimetype='text/plain')

    data = await request.get_json()
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
async def ping():
    return "pong"


def shutdown():
    log.info("Сервер остановлен")


atexit.register(shutdown)

if __name__ == "__main__":
    server_init()
    asyncio.run(app.run_task(host="0.0.0.0", port=8084))
