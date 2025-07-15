# /agent/migrate_sessions.py, created 2025-07-14 19:12 EEST
import logging
from managers.db import Database

logging.basicConfig(level=logging.DEBUG, format='[%(asctime)s] #%(levelname)s: %(message)s', filename='/app/logs/colloqium_core.log', filemode='a')

def migrate_sessions_table():
    SESSION_DB = Database()
    try:
        logging.info("#INFO: Проверка и миграция таблицы sessions")
        result = SESSION_DB.fetch_one("SELECT sql FROM sqlite_master WHERE type='table' AND name='sessions'")
        if result:
            current_schema = result[0]
            if 'chat_id' in current_schema:
                logging.info("#INFO: Обнаружен столбец chat_id в таблице sessions, выполняется миграция")
                SESSION_DB.execute('''
                    CREATE TABLE sessions_new (
                        session_id TEXT PRIMARY KEY,
                        user_id INTEGER,
                        FOREIGN KEY (user_id) REFERENCES users(user_id)
                    )
                ''')
                SESSION_DB.execute('''
                    INSERT INTO sessions_new (session_id, user_id)
                    SELECT session_id, user_id FROM sessions
                ''')
                SESSION_DB.execute('DROP TABLE sessions')
                SESSION_DB.execute('ALTER TABLE sessions_new RENAME TO sessions')
                logging.info("#INFO: Миграция таблицы sessions завершена")
            else:
                logging.info("#INFO: Таблица sessions уже соответствует схеме")
        else:
            SESSION_DB.execute('''
                CREATE TABLE IF NOT EXISTS sessions (
                    session_id TEXT PRIMARY KEY,
                    user_id INTEGER,
                    FOREIGN KEY (user_id) REFERENCES users(user_id)
                )
            ''')
            logging.info("#INFO: Создана таблица sessions")
    except Exception as e:
        logging.error(f"#ERROR: Ошибка при миграции таблицы sessions: {str(e)}")
        raise

if __name__ == "__main__":
    migrate_sessions_table()
