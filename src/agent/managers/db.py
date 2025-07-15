from sqlalchemy import create_engine, text
from globals import CONFIG_FILE
import toml
import logging


class Database:
    def __init__(self):
        config = toml.load(CONFIG_FILE)
        db_config = config.get('database', {})
        db_type = db_config.get('type', 'sqlite')
        if db_type == 'sqlite':
            db_url = f"sqlite:///{db_config.get('path', '/app/data/multichat.db')}"
        elif db_type == 'mariadb':
            db_url = f"mysql+mysqlconnector://{db_config['user']}:{db_config['password']}@{db_config['host']}:{db_config.get('port', 3306)}/{db_config['database']}"
        else:
            raise ValueError(f"Unsupported database type: {db_type}")
        self.engine = create_engine(db_url, echo=False)
        logging.info(f"#INFO: Инициализирована БД: {db_url}")

    def get_connection(self):
        return self.engine.connect()

    def execute(self, query, params=None):
        with self.get_connection() as conn:
            result = conn.execute(text(query), params or {})
            conn.commit()
            return result

    def fetch_all(self, query, params=None):
        with self.get_connection() as conn:
            result = conn.execute(text(query), params or {})
            return result.fetchall()

    def fetch_one(self, query, params=None):
        with self.get_connection() as conn:
            result = conn.execute(text(query), params or {})
            return result.fetchone()
