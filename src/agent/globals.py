# /agent/globals.py, updated 2025-07-17 15:45 EEST
import asyncio

user_manager = None
chat_manager = None
post_manager = None
file_manager = None
project_manager = None
replication_manager = None
post_processor = None

# Хранилище для событий переключения чата (user_id:chat_id -> asyncio.Event)
chat_switch_events = {}

LOG_DIR = "/app/logs"
LOG_FILE = LOG_DIR + "/colloquium_core.log"
LOG_SERV = LOG_DIR + "/colloquium_serv.log"
LOG_FORMAT = '[%(asctime)s]. #%(levelname)s(%(name)s): %(message)s'

CONFIG_FILE = "/app/data/colloquium_config.toml"