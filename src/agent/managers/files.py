# /agent/managers/files.py, updated 2025-07-17 18:23 EEST
import logging
import globals
import os
import time
from pathlib import Path
from .db import Database, DataTable
from .project import ProjectManager


class FileManager:
    def __init__(self):
        self.db = Database.get_database()
        self.files_table = DataTable(
            table_name="attached_files",
            template=[
                "id INTEGER PRIMARY KEY AUTOINCREMENT",
                "content BLOB",
                "ts INTEGER",
                "file_name TEXT",
                "project_id INTEGER",
                "FOREIGN KEY (project_id) REFERENCES projects(id)"
            ]
        )
        self.check()  # Проверяем целостность записей при инициализации

    def check(self):
        """Сканирует attached_files, добавляет @ к ссылкам, проверяет файлы на диске, преобразует устаревшие ссылки в вложения."""
        rows = self.files_table.select_from(
            columns=['id', 'file_name', 'content', 'project_id', 'ts']
        )
        for row in rows:
            file_id, file_name, content, project_id, ts = row
            is_link = not content or file_name.startswith('@')
            if is_link and not file_name.startswith('@'):
                # Добавляем префикс @ для ссылок
                new_file_name = f"@{file_name}"
                self.files_table.update(
                    conditions={'id': file_id},
                    values={'file_name': new_file_name}
                )
                logging.debug(f"Added @ prefix to link: id={file_id}, file_name={file_name} -> {new_file_name}")
                file_name = new_file_name
            if is_link:
                # Проверяем наличие файла на диске
                clean_file_name = file_name.lstrip('@')
                content = globals.project_manager.read_project_file(clean_file_name)
                if content is None:
                    # Файл отсутствует, преобразуем в вложение
                    self.files_table.update(
                        conditions={'id': file_id},
                        values={
                            'file_name': clean_file_name,
                            'content': b'file was removed?',
                            'ts': ts
                        }
                    )
                    logging.warning(
                        f"Converted stale link to attachment: id={file_id}, file_name={file_name}, project_id={project_id}")

    def _dedup(self, project_id=None):
        """Удаляет дубликаты файлов по file_name и project_id, сохраняя запись с минимальным id."""
        conditions = {'project_id': project_id} if project_id is not None else {}
        query = 'SELECT COUNT(*) as count, file_name, project_id FROM attached_files'
        if conditions:
            query += ' WHERE project_id = :project_id'
        query += ' GROUP BY file_name, project_id HAVING count > 1'
        duplicates = self.db.fetch_all(query, conditions)
        for count, file_name, proj_id in duplicates:
            # Находим все ID для file_name и project_id, сохраняем минимальный
            file_ids = self.db.fetch_all(
                'SELECT id FROM attached_files WHERE file_name = :file_name AND project_id IS :project_id ORDER BY id',
                {'file_name': file_name, 'project_id': proj_id}
            )
            min_id = file_ids[0][0]  # Минимальный ID
            for file_id in file_ids[1:]:  # Удаляем все ID, кроме минимального
                self.files_table.unlink(conditions={'id': file_id[0]})
                logging.debug(f"Deleted duplicate file: id={file_id[0]}, file_name={file_name}, project_id={proj_id}")

    def exists(self, file_name, project_id=None):
        """Проверяет существование файла по file_name и project_id, сначала для ссылки (@file_name), затем для вложения (file_name). Возвращает file_id или None."""
        # Сначала проверяем ссылку (@file_name)
        conditions = {'file_name': f"@{file_name}"}
        if project_id is not None:
            conditions['project_id'] = project_id
        row = self.files_table.select_from(
            conditions=conditions,
            columns=['id'],
            limit=1
        )
        if row:
            logging.debug(f"File link exists: file_name=@{file_name}, project_id={project_id}, id={row[0][0]}")
            return row[0][0]

        # Если ссылка не найдена, проверяем вложение (file_name)
        conditions = {'file_name': file_name}
        if project_id is not None:
            conditions['project_id'] = project_id
        row = self.files_table.select_from(
            conditions=conditions,
            columns=['id'],
            limit=1
        )
        if row:
            logging.debug(f"File exists: file_name={file_name}, project_id={project_id}, id={row[0][0]}")
            return row[0][0]

        logging.debug(f"File not found: file_name={file_name}, project_id={project_id}")
        return None

    def get_file(self, file_id):
        row = self.files_table.select_from(
            conditions={'id': file_id},
            columns=['id', 'content', 'ts', 'file_name', 'project_id'],
            limit=1
        )
        if not row:
            logging.warning(f"Файл id={file_id} не найден")
            return None
        file_name = row[0][3].lstrip('@')  # Удаляем префикс @ для возврата
        file_data = {'id': row[0][0], 'content': row[0][1], 'ts': row[0][2], 'file_name': file_name,
                     'project_id': row[0][4]}
        if not file_data['content']:  # Если контент пустой, подгружаем с диска
            content = globals.project_manager.read_project_file(file_name)
            if content:
                file_data['content'] = content
        logging.debug(f"Получен файл id={file_id}, file_name={file_name}, project_id={file_data['project_id']}")
        return file_data

    def add_file(self, content, file_name, timestamp, project_id=None):
        # Проверяем существование файла через exists
        file_id = self.exists(file_name, project_id)
        if file_id:
            logging.debug(f"Файл file_name={file_name}, project_id={project_id} уже существует, id={file_id}")
            return file_id
        file_id = self.files_table.insert_into(
            values={
                'content': content,
                'ts': timestamp,
                'file_name': f"@{file_name}" if not content else file_name,  # Добавляем префикс @ для пустого контента
                'project_id': project_id
            },
            ignore=True  # Дополнительная защита от дубликатов
        )
        logging.debug(f"Добавлен файл id={file_id}, file_name={file_name}, project_id={project_id}")
        return file_id

    def update_file(self, file_id, content, file_name, timestamp, project_id=None):
        self.files_table.update(
            conditions={'id': file_id},
            values={
                'content': content,
                'file_name': f"@{file_name}" if not content else file_name,  # Добавляем префикс @ для пустого контента
                'ts': timestamp,
                'project_id': project_id
            }
        )
        logging.debug(f"Обновлён файл id={file_id}, file_name={file_name}, project_id={file_id}")

    def unlink(self, file_id):
        """Удаляет запись из attached_files, не затрагивая файл на диске."""
        self.files_table.delete_from(conditions={'id': file_id})
        logging.debug(f"Unlinked file id={file_id}")

    def backup_file(self, file_id):
        """Создаёт бэкап файла по file_id, если это ссылка (@file_name или пустой content)."""
        row = self.files_table.select_from(
            conditions={'id': file_id},
            columns=['file_name', 'content', 'project_id'],
            limit=1
        )
        logging.debug(f"File record for backup: {row}")
        if not row or not row[0]:
            logging.warning(f"Файл id={file_id} не найден для бэкапа")
            return None
        try:
            file_name, content, project_id = row[0]
        except ValueError as e:
            logging.error(f"Ошибка распаковки результата запроса в backup_file: id={file_id}, row={row}, error={str(e)}")
            return None
        if not content and not file_name.startswith('@'):
            logging.warning(f"Файл id={file_id} не является ссылкой, бэкап невозможен")
            return None
        clean_file_name = file_name.lstrip('@')
        parts = clean_file_name.split('/', 1)
        relative_path = parts[1] if len(parts) > 1 else clean_file_name
        backup_path = f"/agent/projects/backups/{relative_path}.{int(time.time())}"
        os.makedirs(os.path.dirname(backup_path), exist_ok=True)
        content = globals.project_manager.read_project_file(clean_file_name)
        if content is None:
            logging.warning(f"Файл {clean_file_name} не найден на диске для бэкапа")
            return None
        with open(backup_path, 'wb') as f:
            f.write(content)
        logging.debug(f"Created backup: {backup_path}")
        return backup_path

    def remove_file(self, file_id):
        """Удаляет ссылку из attached_files и перемещает файл в бэкап."""
        row = self.files_table.select_from(
            conditions={'id': file_id},
            columns=['file_name', 'content', 'project_id'],
            limit=1
        )
        if not row:
            logging.warning(f"Файл id={file_id} не найден для удаления")
            return
        file_name, content, project_id = row
        if not content and not file_name.startswith('@'):
            logging.warning(f"Файл id={file_id} не является ссылкой, удаление невозможно")
            return
        # Создаём бэкап
        backup_path = self.backup_file(file_id)
        if backup_path:
            # Удаляем файл с диска
            clean_file_name = file_name.lstrip('@')
            parts = clean_file_name.split('/', 1)
            project_name = parts[0] if len(parts) > 1 else None
            relative_path = parts[1] if len(parts) > 1 else clean_file_name
            file_path = Path('/app/projects') / project_name / relative_path
            if file_path.exists():
                file_path.unlink()
                logging.debug(f"Removed file from disk: {file_path}")
        # Удаляем запись из attached_files
        self.unlink(file_id)

    def list_files(self, user_id, project_id=None):
        # Удаляем дубликаты перед синхронизацией
        self._dedup(project_id)
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
            current_files = self.files_table.select_from(
                conditions={'project_id': project_id},
                columns=['id', 'file_name']
            )
            current_file_names = {file_name.lstrip('@') for _, file_name in current_files}  # Удаляем префикс @
            project_file_names = {file['file_name'] for file in project_files}
            # Добавляем новые файлы
            for file in project_files:
                file_name = f"{project_name}/{file['file_name']}"
                if file_name not in current_file_names:
                    self.add_file(
                        content=b'',
                        file_name=file_name,
                        timestamp=file['ts'],
                        project_id=project_id
                    )
                    logging.debug(f"Added new file to attached_files: {file_name}")
            # Удаляем отсутствующие файлы
            for file_id, file_name in current_files:
                relative_path = file_name.lstrip('@').split('/', 1)[1] if '/' in file_name.lstrip(
                    '@') else file_name.lstrip('@')
                if relative_path not in project_file_names:
                    self.files_table.delete_from(conditions={'id': file_id})
                    logging.debug(f"Deleted file from attached_files: {file_name}")
        # Возвращаем файлы из attached_files
        conditions = {'project_id': project_id} if project_id is not None else {}
        rows = self.files_table.select_from(
            conditions=conditions,
            columns=['id', 'file_name', 'ts', 'project_id']
        )
        files = [{'id': row[0], 'file_name': row[1].lstrip('@'), 'ts': row[2], 'project_id': row[3]} for row in rows]
        logging.debug(f"Возвращено {len(files)} файлов для user_id={user_id}, project_id={project_id}")
        return files