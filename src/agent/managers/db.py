# /agent/managers/db.py, updated 2025-07-18 14:39 EEST
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError
from lib.basic_logger import BasicLogger
import globals

log = globals.get_logger("db")

class Database:
    _instance = None

    @classmethod
    def get_database(cls):
        if cls._instance is None:
            cls._instance = Database()
        return cls._instance

    def __init__(self):
        self.engine = create_engine('sqlite:////app/data/multichat.db', echo=False)
        self._init_tables()
        log.info("Инициализирована БД: %s", str(self.engine.url))

    def _init_tables(self):
        self.execute("""
            CREATE TABLE IF NOT EXISTS user_settings (
                user_id INTEGER PRIMARY KEY,
                search_mode TEXT DEFAULT 'auto',
                search_sources TEXT DEFAULT '["web","x","news","rss"]',
                max_search_results INTEGER DEFAULT 20,
                from_date TEXT,
                to_date TEXT,
                FOREIGN KEY (user_id) REFERENCES users(user_id)
            )
        """)

    def execute(self, query, params=None):
        try:
            with self.engine.connect() as conn:
                params = params if params is not None else {}
                result = conn.execute(text(query), params)
                conn.commit()
                return result
        except SQLAlchemyError as e:
            log.excpt("Ошибка выполнения запроса: %s, params=~C95%s~C00, error=%s",
                      query, str(params), str(e), exc_info=(type(e), e, e.__traceback__))
            raise

    def fetch_one(self, query, params=None):
        try:
            with self.engine.connect() as conn:
                params = params if params is not None else {}
                result = conn.execute(text(query), params)
                row = result.fetchone()
                return row
        except SQLAlchemyError as e:
            log.excpt("Ошибка fetch_one: %s, params=~C95%s~C00, error=%s",
                      query, str(params), str(e), exc_info=(type(e), e, e.__traceback__))
            raise

    def fetch_all(self, query, params=None):
        try:
            with self.engine.connect() as conn:
                params = params if params is not None else {}
                result = conn.execute(text(query), params)
                rows = result.fetchall()
                log.debug("Выбрано %d строк из запроса: %s, params=~C95%s~C00", len(rows), query, str(params))
                return rows
        except SQLAlchemyError as e:
            log.excpt("Ошибка fetch_all: %s, params=~C95%s~C00, error=%s",
                      query, str(params), str(e), exc_info=(type(e), e, e.__traceback__))
            raise

class DataTable:
    def __init__(self, table_name: str, template: list):
        self.db = Database.get_database()
        self.table_name = table_name
        self.template = template
        self.create()
        self.upgrade()

    def create(self):
        """Создаёт таблицу на основе template."""
        try:
            fields = ", ".join(self.template)
            query = f"CREATE TABLE IF NOT EXISTS {self.table_name} ({fields})"
            self.db.execute(query)
            log.debug("Таблица %s создана или уже существует", self.table_name)
        except Exception as e:
            log.excpt("Не удалось создать таблицу %s: %s", self.table_name, str(e), exc_info=(type(e), e, e.__traceback__))
            raise

    def upgrade(self):
        """Проверяет и обновляет структуру таблицы, добавляя недостающие поля."""
        try:
            current_columns = self.db.fetch_all(f"PRAGMA table_info({self.table_name})")
            current_column_names = {row[1] for row in current_columns}
            template_columns = set()
            for field in self.template:
                column_name = field.split()[0]
                if column_name not in ('PRIMARY', 'FOREIGN'):
                    template_columns.add(column_name)
            missing_columns = []
            for field in self.template:
                column_name = field.split()[0]
                if column_name not in current_column_names and column_name not in ('PRIMARY', 'FOREIGN'):
                    missing_columns.append(field)
            for column_def in missing_columns:
                query = f"ALTER TABLE {self.table_name} ADD COLUMN {column_def}"
                self.db.execute(query)
                log.debug("Добавлен столбец %s в таблицу %s", column_def, self.table_name)
            if not missing_columns:
                log.debug("Таблица %s актуальна", self.table_name)
        except Exception as e:
            log.excpt("Не удалось обновить таблицу %s: %s", self.table_name, str(e), exc_info=(type(e), e, e.__traceback__))
            raise

    def insert_into(self, values: dict, ignore: bool = False):
        """Вставляет запись в таблицу, с опцией игнорирования дубликатов."""
        try:
            fields = ", ".join(values.keys())
            placeholders = ", ".join([f":{key}" for key in values.keys()])
            insert_type = "INSERT OR IGNORE" if ignore else "INSERT"
            query = f"{insert_type} INTO {self.table_name} ({fields}) VALUES ({placeholders})"
            self.db.execute(query, values)
            log.debug("Вставлено в %s: ~C95%s~C00", self.table_name, str(values))
            return self.db.fetch_one(f"SELECT last_insert_rowid()")[0]
        except Exception as e:
            log.excpt("Не удалось вставить в %s: %s", self.table_name, str(e), exc_info=(type(e), e, e.__traceback__))
            raise

    def insert_or_replace(self, values: dict):
        """Вставляет или заменяет запись в таблице."""
        try:
            fields = ", ".join(values.keys())
            placeholders = ", ".join([f":{key}" for key in values.keys()])
            query = f"INSERT OR REPLACE INTO {self.table_name} ({fields}) VALUES ({placeholders})"
            self.db.execute(query, values)
            log.debug("Вставлено или заменено в %s: ~C95%s~C00", self.table_name, str(values))
            return self.db.fetch_one(f"SELECT last_insert_rowid()")[0]
        except Exception as e:
            log.excpt("Не удалось вставить или заменить в %s: %s", self.table_name, str(e), exc_info=(type(e), e, e.__traceback__))
            raise

    def update(self, values: dict, conditions: dict):
        """Обновляет записи в таблице по условиям."""
        try:
            set_clause = ", ".join([f"{key} = :{key}" for key in values.keys()])
            conditions_clause = " AND ".join([f"{key} = :cond_{key}" for key in conditions.keys()])
            query = f"UPDATE {self.table_name} SET {set_clause} WHERE {conditions_clause}"
            params = {**values, **{f"cond_{key}": value for key, value in conditions.items()}}
            self.db.execute(query, params)
            log.debug("Обновлено %s: values=~C95%s~C00, conditions=~C95%s~C00", self.table_name, str(values), str(conditions))
        except Exception as e:
            log.excpt("Не удалось обновить %s: %s", self.table_name, str(e), exc_info=(type(e), e, e.__traceback__))
            raise

    def delete_from(self, conditions: dict):
        """Удаляет записи из таблицы по условиям."""
        try:
            conditions_clause = " AND ".join([f"{key} = :{key}" for key in conditions.keys()])
            query = f"DELETE FROM {self.table_name} WHERE {conditions_clause}"
            params = conditions
            self.db.execute(query, params)
            log.debug("Удалено из %s: conditions=~C95%s~C00", self.table_name, str(conditions))
        except Exception as e:
            log.excpt("Не удалось удалить из %s: %s", self.table_name, str(e), exc_info=(type(e), e, e.__traceback__))
            raise

    def select_from(self, conditions: dict = None, order_by: str = None, limit: int = None, joins: list = None, columns: list = None):
        """Выбирает записи из таблицы с условиями и поддержкой JOIN."""
        try:
            columns_str = ", ".join(columns) if columns else '*'
            query = f"SELECT {columns_str} FROM {self.table_name} p"
            params = {}
            if joins:
                for join in joins:
                    table, alias, condition = join
                    query += f" JOIN {table} {alias} ON {condition}"
            if conditions:
                conditions_str = " AND ".join([f"p.{key} = :{key}" for key in conditions.keys()])
                query += f" WHERE {conditions_str}"
                params = conditions
            if order_by:
                query += f" ORDER BY {order_by}"
            if limit:
                query += f" LIMIT {limit}"
            result = self.db.fetch_all(query, params)
            log.debug("Выбрано %d строк из %s: query=%s", len(result), self.table_name, query)
            return result
        except Exception as e:
            log.excpt("Не удалось выполнить выборку %s: %s", query, str(e), exc_info=(type(e), e, e.__traceback__))
            raise