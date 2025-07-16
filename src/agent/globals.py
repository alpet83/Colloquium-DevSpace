# /agent/globals.py, updated 2025-07-14 17:14 EEST
user_manager = None
chat_manager = None
post_manager = None
file_manager = None
project_manager = None
replication_manager = None


LOG_DIR = "/app/logs"
LOG_FILE = LOG_DIR + "/colloquium_core.log"
LOG_SERV = LOG_DIR + "/colloquium_serv.log"
LOG_FORMAT = '[%(asctime)s]. #%(levelname)s(%(name)s): %(message)s'

CONFIG_FILE = "/app/data/colloquium_config.toml"
