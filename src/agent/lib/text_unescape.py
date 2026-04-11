# Литералы вида \\xF0\\x9F… и \\uXXXX в тексте (часто от моделей) → символы UTF-8, если последовательность валидна.
from __future__ import annotations

import re

# Подряд идущие \xHH (один обратный слэш в строке Python).
_X_RUN = re.compile(r"(?:\\x[0-9a-fA-F]{2})+", re.DOTALL)

# BMP и дополнительные плоскости
_U16 = re.compile(r"\\u([0-9a-fA-F]{4})")
_U32 = re.compile(r"\\U([0-9a-fA-F]{8})")


def _x_run_repl(match: re.Match) -> str:
    raw = match.group(0)
    pairs = re.findall(r"\\x([0-9a-fA-F]{2})", raw)
    bs = bytes(int(h, 16) for h in pairs)
    if len(bs) == 1:
        b = bs[0]
        # Одиночные управляющие C0 не подменяем (кроме таб/перевод строки).
        if b < 32 and b not in (9, 10, 13):
            return raw
    try:
        return bs.decode("utf-8")
    except UnicodeDecodeError:
        return raw


def unescape_utf8_literal_escapes(text: str) -> str:
    """
    Заменяет в тексте:
    - максимальные цепочки \\xHH на декод UTF-8 (если декод успешен);
    - \\uHHHH и \\UHHHHHHHH на один символ Unicode.

    Не трогает одиночные \\xHH с байтом C0-кроме TAB/LF/CR (чтобы не вставлять SI и т.п.).
    """
    if not text or "\\" not in text:
        return text
    out = _X_RUN.sub(_x_run_repl, text)
    out = _U32.sub(lambda m: chr(int(m.group(1), 16)), out)
    out = _U16.sub(lambda m: chr(int(m.group(1), 16)), out)
    return out
