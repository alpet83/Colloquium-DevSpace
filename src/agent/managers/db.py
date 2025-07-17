# /agent/managers/db.py, updated 2025-07-17 13:43 EEST
import logging
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError

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
        logging.info(f"Инициализирована БД: {self.engine.url}")

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
            logging.error(f"Ошибка выполнения запроса: {query}, params={params}, error={str(e)}")
            raise

    def fetch_one(self, query, params=None):
        try:
            with self.engine.connect() as conn:
                params = params if params is not None else {}
                result = conn.execute(text(query), params)
                row = result.fetchone()
                return row
        except SQLAlchemyError as e:
            logging.error(f"Ошибка fetch_one: {query}, params={params}, error={str(e)}")
            raise

    def fetch_all(self, query, params=None):
        try:
            with self.engine.connect() as conn:
                params = params if params is not None else {}
                result = conn.execute(text(query), params)
                rows = result.fetchall()
                return rows
        except SQLAlchemyError as e:
            logging.error(f"Ошибка fetch_all: {query}, params={params}, error={str(e)}")
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
            logging.debug(f"Table {self.table_name} created or already exists")
        except Exception as e:
            logging.error(f"Failed to create table {self.table_name}: {str(e)}")
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
                logging.debug(f"Added column {column_def} to table {self.table_name}")
            if not missing_columns:
                logging.debug(f"Table {self.table_name} is up-to-date")
        except Exception as e:
            logging.error(f"Failed to upgrade table {self.table_name}: {str(e)}")
            raise

    def insert_into(self, values: dict, ignore: bool = False):
        """Вставляет запись в таблицу, с опцией игнорирования дубликатов."""
        try:
            fields = ", ".join(values.keys())
            placeholders = ", ".join([f":{key}" for key in values.keys()])
            insert_type = "INSERT OR IGNORE" if ignore else "INSERT"
            query = f"{insert_type} INTO {self.table_name} ({fields}) VALUES ({placeholders})"
            self.db.execute(query, values)
            logging.debug(f"Inserted into {self.table_name}: {values}")
            return self.db.fetch_one(f"SELECT last_insert_rowid()")[0]
        except Exception as e:
            logging.error(f"Failed to insert into {self.table_name}: {str(e)}")
            raise

    def insert_or_replace(self, values: dict):
        """Вставляет или заменяет запись в таблице."""
        try:
            fields = ", ".join(values.keys())
            placeholders = ", ".join([f":{key}" for key in values.keys()])
            query = f"INSERT OR REPLACE INTO {self.table_name} ({fields}) VALUES ({placeholders})"
            self.db.execute(query, values)
            logging.debug(f"Inserted or replaced into {self.table_name}: {values}")
            return self.db.fetch_one(f"SELECT last_insert_rowid()")[0]
        except Exception as e:
            logging.error(f"Failed to insert or replace into {self.table_name}: {str(e)}")
            raise

    def update(self, values: dict, conditions: dict):
        """Обновляет записи в таблице по условиям."""
        try:
            set_clause = ", ".join([f"{key} = :{key}" for key in values.keys()])
            conditions_clause = " AND ".join([f"{key} = :cond_{key}" for key in conditions.keys()])
            query = f"UPDATE {self.table_name} SET {set_clause} WHERE {conditions_clause}"
            params = {**values, **{f"cond_{key}": value for key, value in conditions.items()}}
            self.db.execute(query, params)
            logging.debug(f"Updated {self.table_name}: values={values}, conditions={conditions}")
        except Exception as e:
            logging.error(f"Failed to update {self.table_name}: {str(e)}")
            raise

    def delete_from(self, conditions: dict):
        """Удаляет записи из таблицы по условиям."""
        try:
            conditions_clause = " AND ".join([f"{key} = :{key}" for key in conditions.keys()])
            query = f"DELETE FROM {self.table_name} WHERE {conditions_clause}"
            params = conditions
            self.db.execute(query, params)
            logging.debug(f"Deleted from {self.table_name}: conditions={conditions}")
        except Exception as e:
            logging.error(f"Failed to delete from {self.table_name}: {str(e)}")
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
            logging.debug(f"Selected from {self.table_name}: {len(result)} rows, query={query}")
            return result
        except Exception as e:
            logging.error(f"Failed select query {query}: {str(e)}")
            raise