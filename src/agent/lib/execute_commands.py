# /app/agent/lib/execute_commands.py, updated 2025-07-19 15:25 EEST
import subprocess
import os
import pwd
import select
import time
from lib.basic_logger import BasicLogger

log = BasicLogger("execute_commands", "exec_commands")
STDOUT_LOG_FILE = "/app/logs/exec.stdout"
STDERR_LOG_FILE = "/app/logs/exec.stderr"
EOL = "\n"
def tss():
    return time.strftime('%Y-%m-%d %H:%M:%S')

class StdX:
    """Управляет потоком stdout/stderr, индексом reads и списком строк."""
    def __init__(self, stream):
        self.stream = stream
        self.lines = []

    def fd(self):
        return self.stream.fileno()

    def read(self, fds, tag):
        """Считывает строку из потока, если она есть, возвращает True, если данные получены."""
        _fd = self.fd()
        if _fd in fds:
            line = self.stream.readline()
            if isinstance(line, str):
                if len(line) > 1:
                    self.lines.append(line)
                    log.debug("%s: '%s'", tag, line.strip("\n"))
                return line
        return False

    def __del__(self):
        """Автоматически закрывает поток."""
        if self.stream:
            self.stream.close()

    def store(self, file_path):
        """Записывает строки в лог-файл с временной меткой и командой."""
        with open(file_path, 'a') as log_file:
            if self.lines:
                log_file.write(EOL.join(self.lines) + EOL)
            log_file.close()

    def output(self, tag, max_lines=100, max_bytes=4096):
        ls = self.lines
        output = ls[-max_lines:] if len(ls) > max_lines else ls
        msg = EOL.join(output)
        if len(msg) > max_bytes:
            msg[:max_bytes]
            msg += "\n... (output truncated due to size limit)";
        return f"<{tag}>{msg}</{tag}>"

def execute(shell_command: str, user_inputs: list, user_name: str, cwd: str = '/app/projects',
            timeout: int = 300) -> dict:
    """Выполняет команду в указанном cwd от пользователя agent, возвращает ограниченный вывод в тегах."""
    if not shell_command:
        log.error("Пустая команда")
        return {"status": "error", "message": "<stdout>Error: Empty shell command</stdout>", "user_name": user_name}

    log.debug("Выполнение команды: %s, user_inputs=%s, timeout=%d, cwd=%s",
              shell_command[:50], user_inputs, timeout, cwd)
    script = '/app/projects/cmds.sh'
    msg = ''
    process = None
    try:
        # Создаём директорию для логов
        log_dir = os.path.dirname(STDOUT_LOG_FILE)
        if not os.path.exists(log_dir):
            os.makedirs(log_dir)
        # Создаём временный файл cmds.sh
        with open(script, 'w') as f:
            f.write(f"#!/bin/bash\n{shell_command}\n")
        os.chmod(script, 0o755)
        os.chown(script, pwd.getpwnam('agent').pw_uid, -1)
        log.debug("Создан скрипт %s с владельцем agent", script)

        # Запускаем команду
        cmd = ['su', 'agent', '-c', f'/app/projects/cmds.sh']
        process = subprocess.Popen(
            cmd,
            cwd=cwd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )
        _in = process.stdin
        _out = StdX(process.stdout)
        _err = StdX(process.stderr)
        reads = [_out.fd(), _err.fd()]
        start_time = time.time()
        active = True

        # Обрабатываем вывод и интерактивный ввод
        while active and (time.time() - start_time) < timeout:
            active = process.poll() is None
            fds = select.select(reads, [], [], 0.1)[0]
            line = _out.read(fds, 'stdout')
            if line:
                for user_input in user_inputs:
                    if user_input["rqs"] in line:
                        _in.write(user_input["ack"] + '\n')
                        _in.flush()
                        log.debug("Отправлен ввод для rqs=%s: %s", user_input["rqs"], user_input["ack"])

            err = _err.read(fds, 'stderr')
            active |= bool(line) or bool(err)
            print(f"[{tss()}] #EXEC: process is running: {active}\r")

        # Записываем полный вывод в логи
        _out.store(STDOUT_LOG_FILE)
        _err.store(STDERR_LOG_FILE)

        # Закрываем stdin и удаляем временный файл
        _in.close()
        if os.path.exists(script):
            os.unlink(script)
        # Формируем ответ с тегами <stdout> и <stderr>
        msg = _out.output('stdout')
        if _err.lines:
            msg += _err.output('stderr')

        log.info("Выполнение завершено: %s, код возврата=%d",
                 shell_command, process.returncode)

        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
            _in.close()
            if os.path.exists(script):
                os.unlink(script)
            log.error("Таймаут выполнения команды %s", shell_command)
            return {"status": "warn", "message": f"{msg}\nWarn: exec timed out", "user_name": user_name}

        errno = process.returncode
        if errno == 0:
            return {"status": "success", "message": msg, "user_name": user_name}
        return {"status": "error", "message": f"{msg}\nError: Command failed with code {errno}",
                "user_name": user_name}
    except Exception as e:
        log.excpt("Сбой выполнения команды %s: ", shell_command, e=e)
        if process:
            _in.close()
        if os.path.exists(script):
            os.unlink(script)
        return {"status": "error", "message": f"{msg}\nError: Failed to execute command: {str(e)}",
                "user_name": user_name}