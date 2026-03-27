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
import uuid
import logging
import base64
import subprocess
import threading
import globals as g
from lib.execute_commands import execute
from lib.basic_logger import BasicLogger
from quart import Quart, request, Response
from typing import Optional, Dict, Any


def tss():
    return time.strftime('[%Y-%m-%d %H:%M:%S]')


def is_admin_ip():
    """Check if request is from admin IP (localhost or 127.0.0.1)."""
    try:
        client_ip = request.remote_addr or ""
        return client_ip in ['127.0.0.1', 'localhost', '::1']
    except:
        return False


# Process registry - in-memory store for spawned processes
PROCESS_REGISTRY: Dict[str, Dict[str, Any]] = {}
PROCESS_REGISTRY_LOCK = threading.Lock()  # Thread-safe lock for registry access
PROCESS_TTL_SECONDS = 3600  # 1 hour default
PROCESS_MAX_PER_PROJECT = 10
PROCESS_HARD_TIMEOUT = 7200  # 2 hours absolute max
PROCESS_IO_MAX_BYTES = 1048576  # 1 MB max buffer size
PROCESS_LOG_FILE = "/app/logs/mcp_processes.log"


def read_process_cpu_time_ms(pid: Optional[int]) -> Optional[int]:
    """Return process CPU time in milliseconds from /proc/<pid>/stat."""
    if not pid:
        return None

    try:
        with open(f"/proc/{pid}/stat", "r", encoding="utf-8") as f:
            line = f.read().strip()

        close_paren_idx = line.rfind(")")
        if close_paren_idx == -1:
            return None

        # Remaining fields start at state (field #3 in proc stat docs).
        rest = line[close_paren_idx + 2:].split()
        if len(rest) < 13:
            return None

        utime_ticks = int(rest[11])
        stime_ticks = int(rest[12])
        clk_tck = os.sysconf("SC_CLK_TCK")
        if clk_tck <= 0:
            return None

        return int(((utime_ticks + stime_ticks) * 1000) / clk_tck)
    except Exception:
        return None


def setup_process_logger():
    """Setup dedicated logger for process management."""
    logger = logging.getLogger("mcp_processes")
    logger.setLevel(logging.DEBUG)
    if not logger.handlers:
        fh = logging.FileHandler(PROCESS_LOG_FILE, encoding='utf-8')
        fh.setLevel(logging.DEBUG)
        formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
        fh.setFormatter(formatter)
        logger.addHandler(fh)
    return logger


PROCESS_LOGGER = setup_process_logger()


app = Quart(__name__)


PROJECTS_DIR = "/app/projects"
CONFIG_PATH = "/app/data/mcp_config.toml"
SECRET_TOKEN = g.MCP_AUTH_TOKEN or os.getenv('MCP_AUTH_TOKEN', 'default-test-token')
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


# ============================================================================
# Process Management Functions
# ============================================================================

async def spawn_process(
    project_id: int,
    command: str,
    engine: str = "bash",           # "bash" or "python"
    cwd: Optional[str] = None,
    env: Optional[Dict[str, str]] = None,
    timeout: int = 3600,
) -> str:
    """Spawn a subprocess and return process_guid (UUID)."""
    process_guid = str(uuid.uuid4())
    
    # Create process registry entry
    now = time.time()
    entry = {
        "process_guid": process_guid,
        "project_id": project_id,
        "command": command,
        "engine": engine,
        "cwd": cwd or os.getcwd(),
        "env": env or {},
        "subprocess": None,
        "pid": None,
        "stdin_lock": asyncio.Lock(),
        "stdout_buffer": b"",
        "stderr_buffer": b"",
        "exit_code": None,
        "cpu_time_ms": 0,
        "signal": None,
        "started_at": now,
        "finished_at": None,
        "last_io_ts": now,
        "ttl_seconds": timeout,
        "status": "starting",
    }
    
    with PROCESS_REGISTRY_LOCK:
        # Check per-project limit
        project_processes = [p for p in PROCESS_REGISTRY.values() if p["project_id"] == project_id]
        if len(project_processes) >= PROCESS_MAX_PER_PROJECT:
            msg = f"Max processes per project ({PROCESS_MAX_PER_PROJECT}) reached"
            PROCESS_LOGGER.warning(f"Cannot spawn: {msg} (project {project_id})")
            raise RuntimeError(msg)
        
        PROCESS_REGISTRY[process_guid] = entry
    
    # Spawn subprocess
    try:
        if engine == "bash":
            proc = await asyncio.create_subprocess_shell(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                stdin=subprocess.PIPE,
                cwd=entry["cwd"],
                env=entry["env"] if entry["env"] else None,
            )
        else:  # python
            proc = await asyncio.create_subprocess_exec(
                "python3", "-c", command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                stdin=subprocess.PIPE,
                cwd=entry["cwd"],
                env=entry["env"] if entry["env"] else None,
            )
        
        with PROCESS_REGISTRY_LOCK:
            PROCESS_REGISTRY[process_guid]["subprocess"] = proc
            PROCESS_REGISTRY[process_guid]["pid"] = proc.pid
            initial_cpu_ms = read_process_cpu_time_ms(proc.pid)
            if initial_cpu_ms is not None:
                PROCESS_REGISTRY[process_guid]["cpu_time_ms"] = initial_cpu_ms
            PROCESS_REGISTRY[process_guid]["status"] = "running"
        
        PROCESS_LOGGER.info(
            f"Process spawned: {process_guid} (project={project_id}, engine={engine}, cmd={command[:100]})"
        )
        
        # Start I/O reader task
        asyncio.create_task(_read_process_output(process_guid))
        
        return process_guid
    except Exception as e:
        with PROCESS_REGISTRY_LOCK:
            PROCESS_REGISTRY[process_guid]["status"] = "error"
            PROCESS_REGISTRY[process_guid]["stderr_buffer"] = str(e).encode()
        PROCESS_LOGGER.error(f"Failed to spawn {process_guid}: {e}")
        raise


async def _read_process_output(process_guid: str):
    """Background task to read stdout/stderr."""
    try:
        with PROCESS_REGISTRY_LOCK:
            entry = PROCESS_REGISTRY.get(process_guid)
            if not entry or not entry["subprocess"]:
                return
            proc = entry["subprocess"]
        
        while True:
            # Read with size limit per read
            try:
                stdout_chunk = await asyncio.wait_for(proc.stdout.read(4096), timeout=0.5)
                if stdout_chunk:
                    with PROCESS_REGISTRY_LOCK:
                        entry = PROCESS_REGISTRY.get(process_guid)
                        if entry:
                            # Limit total buffer size
                            if len(entry["stdout_buffer"]) < PROCESS_IO_MAX_BYTES:
                                entry["stdout_buffer"] += stdout_chunk
                                entry["last_io_ts"] = time.time()
            except asyncio.TimeoutError:
                pass
            except Exception:
                break
            
            # Check if process is done
            try:
                with PROCESS_REGISTRY_LOCK:
                    entry = PROCESS_REGISTRY.get(process_guid)
                    if not entry:
                        break
                    proc = entry["subprocess"]
                    if proc and proc.pid:
                        cpu_ms = read_process_cpu_time_ms(proc.pid)
                        if cpu_ms is not None:
                            entry["cpu_time_ms"] = cpu_ms
                
                retcode = proc.returncode
                if retcode is not None:
                    # Process finished
                    with PROCESS_REGISTRY_LOCK:
                        entry = PROCESS_REGISTRY[process_guid]
                        entry["exit_code"] = retcode
                        entry["status"] = "finished"
                        entry["finished_at"] = time.time()
                    PROCESS_LOGGER.info(f"Process {process_guid} exited with code {retcode}")
                    break
            except Exception:
                break
            
            await asyncio.sleep(0.1)
    except Exception as e:
        PROCESS_LOGGER.error(f"Error reading output for {process_guid}: {e}")


async def process_io(
    process_guid: str,
    input_data: Optional[bytes] = None,
    read_timeout_ms: int = 5000,
    max_bytes: int = 65536,
) -> Dict[str, Any]:
    """Read from and/or write to a process."""
    try:
        with PROCESS_REGISTRY_LOCK:
            entry = PROCESS_REGISTRY.get(process_guid)
            if not entry:
                raise ValueError(f"Process {process_guid} not found")
            
            proc = entry["subprocess"]
            is_alive = proc and proc.returncode is None
            
            result = {
                "process_guid": process_guid,
                "alive": is_alive,
                "exit_code": entry.get("exit_code"),
                "timestamp": time.time(),
                "stdout_fragment": b"",
                "stderr_fragment": b"",
            }
            
            # Write to stdin if provided
            if input_data and proc and entry["status"] == "running":
                try:
                    async with entry["stdin_lock"]:
                        proc.stdin.write(input_data)
                        await proc.stdin.drain()
                except Exception as e:
                    PROCESS_LOGGER.warning(f"Failed to write to stdin for {process_guid}: {e}")
            
            # Extract recent data from buffers
            stdout_buf = entry.get("stdout_buffer", b"")
            stderr_buf = entry.get("stderr_buffer", b"")
            
            # Return last max_bytes of each buffer
            result["stdout_fragment"] = stdout_buf[-max_bytes:] if stdout_buf else b""
            result["stderr_fragment"] = stderr_buf[-max_bytes:] if stderr_buf else b""
            
            # Update last_io_ts
            entry["last_io_ts"] = time.time()
        
        return result
    except Exception as e:
        PROCESS_LOGGER.error(f"Error during process_io for {process_guid}: {e}")
        raise


async def process_kill(process_guid: str, signal_name: str = "SIGTERM") -> Dict[str, Any]:
    """Terminate a process."""
    try:
        with PROCESS_REGISTRY_LOCK:
            entry = PROCESS_REGISTRY.get(process_guid)
            if not entry:
                raise ValueError(f"Process {process_guid} not found")
            
            proc = entry["subprocess"]
            if not proc or entry["status"] != "running":
                return {
                    "process_guid": process_guid,
                    "killed": False,
                    "reason": "Process not running",
                }
            
            # Send signal
            try:
                sig = getattr(asyncio.subprocess, signal_name, 15)  # default SIGTERM
                proc.send_signal(sig)
                PROCESS_LOGGER.info(f"Sent {signal_name} to process {process_guid}")
            except Exception as e:
                PROCESS_LOGGER.warning(f"Failed to send signal to {process_guid}: {e}")
            
            return {
                "process_guid": process_guid,
                "killed": True,
                "signal": signal_name,
            }
    except Exception as e:
        PROCESS_LOGGER.error(f"Error during process_kill for {process_guid}: {e}")
        raise


async def process_status(process_guid: str) -> Dict[str, Any]:
    """Get process status."""
    try:
        with PROCESS_REGISTRY_LOCK:
            entry = PROCESS_REGISTRY.get(process_guid)
            if not entry:
                raise ValueError(f"Process {process_guid} not found")
            
            proc = entry["subprocess"]
            is_alive = proc and proc.returncode is None

            # Refresh CPU sample for live processes.
            if is_alive and proc and proc.pid:
                cpu_ms = read_process_cpu_time_ms(proc.pid)
                if cpu_ms is not None:
                    entry["cpu_time_ms"] = cpu_ms

            now = time.time()
            finished_at = entry.get("finished_at")
            runtime_ms = int(((finished_at or now) - entry["started_at"]) * 1000)
            if runtime_ms < 0:
                runtime_ms = 0
            
            result = {
                "process_guid": process_guid,
                "status": entry["status"],
                "alive": is_alive,
                "exit_code": entry.get("exit_code"),
                "started_at": entry["started_at"],
                "finished_at": finished_at,
                "last_io_ts": entry.get("last_io_ts"),
                "command": entry.get("command", ""),
                "engine": entry.get("engine"),
                "pid": entry.get("pid"),
                "runtime_ms": runtime_ms,
                "cpu_time_ms": entry.get("cpu_time_ms", 0),
            }
            
            return result
    except Exception as e:
        PROCESS_LOGGER.error(f"Error during process_status for {process_guid}: {e}")
        raise


async def process_list(project_id: Optional[int] = None) -> list:
    """List all processes, optionally filtered by project_id."""
    try:
        with PROCESS_REGISTRY_LOCK:
            results = []
            for process_guid, entry in PROCESS_REGISTRY.items():
                if project_id and entry["project_id"] != project_id:
                    continue
                
                proc = entry.get("subprocess")
                is_alive = proc and proc.returncode is None
                
                results.append({
                    "process_guid": process_guid,
                    "project_id": entry["project_id"],
                    "status": entry["status"],
                    "alive": is_alive,
                    "command": entry.get("command", ""),
                    "exit_code": entry.get("exit_code"),
                    "started_at": entry["started_at"],
                })
            
            return results
    except Exception as e:
        PROCESS_LOGGER.error(f"Error during process_list: {e}")
        raise


async def process_wait(
    process_guid: str,
    wait_timeout_ms: int = 30000,
    wait_condition: str = "any_output",  # "any_output", "finished", "all_finished"
) -> Dict[str, Any]:
    """Wait for process condition (output or exit). Non-blocking with timeout."""
    start_time = time.time()
    timeout_s = wait_timeout_ms / 1000.0
    
    try:
        with PROCESS_REGISTRY_LOCK:
            entry = PROCESS_REGISTRY.get(process_guid)
            if not entry:
                raise ValueError(f"Process {process_guid} not found")
        
        while True:
            curr_time = time.time()
            elapsed = curr_time - start_time
            
            if elapsed > timeout_s:
                return {
                    "process_guid": process_guid,
                    "timeout": True,
                    "condition": wait_condition,
                }
            
            with PROCESS_REGISTRY_LOCK:
                entry = PROCESS_REGISTRY.get(process_guid)
                if not entry:
                    raise ValueError(f"Process {process_guid} not found")
                
                proc = entry["subprocess"]
                is_alive = proc and proc.returncode is None
                
                # Check condition
                if wait_condition == "any_output":
                    if entry["stdout_buffer"] or entry["stderr_buffer"]:
                        return {
                            "process_guid": process_guid,
                            "satisfied": True,
                            "condition": "any_output",
                            "stdout_size": len(entry["stdout_buffer"]),
                            "stderr_size": len(entry["stderr_buffer"]),
                        }
                elif wait_condition == "finished":
                    if not is_alive:
                        return {
                            "process_guid": process_guid,
                            "satisfied": True,
                            "condition": "finished",
                            "exit_code": entry.get("exit_code"),
                        }
            
            await asyncio.sleep(0.2)
    except Exception as e:
        PROCESS_LOGGER.error(f"Error during process_wait for {process_guid}: {e}")
        raise


async def cleanup_stale_processes():
    """Background task to clean up expired processes."""
    while True:
        try:
            await asyncio.sleep(60)  # Check every minute
            
            now = time.time()
            with PROCESS_REGISTRY_LOCK:
                to_delete = []
                for process_guid, entry in PROCESS_REGISTRY.items():
                    # Check hard timeout (absolute max lifetime)
                    if now - entry["started_at"] > PROCESS_HARD_TIMEOUT:
                        to_delete.append(process_guid)
                        PROCESS_LOGGER.warning(
                            f"Removing process {process_guid} due to hard timeout ({PROCESS_HARD_TIMEOUT}s)"
                        )
                        continue
                    
                    # Check TTL without recent I/O
                    if now - entry["last_io_ts"] > entry["ttl_seconds"]:
                        to_delete.append(process_guid)
                        PROCESS_LOGGER.warning(
                            f"Removing process {process_guid} due to inactivity (TTL={entry['ttl_seconds']}s)"
                        )
                
                for process_guid in to_delete:
                    entry = PROCESS_REGISTRY.pop(process_guid, None)
                    if entry and entry.get("subprocess"):
                        try:
                            entry["subprocess"].kill()
                        except:
                            pass
        except Exception as e:
            PROCESS_LOGGER.error(f"Error in cleanup_stale_processes: {e}")



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


# ============================================================================
# Process Management HTTP Endpoints
# ============================================================================

@app.route('/process/spawn', methods=['POST'])
async def process_spawn_endpoint():
    """Spawn a new subprocess."""
    if not is_admin_ip() and request.headers.get('Authorization') != f"Bearer {SECRET_TOKEN}":
        return Response("Unauthorized", status=401, mimetype='text/plain')
    
    try:
        data = await request.get_json()
        if not data:
            return Response(
                json.dumps({"error": "Invalid or missing JSON body"}),
                status=400,
                mimetype='application/json'
            )
        
        project_id = data.get('project_id')
        command = data.get('command')
        engine = data.get('engine', 'bash')
        cwd = data.get('cwd')
        env = data.get('env')
        timeout = data.get('timeout', 3600)
        
        if not project_id or not command:
            return Response("Missing project_id or command", status=400, mimetype='application/json')
        
        process_guid = await spawn_process(project_id, command, engine, cwd, env, timeout)
        return Response(
            json.dumps({"process_guid": process_guid, "status": "spawned"}),
            status=200,
            mimetype='application/json'
        )
    except Exception as e:
        return Response(
            json.dumps({"error": str(e)}),
            status=500,
            mimetype='application/json'
        )


@app.route('/process/io', methods=['POST'])
async def process_io_endpoint():
    """Read from/write to process."""
    if not is_admin_ip() and request.headers.get('Authorization') != f"Bearer {SECRET_TOKEN}":
        return Response("Unauthorized", status=401, mimetype='text/plain')
    
    data = await request.get_json()
    try:
        process_guid = data.get('process_guid')
        if not process_guid:
            return Response(
                json.dumps({"error": "Missing required argument: process_guid"}),
                status=400,
                mimetype='application/json'
            )
        input_data = data.get('input')
        read_timeout_ms = data.get('read_timeout_ms', 5000)
        max_bytes = data.get('max_bytes', 65536)
        
        if input_data and isinstance(input_data, str):
            input_data = input_data.encode()
        
        result = await process_io(process_guid, input_data, read_timeout_ms, max_bytes)
        
        # Base64 encode binary data for JSON
        result["stdout_fragment"] = base64.b64encode(result["stdout_fragment"]).decode()
        result["stderr_fragment"] = base64.b64encode(result["stderr_fragment"]).decode()
        
        return Response(json.dumps(result), status=200, mimetype='application/json')
    except Exception as e:
        return Response(
            json.dumps({"error": str(e)}),
            status=400,
            mimetype='application/json'
        )


@app.route('/process/kill', methods=['POST'])
async def process_kill_endpoint():
    """Terminate a process."""
    if not is_admin_ip() and request.headers.get('Authorization') != f"Bearer {SECRET_TOKEN}":
        return Response("Unauthorized", status=401, mimetype='text/plain')
    
    data = await request.get_json()
    try:
        process_guid = data.get('process_guid')
        if not process_guid:
            return Response(
                json.dumps({"error": "Missing required argument: process_guid"}),
                status=400,
                mimetype='application/json'
            )
        signal_name = data.get('signal', 'SIGTERM')
        
        result = await process_kill(process_guid, signal_name)
        return Response(json.dumps(result), status=200, mimetype='application/json')
    except Exception as e:
        return Response(
            json.dumps({"error": str(e)}),
            status=400,
            mimetype='application/json'
        )


@app.route('/process/status', methods=['GET'])
async def process_status_endpoint():
    """Get process status."""
    if not is_admin_ip() and request.headers.get('Authorization') != f"Bearer {SECRET_TOKEN}":
        return Response("Unauthorized", status=401, mimetype='text/plain')
    
    process_guid = request.args.get('process_guid')
    if not process_guid:
        return Response(
            json.dumps({"error": "Missing required query parameter: process_guid"}),
            status=400,
            mimetype='application/json'
        )
    try:
        result = await process_status(process_guid)
        return Response(json.dumps(result), status=200, mimetype='application/json')
    except Exception as e:
        return Response(
            json.dumps({"error": str(e)}),
            status=400,
            mimetype='application/json'
        )


@app.route('/process/list', methods=['GET'])
async def process_list_endpoint():
    """List processes."""
    if not is_admin_ip() and request.headers.get('Authorization') != f"Bearer {SECRET_TOKEN}":
        return Response("Unauthorized", status=401, mimetype='text/plain')
    
    project_id = request.args.get('project_id')
    try:
        project_id = int(project_id) if project_id else None
        results = await process_list(project_id)
        return Response(json.dumps({"processes": results}), status=200, mimetype='application/json')
    except Exception as e:
        return Response(
            json.dumps({"error": str(e)}),
            status=400,
            mimetype='application/json'
        )


@app.route('/process/wait', methods=['POST'])
async def process_wait_endpoint():
    """Wait for process condition."""
    if not is_admin_ip() and request.headers.get('Authorization') != f"Bearer {SECRET_TOKEN}":
        return Response("Unauthorized", status=401, mimetype='text/plain')
    
    data = await request.get_json()
    try:
        process_guid = data.get('process_guid')
        if not process_guid:
            return Response(
                json.dumps({"error": "Missing required argument: process_guid"}),
                status=400,
                mimetype='application/json'
            )
        wait_timeout_ms = data.get('wait_timeout_ms', 30000)
        wait_condition = data.get('wait_condition', 'any_output')
        
        result = await process_wait(process_guid, wait_timeout_ms, wait_condition)
        return Response(json.dumps(result), status=200, mimetype='application/json')
    except Exception as e:
        return Response(
            json.dumps({"error": str(e)}),
            status=400,
            mimetype='application/json'
        )


@app.route('/ping')

async def ping():
    return "pong"


def shutdown():
    log.info("Сервер остановлен")


atexit.register(shutdown)


async def run_server():
    """Main async server runner."""
    # Start background cleanup task
    asyncio.create_task(cleanup_stale_processes())
    PROCESS_LOGGER.info("Process cleanup task started")
    
    # Run Quart app
    await app.run_task(host="0.0.0.0", port=8084)


if __name__ == "__main__":
    server_init()
    asyncio.run(run_server())

