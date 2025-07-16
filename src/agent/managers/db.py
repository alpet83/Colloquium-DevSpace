# /agent/managers/db.py, updated 2025-07-15 18:40 EEST
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
        logging.info(f"Инициализирована БД: {self.engine.url}")

    def execute(self, query, params=None):
        try:
            with self.engine.connect() as conn:
                # Если params не указаны, используем пустой список
                params = params if params is not None else []
                # Если params — кортеж, преобразуем в список кортежей
                if isinstance(params, tuple):
                    params = [params]
                result = conn.execute(text(query), params)
                conn.commit()
                # logging.debug(f"#DEBUG: Выполнен запрос: {query}, params={params}")
                return result
        except SQLAlchemyError as e:
            logging.error(f"#ERROR: Ошибка выполнения запроса: {query}, params={params}, error={str(e)}")
            raise

    def fetch_one(self, query, params=None):
        try:
            with self.engine.connect() as conn:
                # Если params не указаны, используем пустой список
                params = params if params is not None else []
                # Если params — кортеж, преобразуем в список кортежей
                if isinstance(params, tuple):
                    params = [params]
                result = conn.execute(text(query), params)
                row = result.fetchone()
                # logging.debug(f"#DEBUG: Выполнен fetch_one: {query}, params={params}, result={row}")
                return row
        except SQLAlchemyError as e:
            logging.error(f"#ERROR: Ошибка fetch_one: {query}, params={params}, error={str(e)}")
            raise

    def fetch_all(self, query, params=None):
        try:
            with self.engine.connect() as conn:
                # Если params не указаны, используем пустой список
                params = params if params is not None else []
                # Если params — кортеж, преобразуем в список кортежей
                if isinstance(params, tuple):
                    params = [params]
                result = conn.execute(text(query), params)
                rows = result.fetchall()
                # logging.debug(f"#DEBUG: Выполнен fetch_all: {query}, params={params}, results={len(rows)}")
                return rows
        except SQLAlchemyError as e:
            logging.error(f"#ERROR: Ошибка fetch_all: {query}, params={params}, error={str(e)}")
            raise