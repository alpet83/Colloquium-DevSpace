import sqlite3
import logging
import os

LOG_FILE = "/app/logs/colloqium_core.log"
logging.basicConfig(level=logging.INFO, format='[%(asctime)s] #%(levelname)s: %(message)s', filename=LOG_FILE, filemode='a')

CHAT_DB = '/app/data/multichat.db'

def reset_admin():
    conn = sqlite3.connect(CHAT_DB)
    cur = conn.cursor()
    cur.execute("DELETE FROM users WHERE user_name = 'admin'")
    conn.commit()
    logging.info("#INFO: Учётная запись admin удалена из базы данных")
    conn.close()

if __name__ == "__main__":
    reset_admin()