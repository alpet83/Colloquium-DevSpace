# /agent/managers/project.py, updated 2025-07-26 17:00 EEST
import os
import re
import time
from pathlib import Path
from .db import Database, DataTable
from lib.sandwich_pack import SandwichPack
from lib.basic_logger import BasicLogger
import globals

log = globals.get_logger("projectman")

# Верхняя граница длительности одного прохода rglob+add_file (сек), с запасом до типичного HTTP-таймаута клиента.
_SCAN_BUDGET_DEFAULT_SEC = 25.0
_SCAN_BUDGET_MARGIN_SEC = 0.75


def _scan_budget_seconds() -> float:
    raw = os.environ.get("CQDS_SCAN_MAX_SECONDS", "").strip()
    if raw:
        try:
            return max(5.0, min(float(raw), 600.0))
        except ValueError:
            pass
    return _SCAN_BUDGET_DEFAULT_SEC


class ProjectManager:
    def __init__(self, project_id=None):
        self.db = Database.get_database()
        self.projects_dir = Path('/app/projects')
        self.project_id = project_id
        self.project_name = None
        self.description = None
        self.local_git = None
        self.public_git = None
        self.dependencies = None
        self.mcp_server_url = None
        self.projects_table = DataTable(
            table_name="projects",
            template=[
                "id INTEGER PRIMARY KEY AUTOINCREMENT",
                "project_name TEXT NOT NULL UNIQUE",
                "description TEXT",
                "local_git TEXT",
                "public_git TEXT",
                "dependencies TEXT",
                "mcp_server_url TEXT"
            ]
        )
        if project_id is not None:
            self.load()

    def load(self):
        if self.project_id is None:
            log.warn("Невозможно загрузить проект: project_id=%s", str(self.project_id))
            return
        row = self.projects_table.select_from(
            conditions={'id': self.project_id},
            columns=['id', 'project_name', 'description', 'local_git', 'public_git', 'dependencies', 'mcp_server_url'],
            limit=1
        )
        if not row:
            log.warn("Проект id=%d не найден", self.project_id)
            self.project_id = None
            return
        self.project_id = row[0][0]
        self.project_name = row[0][1]
        self.description = row[0][2]
        self.local_git = row[0][3]
        self.public_git = row[0][4]
        self.dependencies = row[0][5]
        self.mcp_server_url = row[0][6]
        # Keep load() lightweight: file-system scan is an explicit operation.
        log.debug("Загружен проект id=%d, project_name=%s", self.project_id, self.project_name)

    def abs_file_name(self, file_name: str, project_name: str):
        if file_name.startswith(project_name):
            return self.projects_dir / file_name
        else:
            return self.projects_dir / project_name / file_name

    def locate_file(self, file_name, project_id=None):
        """Возвращает Path для файла, используя project_name из БД, если project_id отличается."""
        base = self.projects_dir
        file_name = file_name.lstrip('@')
        default = self.abs_file_name(file_name, 'default')

        if project_id is None or project_id == self.project_id:
            if self.project_name is None:
                log.warn("project_name не установлен для project_id=%s", str(self.project_id))
                return default
            project_name = self.project_name
        elif project_id == 0:
            project_name = '.chat-meta'
        else:
            if project_id < 0:
                log.error("Недопустимый project_id=%d", project_id)
                return default / file_name
            row = self.projects_table.select_row(
                columns=['project_name'],
                conditions={'id': project_id}
            )
            if not row:
                log.warn("Проект id=%d не найден", project_id)
                return default
            project_name = row[0]

        file_path = self.abs_file_name(file_name, project_name)
        try:
            file_path = file_path.resolve()
            if not str(file_path).startswith(str(base)):
                log.error("Недопустимый путь файла: %s", str(file_path))
                return default / file_name
            return file_path
        except Exception as e:
            log.excpt("Ошибка получения пути файла %s: %s", file_name, str(e))
            return default / file_name

    @staticmethod
    def normalize_mcp_server_url(url):
        if url is None:
            return None
        val = str(url).strip()
        if not val:
            return None
        if not (val.startswith('http://') or val.startswith('https://')):
            val = 'http://' + val
        return val.rstrip('/')

    def update(self, project_name, description=None, local_git=None, public_git=None, dependencies=None, mcp_server_url=None):
        mcp_server_url = self.normalize_mcp_server_url(mcp_server_url)
        try:
            self.projects_table.update(
                conditions={'id': self.project_id},
                values={
                    'project_name': project_name,
                    'description': description,
                    'local_git': local_git,
                    'public_git': public_git,
                    'dependencies': dependencies,
                    'mcp_server_url': mcp_server_url
                }
            )
            self.project_name = project_name
            self.description = description
            self.local_git = local_git
            self.public_git = public_git
            self.dependencies = dependencies
            self.mcp_server_url = mcp_server_url
            log.debug("Обновлён проект id=%d, project_name=%s", self.project_id, project_name)
        except Exception as e:
            log.excpt("Ошибка обновления проекта project_id=%d: %s", self.project_id, str(e))
            raise

    @staticmethod
    def get(project_id):
        import globals
        if project_id is None:
            return globals.project_manager

        registry = getattr(globals, 'project_registry', None)
        if not isinstance(registry, dict):
            registry = {}
            globals.project_registry = registry

        if project_id == getattr(globals.project_manager, 'project_id', None):
            log.debug("Возвращён глобальный ProjectManager для project_id=%d", project_id)
            registry[project_id] = globals.project_manager
            return globals.project_manager

        cached = registry.get(project_id)
        if cached is not None and getattr(cached, 'project_id', None) == project_id:
            return cached

        row = DataTable(
            table_name="projects",
            template=[
                "id INTEGER PRIMARY KEY AUTOINCREMENT",
                "project_name TEXT NOT NULL UNIQUE",
                "description TEXT",
                "local_git TEXT",
                "public_git TEXT",
                "dependencies TEXT",
                "mcp_server_url TEXT"
            ]
        ).select_from(
            conditions={'id': project_id},
            columns=['id', 'project_name'],
            limit=1
        )
        if not row:
            log.warn("Проект id=%d не найден", project_id)
            return None
        log.debug("Создаётся новый ProjectManager для project_id=%d", project_id)
        manager = ProjectManager(project_id=project_id)
        registry[project_id] = manager
        return manager

    @staticmethod
    def mark_scan_stale(project_id: int, reason: str = "mutation"):
        if project_id is None or project_id <= 0:
            return
        state = getattr(globals, 'project_scan_state', None)
        if not isinstance(state, dict):
            state = {}
            globals.project_scan_state = state
        current = state.get(project_id, {})
        current.update({
            'project_id': project_id,
            'stale': True,
            'reason': reason,
            'updated_at': int(time.time()),
        })
        state[project_id] = current

    @staticmethod
    def mark_scan_fresh(project_id: int, files_count: int, duration_sec: float, time_limited: bool = False):
        if project_id is None or project_id <= 0:
            return
        state = getattr(globals, 'project_scan_state', None)
        if not isinstance(state, dict):
            state = {}
            globals.project_scan_state = state
        state[project_id] = {
            'project_id': project_id,
            'stale': False,
            'reason': None,
            'updated_at': int(time.time()),
            'files_count': int(files_count),
            'duration_sec': float(duration_sec),
            'scan_time_limited': bool(time_limited),
        }
        globals.bump_project_index_epoch(project_id)

    def create_project(self, project_name, description='', local_git=None, public_git=None, dependencies=None, mcp_server_url=None):
        mcp_server_url = self.normalize_mcp_server_url(mcp_server_url)
        try:
            project_dir = self.projects_dir / project_name
            project_dir.mkdir(exist_ok=True)
            project_id = self.projects_table.insert_into(
                values={
                    'project_name': project_name,
                    'description': description,
                    'local_git': local_git,
                    'public_git': public_git,
                    'dependencies': dependencies,
                    'mcp_server_url': mcp_server_url
                }
            )
            ProjectManager.mark_scan_stale(project_id, reason='project_created')
            log.info("Создан проект id=%d, project_name=%s", project_id, project_name)
            return project_id
        except Exception as e:
            log.excpt("Ошибка создания проекта project_name=%s: %s", project_name, str(e))
            raise

    def list_projects(self):
        rows = self.projects_table.select_from(
            columns=['id', 'project_name', 'description', 'local_git', 'public_git', 'dependencies', 'mcp_server_url'],
            conditions="id > 0"
        )
        projects = [
            {
                'id': row[0],
                'project_name': row[1],
                'description': row[2],
                'local_git': row[3],
                'public_git': row[4],
                'dependencies': row[5],
                'mcp_server_url': row[6]
            }
            for row in rows
        ]
        log.debug("Возвращено %d проектов", len(projects))
        return projects

    def scan_project_files(self, project_name=None):
        if project_name is None:
            project_name = self.project_name
        if project_name is None:
            return []

        try:
            started = time.monotonic()
            ignored_count = 0
            if not SandwichPack._block_classes:
                SandwichPack.load_block_classes()
            project_dir = self.projects_dir / project_name
            if not project_dir.exists():
                log.warn("Директория проекта %s не существует", project_name)
                return []
            log.debug("Сканирование файлов проекта в %s", project_dir)
            files = []
            ignore_file = project_dir / '.scan_ignore.txt'
            ignore_patterns = []
            if ignore_file.exists():
                with ignore_file.open('r') as f:
                    ignore_patterns = [line.strip() for line in f if line.strip() and not line.startswith('#')]
                log.debug("Загружены %d паттернов из .scan_ignore.txt для %s", len(ignore_patterns), project_name)

            budget = _scan_budget_seconds()
            deadline = started + max(0.0, budget - _SCAN_BUDGET_MARGIN_SEC)
            time_limited = False

            for file_path in project_dir.rglob('*'):
                if time.monotonic() >= deadline:
                    time_limited = True
                    log.warn(
                        "scan_project_files: достигнут лимит времени %.1fs (CQDS_SCAN_MAX_SECONDS), проект=%s, "
                        "поддерживаемых файлов=%d — остановка до обрыва запроса клиентом",
                        budget,
                        project_name,
                        len(files),
                    )
                    break

                try:
                    is_reg = file_path.is_file()
                except OSError as e:
                    log.warn("scan_project_files: пропуск (is_file) %s: %s", file_path, e)
                    continue
                if not is_reg:
                    continue

                try:
                    relative_path = str(file_path.relative_to(project_dir)).replace('\\', '/')
                except (OSError, ValueError) as e:
                    log.warn("scan_project_files: пропуск (relative_to) %s: %s", file_path, e)
                    continue

                ignore = False
                for pattern in ignore_patterns:
                    try:
                        if re.search(pattern, relative_path):
                            ignore = True
                            break
                    except re.error as e:
                        log.error("Некорректный regex паттерн '%s' в .scan_ignore.txt: %s", pattern, str(e))
                        continue
                if ignore:
                    ignored_count += 1
                    continue

                try:
                    name = file_path.name
                    extension = '.' + file_path.suffix.lower().lstrip('.') if file_path.suffix else ''
                    if not SandwichPack.supported_type(extension) and not SandwichPack.supported_type(name):
                        continue
                    st_mtime = int(file_path.stat().st_mtime)
                    files.append({
                        'file_name': relative_path,
                        'full_path': str(file_path),
                        'ts': st_mtime,
                    })
                    file_mod = os.path.getmtime(file_path)
                    reg_path = Path(project_name) / relative_path
                    if self.project_name == project_name:
                        globals.file_manager.add_file(reg_path, None, file_mod, self.project_id)
                except (OSError, ValueError) as e:
                    log.warn("scan_project_files: пропуск %s: %s", relative_path, e)
                except Exception as e:
                    log.warn("scan_project_files: пропуск %s: %s", relative_path, e)

            duration = time.monotonic() - started
            if duration >= 10:
                log.warn("PERF_WARN scan_project_files project_id=%s project_name=%s took=%.2fs files=%d ignored=%d",
                         str(self.project_id), project_name, duration, len(files), ignored_count)
            if time_limited:
                log.info(
                    "scan_project_files: проект=%s частичный проход (time_limited), повторите scan при необходимости",
                    project_name,
                )
            ProjectManager.mark_scan_fresh(self.project_id, len(files), duration, time_limited=time_limited)
            log.debug("Найдено %d поддерживаемых файлов в проекте %s, пропущено из-за фильтрации %d, duration=%.2fs",
                      len(files), project_name, ignored_count, duration)
            return files
        except Exception as e:
            log.excpt("Ошибка сканирования файлов проекта %s: ", project_name, e=e)
            raise