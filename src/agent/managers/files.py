# /agent/managers/files.py, updated 2025-07-18 22:08 EEST
import globals
import os
import time
from pathlib import Path
from .db import Database, DataTable
from .project import ProjectManager
from lib.basic_logger import BasicLogger

log = globals.get_logger("fileman")

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
        self.check()

    def check(self):
        """Сканирует attached_files, добавляет @ к ссылкам, проверяет файлы на диске, преобразует устаревшие ссылки в вложения."""
        rows = self.files_table.select_from(
            columns=['id', 'file_name', 'content', 'project_id', 'ts']
        )
        for row in rows:
            file_id, file_name, content, project_id, ts = row
            is_link = not content or file_name.startswith('@')
            if is_link and not file_name.startswith('@'):
                new_file_name = f"@{file_name}"
                self.files_table.update(
                    conditions={'id': file_id},
                    values={'file_name': new_file_name}
                )
                log.debug("Добавлен префикс @ для ссылки: id=%d, file_name=%s -> %s", file_id, file_name, new_file_name)
                file_name = new_file_name
            if is_link:
                clean_file_name = file_name.lstrip('@')
                content = globals.project_manager.read_project_file(clean_file_name)
                if content is None:
                    self.files_table.update(
                        conditions={'id': file_id},
                        values={
                            'file_name': clean_file_name,
                            'content': b'file was removed?',
                            'ts': ts
                        }
                    )
                    log.warn("Преобразована устаревшая ссылка в вложение: id=%d, file_name=%s, project_id=%s",
                             file_id, file_name, str(project_id))

    def _dedup(self, project_id=None):
        """Удаляет дубликаты файлов по file_name и project_id, сохраняя запись с минимальным id."""
        conditions = {'project_id': project_id} if project_id is not None else {}
        query = 'SELECT COUNT(*) as count, file_name, project_id FROM attached_files'
        if conditions:
            query += ' WHERE project_id = :project_id'
        query += ' GROUP BY file_name, project_id HAVING count > 1'
        duplicates = self.db.fetch_all(query, conditions)
        for count, file_name, proj_id in duplicates:
            file_ids = self.db.fetch_all(
                'SELECT id FROM attached_files WHERE file_name = :file_name AND project_id IS :project_id ORDER BY id',
                {'file_name': file_name, 'project_id': proj_id}
            )
            min_id = file_ids[0][0]
            for file_id in file_ids[1:]:
                self.files_table.unlink(conditions={'id': file_id[0]})
                log.debug("Удалён дубликат файла: id=%d, file_name=%s, project_id=%s", file_id[0], file_name, str(proj_id))

    def exists(self, file_name, project_id=None):
        """Проверяет существование файла по file_name и project_id, сначала для ссылки (@file_name), затем для вложения (file_name)."""
        conditions = {'file_name': f"@{file_name}"}
        if project_id is not None:
            conditions['project_id'] = project_id
        row = self.files_table.select_from(
            columns=['id'],
            conditions=conditions,
            limit=1
        )
        if row:
            log.debug("Ссылка на файл существует: file_name=%s, project_id=%s, id=%d", f"@{file_name}", str(project_id), row[0][0])
            return row[0][0]

        conditions = {'file_name': file_name}
        if project_id is not None:
            conditions['project_id': project_id
        row = self.files_table.select_from(
            columns=['id'],
            conditions=conditions,
            limit=1
        )
        if row:
            log.debug("Файл существует: file_name=%s, project_id=%s, id=%d", file_name, str(project_id), row[0][0])
            return row[0][0]

        log.debug("Файл не найден: file_name=%s, project_id=%s", file_name, str(project_id))
        return None

    def get_file(self, file_id):
        row = self.files_table.select_from(
            conditions={'id': file_id},
            columns=['id', 'content', 'ts', 'file_name', 'project_id'],
            limit=1
        )
        if not row:
            log.warn("Файл id=%d не найден", file_id)
            return None
        file_name = row[0][3].lstrip('@')
        file_data = {'id': row[0][0], 'content': row[0][1], 'ts': row[0][2], 'file_name': file_name,
                     'project_id': row[0][4]}
        if not file_data['content']:
            content = globals.project_manager.read_project_file(file_name)
            if content:
                file_data['content'] = content
        log.debug("Получен файл id=%d, file_name=%s, project_id=%s", file_id, file_name, str(file_data['project_id']))
        return file_data

    def add_file(self, content, file_name, timestamp, project_id=None):
        # Проверка безопасности пути
        try:
            safe_path = (Path('/app/projects') / file_name).resolve()
            if not str(safe_path).startswith('/app/projects'):
                log.error("Недопустимый путь файла: %s", file_name)
                raise ValueError("File path outside /app/projects")
        except Exception as e:
            log.excpt("Ошибка проверки пути файла %s: %s", file_name, str(e), exc_info=(type(e), e, e.__traceback__))
            raise

        file_id = self.exists(file_name, project_id)
        if file_id:
            log.debug("Файл file_name=%s, project_id=%s уже существует, id=%d", file_name, str(project_id), file_id)
            return file_id
        file_id = self.files_table.insert_into(
            values={
                'content': content,
                'ts': timestamp,
                'file_name': f"@{file_name}" if not content else file_name,
                'project_id': project_id
            },
            ignore=True
        )
        if content:
            globals.project_manager.write_file(file_name, content.decode('utf-8', errors='replace'))
        log.debug("Добавлен файл id=%d, file_name=%s, project_id=%s", file_id, file_name, str(project_id))
        return file_id

    def update_file(self, file_id, content, file_name, timestamp, project_id=None):
        row = self.files_table.select_from(
            conditions={'id': file_id},
            columns=['file_name', 'content'],
            limit=1
        )
        if not row:
            log.warn("Файл id=%d не найден для обновления", file_id)
            return
        old_file_name, old_content = row[0]
        is_link = not old_content or old_file_name.startswith('@')
        db_content = content
        if is_link:
            backup_path = self.backup_file(file_id)
            if not backup_path:
                log.error("Не удалось создать бэкап для file_id=%d", file_id)
                return
            clean_file_name = file_name.lstrip('@')
            # Проверка безопасности пути
            try:
                safe_path = (Path('/app/projects') / clean_file_name).resolve()
                if not str(safe_path).startswith('/app/projects'):
                    log.error("Недопустимый путь файла: %s", clean_file_name)
                    return
            except Exception as e:
                log.excpt("Ошибка проверки пути файла %s: %s", clean_file_name, str(e), exc_info=(type(e), e, e.__traceback__))
                return
            if content:
                try:
                    globals.project_manager.write_file(clean_file_name, content.decode('utf-8', errors='replace'))
                    log.debug("Записан файл на диск: %s", clean_file_name)
                    db_content = None  # Не дублируем контент для ссылок
                except Exception as e:
                    log.excpt("Ошибка записи файла %s: %s", clean_file_name, str(e), exc_info=(type(e), e, e.__traceback__))
                    return
        self.files_table.update(
            conditions={'id': file_id},
            values={
                'content': db_content,
                'file_name': f"@{file_name}" if is_link else file_name,
                'ts': timestamp,
                'project_id': project_id
            }
        )
        log.debug("Обновлён файл id=%d, file_name=%s, project_id=%s, is_link=%s", file_id, file_name, str(project_id), str(is_link))

    def unlink(self, file_id):
        """Удаляет запись из attached_files, не затрагивая файл на диске."""
        self.files_table.delete_from(conditions={'id': file_id})
        log.debug("Удалена запись файла id=%d", file_id)

    def backup_file(self, file_id):
        """Создаёт бэкап файла по file_id, если это ссылка (@file_name или пустой content)."""
        row = self.files_table.select_from(
            conditions={'id': file_id},
            columns=['file_name', 'content', 'project_id'],
            limit=1
        )
        log.debug("Запись файла для бэкапа: ~C95%s~C00", str(row))
        if not row or not row[0]:
            log.warn("Файл id=%d не найден для бэкапа", file_id)
            return None
        try:
            file_name, content, project_id = row[0]
        except ValueError as e:
            log.error("Ошибка распаковки результата запроса в backup_file: id=%d, row=%s, error=%s", file_id, str(row), str(e))
            return None
        if not content and not file_name.startswith('@'):
            log.warn("Файл id=%d не является ссылкой, бэкап невозможен", file_id)
            return None
        clean_file_name = file_name.lstrip('@')
        parts = clean_file_name.split('/', 1)
        relative_path = parts[1] if len(parts) > 1 else clean_file_name
        backup_path = f"/agent/projects/backups/{relative_path}.{int(time.time())}"
        os.makedirs(os.path.dirname(backup_path), exist_ok=True)
        content = globals.project_manager.read_project_file(clean_file_name)
        if content is None:
            log.warn("Файл %s не найден на диске для бэкапа", clean_file_name)
            return None
        with open(backup_path, 'wb') as f:
            f.write(content)
        log.debug("Создан бэкап: %s", backup_path)
        return backup_path

    def remove_file(self, file_id):
        """Удаляет ссылку из attached_files и перемещает файл в бэкап."""
        row = self.files_table.select_from(
            conditions={'id': file_id},
            columns=['file_name', 'content', 'project_id'],
            limit=1
        )
        if not row:
            log.warn("Файл id=%d не найден для удаления", file_id)
            return
        file_name, content, project_id = row
        if not content and not file_name.startswith('@'):
            log.warn("Файл id=%d не является ссылкой, удаление невозможно", file_id)
            return
        backup_path = self.backup_file(file_id)
        if backup_path:
            clean_file_name = file_name.lstrip('@')
            parts = clean_file_name.split('/', 1)
            project_name = parts[0] if len(parts) > 1 else None
            relative_path = parts[1] if len(parts) > 1 else clean_file_name
            file_path = Path('/app/projects') / project_name / relative_path
            if file_path.exists():
                file_path.unlink()
                log.debug("Удалён файл с диска: %s", str(file_path))
        self.unlink(file_id)

    def list_files(self, user_id, project_id=None):
        """Возвращает список файлов из attached_files, удаляя ссылки на отсутствующие файлы."""
        self._dedup(project_id)
        rows = self.files_table.select_from(
            conditions={'project_id': project_id} if project_id is not None else {},
            columns=['id', 'file_name', 'ts', 'project_id']
        )
        files = []
        deleted = []
        for row in rows:
            file_id, file_name, ts, project_id = row
            if file_name.startswith('@'):
                clean_file_name = file_name.lstrip('@')
                file_path = Path('/app/projects') / clean_file_name
                if not file_path.exists():
                    self.files_table.delete_from(conditions={'id': file_id})
                    deleted.append(file_name)
                    log.debug("Удалена отсутствующая ссылка: id=%d, file_name=%s", file_id, file_name)
                else:
                    files.append({'id': file_id, 'file_name': clean_file_name, 'ts': ts, 'project_id': project_id})
            else:
                files.append({'id': file_id, 'file_name': file_name, 'ts': ts, 'project_id': project_id})
        if deleted:
            log.warn("Удалены отсутствующие файлы из attached_files: ~C95%s~C00", deleted)
        log.debug("Возвращено %d файлов для user_id=%d, project_id=%s", len(files), user_id, str(project_id))
        return files