# /agent/lib/basic_logger.py, updated 2025-07-18 14:28 EEST
import os
import time
import bz2
import tarfile
import sys
import logging
import traceback
from datetime import datetime, timedelta
from lib.esctext import colorize_msg, format_color, format_uncolor

class BasicLogger:
    # Уровни логирования
    ERROR = 1
    WARN = 2
    INFO = 3
    DEBUG = 4
    # Сопоставление строковых значений с числовыми
    VERBOSITY_MAP = {
        "ERROR": ERROR,
        "WARN": WARN,
        "INFO": INFO,
        "DEBUG": DEBUG
    }

    def __init__(self, sub_dir: str, prefix: str, stdout=None):
        self.sub_dir = sub_dir
        self.log_prefix = prefix
        self.std_out = stdout or sys.stdout
        self.lines = 0
        self.indent = ""
        self.last_msg = ""
        self.last_msg_t = 0.0
        self.size_limit = 300 * 1024 * 1024  # 300 MB
        self.file_name = ""
        self.real_name = ""
        self.log_fd = None
        self.last_create = 0
        self.log_dir = "/app/logs/"
        self.initializing = False  # Флаг для предотвращения рекурсии
        # Инициализация verbosity из переменной окружения
        verbosity_str = os.getenv("LOG_VERBOSITY", "DEBUG")
        try:
            # Пробуем интерпретировать как число
            self.verbosity = int(verbosity_str)
        except ValueError:
            # Если не число, проверяем строковое значение
            self.verbosity = self.VERBOSITY_MAP.get(verbosity_str.upper(), self.DEBUG)
        # Ограничиваем диапазон
        if self.verbosity < self.ERROR:
            self.verbosity = self.ERROR
        if self.verbosity > self.DEBUG:
            self.verbosity = self.DEBUG
        # Безопасное логирование инициализации через logging.info
        logging.info("Initialized logger %s with verbosity=%d", prefix, self.verbosity)

    def cleanup(self):
        prev_date = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        base = os.path.realpath(os.path.join(self.log_dir, self.sub_dir))
        path = os.path.join(base, prev_date)
        cwd = os.getcwd()
        if os.path.exists(path) and os.path.isdir(path):
            os.chdir(base)
            try:
                with tarfile.open(f"{prev_date}.tar.bz2", "w:bz2") as tar:
                    tar.add(prev_date)
                os.system(f"rm -rf {prev_date}")
            except Exception as e:
                self.log_msg("#EXCEPTION: Failed to archive %s: %s", path, str(e), echo=lambda x: print(x, file=sys.stderr))
            os.chdir(cwd)
        self.close("logger destruct")

    def log_filename(self, create_link=True):
        if self.file_name and os.path.exists(self.file_name):
            return self.file_name

        elps = time.time() - self.last_create
        if elps < 600:
            self.log_msg("~C91#WARN:~C00 log previously was created %d seconds ago. Renaming requested from %s",
                         int(elps), self._format_backtrace(), echo=lambda x: print(x, file=sys.stderr))

        base = os.path.realpath(os.path.join(self.log_dir, self.sub_dir))
        day_dir = datetime.now().strftime("%Y-%m-%d")
        path = os.path.join(base, day_dir)
        relative = os.path.join(".", self.sub_dir, day_dir)
        if not os.path.exists(path):
            os.makedirs(path, mode=0o770, exist_ok=True)

        # Ссылка на каталог текущего дня: /app/logs/<sub_dir>.td -> ./<sub_dir>/YYYY-MM-DD
        syml = os.path.join(self.log_dir, f"{self.log_prefix}.td")
        if self._make_symlink(relative, syml):
            path = syml

        result = os.path.join(path, f"{self.log_prefix}_{datetime.now().strftime('%H%M')}.log")
        try:
            with open(result, "wb") as f:
                f.write("☺".encode("utf-8"))
        except Exception as e:
            self.log_msg("#EXCEPTION: %s from %s", str(e), self._format_backtrace(), echo=lambda x: print(x, file=sys.stderr))

        # Ссылка на текущий лог-файл: /app/logs/<prefix>.log -> ./<sub_dir>/YYYY-MM-DD/<prefix>_HHMM.log
        relative = result.replace(self.log_dir, "./")
        symf = os.path.join(self.log_dir, f"{self.log_prefix}.log")
        self.real_name = result
        self.file_name = result

        if create_link:
            self._make_symlink(relative, symf)
            self.file_name = symf

        return result

    def _make_symlink(self, path, syml):
        import logging
        if os.path.islink(path):
            logging.warning("trying to create link for link %s, targeted to %s", path, os.readlink(path))
            os.unlink(path)
            return False

        if os.path.exists(syml):
            exist = os.readlink(syml) if os.path.islink(syml) else syml
            if os.path.isfile(path) or path != exist:
                logging.info("%s => %s", syml, exist)
                os.unlink(syml)
            else:
                return True

        logging.info("creating symbol link %s for %s", syml, path)
        attempts = 0
        while attempts < 5:
            try:
                if os.path.exists(syml):
                    os.unlink(syml)
                os.symlink(path, syml)
                if os.readlink(syml) == path:
                    logging.info("#LINKED: %s", syml)
                    break
                else:
                    logging.warning("#FAILED(%d): create symbol link %s, removing", attempts, syml)
                    os.unlink(syml)
            except Exception as e:
                logging.error("#FAILED(%d): create symbol link %s: %s", attempts, syml, str(e))
            attempts += 1
        return os.path.exists(syml)

    def file_size(self):
        if self.log_fd and self.log_fd.fileno() != -1:
            return self.log_fd.tell()
        if os.path.exists(self.real_name):
            return os.path.getsize(self.real_name)
        return 0

    def archive(self, size_above=1024*1024):
        if self.file_size() >= size_above:
            if self.log_fd and self.log_fd.fileno() != -1:
                self.log_fd.close()
                self.log_fd = None
            try:
                with open(self.real_name, "rb") as f_in, open(f"{self.real_name}.bz2", "wb") as f_out:
                    f_out.write(bz2.compress(f_in.read()))
                archive = f"{self.real_name}.bz2"
                msg = f"#COMPRESS_LOG: {self.real_name}"
                if os.path.exists(archive):
                    msg += f" archive size = {os.path.getsize(archive)}"
                    os.unlink(self.real_name)
                    if self.real_name != self.file_name:
                        os.unlink(self.file_name)
                    msg += ", removed original log and link"
                self.log_msg(msg)
                with open(os.path.join(self.log_dir, "compressed.log"), "a", encoding="utf-8") as f:
                    f.write(f"{self._tss()} {msg}\n")
                self.file_name = ""
            except Exception as e:
                self.log_msg("#EXCEPTION: Failed to compress %s: %s", self.real_name, str(e))
        elif os.path.islink(self.file_name):
            os.unlink(self.file_name)

    def close(self, reason):
        if self.log_fd and self.log_fd.fileno() != -1:
            self.log_fd.close()
            self.log_fd = None
        self.archive()
        # trace = self._format_backtrace()
        # self.log_msg("~C93#CLOSED_LOG:~C00 real name %s called due %s from %s", self.real_name, reason, trace)
        self.real_name = ""
        self.file_name = ""

    def _tss(self):
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]

    def _pr_time(self):
        return time.time()

    def _format_backtrace(self):
        import traceback
        return "".join(traceback.format_stack(limit=5)).strip()

    def log_msg(self, fmt, *args, echo=None):
        if not fmt or self.initializing:
            return
        self.initializing = True
        try:
            msg = format_color(fmt, *args)
            if not self.log_fd or (self.log_fd and self.log_fd.fileno() == -1):
                self.file_name = self.log_filename()
                self.log_fd = open(self.file_name, "ab")
                self.last_create = time.time()

            if len(msg) > 20480:
                msg = f"#TRUNCATED: {msg[:20480]}"

            ts = self._tss()
            elps = self._pr_time() - self.last_msg_t
            if "#PERF" in msg:
                ts += f" +{elps:5.2f} "

            self.last_msg = msg
            self.last_msg_t = self._pr_time()
            colored_msg = colorize_msg(msg)
            if self.log_fd:
                self.log_fd.write(f"[{ts}]. {self.indent}{msg}\n".encode("utf-8"))
                self.log_fd.flush()
            if callable(echo):
                echo(format_uncolor(fmt, *args))  # Только чистый текст для logging
            elif self.std_out:
                self.std_out.write(f"/{ts}/. {self.indent}{colored_msg}\n")
                self.std_out.flush()

            self.lines += msg.count("\n") + 1

            size = self.file_size()
            minute = datetime.now().minute
            huge_size = size > self.size_limit
            if huge_size or (minute == 0 and self.lines >= 15000):
                self.close(f"log size {size}, lines {self.lines}")
                self.file_name = self.log_filename()
                self.log_fd = open(self.file_name, "ab")
                msg = format_color("#LOG_ROTATE: %s reaches size %.1f MiB, check for flood", self.file_name, size / 1024 / 1024)
                if huge_size:
                    raise Exception(msg)
                else:
                    self.log_msg(msg, echo=echo)
        finally:
            self.initializing = False

    def debug(self, fmt, *args):
        if self.verbosity >= self.DEBUG:
            self.log_msg(f"~C93#DBG:~C00 {fmt}", *args, echo=logging.debug)

    def warn(self, fmt, *args):
        if self.verbosity >= self.WARN:
            self.log_msg(f"~C31#WARN:~C00 {fmt}", *args, echo=logging.warning)

    def info(self, fmt, *args):
        if self.verbosity >= self.INFO:
            self.log_msg(f"~C95#INFO:~C00 {fmt}", *args, echo=logging.info)

    def error(self, fmt, *args):
        self.log_msg(f"~C91#ERROR:~C00 {fmt}", *args, echo=logging.error)

    def excpt(self, fmt, *args, e=None, exc_info=None):
        if e:
            exc_info = (type(e), e, e.__traceback__)
        backtrace = "".join(traceback.format_exception(*exc_info)) if exc_info else self._format_backtrace()
        self.log_msg(f"~C91#EXCEPTION:~C00 {fmt}", *args, echo=logging.error)
        self.log_msg("~C31#TRACEBACK:~C00\n %s", backtrace)
