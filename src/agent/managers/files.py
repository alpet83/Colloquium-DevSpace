# /agent/managers/files.py, updated 2025-07-26 19:45 EEST
import globals
import os
import time
import pwd
from pathlib import Path
from .db import Database, DataTable
from .project import ProjectManager
from lib.basic_logger import BasicLogger

log = globals.get_logger("fileman")


def _qfn(file_name, project_id: int):
    file_name = str(file_name).lstrip("@").lstrip("/")
    return globals.project_manager.locate_file(file_name, project_id)


def _mod_time(file_name: str, project_id: int):
    qfn = _qfn(file_name, project_id)
    return os.path.getmtime(qfn)


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
            if is_link and not self.link_valid(file_id) and project_id > 0:
                log.warn("Для проекта %3d, @%5d: %s  более не существует на диске", project_id, file_id, file_name)


    def _dedup(self, project_id: int = None):
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
                self.unlink(file_id[0])
                log.debug("Удалён дубликат файла: id=%d, file_name=%s, project_id=%s", file_id[0], file_name, str(proj_id))

    def exists(self, file_name, project_id=None, disk_check=True):
        file_id = self.find(file_name, project_id)
        if file_id is None:
            return False
        if not disk_check:
            log.debug("Ссылка на файл существует: %s, project_id=%s, id=%d", str(file_name), project_id, file_id)
            return True
        return self.link_valid(file_id)

    def find(self, file_name, project_id=None):
        conditions = {'file_name': f"@{file_name}"}
        if project_id is not None:
            conditions['project_id'] = project_id

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
        log.warn("link_valid: Не найден %d: %s ", file_id, qfn)
        return False

    def get_record(self, file_id: int, err_fmt: str = "Failed get_record for %d"):
        if file_id is None:
            raise FileExistsError("attempt get record for file_id = None")

        rec = self.files_table.select_row(
            conditions={'id': file_id},
            columns=['id', 'file_name', 'content', 'ts', 'project_id'],
        )
        if rec:
            return rec
        log.warn(err_fmt, file_id)
        return None

    def get_file(self, file_id: int):
        rec = self.get_record(file_id)
        if not rec:
            log.warn("Запись id=%d не найдена в attached_files", file_id)
            return None
        file_name = rec[1].lstrip('@')
        project_id = rec[4]
        file_data = {'id': rec[0], 'file_name': file_name, 'content': rec[2], 'ts': rec[3], 'project_id': project_id}
        content = None
        if file_data['content'] is None:
            file_path = _qfn(file_name, project_id)
            if not file_path.exists():
                log.warn("Реальный файл %s не существует", str(file_path))
                return None
            with file_path.open('r', encoding='utf-8') as f:
                content = f.read()

            if content is None:
                log.error("Ошибка считывания реального файла %s", file_name)
                return None
        else:
            content = file_data['content']

        file_data['content'] = globals.unitext(content)
        log.debug("Получен файл id=%d, file_name=%s, project_id=%s", file_id, file_name, str(project_id))
        return file_data

    def get_file_name(self, file_id: int):
        if file_id is None or file_id <= 0:
            raise FileExistsError("Not specified valid file_id")
        rec = self.get_record(file_id)
        if rec:
            file_name = rec[1].lstrip('@')
            project_id = rec[4]
            return file_name
        return None

    def add_file(self, file_name: str, content: str, timestamp=None, project_id=None):
        file_name = str(file_name).lstrip("@").lstrip("/")
        if len(file_name) > 300:
            raise ValueError(f"Слишком длинное имя файла {len(file_name)}")
        file_id = self.exists(file_name, project_id)
        if file_id:
            return file_id
        if timestamp is None:
            timestamp = int(time.time())

        if content:
            self.write_file(file_name, content, project_id)

        if timestamp is None:
            timestamp = _mod_time(file_name, project_id)

        effective_file_name = f"@{file_name}" if not content else file_name
        if project_id == 0:
            effective_file_name = f"@{file_name}"
        file_id = self.files_table.insert_into(
            values={
                'content': None,
                'ts': timestamp,
                'file_name': effective_file_name,
                'project_id': project_id
            },
            ignore=True
        )
        log.debug("Добавлен файл id=%d, file_name=%s, project_id=%s", file_id, file_name, str(project_id))
        return file_id

    def update_file(self, file_id: int, content: str, timestamp=None, project_id=None):
        if not self.link_valid(file_id):
            log.error("Файл id=%d не найден для обновления", file_id)
            return -1

        backup_path = self.backup_file(file_id)
        if not backup_path:
            log.error("Не удалось создать бэкап для file_id=%d", file_id)
            return -3

        file_name = self.get_file_name(file_id).lstrip('@')
        safe_path = _qfn(file_name, project_id)
        if content:
            try:
                wb = self.write_file(file_name, content, project_id)
                if wb < len(content):
                    log.error("Частично записано в файл %s, %d / %d", file_name, wb, len(content))
                    return -4
                os.chown(safe_path, pwd.getpwnam('agent').pw_uid, -1)
                log.debug("Установлен владелец agent для файла: %s", file_name)
                if timestamp is None:
                    timestamp = _mod_time(file_name, project_id)

            except Exception as e:
                log.excpt("Ошибка записи файла %s: ", file_name, e=e)
                return -6
        effective_file_name = f"@{file_name}" if project_id == 0 else file_name
        self.files_table.update(
            conditions={'id': file_id},
            values={
                'content': None,
                'file_name': effective_file_name,
                'ts': timestamp,
                'project_id': project_id
            }
        )
        log.debug("Обновлён файл id=%d, file_name=%s, project_id=%s", file_id, file_name, str(project_id))
        return file_id

    def move_file(self, file_id: int, new_name: str, project_id=None, overwrite: bool = False):
        if not self.link_valid(file_id):
            log.error("Файл id=%d не найден для переименования", file_id)
            return -1

        old_file_name = self.get_file_name(file_id).lstrip('@')
        new_name = str(new_name).lstrip("@").lstrip("/")
        old_path = _qfn(old_file_name, project_id)
        new_path = _qfn(new_name, project_id)

        log.debug("Checking if target file %s exists", new_name)
        existing_file_id = self.find(new_name, project_id)
        if existing_file_id and not overwrite:
            log.error("Целевой файл %s уже существует, file_id=%d", new_name, existing_file_id)
            return -2
        elif existing_file_id:
            log.debug("Target file %s exists, overwrite=%s, removing existing file_id=%d", new_name, overwrite, existing_file_id)
            self.unlink(existing_file_id)

        backup_path = self.backup_file(file_id)
        if not backup_path:
            log.error("Не удалось создать бэкап для file_id=%d перед переименованием", file_id)
            return -3

        try:
            os.makedirs(new_path.parent, exist_ok=True)
            os.rename(old_path, new_path)
            os.chown(new_path, pwd.getpwnam('agent').pw_uid, -1)
            log.debug("Moved file_id=%d from %s to %s", file_id, old_file_name, new_name)
        except Exception as e:
            log.excpt("Ошибка переименования файла id=%d с %s на %s: ", file_id, old_file_name, new_name, e=e)
            return -6

        effective_new_name = f"@{new_name}" if project_id == 0 else new_name
        self.files_table.update(
            conditions={'id': file_id},
            values={
                'file_name': effective_new_name,
                'ts': int(time.time()),
                'project_id': project_id
            }
        )
        log.debug("Updated file record id=%d, new_name=%s, project_id=%s", file_id, new_name, str(project_id))
        return file_id

    def unlink(self, file_id: int):
        if file_id > 0:
            self.files_table.delete_from(conditions={'id': file_id})
            log.debug("Удалена запись файла id=%d через unlink", file_id)

    def backup_file(self, file_id: int):
        proj_man = globals.project_manager
        proj_dir = proj_man.projects_dir
        if proj_dir is None:
            log.warn("Попытка бэкапа без выбранного проекта")
            return None

        row = self.files_table.select_row(
            conditions={'id': file_id},
            columns=['file_name', 'content', 'project_id'])
        log.debug("Запись файла для бэкапа: ~%s", str(row))
        if not row:
            log.warn("Файл id=%d не найден для бэкапа", file_id)
            return None
        try:
            file_name, content, project_id = row
        except ValueError as e:
            log.error("Ошибка распаковки результата запроса в backup_file: id=%d, row=%s, error=%s", file_id, str(row), str(e))
            return None
        if not content and not file_name.startswith('@'):
            log.warn("Файл id=%d не является ссылкой, бэкап невозможен", file_id)
            return None
        clean_file_name = file_name.lstrip('@')
        file_path = _qfn(clean_file_name, project_id)

        proj_dir = str(proj_dir)
        new_path = str(file_path).replace(proj_dir, proj_dir + '/backups/') + f".{int(time.time())}"
        log.debug("Formed backup path: %s", new_path)
        backup_path = Path(new_path)
        os.makedirs(backup_path.parent, exist_ok=True)
        try:
            os.chown(backup_path.parent, pwd.getpwnam('agent').pw_uid, -1)
            log.debug("Установлен владелец agent для папки бэкапа: %s", str(backup_path.parent))
        except Exception as e:
            log.excpt("Ошибка установки владельца для папки бэкапа %s: ", str(backup_path.parent), e=e)
        file = self.get_file(file_id)
        if file is None:
            log.warn("Файл %s не найден на диске для бэкапа", clean_file_name)
            return None
        content = file['content']
        if content is None:
            log.warn("backup failed: Нет контента для %s, нечего сохранить", clean_file_name)
            return None

        with open(backup_path, 'w', encoding='utf-8') as f:
            f.write(content)
        try:
            os.chown(backup_path, pwd.getpwnam('agent').pw_uid, -1)
            log.debug("Установлен владелец agent для бэкапа: %s", str(backup_path))
        except Exception as e:
            log.excpt("Ошибка установки владельца для бэкапа %s: ", str(backup_path), e=e)
        log.debug("Создан бэкап: %s", str(backup_path))
        return str(backup_path)

    def remove_file(self, file_id: int):
        row = self.files_table.select_from(
            conditions={'id': file_id},
            columns=['file_name', 'content', 'project_id'],
            limit=1
        )
        if not row:
            log.warn("Файл id=%d не найден для удаления", file_id)
            return
        file_name, content, project_id = row[0]
        if not content and not file_name.startswith('@'):
            log.warn("Запись id=%d не является ссылкой, удаление невозможно", file_id)
            return
        backup_path = self.backup_file(file_id)
        if backup_path and project_id > 0:
            clean_file_name = file_name.lstrip('@')
            file_path = _qfn(clean_file_name, project_id)
            if file_path.exists():
                file_path.unlink()
                log.debug("Удалён файл с диска: %s", str(file_path))
            self.unlink(file_id)

    def write_file(self, file_name, content, project_id=None):
        safe_path = _qfn(file_name, project_id)
        log.debug("Creating directory for file: %s", safe_path.parent)
        os.makedirs(safe_path.parent, exist_ok=True)
        try:
            os.chown(safe_path.parent, pwd.getpwnam('agent').pw_uid, -1)
            log.debug("Установлен владелец agent для папки: %s", str(safe_path.parent))
        except Exception as e:
            log.excpt("Ошибка установки владельца для папки %s: ", str(safe_path.parent), e=e)
        with safe_path.open('w', encoding='utf-8') as f:
            try:
                wb = f.write(content)
            except Exception as e:
                log.excpt("Ошибка записи в %s: ", str(safe_path), e=e)
                return 0
        try:
            os.chown(safe_path, pwd.getpwnam('agent').pw_uid, -1)
            log.debug("Установлен владелец agent для файла: %s", file_name)
        except Exception as e:
            log.excpt("Ошибка установки владельца для файла %s: ", file_name, e=e)
        if wb < len(content):
            log.error("Частично записано в файл %s, %d / %d", file_name, wb, len(content))
            return wb
        log.debug("Записано в файл %s, %d / %d", file_name, wb, len(content))
        return wb

    def list_files(self, user_id: int, project_id=None):
        self._dedup(project_id)
        if project_id is None:
            # log.debug("Запрошены все зарегистрированные файлы для user_id=%d", user_id)
            conditions = {}
        else:
            # log.debug("Запрошены файлы для project_id=%d, user_id=%d", project_id, user_id)
            conditions = {'project_id': project_id}
        query = 'SELECT id, file_name, ts, project_id FROM attached_files'
        if conditions:
            query += ' WHERE project_id = :project_id'
        # log.debug("Выполняется SQL-запрос: %s, conditions=%s", query, str(conditions))
        rows = self.db.fetch_all(query, conditions)
        # log.debug("Получено %d строк из attached_files", len(rows))
        files = []
        deleted = []
        for row in rows:
            file_id, file_name, ts, project_id = row
            clean_file_name = file_name.lstrip('@')
            if file_name.startswith('@'):
                file_path = _qfn(clean_file_name, project_id)
                if not file_path.exists():
                    log.warn("Имеется отсутствующая ссылка: id=%3d, project_id=%2d, qfn=%s",
                             file_id, project_id, str(file_path))
                    deleted.append(file_id)
                else:
                    files.append({'id': file_id, 'file_name': clean_file_name, 'ts': ts, 'project_id': project_id})
            else:
                files.append({'id': file_id, 'file_name': clean_file_name, 'ts': ts, 'project_id': project_id})
        if deleted:
            log.warn("Имеются отсутствующие файлы в attached_files: %s", deleted)
        return files