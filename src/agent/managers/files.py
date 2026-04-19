# /agent/managers/files.py, updated 2025-07-26 19:45 EEST
import globals
import os
import threading
import time
import pwd
from pathlib import Path
from contextlib import contextmanager
from .db import Database, DataTable
from .project import ProjectManager
from .runtime_config import get_float, get_int
from lib.basic_logger import BasicLogger
from lib.file_link_prefix import LEGACY_AT, REF, has_storage_prefix, sql_link_prefixed_params, store_storage_path, strip_storage_prefix
from lib.text_bytes import (
    decode_file_bytes,
    decode_known_text_bytes,
    encode_text_to_bytes,
    normalize_codec_name,
    normalize_eol_label,
)

log = globals.get_logger("fileman")

# Минимум строк выборки, после которого при verify_links логируем #PERF (только если notify_heavy_ops).
_PERF_VERIFY_LINKS_ROW_WARN = 80


def _mark_project_scan_stale(project_id: int, reason: str):
    try:
        ProjectManager.mark_scan_stale(project_id, reason=reason)
    except Exception as e:
        log.debug("Не удалось отметить scan stale для project_id=%s: %s", str(project_id), str(e))


def _qfn(file_name, project_id: int) -> Path:
    file_name = strip_storage_prefix(str(file_name)).lstrip("/")
    if project_id in (None, 0):
        pm = globals.project_manager
    else:
        pm = ProjectManager.get(project_id)
    if pm is None:
        pm = ProjectManager()
    return pm.locate_file(file_name, project_id)


def _mod_time(file_name: str, project_id: int):
    qfn = _qfn(file_name, project_id)
    return os.path.getmtime(qfn)


def _like_escape(value: str) -> str:
    """% _ \\ для шаблона LIKE с ESCAPE '\\' (PostgreSQL, SQLite)."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _calc_segments(path: str) -> int:
    """Сегментов в пути после снятия маркера ссылки (®/@) и нормализации слэшей (как в file_tree)."""
    s = strip_storage_prefix(str(path)).replace("\\", "/").strip("/")
    if not s:
        return 0
    return len([p for p in s.split("/") if p])


_seg_lock = threading.Lock()
_seg_ready = False
_seg_idx_ok = False


def _prefix_sql(path_prefix: str) -> tuple[str, dict]:
    """Фрагмент WHERE: ссылка ®path или @path под префиксом; плюс legacy без маркера."""
    pn = str(path_prefix).replace("\\", "/").strip("/")
    if not pn:
        return "", {}
    esc = _like_escape(pn)
    return (
        "("
        "(file_name LIKE :_tree_pfx_like_ref ESCAPE '\\' OR file_name = :_tree_pfx_eq_ref OR "
        "file_name LIKE :_tree_pfx_like_at ESCAPE '\\' OR file_name = :_tree_pfx_eq_at) OR "
        "("
        "file_name NOT LIKE :_pfxscan_ref AND file_name NOT LIKE :_pfxscan_at "
        "AND (file_name LIKE :_tree_pfx_like_plain ESCAPE '\\' OR file_name = :_tree_pfx_eq_plain)"
        ")"
        ")",
        {
            "_tree_pfx_like_ref": f"{REF}{esc}/%",
            "_tree_pfx_eq_ref": f"{REF}{pn}",
            "_tree_pfx_like_at": f"@{esc}/%",
            "_tree_pfx_eq_at": f"@{pn}",
            "_pfxscan_ref": f"{REF}%",
            "_pfxscan_at": "@%",
            "_tree_pfx_like_plain": f"{esc}/%",
            "_tree_pfx_eq_plain": pn,
        },
    )


class FileManager:
    """Хранилище ссылок на файлы проектов. Параметр ``notify_heavy_ops`` задаёт хост-приложение:
    при ``True`` (типично HTTP-ядро) при тяжёлых операциях (крупный ``file_index`` с ``verify_links``, далее — прочие
    долгие запросы к БД из этого менеджера) пишутся предупреждения ``#PERF``; в фоновых воркерах оставляют ``False``."""

    def resolve_disk_path(self, file_name: str, project_id) -> Path:
        """Абсолютный путь к файлу в дереве проекта (как для get_file)."""
        return _qfn(file_name, project_id)

    def is_acceptable_file(self, file_path: Path) -> bool:
        """Scan/index: см. lib.file_type_detector.is_acceptable_file (BL, SP WL, MIME, bytes_txt)."""
        from lib import file_type_detector

        return file_type_detector.is_acceptable_file(file_path)

    def __init__(self, *, notify_heavy_ops: bool = False):
        self.notify_heavy_ops = bool(notify_heavy_ops)
        self.db = Database.get_database()
        _ttl0 = get_int("FILE_LINK_TTL_MAX", 3, 1, 10_000)
        self.files_table = DataTable(
            table_name="attached_files",
            template=[
                "id INTEGER PRIMARY KEY AUTOINCREMENT",
                "content BLOB",
                "ts INTEGER",
                "file_name TEXT",
                "path_seg_count INTEGER",
                f"missing_ttl INTEGER DEFAULT {_ttl0}",
                "missing_checked_ts INTEGER DEFAULT 0",
                "project_id INTEGER",
                "file_encoding TEXT",
                "file_eol TEXT",
                "FOREIGN KEY (project_id) REFERENCES projects(id)"
            ]
        )
        self.spans_table = DataTable(
            table_name="file_spans",
            template=[
                "hash TEXT PRIMARY KEY",
                "file_id INTEGER",
                "meta_data JSON",
                "block_code TEXT",
                "FOREIGN KEY (file_id) REFERENCES attached_files(id)"
            ]
        )
        # Heavy link-validation pass is intentionally deferred to background startup task.
        # Keep constructor fast to avoid delaying API bind on cold boot.
        self._initialize_missing_ttl()

    def ensure_tree_segments(self) -> None:
        """Один раз на процесс: backfill колонки path_seg_count и индекс для срезов file_tree."""
        global _seg_ready, _seg_idx_ok
        if _seg_ready:
            return
        with _seg_lock:
            if _seg_ready:
                return
            batch = 8000
            updated = 0
            while True:
                rows = self.db.fetch_all(
                    f"SELECT id, file_name FROM attached_files WHERE path_seg_count IS NULL LIMIT {batch}"
                )
                if not rows:
                    break
                for fid, fn in rows:
                    c = _calc_segments(fn)
                    self.db.execute(
                        "UPDATE attached_files SET path_seg_count = :c WHERE id = :id",
                        {"c": c, "id": int(fid)},
                    )
                    updated += 1
            if updated:
                log.info("path_seg_count: заполнено столбцов для дерева файлов, строк=%d", updated)
            if not _seg_idx_ok:
                try:
                    self.db.execute(
                        "CREATE INDEX IF NOT EXISTS idx_attached_files_project_path_seg "
                        "ON attached_files (project_id, path_seg_count)"
                    )
                except Exception as e:
                    log.debug("CREATE INDEX path_seg_count: %s", e)
                _seg_idx_ok = True
            _seg_ready = True

    @property
    def missing_ttl_max(self) -> int:
        return get_int("FILE_LINK_TTL_MAX", 3, 1, 10_000)

    @property
    def missing_probe_cooldown_sec(self) -> int:
        return get_int("FILE_LINK_TTL_CHECK_COOLDOWN_SEC", 120, 0, 86_400)

    @property
    def check_coop_interval_sec(self) -> float:
        return get_float("FILE_CHECK_COOP_INTERVAL_SEC", 1.0, 0.0, 60.0)

    @property
    def check_coop_sleep_sec(self) -> float:
        return get_float("FILE_CHECK_COOP_SLEEP_SEC", 0.003, 0.0, 1.0)

    @staticmethod
    def _file_lock_key(file_name: str, project_id=None) -> str:
        try:
            qfn = _qfn(file_name, project_id)
            return f"file:{str(qfn.resolve())}"
        except Exception:
            return f"file:{project_id}:{str(file_name)}"

    @contextmanager
    def _file_lock(self, file_name: str, project_id=None):
        lock = globals.get_named_lock(self._file_lock_key(file_name, project_id))
        lock.acquire()
        try:
            yield
        finally:
            lock.release()

    def check(self):
        self._initialize_missing_ttl()
        coop_interval = self.check_coop_interval_sec
        coop_sleep = self.check_coop_sleep_sec
        next_coop_yield = time.monotonic() + coop_interval if coop_interval > 0 and coop_sleep > 0 else 0.0
        rows = self.files_table.select_from(
            columns=['id', 'file_name', 'content', 'project_id', 'ts']
        )
        for row in rows:
            if next_coop_yield and time.monotonic() >= next_coop_yield:
                # Cooperative yield to reduce long single-threaded startup monopolization.
                time.sleep(coop_sleep)
                next_coop_yield = time.monotonic() + coop_interval
            file_id, file_name, content, project_id, ts = row
            is_link = not content or has_storage_prefix(file_name)
            if is_link and not has_storage_prefix(file_name):
                fixed = store_storage_path(file_name)
                self.files_table.update(
                    conditions={'id': file_id},
                    values={'file_name': fixed, 'path_seg_count': _calc_segments(fixed)},
                )
                log.debug("Добавлен префикс ссылки ® для строки: id=%d, file_name=%s", file_id, file_name)
                file_name = fixed
            if is_link and project_id is not None:
                why, _ttl = self._link_reject_reason(file_id, mutate=True)
                if why is not None and why != "no_record":
                    self._log_check_reject(int(project_id), file_id, file_name, why)

    def _initialize_missing_ttl(self):
        try:
            self.db.execute(
                "UPDATE attached_files "
                "SET missing_ttl = :ttl "
                "WHERE missing_ttl IS NULL OR missing_ttl < 0",
                {'ttl': self.missing_ttl_max},
            )
            self.db.execute(
                "UPDATE attached_files "
                "SET missing_checked_ts = 0 "
                "WHERE missing_checked_ts IS NULL OR missing_checked_ts < 0",
                {},
            )
        except Exception as e:
            log.warn("Не удалось инициализировать TTL для attached_files: %s", str(e))

    def _set_missing_ttl(self, file_id: int, ttl: int, checked_ts: int | None = None):
        checked_value = int(time.time()) if checked_ts is None else int(checked_ts)
        self.db.execute(
            "UPDATE attached_files SET missing_ttl = :ttl, missing_checked_ts = :checked WHERE id = :id",
            {'ttl': max(0, min(int(ttl), self.missing_ttl_max)), 'checked': checked_value, 'id': int(file_id)},
        )

    def _mark_link_healthy(self, file_id: int):
        row = self.db.fetch_one(
            "SELECT COALESCE(missing_ttl, :ttl) FROM attached_files WHERE id = :id",
            {'id': int(file_id), 'ttl': self.missing_ttl_max},
        )
        prev_ttl = int(row[0]) if row else self.missing_ttl_max
        self._set_missing_ttl(file_id, self.missing_ttl_max)
        if prev_ttl < self.missing_ttl_max:
            log.info("TTL-recover link id=%d ttl=%d->%d", int(file_id), prev_ttl, self.missing_ttl_max)

    def _link_health_state(self, file_id: int, mutate: bool = True) -> tuple[bool, int]:
        row = self.db.fetch_one(
            "SELECT file_name, project_id, COALESCE(missing_ttl, :ttl), COALESCE(missing_checked_ts, 0) "
            "FROM attached_files WHERE id = :id",
            {'id': int(file_id), 'ttl': self.missing_ttl_max},
        )
        if not row:
            return False, 0

        file_name, project_id, missing_ttl, missing_checked_ts = row
        missing_ttl = max(0, min(int(missing_ttl), self.missing_ttl_max))
        missing_checked_ts = int(missing_checked_ts or 0)

        clean_name = strip_storage_prefix(str(file_name))
        file_path = _qfn(clean_name, project_id)
        path_exists = os.path.exists(file_path)

        # If TTL is degraded, keep link hidden until explicit rescan restores it.
        if path_exists and missing_ttl < self.missing_ttl_max:
            return False, missing_ttl

        if path_exists:
            return True, missing_ttl

        now_ts = int(time.time())
        should_degrade = mutate and (now_ts - missing_checked_ts >= self.missing_probe_cooldown_sec)
        if should_degrade and missing_ttl > 0:
            next_ttl = missing_ttl - 1
            self._set_missing_ttl(file_id, next_ttl, now_ts)
            log.warn("TTL-degrade link id=%d qfn=%s ttl=%d->%d", int(file_id), str(file_path), missing_ttl, next_ttl)
            missing_ttl = next_ttl
        elif mutate and should_degrade:
            self._set_missing_ttl(file_id, 0, now_ts)

        return False, missing_ttl

    def _link_reject_reason(self, file_id: int, mutate: bool = True) -> tuple[str | None, int]:
        """
        (причина, missing_ttl): причина None — ссылка ок.
        missing — нет пути на диске; filtered — не is_acceptable_file (BL/SP/MIME/эвристика);
        ttl_hidden — файл есть, но missing_ttl < max (порча TTL).
        _link_health_state смотрит только exists + TTL, не фильтр.
        """
        rec = self.get_record(file_id)
        if rec is None:
            return "no_record", 0
        is_valid, missing_ttl = self._link_health_state(file_id, mutate=mutate)
        qfn = _qfn(rec[1], rec[4])
        from lib.file_type_detector import is_acceptable_file

        if is_valid:
            if qfn.is_file() and not is_acceptable_file(qfn):
                return "filtered", missing_ttl
            return None, missing_ttl
        if qfn.exists() and qfn.is_file() and not is_acceptable_file(qfn):
            return "filtered", missing_ttl
        if qfn.exists() and qfn.is_file() and missing_ttl < self.missing_ttl_max:
            return "ttl_hidden", missing_ttl
        return "missing", missing_ttl

    def _log_check_reject(self, project_id: int, file_id: int, file_name: str, why: str) -> None:
        if why == "filtered":
            log.warn(
                "Для проекта %3d, @%5d: %s не проходит фильтрацию скана (BL/SP/MIME)",
                project_id,
                file_id,
                file_name,
            )
        elif why == "ttl_hidden":
            log.warn(
                "Для проекта %3d, @%5d: %s скрыт по TTL ссылки (ожидается восстановление при скане)",
                project_id,
                file_id,
                file_name,
            )
        elif why == "missing":
            log.warn("Для проекта %3d, @%5d: %s отсутствует на диске", project_id, file_id, file_name)
        # no_record: get_record уже залогировал при необходимости

    def _log_link_valid_reject(self, file_id: int, qfn: Path, why: str, missing_ttl: int) -> None:
        if why == "filtered":
            log.warn("link_valid: id=%d %s — не проходит фильтрацию скана (BL/SP/MIME)", file_id, qfn)
        elif why == "ttl_hidden":
            log.warn(
                "link_valid: id=%d %s — скрыт по TTL (ttl=%d)",
                file_id,
                qfn,
                missing_ttl,
            )
        elif why == "missing":
            log.warn("link_valid: id=%d %s — отсутствует на диске", file_id, qfn)

    def _dedup(self, project_id: int = None):
        conditions = {'project_id': project_id} if project_id is not None else {}
        query = 'SELECT COUNT(*) as count, file_name, project_id FROM attached_files'
        if conditions:
            query += ' WHERE project_id = :project_id'
        query += ' GROUP BY file_name, project_id HAVING COUNT(*) > 1'
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
        fn = str(file_name).lstrip("/")
        for stored in (store_storage_path(fn), LEGACY_AT + fn):
            conditions = {'file_name': stored}
            if project_id is not None:
                conditions['project_id'] = project_id
            row = self.files_table.select_row(columns=['id'], conditions=conditions)
            if row:
                return row[0]
        return None

    def link_valid(self, file_id: int):
        why, missing_ttl = self._link_reject_reason(file_id, mutate=True)
        if why is None:
            return True
        if why == "no_record":
            return False
        rec = self.get_record(file_id)
        if rec is None:
            return False
        qfn = _qfn(rec[1], rec[4])
        self._log_link_valid_reject(file_id, qfn, why, missing_ttl)
        return False

    def get_record(self, file_id: int, err_fmt: str = "Failed get_record for %d"):
        if file_id is None:
            raise FileExistsError("attempt get record for file_id = None")

        rec = self.files_table.select_row(
            conditions={'id': file_id},
            columns=['id', 'file_name', 'content', 'ts', 'project_id', 'file_encoding', 'file_eol'],
        )
        if rec:
            return rec
        log.warn(err_fmt, file_id)
        return None

    def _sync_disk_text_meta(self, file_id: int, encoding: str, eol: str) -> None:
        try:
            self.files_table.update(
                conditions={"id": int(file_id)},
                values={
                    "file_encoding": normalize_codec_name(encoding),
                    "file_eol": normalize_eol_label(eol),
                },
            )
        except Exception as e:
            log.debug("Не удалось сохранить file_encoding/file_eol id=%s: %s", file_id, e)

    def _resolve_write_text_format(self, file_id: int | None) -> tuple[str, str]:
        if file_id is None:
            return "utf-8", "lf"
        row = self.files_table.select_row(
            conditions={"id": int(file_id)},
            columns=["file_encoding", "file_eol"],
        )
        if not row:
            return "utf-8", "lf"
        fe, fol = row[0], row[1]
        return normalize_codec_name(fe), normalize_eol_label(fol)

    def get_file(self, file_id: int):
        rec = self.get_record(file_id)
        if not rec:
            log.warn("Запись id=%d не найдена в attached_files", file_id)
            return None
        file_name = strip_storage_prefix(rec[1])
        project_id = rec[4]
        file_enc_stored, file_eol_stored = rec[5], rec[6]
        file_data = {
            "id": rec[0],
            "file_name": file_name,
            "content": rec[2],
            "ts": rec[3],
            "project_id": project_id,
            "file_encoding": normalize_codec_name(file_enc_stored),
            "file_eol": normalize_eol_label(file_eol_stored),
        }
        content = None
        if file_data['content'] is None:
            file_path = _qfn(file_name, project_id)
            if not file_path.exists():
                log.warn("Реальный файл %s не существует", str(file_path))
                return None
            try:
                raw = file_path.read_bytes()
            except OSError as e:
                log.warn("Не удалось прочитать %s: %s", str(file_path), e)
                return None
            decoded = decode_file_bytes(raw)
            if decoded is None:
                log.debug("Файл не текст или бинарный, пропуск %s (file_id=%d)", file_name, file_id)
                return None
            content, src_enc, src_eol = decoded
            new_enc = normalize_codec_name(src_enc)
            new_eol = normalize_eol_label(src_eol)
            if (
                file_enc_stored is None
                or file_eol_stored is None
                or normalize_codec_name(file_enc_stored) != new_enc
                or normalize_eol_label(file_eol_stored) != new_eol
            ):
                self._sync_disk_text_meta(file_id, src_enc, src_eol)
            file_data["file_encoding"] = new_enc
            file_data["file_eol"] = new_eol
            if src_enc not in ("utf-8", "utf-8-sig"):
                log.debug("Файл %s декодирован как %s (file_id=%d)", file_name, src_enc, file_id)
        else:
            content_raw = file_data["content"]
            if isinstance(content_raw, bytes):
                content, enc_b, eol_b = decode_known_text_bytes(content_raw)
                if file_enc_stored is None and file_eol_stored is None:
                    self._sync_disk_text_meta(file_id, enc_b, eol_b)
                    file_data["file_encoding"] = normalize_codec_name(enc_b)
                    file_data["file_eol"] = normalize_eol_label(eol_b)
            else:
                content = content_raw

        file_data['content'] = globals.unitext(content)
        # log.debug("Получен файл id=%d, file_name=%s, project_id=%s", file_id, file_name, str(project_id))
        return file_data

    def get_file_name(self, file_id: int):
        if file_id is None or file_id <= 0:
            raise FileExistsError("Not specified valid file_id")
        rec = self.get_record(file_id)
        if rec:
            return strip_storage_prefix(rec[1])
        return None

    def add_file(self, file_name: str, content: str, timestamp=None, project_id=None):
        file_name = str(file_name).lstrip("/")
        if len(file_name) > 300:
            raise ValueError(f"Слишком длинное имя файла {len(file_name)}")
        # exists() returns bool, but add_file must work with numeric id.
        file_id = self.find(file_name, project_id=project_id)
        if file_id is not None:
            # Preserve file_id and re-activate degraded link during scan/update paths.
            self._mark_link_healthy(file_id)
            return file_id
        if timestamp is None:
            timestamp = int(time.time())

        if content:
            self.write_file(file_name, content, project_id)

        if timestamp is None:
            timestamp = _mod_time(file_name, project_id)

        file_id = self._add_link(file_name, project_id, timestamp)
        if file_id is None:
            # On PostgreSQL with conflict-ignore, insert may return None; reuse existing link id.
            file_id = self.find(file_name, project_id=project_id)
        _mark_project_scan_stale(project_id, reason='file_added')
        log.debug("Добавлен файл id=%s, file_name=%s, project_id=%s", str(file_id), file_name, str(project_id))
        return file_id

    def _add_link(self, file_name: str, project_id: int, timestamp):
        stored = store_storage_path(file_name)
        return self.files_table.insert_into(
            values={
                'content': None,
                'ts': timestamp,
                'file_name': stored,
                'path_seg_count': _calc_segments(stored),
                'missing_ttl': self.missing_ttl_max,
                'missing_checked_ts': int(time.time()),
                'project_id': project_id,
                'file_encoding': 'utf-8',
                'file_eol': 'lf',
            },
            ignore=True
        )

    def update_file(self, file_id: int, content: str, timestamp=None, project_id=None):
        if not self.link_valid(file_id):
            log.error("Файл id=%d не найден для обновления", file_id)
            return -1

        file_name = self.get_file_name(file_id)
        safe_path = _qfn(file_name, project_id)
        with self._file_lock(file_name, project_id):
            backup_path = self.backup_file(file_id)
            if not backup_path:
                log.error("Не удалось создать бэкап для file_id=%d", file_id)
                return -3

            if content:
                try:
                    wb = self.write_file(file_name, content, project_id, file_id=file_id)
                    if wb <= 0:
                        log.error("Запись в файл %s не выполнена (wb=%s)", file_name, wb)
                        return -4
                    os.chown(safe_path, pwd.getpwnam('agent').pw_uid, -1)
                    log.debug("Установлен владелец agent для файла: %s", file_name)
                    if timestamp is None:
                        timestamp = _mod_time(file_name, project_id)

                except Exception as e:
                    log.excpt("Ошибка записи файла %s   : ", file_name, e=e)
                    return -6

            stored = store_storage_path(file_name)
            self.files_table.update(
                conditions={'id': file_id},
                values={
                    'content': None,
                    'file_name': stored,
                    'path_seg_count': _calc_segments(stored),
                    'ts': timestamp,
                    'missing_ttl': self.missing_ttl_max,
                    'missing_checked_ts': int(time.time()),
                    'project_id': project_id
                }
            )
        _mark_project_scan_stale(project_id, reason='file_updated')
        log.debug("Обновлён файл id=%d, file_name=%s, project_id=%s", file_id, file_name, str(project_id))
        return file_id

    def move_file(self, file_id: int, new_name: str, project_id=None, overwrite: bool = False):
        if not self.link_valid(file_id):
            log.error("Файл id=%d не найден для переименования", file_id)
            return -1

        old_file_name = self.get_file_name(file_id)
        new_name = str(new_name).lstrip("/")
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

        # Lock both source and target path to avoid concurrent rename/write races.
        lock_keys = sorted({self._file_lock_key(old_file_name, project_id), self._file_lock_key(new_name, project_id)})
        lock_a = globals.get_named_lock(lock_keys[0])
        lock_b = globals.get_named_lock(lock_keys[1])
        lock_a.acquire()
        lock_b.acquire()
        try:
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

            effective_new_name = store_storage_path(new_name)
            self.files_table.update(
                conditions={'id': file_id},
                values={
                    'file_name': effective_new_name,
                    'path_seg_count': _calc_segments(effective_new_name),
                    'ts': int(time.time()),
                    'missing_ttl': self.missing_ttl_max,
                    'missing_checked_ts': int(time.time()),
                    'project_id': project_id
                }
            )
        finally:
            lock_b.release()
            lock_a.release()
        _mark_project_scan_stale(project_id, reason='file_moved')
        log.debug("Updated file record id=%d, new_name=%s, project_id=%s", file_id, new_name, str(project_id))
        return file_id

    def unlink(self, file_id: int):
        if file_id > 0:
            fid = int(file_id)
            self.db.execute("DELETE FROM file_spans WHERE file_id = :fid", {"fid": fid})
            self.files_table.delete_from(conditions={"id": fid})
            log.debug("Удалена запись файла id=%d через unlink", file_id)

    def purge_stale_attached_row(self, file_id: int, project_id: int) -> None:
        """Удалить строку @-ссылки после исчерпания missing_ttl (unlink: file_spans, затем attached_files)."""
        fid = int(file_id)
        if fid <= 0:
            return
        self.unlink(fid)
        log.debug(
            "purge_stale_attached_row: id=%d project_id=%s (TTL/stale purge)",
            fid,
            str(project_id),
        )
        _mark_project_scan_stale(int(project_id), reason="stale_link_ttl_purged")

    def backup_file(self, file_id: int):
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
        if project_id in (None, 0):
            proj_man = globals.project_manager
        else:
            proj_man = ProjectManager.get(project_id)
        if proj_man is None:
            proj_man = ProjectManager()
        proj_dir = proj_man.projects_dir
        if proj_dir is None:
            log.warn("Попытка бэкапа без выбранного проекта")
            return None
        if not content and not has_storage_prefix(file_name):
            log.warn("Файл id=%d не является ссылкой, бэкап невозможен", file_id)
            return None
        clean_file_name = strip_storage_prefix(file_name)
        with self._file_lock(clean_file_name, project_id):
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

            enc = file.get('file_encoding') or 'utf-8'
            eol = file.get('file_eol') or 'lf'
            raw, _ = encode_text_to_bytes(content, enc, eol)
            with open(backup_path, 'wb') as f:
                f.write(raw)
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
        if not content and not has_storage_prefix(file_name):
            log.warn("Запись id=%d не является ссылкой, удаление невозможно", file_id)
            return
        clean_file_name = strip_storage_prefix(file_name)
        with self._file_lock(clean_file_name, project_id):
            backup_path = self.backup_file(file_id)
            if backup_path and project_id > 0:
                file_path = _qfn(clean_file_name, project_id)
                if file_path.exists():
                    file_path.unlink()
                    log.debug("Удалён файл с диска: %s", str(file_path))
                self.unlink(file_id)
                _mark_project_scan_stale(project_id, reason='file_removed')

    def write_file(self, file_name, content, project_id=None, *, file_id: int | None = None):
        # file_name — относительный путь без маркера ®/@ (см. _qfn)
        enc, eol = self._resolve_write_text_format(file_id)
        raw, eff_enc = encode_text_to_bytes(content, enc, eol)
        with self._file_lock(file_name, project_id):
            safe_path = _qfn(file_name, project_id)
            log.debug("Creating directory for file: %s", safe_path.parent)
            os.makedirs(safe_path.parent, exist_ok=True)
            try:
                os.chown(safe_path.parent, pwd.getpwnam('agent').pw_uid, -1)
                log.debug("Установлен владелец agent для папки: %s", str(safe_path.parent))
            except Exception as e:
                log.excpt("Ошибка установки владельца для папки %s: ", str(safe_path.parent), e=e)
            try:
                with safe_path.open("wb") as f:
                    wb = f.write(raw)
            except Exception as e:
                log.excpt("Ошибка записи в %s: ", str(safe_path), e=e)
                return 0
            try:
                os.chown(safe_path, pwd.getpwnam('agent').pw_uid, -1)
                log.debug("Установлен владелец agent для файла: %s", file_name)
            except Exception as e:
                log.excpt("Ошибка установки владельца для файла %s: ", file_name, e=e)
            if wb < len(raw):
                log.error("Частично записано в файл %s, %d / %d байт", file_name, wb, len(raw))
                return wb
            if file_id is not None:
                try:
                    self.files_table.update(
                        conditions={"id": int(file_id)},
                        values={
                            "file_encoding": eff_enc,
                            "file_eol": normalize_eol_label(eol),
                        },
                    )
                except Exception as ex:
                    log.debug("Не обновили file_encoding после записи id=%s: %s", file_id, ex)
            log.debug("Записано в файл %s, %d байт (%s, %s)", file_name, wb, eff_enc, eol)
            return wb

    def list_files(self, project_id=None, sql_filter=None, as_map: bool = False):
        self._dedup(project_id)
        conditions = []
        if project_id is not None:
            conditions.append(('project_id', '=', project_id))
        if sql_filter:
            conditions.append(sql_filter)

        rows = self.files_table.select_from(columns=['id', 'file_name', 'ts', 'project_id'],
                                            conditions=conditions)
        files = {}
        orphaned = []
        for row in rows:
            file_id, file_name, ts, project_id = row
            clean_file_name = strip_storage_prefix(file_name)
            if has_storage_prefix(file_name):
                is_valid, missing_ttl = self._link_health_state(file_id, mutate=True)
                if not is_valid:
                    file_path = _qfn(clean_file_name, project_id)
                    log.warn("Имеется отсутствующая ссылка: id=%3d, project_id=%2d, qfn=%s",
                             file_id or -1, project_id or -1, str(file_path))
                    orphaned.append(f"{file_id}:ttl={missing_ttl}")
                    continue
            files[file_id] = {'id': file_id, 'file_name': clean_file_name, 'ts': ts, 'project_id': project_id}
        if orphaned:
            log.warn("Имеются отсутствующие файлы в attached_files: %s", orphaned)
        if as_map:
            return files
        else:
            return list(files.values())

    def file_index(
        self,
        project_id: int = None,
        modified_since: int = None,
        file_ids: list = None,
        include_size: bool = False,
        *,
        verify_links: bool = True,
        path_prefix: str | None = None,
        max_segments: int | None = None,
        min_segments: int | None = None,
        name_only: bool = False,
    ) -> list:
        """Return lightweight file index (no content). Supports combinable filters:
        - project_id: restrict to one project
        - modified_since: Unix timestamp, return only files with ts >= value
        - file_ids: list of specific IDs to return
        - include_size: if True, stat() each file for size_bytes (slower on Docker FS)
        - verify_links: if True (default), each @-link is checked on disk via _link_health_state (slow at scale).
        - path_prefix: optional relative path (no leading slash); restrict to rows under this directory prefix.
        - max_segments: абсолютная глубина пути (сегменты от корня репо); path_seg_count <= bound.
        - min_segments: path_seg_count > bound; для «глубоких» имён в ленивом дереве.
        - name_only: только file_name и project_id (без id/ts); для глубокого среза дерева.
        - При ``verify_links`` и ``self.notify_heavy_ops`` и большом числе строк — предупреждение ``#PERF`` в лог.
        """
        if name_only and include_size:
            include_size = False
        if name_only:
            verify_links = False
        clauses = ['1=1']
        params = {}
        if project_id is not None:
            clauses.append('project_id = :project_id')
            params['project_id'] = project_id
        if modified_since is not None:
            clauses.append('ts >= :modified_since')
            params['modified_since'] = modified_since
        if file_ids:
            safe_ids = ','.join(str(int(i)) for i in file_ids)  # int cast prevents injection
            clauses.append(f'id IN ({safe_ids})')
        clauses.append('COALESCE(missing_ttl, :missing_ttl_max) >= :missing_ttl_max')
        params['missing_ttl_max'] = self.missing_ttl_max
        if path_prefix:
            frag, extra = _prefix_sql(path_prefix)
            if frag:
                clauses.append(frag)
                params.update(extra)
        if max_segments is not None:
            clauses.append('(path_seg_count IS NULL OR path_seg_count <= :_max_seg)')
            params['_max_seg'] = int(max_segments)
        if min_segments is not None:
            clauses.append('(path_seg_count IS NOT NULL AND path_seg_count > :_min_seg)')
            params['_min_seg'] = int(min_segments)
        select_cols = "file_name, project_id" if name_only else "id, file_name, ts, project_id"
        query = (
            f"SELECT {select_cols} FROM attached_files "
            f"WHERE {' AND '.join(clauses)} ORDER BY file_name"
        )
        rows = self.db.fetch_all(query, params)
        result = []
        if name_only:
            for row in rows:
                file_name, proj_id = row
                clean_name = strip_storage_prefix(str(file_name))
                result.append({'id': None, 'file_name': clean_name, 'ts': 0, 'project_id': proj_id})
            return result
        if verify_links and self.notify_heavy_ops and len(rows) >= _PERF_VERIFY_LINKS_ROW_WARN:
            log.warn(
                "#PERF file_index(verify_links=ON): ожидается блокирующая проверка @-ссылок на диске/в БД "
                "(notify_heavy_ops=True); project_id=%s rows=%d — см. verify_links в managers/files.py",
                str(project_id),
                len(rows),
            )
        for row in rows:
            file_id, file_name, ts, proj_id = row
            clean_name = strip_storage_prefix(file_name)
            if verify_links and has_storage_prefix(file_name):
                is_valid, _ttl = self._link_health_state(file_id, mutate=True)
                if not is_valid:
                    continue
            entry = {'id': file_id, 'file_name': clean_name, 'ts': ts, 'project_id': proj_id}
            if include_size and has_storage_prefix(file_name):
                try:
                    fp = _qfn(clean_name, proj_id)
                    if fp.exists():
                        entry['size_bytes'] = fp.stat().st_size
                except Exception:
                    pass
            result.append(entry)
        return result

    def active_files_latest_ts(self, project_id: int) -> int | None:
        """Max ``ts`` among active (non-degraded TTL) links for a project — cheap vs full ``file_index``."""
        ttl_max = self.missing_ttl_max
        row = self.db.fetch_one(
            "SELECT MAX(ts) FROM attached_files WHERE project_id = :project_id "
            "AND COALESCE(missing_ttl, :ttl_max) >= :ttl_max",
            {"project_id": project_id, "ttl_max": ttl_max},
        )
        if not row or row[0] is None:
            return None
        return int(row[0])

    def ttl_status(self, project_id: int = None, sample_limit: int = 10) -> dict:
        ttl_max = self.missing_ttl_max
        sample_limit = max(1, min(int(sample_limit), 50))

        _frag, _fp = sql_link_prefixed_params()
        clauses = [_frag]
        params = {'ttl_max': ttl_max, **_fp}
        if project_id is not None:
            clauses.append('project_id = :project_id')
            params['project_id'] = project_id
        where_sql = ' AND '.join(clauses)

        summary_row = self.db.fetch_one(
            (
                "SELECT "
                "COUNT(*) AS total_links, "
                "SUM(CASE WHEN COALESCE(missing_ttl, :ttl_max) < :ttl_max THEN 1 ELSE 0 END) AS degraded_links, "
                "SUM(CASE WHEN COALESCE(missing_ttl, :ttl_max) = 0 THEN 1 ELSE 0 END) AS ttl_zero_links "
                "FROM attached_files "
                f"WHERE {where_sql}"
            ),
            params,
        )

        degraded_rows = self.db.fetch_all(
            (
                "SELECT id, file_name, project_id, COALESCE(missing_ttl, :ttl_max) AS ttl, "
                "COALESCE(missing_checked_ts, 0) AS checked_ts "
                "FROM attached_files "
                f"WHERE {where_sql} AND COALESCE(missing_ttl, :ttl_max) < :ttl_max "
                "ORDER BY ttl ASC, checked_ts ASC "
                f"LIMIT {sample_limit}"
            ),
            params,
        )

        problems = []
        for row in degraded_rows:
            file_id, file_name, proj_id, ttl, checked_ts = row
            problems.append(
                {
                    'id': int(file_id),
                    'file_name': strip_storage_prefix(str(file_name)),
                    'project_id': proj_id,
                    'missing_ttl': int(ttl),
                    'missing_checked_ts': int(checked_ts or 0),
                }
            )

        total_links = int(summary_row[0] or 0) if summary_row else 0
        degraded_links = int(summary_row[1] or 0) if summary_row else 0
        ttl_zero_links = int(summary_row[2] or 0) if summary_row else 0

        return {
            'ttl_max': ttl_max,
            'total_links': total_links,
            'degraded_links': degraded_links,
            'ttl_zero_links': ttl_zero_links,
            'sample_limit': sample_limit,
            'degraded_sample': problems,
        }

    def project_stats(self, project_id: int) -> dict:
        """Return file link counts and backup/undo stack stats for a project."""
        ttl_max = self.missing_ttl_max
        _psql, _pprm = sql_link_prefixed_params()
        row = self.db.fetch_one(
            "SELECT "
            "COUNT(*) AS total, "
            "SUM(CASE WHEN COALESCE(missing_ttl, :ttl) >= :ttl THEN 1 ELSE 0 END) AS active "
            "FROM attached_files WHERE project_id = :pid AND " + _psql,
            {'pid': project_id, 'ttl': ttl_max, **_pprm},
        )
        total_links = int(row[0] or 0) if row else 0
        active_links = int(row[1] or 0) if row else 0

        backup_count = 0
        backup_size = 0
        oldest_ts: int | None = None
        newest_ts: int | None = None

        pm = ProjectManager.get(project_id)
        if pm and pm.projects_dir and pm.project_name:
            backup_dir = Path(pm.projects_dir) / 'backups' / pm.project_name
            if backup_dir.exists():
                for f in backup_dir.rglob('*'):
                    if not f.is_file():
                        continue
                    backup_count += 1
                    try:
                        st = f.stat()
                        backup_size += st.st_size
                        ts_part = f.name.rsplit('.', 1)[-1]
                        if ts_part.isdigit():
                            ts = int(ts_part)
                            if oldest_ts is None or ts < oldest_ts:
                                oldest_ts = ts
                            if newest_ts is None or ts > newest_ts:
                                newest_ts = ts
                    except OSError:
                        pass

        return {
            'files': {
                'total_links': total_links,
                'active_links': active_links,
            },
            'backups': {
                'count': backup_count,
                'size_bytes': backup_size,
                'oldest_ts': oldest_ts,
                'newest_ts': newest_ts,
            },
        }

