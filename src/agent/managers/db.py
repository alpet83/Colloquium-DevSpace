# /agent/managers/db.py, updated 2025-07-26 15:30 EEST
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
            log.excpt("Ошибка выполнения запроса: %s, params=~%s, error=%s",
                      query[:50] + "..." if len(query) > 50 else query, str(params), str(e))
            raise

    def fetch_one(self, query, params=None):
        try:
            with self.engine.connect() as conn:
                params = params if params is not None else {}
                result = conn.execute(text(query), params)
                row = result.fetchone()
                return row
        except SQLAlchemyError as e:
            log.excpt("Ошибка fetch_one: %s, params=~%s, error=%s",
                      query[:50] + "..." if len(query) > 50 else query, str(params), str(e))
            raise

    def fetch_all(self, query, params=None):
        try:
            with self.engine.connect() as conn:
                params = params if params is not None else {}
                result = conn.execute(text(query), params)
                rows = result.fetchall()
                return rows
        except SQLAlchemyError as e:
            log.excpt("Ошибка fetch_all: %s, params=~%s, error=%s",
                      query[:50] + "..." if len(query) > 50 else query, str(params), str(e))
            raise


class DataTable:
    def __init__(self, table_name: str, template: list):
        self.db = Database.get_database()
        self.table_name = table_name
        self.template = template
        self.create()
        self.upgrade()

    def create(self):
        try:
            fields = ", ".join(self.template)
            query = f"CREATE TABLE IF NOT EXISTS {self.table_name} ({fields})"
            self.db.execute(query)
            log.debug("Таблица %s создана или уже существует", self.table_name)
        except Exception as e:
            log.excpt("Не удалось создать таблицу %s: %s", self.table_name, str(e))
            raise

    def upgrade(self):
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
                log.debug("Добавлен столбец %s в таблицу %s", column_def.split()[0], self.table_name)
            if not missing_columns:
                log.debug("Таблица %s актуальна", self.table_name)
        except Exception as e:
            log.excpt("Не удалось обновить таблицу %s: %s", self.table_name, str(e))
            raise

    def insert_into(self, values: dict, ignore: bool = False):
        try:
            fields = ", ".join(values.keys())
            placeholders = ", ".join([f":{key}" for key in values.keys()])
            insert_type = "INSERT OR IGNORE" if ignore else "INSERT"
            query = f"{insert_type} INTO {self.table_name} ({fields}) VALUES ({placeholders})"
            self.db.execute(query, values)
            log.debug("Вставлено в %s: ~%s", self.table_name, str(values))
            return self.db.fetch_one(f"SELECT last_insert_rowid()")[0]
        except Exception as e:
            log.excpt("Не удалось вставить в %s: %s", self.table_name, str(e))
            raise

    def insert_or_replace(self, values: dict):
        try:
            fields = ", ".join(values.keys())
            placeholders = ", ".join([f":{key}" for key in values.keys()])
            query = f"INSERT OR REPLACE INTO {self.table_name} ({fields}) VALUES ({placeholders})"
            self.db.execute(query, values)
            log.debug("Вставлено или заменено в %s: ~%s", self.table_name, str(values))
            return self.db.fetch_one(f"SELECT last_insert_rowid()")[0]
        except Exception as e:
            log.excpt("Не удалось вставить или заменить в %s: %s", self.table_name, str(e))
            raise

    def update(self, values: dict, conditions: dict):
        try:
            set_clause = ", ".join([f"{key} = :{key}" for key in values.keys()])
            conditions_clause = " AND ".join([f"{key} = :cond_{key}" for key in conditions.keys()])
            query = f"UPDATE {self.table_name} SET {set_clause} WHERE {conditions_clause}"
            params = {**values, **{f"cond_{key}": value for key, value in conditions.items()}}
            self.db.execute(query, params)
            log.debug("Обновлено %s: values=~%s, conditions=~%s", self.table_name, str(values), str(conditions))
        except Exception as e:
            log.excpt("Не удалось обновить %s: %s", self.table_name, str(e))
            raise

    def delete_from(self, conditions: dict):
        try:
            conditions_clause = " AND ".join([f"{key} = :{key}" for key in conditions.keys()])
            query = f"DELETE FROM {self.table_name} WHERE {conditions_clause}"
            params = conditions
            self.db.execute(query, params)
            log.debug("Удалено из %s: conditions=~%s", self.table_name, str(conditions))
        except Exception as e:
            log.excpt("Не удалось удалить из %s: %s", self.table_name, str(e))
            raise

    def select_from(self, columns: list = None, conditions=None, order_by: str = None,
                    limit: int = None, joins: list = None, fetch_all: bool = True):
        try:
            columns_str = ", ".join(columns) if columns else '*'
            query = f"SELECT {columns_str} FROM {self.table_name} p"
            params = {}
            if joins:
                for join in joins:
                    table, alias, condition = join
                    query += f" JOIN {table} {alias} ON {condition}"
            if conditions:
                if isinstance(conditions, str):
                    query += f" WHERE {conditions}"
                elif isinstance(conditions, list):
                    conditions_list = []
                    for cond in conditions:
                        if isinstance(cond, tuple) and len(cond) == 3:
                            key, op, value = cond
                            if op == 'IN' and isinstance(value, (list, tuple)):
                                placeholders = ', '.join([f':{key}_{i}' for i in range(len(value))])
                                conditions_list.append(f"p.{key} IN ({placeholders})")
                                for i, val in enumerate(value):
                                    params[f"{key}_{i}"] = val
                            else:
                                conditions_list.append(f"p.{key} {op} :{key}")
                                params[key] = value
                        else:
                            raise ValueError(f"Invalid condition format: {cond}")
                    if conditions_list:
                        conditions_str = " AND ".join(conditions_list)
                        query += f" WHERE {conditions_str}"
                else:
                    conditions_list = []
                    for key, value in conditions.items():
                        conditions_list.append(f"p.{key} = :{key}")
                        params[key] = value
                    if conditions_list:
                        conditions_str = " AND ".join(conditions_list)
                        query += f" WHERE {conditions_str}"
            if order_by:
                query += f" ORDER BY {order_by}"
            if limit:
                query += f" LIMIT {limit}"
            db = self.db
            if fetch_all:
                result = db.fetch_all(query, params)
            else:
                result = db.fetch_one(query, params)
            return result
        except Exception as e:
            log.excpt("Не удалось выполнить выборку %s: ", query[:50] + "..." if len(query) > 50 else query, e=e)
            raise

    def select_row(self, columns: list = None, conditions=None, order_by: str = None, joins: list = None):
        return self.select_from(columns, conditions, order_by, limit=1, joins=joins, fetch_all=False)