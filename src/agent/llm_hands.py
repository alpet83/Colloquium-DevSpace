# /app/agent/llm_hands.py, updated 2025-07-27 15:30 EEST
import re
import globals
from processors.block_processor import CommandProcessor, ProcessResult
from processors.shell_code import ShellCodeProcessor
from processors.file_processors import FileEditProcessor, FileReplaceProcessor, FileMoveProcessor, FileUndoProcessor
from processors.entity_processor import EntityUpdateProcessor
from processors.patch_processor import CodePatchProcessor

log = globals.get_logger("llm_hands")

def process_message(text, timestamp, user_name: str, rql: int = 0) -> dict:
    """Обрабатывает сообщение, применяя процессоры для специальных тегов.

    Args:
        text (str or bytes): Текст сообщения.
        timestamp (int): Временная метка сообщения.
        user_name (str): Имя пользователя.
        rql (int, optional): Уровень рекурсии диалога. Defaults to 0.

    Returns:
        dict: Результат обработки с ключами handled_cmds, failed_cmds, processed_msg, agent_reply, has_code_file.
    """
    log.debug("Обработка сообщения: text=%s, timestamp=%d, user_name=%s, rql=%d", text[:50], timestamp,
              user_name or "@self", rql)
    if isinstance(text, bytes):
        text = text.decode('utf-8', errors='replace')
        log.warn("Входной текст был байтовым, декодирован: %s", text[:50])
    elif not isinstance(text, str):
        log.error("Неверный тип входного текста: %s", type(text))
        return {"handled_cmds": 0, "failed_cmds": 1, "processed_msg": text,
                "agent_reply": f"@{user_name or 'Unknown'} <stdout>Error: Invalid input type</stdout>"}

    processors = [
        CommandProcessor(),
        FileEditProcessor(),
        CodePatchProcessor(),
        FileMoveProcessor(),
        FileUndoProcessor(),
        FileReplaceProcessor(),
        ShellCodeProcessor(),
        EntityUpdateProcessor()
    ]
    log.info("Инициализировано %d процессоров для обработки сообщения", len(processors))

    tag_pattern = r'<(' + '|'.join(re.escape(processor.tag) for processor in processors) + r')\b'
    log.debug("Сформирован динамический паттерн для тегов: %s", tag_pattern)

    processed_msg = text
    agent_reply = []
    has_code_file = False
    handled_cmds = 0
    failed_cmds = 0
    command_text = text.replace('@agent', '', 1).strip()
    have_tag = re.search(tag_pattern, command_text, re.DOTALL)
    if have_tag:
        for processor in processors:
            result = processor.process(command_text, user_name)
            processed_msg = result["processed_message"]
            if processed_msg:
                command_text = processed_msg
            agent_reply.extend(result["agent_messages"])
            handled_cmds += result["handled_cmds"]
            failed_cmds += result["failed_cmds"]
            if result["has_code_file"]:
                has_code_file = True
            if handled_cmds > 0 or failed_cmds > 0:
                log.debug("Процессор %s обработал %d команд, неуспешно %d",
                          processor.tag, handled_cmds, failed_cmds)
    else:
        log.debug("Нет тегов согласно паттерну для %s", command_text)

    agent_reply_text = "\n".join(agent_reply) if agent_reply else None
    return {"handled_cmds": handled_cmds, "failed_cmds": failed_cmds, "processed_msg": processed_msg,
            "agent_reply": agent_reply_text, "has_code_file": has_code_file}