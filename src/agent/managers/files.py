# /agent/managers/files.py, updated 2025-07-19 09:00 EEST
import globals
import os
import time
import pwd
from pathlib import Path
from .db import Database, DataTable
from .project import ProjectManager
from lib.basic_logger import BasicLogger

log = globals.get_logger("fileman")


def _qfn(file_name, project_id):
    file_name = str(file_name).lstrip("@").lstrip("/")
    return globals.project_manager.locate_file(file_name, project_id)


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
            if is_link and not self.link_valid(file_id):
                log.warn("%50s @ %d более не существует на диске, удаляется запись %d", file_name, project_id, file_id)
                self.unlink(file_id)

    def _dedup(self, project_id: int = None):
        """Удаляет дубликаты файлов по file_name и project_id, сохраняя запись с минимальным id."""
        if project_id is None:
            project_id = globals.project_manager.project_id
        if project_id is None:  # no project was selected
            return
        conditions = {'project_id': project_id}
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
                self.unlink(min_id)
                log.debug("Удалён дубликат файла: id=%d, file_name=%s, project_id=%s", file_id[0], file_name, str(proj_id))

    def exists(self, file_name, project_id=None, disk_check=True):
        file_id = self.find(file_name, project_id)
        if not disk_check:
            log.debug("Ссылка на файл существует: %40s, project_id=%s, id=%d", str(file_name), project_id, file_id)
            return True
        return self.link_valid(file_id)

    def find(self, file_name, project_id=None):   # возвращает file_id если файл подходит
        conditions = {'file_name': f"@{file_name}"}
        if project_id is None:
            conditions['project_id'] = globals.project_manager.project_id
        else:
            conditions['project_id'] = project_id  # излюбленное место для ошибки LLM?

        row = self.files_table.select_row(
            columns=['id'],
            conditions=conditions
        )
        return row[0] if row else None

    def link_valid(self, file_id: int):
        rec = self.get_record(file_id)
        if rec is None:
            return False
        file_name = rec[1]
        project_id = rec[4]
        qfn = _qfn(file_name, project_id)
        if os.path.exists(qfn):
            return True
        log.warn("link_valid: Не найден %5d: %s ", file_id, qfn)
        return False

    def get_record(self, file_id: int):
        records = self.files_table.select_from(
            conditions={'id': file_id},
            columns=['id', 'file_name', 'content', 'ts', 'project_id'],
            limit=1
        )
        if records:
            return records[0]
        log.error("Failed get_record for %d", file_id)
        return None
    
    def get_file(self, file_id: int):
        rec = self.get_record(file_id)
        if not rec:
            log.warn("Запись id=%d не найдена в attached_files", file_id)
            return None
        file_name = rec[1].lstrip('@')
        file_data = {'id': rec[0], 'file_name': file_name, 'content': rec[2], 'ts': rec[3], 'project_id': rec[4]}
        content = None
        if file_data['content'] is None:
            file_path = _qfn(file_name, file_data['project_id'])

            if not file_path.exists():
                log.warn("Реальный файл %s не существует", str(file_path))
                return None
            with file_path.open('r') as f:
                content = f.read()

            if content is None:
                log.error("Ошибка считывания реального файла %s", file_name)
                return None
        else:
            content = file_data['content']

        if isinstance(content, bytes):
            content = content.decode('utf-8', errors='replace')
        file_data['content'] = content
        log.debug("Получен файл id=%d, file_name=%s, project_id=%s", file_id, file_name, str(file_data['project_id']))
        return file_data
    
    def get_file_name(self, file_id):
        rec = self.get_record(file_id)
        if rec:
            return rec[0]
        return None

    def add_file(self, content, file_name, timestamp, project_id=None):
        file_name = str(file_name).lstrip("@").lstrip("/")
        # Проверка безопасности пути
        safe_path = _qfn(file_name, None)
        file_id = self.exists(file_name, project_id)
        if file_id:
            return file_id
        wb = 0
        if content:
            self.write_file(file_name, content, project_id)
        file_id = self.files_table.insert_into(
            values={
                'content': None,  # reserver for add_attach
                'ts': timestamp,
                'file_name': f"@{file_name}" if not content else file_name,
                'project_id': project_id
            },
            ignore=True
        )

        log.debug("Добавлен файл id=%d, file_name=%s, project_id=%s", file_id, file_name, str(project_id))
        return file_id

    def update_file(self, file_id: int, content, file_name, timestamp, project_id=None):
        row = self.files_table.select_from(
            conditions={'id': file_id},
            columns=['file_name', 'content'],
            limit=1
        )
        if not row:
            log.warn("Файл id=%d не найден для обновления", file_id)
            return -1
        old_file_name, old_content = row[0]
        if old_content:
            log.warn("Попытка обновить attach %d %s методом update_file", file_id, old_file_name)
            return -2

        backup_path = self.backup_file(file_id)
        if not backup_path:
            log.error("Не удалось создать бэкап для file_id=%d", file_id)
            return -3
        file_name = file_name.lstrip('@')
        safe_path = _qfn(file_name, None)  # for default project
        if content:
            try:
                self.write_file(file_name, content)
                # Устанавливаем владельца agent для обновлённого файла
                os.chown(safe_path, pwd.getpwnam('agent').pw_uid, -1)
                log.debug("Установлен владелец agent для файла: %s", file_name)
            except Exception as e:
                log.excpt("Ошибка записи файла %s: %s", file_name, str(e), e=e)
                return -6
        self.files_table.update(
            conditions={'id': file_id},
            values={
                'content': None,
                'file_name': f"@{file_name}",
                'ts': timestamp,
                'project_id': project_id
            }
        )
        log.debug("Обновлён файл id=%d, file_name=%s, project_id=%s", file_id, file_name, str(project_id))
        return file_id

    def unlink(self, file_id: int):
        """Удаляет запись из attached_files, не затрагивая файл на диске."""
        self.files_table.delete_from(conditions={'id': file_id})
        log.debug("Удалена запись файла id=%d", file_id)

    def backup_file(self, file_id: int):
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
        file = self.get_file(file_id)
        if file is None:
            log.warn("Файл %s не найден на диске для бэкапа", clean_file_name)
            return None
        content = file['content']
        if content is None:
            log.warn("backup failed: Нет контента для %s, нечего сохранить", clean_file_name)
            return None

        with open(backup_path, 'w') as f:
            f.write(content)
        # Устанавливаем владельца agent для бэкапа
        try:
            os.chown(backup_path, pwd.getpwnam('agent').pw_uid, -1)
            log.debug("Установлен владелец agent для бэкапа: %s", backup_path)
        except Exception as e:
            log.excpt("Ошибка установки владельца для бэкапа %s: %s", backup_path, str(e), exc_info=(type(e), e, e.__traceback__))
        log.debug("Создан бэкап: %s", backup_path)
        return backup_path

    def remove_file(self, file_id: int):
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
            log.warn("Запись id=%d не является ссылкой, удаление невозможно", file_id)
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

    def list_files(self, user_id: int, project_id=None):
        """Возвращает список файлов из attached_files, удаляя ссылки на отсутствующие файлы."""
        self._dedup(project_id)
        rows = self.files_table.select_from(
            columns=['id', 'file_name', 'ts', 'project_id'],
            conditions={'project_id': project_id} if project_id is not None else {}
        )
        files = []
        deleted = []
        for row in rows:
            file_id, file_name, ts, project_id = row
            if file_name.startswith('@'):
                clean_file_name = file_name.lstrip('@')
                file_path = _qfn(clean_file_name, project_id)
                if not file_path.exists():
                    # self.files_table.delete_from(conditions={'id': file_id})
                    # deleted.append(f"{file_id}:{file_name}:{project_id}")
                    log.warn("Имеется отсутствующая ссылка: id=%d, qfn=%s", file_id, str(file_path))
                else:
                    files.append({'id': file_id, 'file_name': clean_file_name, 'ts': ts, 'project_id': project_id})
            else:
                files.append({'id': file_id, 'file_name': file_name, 'ts': ts, 'project_id': project_id})
        if deleted:
            log.warn("Удалены отсутствующие файлы из attached_files: ~C95%s~C00", deleted)
        log.debug("Возвращено %d файлов для user_id=%d, project_id=%s", len(files), user_id, str(project_id))
        return files

    def write_file(self, file_name, content, project_id=None):
        safe_path = _qfn(file_name, project_id)
        safe_path.parent.mkdir(parents=True, exist_ok=True)
        with safe_path.open('w', encoding='utf-8') as f:
            try:
                wb = f.write(content)
            except Exception as E:
                log.excpt("Ошибка записи в %s: %s", str(safe_path), str(E), e=E)

        # Устанавливаем владельца agent для нового файла
        try:
            os.chown(safe_path, pwd.getpwnam('agent').pw_uid, -1)
            log.debug("Установлен владелец agent для файла: %s", file_name)
        except Exception as e:
            log.excpt("Ошибка установки владельца для файла %s: %s", file_name, str(e), e=e)
        if wb < len(content):
            log.error("Частично записано в файл %s, %d / %d", file_name, wb, len(content))
            return wb
        log.debug("Записано в файл %s, %d / %d", file_name, wb, len(content))

        return wb
