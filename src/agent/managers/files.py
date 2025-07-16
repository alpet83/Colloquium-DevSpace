# /agent/managers/files.py, updated 2025-07-16 13:15 EEST
import logging
import globals
from .db import Database
from .project import ProjectManager


class FileManager:
    def __init__(self):
        self.db = Database.get_database()

    def get_file(self, file_id):
        row = self.db.fetch_one(
            'SELECT id, content, ts, file_name, project_id FROM attached_files WHERE id = :file_id',
            {'file_id': file_id}
        )
        if not row:
            logging.warning(f"Файл id={file_id} не найден")
            return None
        file_data = {'id': row[0], 'content': row[1], 'ts': row[2], 'file_name': row[3], 'project_id': row[4]}
        if not file_data['content']:  # Если контент пустой, подгружаем из проекта
            project_name = self.get_project_name(file_data['file_name'])
            if project_name:
                content = globals.project_manager.read_project_file(project_name, file_data['file_name'])
                if content:
                    file_data['content'] = content
        logging.debug(f"Получен файл id={file_id}, file_name={file_data['file_name']}, project_id={file_data['project_id']}")
        return file_data

    def add_file(self, content, file_name, timestamp, project_id=None):
        self.db.execute(
            'INSERT INTO attached_files (content, ts, file_name, project_id) VALUES (:content, :ts, :file_name, :project_id)',
            {'content': content, 'ts': timestamp, 'file_name': file_name, 'project_id': project_id}
        )
        row = self.db.fetch_one('SELECT last_insert_rowid()')
        file_id = row[0]
        logging.debug(f"Добавлен файл id={file_id}, file_name={file_name}, project_id={project_id}")
        return file_id

    def update_file(self, file_id, content, file_name, timestamp, project_id=None):
        self.db.execute(
            'UPDATE attached_files SET content = :content, file_name = :file_name, ts = :ts, project_id = :project_id WHERE id = :file_id',
            {'file_id': file_id, 'content': content, 'file_name': file_name, 'ts': timestamp, 'project_id': project_id}
        )
        logging.debug(f"Обновлён файл id={file_id}, file_name={file_name}, project_id={project_id}")

    def delete_file(self, file_id):
        self.db.execute(
            'DELETE FROM attached_files WHERE id = :file_id',
            {'file_id': file_id}
        )
        logging.debug(f"Удалён файл id={file_id}")

    def list_files(self, user_id, project_id=None):
        if project_id is not None:
            project_manager = ProjectManager.get(project_id)
            if not project_manager:
                logging.warning(f"Project id={project_id} not found, returning empty list")
                return []
            project_row = self.db.fetch_one(
                'SELECT project_name FROM projects WHERE id = :project_id',
                {'project_id': project_id}
            )
            if not project_row:
                logging.warning(f"Project id={project_id} not found in DB")
                return []
            project_name = project_row[0]
            # Сканируем файлы проекта
            project_files = project_manager.scan_project_files(project_name)
            # Синхронизируем с attached_files
            current_files = self.db.fetch_all(
                'SELECT id, file_name FROM attached_files WHERE project_id = :project_id',
                {'project_id': project_id}
            )
            current_file_names = {row[1] for row in current_files}
            project_file_names = {file['file_name'] for file in project_files}
            # Добавляем новые файлы
            for file in project_files:
                if file['file_name'] not in current_file_names:
                    self.add_file(
                        content=b'',
                        file_name=f"{project_name}/{file['file_name']}",
                        timestamp=file['ts'],
                        project_id=project_id
                    )
                    logging.debug(f"Added new file to attached_files: {project_name}/{file['file_name']}")
            # Удаляем отсутствующие файлы
            for file_id, file_name in current_files:
                relative_path = file_name.split('/', 1)[1] if '/' in file_name else file_name
                if relative_path not in project_file_names:
                    self.delete_file(file_id)
                    logging.debug(f"Deleted file from attached_files: {file_name}")
        # Возвращаем файлы из attached_files
        query = 'SELECT id, file_name, ts, project_id FROM attached_files'
        params = {}
        if project_id is not None:
            query += ' WHERE project_id = :project_id'
            params['project_id'] = project_id
        rows = self.db.fetch_all(query, params)
        files = [{'id': row[0], 'file_name': row[1], 'ts': row[2], 'project_id': row[3]} for row in rows]
        logging.debug(f"Возвращено {len(files)} файлов для user_id={user_id}, project_id={project_id}")
        return files

    def get_project_name(self, file_name):
        # Предполагаем, что file_name имеет формат <project_name>/<relative_path>
        parts = file_name.split('/', 1)
        return parts[0] if len(parts) > 1 else None