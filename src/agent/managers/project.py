# /agent/managers/project.py, updated 2025-07-16 13:35 EEST
import logging
import os
from pathlib import Path
from .db import Database
from lib.sandwich_pack import SandwichPack

logging.basicConfig(level=logging.DEBUG, format='%(asctime)s %(levelname)s: %(message)s')

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
        self._create_tables()
        if project_id is not None:
            self.load()

    def _create_tables(self):
        logging.info("Создание таблицы projects")
        self.db.execute('''
            CREATE TABLE IF NOT EXISTS projects (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_name TEXT NOT NULL UNIQUE,
                description TEXT,
                local_git TEXT,
                public_git TEXT,
                dependencies TEXT
            )
        ''')

    def load(self):
        if self.project_id is None:
            logging.warning("Cannot load project: project_id is None")
            return
        row = self.db.fetch_one(
            'SELECT id, project_name, description, local_git, public_git, dependencies FROM projects WHERE id = :project_id',
            {'project_id': self.project_id}
        )
        if not row:
            logging.warning(f"Project id={self.project_id} not found")
            self.project_id = None
            return
        self.project_id = row[0]
        self.project_name = row[1]
        self.description = row[2]
        self.local_git = row[3]
        self.public_git = row[4]
        self.dependencies = row[5]
        logging.debug(f"Loaded project id={self.project_id}, project_name={self.project_name}")

    @staticmethod
    def get(project_id):
        import globals
        if project_id == getattr(globals.project_manager, 'project_id', None):
            logging.debug(f"Returning global ProjectManager for project_id={project_id}")
            return globals.project_manager
        db = Database.get_database()
        row = db.fetch_one(
            'SELECT id, project_name FROM projects WHERE id = :project_id',
            {'project_id': project_id}
        )
        if not row:
            logging.warning(f"Project id={project_id} not found")
            return None
        logging.debug(f"Creating new ProjectManager for project_id={project_id}")
        return ProjectManager(project_id=project_id)

    def create_project(self, project_name, description='', local_git=None, public_git=None, dependencies=None):
        try:
            project_dir = self.projects_dir / project_name
            project_dir.mkdir(exist_ok=True)
            self.db.execute(
                '''
                INSERT INTO projects (project_name, description, local_git, public_git, dependencies)
                VALUES (:project_name, :description, :local_git, :public_git, :dependencies)
                ''',
                {
                    'project_name': project_name,
                    'description': description,
                    'local_git': local_git,
                    'public_git': public_git,
                    'dependencies': dependencies
                }
            )
            project_id = self.db.fetch_one('SELECT last_insert_rowid()')[0]
            logging.info(f"Создан проект id={project_id}, project_name={project_name}")
            return project_id
        except Exception as e:
            logging.error(f"Ошибка создания проекта project_name={project_name}: {str(e)}")
            raise

    def list_projects(self):
        rows = self.db.fetch_all('SELECT id, project_name, description, local_git, public_git, dependencies FROM projects')
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
        logging.debug(f"Возвращено {len(projects)} проектов")
        return projects

    def scan_project_files(self, project_name):
        try:
            project_dir = self.projects_dir / project_name
            if not project_dir.exists():
                logging.warning(f"Директория проекта {project_name} не существует")
                return []
            files = []
            for file_path in project_dir.rglob('*'):
                if file_path.is_file() and not any(part.startswith('.') for part in file_path.parts):
                    relative_path = str(file_path.relative_to(project_dir)).replace('\\', '/')
                    extension = '.' + file_path.suffix.lower().lstrip('.') if file_path.suffix else ''
                    if not SandwichPack.supported_type(extension):
                        logging.debug(f"Skipping unsupported file: {relative_path}, extension: {extension}")
                        continue
                    files.append({
                        'file_name': relative_path,
                        'full_path': str(file_path),
                        'ts': int(file_path.stat().st_mtime)
                    })
            logging.debug(f"Найдено {len(files)} файлов в проекте {project_name}")
            return files
        except Exception as e:
            logging.error(f"Ошибка сканирования файлов проекта {project_name}: {str(e)}")
            raise

    def read_project_file(self, project_name, file_name):
        try:
            file_path = self.projects_dir / file_name
            if not file_path.exists():
                logging.warning(f"Файл {file_path} не существует")
                return None
            with file_path.open('rb') as f:
                content = f.read()
            logging.debug(f"Прочитан файл {file_name} из проекта {project_name}, размер={len(content)} байт")
            return content
        except Exception as e:
            logging.error(f"Ошибка чтения файла {file_name} из проекта {project_name}: {str(e)}")
            return None