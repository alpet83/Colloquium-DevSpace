from __future__ import annotations

import re
from pathlib import Path

from lib.basic_logger import BasicLogger


def _normalize_rel_path(rel_path: str) -> str:
    return str(rel_path).replace("\\", "/").lstrip("/")


class ProjectScanFilter:
    """Shared project scan filters based on `.scan_ignore.txt` regex rules."""

    def __init__(self, project_dir: Path, logger: BasicLogger | None = None):
        self.project_dir = Path(project_dir)
        self._log = logger
        self._patterns: list[re.Pattern[str]] = []
        self._load_patterns()

    @property
    def pattern_count(self) -> int:
        return len(self._patterns)

    def _load_patterns(self) -> None:
        ignore_file = self.project_dir / ".scan_ignore.txt"
        if not ignore_file.exists():
            return
        try:
            lines = ignore_file.read_text(encoding="utf-8").splitlines()
        except OSError as e:
            if self._log is not None:
                self._log.warn("Не удалось прочитать .scan_ignore.txt для %s: %s", self.project_dir.name, str(e))
            return
        for raw in lines:
            pattern = raw.strip()
            if not pattern or pattern.startswith("#"):
                continue
            try:
                self._patterns.append(re.compile(pattern))
            except re.error as e:
                if self._log is not None:
                    self._log.error("Некорректный regex паттерн '%s' в .scan_ignore.txt: %s", pattern, str(e))

    def is_excluded(self, rel_path: str) -> bool:
        rel = _normalize_rel_path(rel_path)
        if not rel:
            return False
        for pattern in self._patterns:
            if pattern.search(rel):
                return True
        return False
