# /agent/managers/project.py, updated 2025-07-26 17:00 EEST
import os
from pathlib import Path
from .db import Database, DataTable
from lib.sandwich_pack import SandwichPack
from lib.basic_logger import BasicLogger
import globals

log = globals.get_logger("projectman")


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
        self.projects_table = DataTable(
            table_name="projects",
            template=[
                "id INTEGER PRIMARY KEY AUTOINCREMENT",
                "project_name TEXT NOT NULL UNIQUE",
                "description TEXT",
                "local_git TEXT",
                "public_git TEXT",
                "dependencies TEXT"
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
            columns=['id', 'project_name', 'description', 'local_git', 'public_git', 'dependencies'],
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
        self.scan_project_files()
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

    def update(self, project_name, description=None, local_git=None, public_git=None, dependencies=None):
        try:
            self.projects_table.update(
                conditions={'id': self.project_id},
                values={
                    'project_name': project_name,
                    'description': description,
                    'local_git': local_git,
                    'public_git': public_git,
                    'dependencies': dependencies
                }
            )
            self.project_name = project_name
            self.description = description
            self.local_git = local_git
            self.public_git = public_git
            self.dependencies = dependencies
            log.debug("Обновлён проект id=%d, project_name=%s", self.project_id, project_name)
        except Exception as e:
            log.excpt("Ошибка обновления проекта project_id=%d: %s", self.project_id, str(e))
            raise

    @staticmethod
    def get(project_id):
        import globals
        if project_id == getattr(globals.project_manager, 'project_id', None):
            log.debug("Возвращён глобальный ProjectManager для project_id=%d", project_id)
            return globals.project_manager
        row = DataTable(
            table_name="projects",
            template=[
                "id INTEGER PRIMARY KEY AUTOINCREMENT",
                "project_name TEXT NOT NULL UNIQUE",
                "description TEXT",
                "local_git TEXT",
                "public_git TEXT",
                "dependencies TEXT"
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
        return ProjectManager(project_id=project_id)

    def create_project(self, project_name, description='', local_git=None, public_git=None, dependencies=None):
        try:
            project_dir = self.projects_dir / project_name
            project_dir.mkdir(exist_ok=True)
            project_id = self.projects_table.insert_into(
                values={
                    'project_name': project_name,
                    'description': description,
                    'local_git': local_git,
                    'public_git': public_git,
                    'dependencies': dependencies
                }
            )
            log.info("Создан проект id=%d, project_name=%s", project_id, project_name)
            return project_id
        except Exception as e:
            log.excpt("Ошибка создания проекта project_name=%s: %s", project_name, str(e))
            raise

    def list_projects(self):
        rows = self.projects_table.select_from(
            columns=['id', 'project_name', 'description', 'local_git', 'public_git', 'dependencies'],
            conditions="id > 0"
        )
        projects = [
            {
                'id': row[0],
                'project_name': row[1],
                'description': row[2],
                'local_git': row[3],
                'public_git': row[4],
                'dependencies': row[5]
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
            project_dir = self.projects_dir / project_name
            if not project_dir.exists():
                log.warn("Директория проекта %s не существует", project_name)
                return []
            log.debug("Сканирование файлов проекта в %s", project_dir)
            files = []
            for file_path in project_dir.rglob('*'):
                # and not any(part.startswith('.') for part in file_path.parts)
                if file_path.is_file():
                    relative_path = str(file_path.relative_to(project_dir)).replace('\\', '/')
                    name = file_path.name   # for specific supported files like .env, .profile, .bashrc
                    extension = '.' + file_path.suffix.lower().lstrip('.') if file_path.suffix else ''
                    if not SandwichPack.supported_type(extension) and not SandwichPack.supported_type(name):
                        continue
                    files.append({
                        'file_name': relative_path,
                        'full_path': str(file_path),
                        'ts': int(file_path.stat().st_mtime)
                    })
                    file_mod = os.path.getmtime(file_path)
                    reg_path = Path(project_name) / relative_path
                    if self.project_name == project_name:
                        globals.file_manager.add_file(reg_path, None, file_mod, self.project_id)
            log.debug("Найдено %d поддерживаемых файлов в проекте %s", len(files), project_name)
            return files
        except Exception as e:
            log.excpt("Ошибка сканирования файлов проекта %s: %s", project_name, str(e))
            raise
