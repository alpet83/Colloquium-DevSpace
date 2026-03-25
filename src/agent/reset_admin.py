import logging
import os
from managers.db import Database

LOG_FILE = "/app/logs/colloqium_core.log"
logging.basicConfig(level=logging.INFO, format='[%(asctime)s] #%(levelname)s: %(message)s', filename=LOG_FILE, filemode='a')

def reset_admin():
    db = Database.get_database()
    db.execute("DELETE FROM users WHERE user_name = :user_name", {"user_name": "admin"})
    logging.info("#INFO: Учётная запись admin удалена из базы данных")

if __name__ == "__main__":
    reset_admin()