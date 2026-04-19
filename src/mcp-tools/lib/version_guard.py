from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Optional


class VersionGuard:
    """
    Detects when a running MCP server process is "stale" vs filesystem changes.

    On init it snapshots `st_mtime_ns` for all already-imported `.py` modules whose
    source files live under `base_dir`. On each check it compares current mtimes
    against the snapshot and returns a warning if anything changed.
    """

    def __init__(
        self,
        *,
        base_dir: Path,
        message: str,
        track_new_modules: bool = True,
        check_interval_sec: float = 1.0,
    ) -> None:
        self._base_dir = Path(base_dir).resolve()
        self._message = str(message)
        self._track_new_modules = bool(track_new_modules)
        self._check_interval_sec = float(check_interval_sec)

        self._mtimes_ns: dict[Path, int] = {}
        self._last_check_unix: float = 0.0
        self._cached_warning: Optional[str] = None

        self._snapshot_loaded_modules()

    def _module_file_paths(self) -> list[Path]:
        out: list[Path] = []
        for mod in list(sys.modules.values()):
            if mod is None:
                continue
            f = getattr(mod, "__file__", None)
            if not isinstance(f, str) or not f:
                continue

            p = Path(f)
            # Prefer `.py` if we ended up on `.pyc`.
            if p.suffix in (".pyc", ".pyo"):
                py = p.with_suffix(".py")
                if py.exists():
                    p = py
                else:
                    continue

            if p.suffix != ".py":
                continue

            try:
                rp = p.resolve()
            except OSError:
                continue
            except Exception:
                continue

            try:
                rp.relative_to(self._base_dir)
            except ValueError:
                continue

            if not rp.is_file():
                continue

            out.append(rp)

        # de-dup while preserving (arbitrary) order
        seen: set[Path] = set()
        uniq: list[Path] = []
        for p in out:
            if p in seen:
                continue
            seen.add(p)
            uniq.append(p)
        return uniq

    def _snapshot_loaded_modules(self) -> None:
        for p in self._module_file_paths():
            try:
                self._mtimes_ns[p] = p.stat().st_mtime_ns
            except OSError:
                # ignore missing/unreadable paths
                pass

    def _maybe_track_new_modules(self) -> None:
        if not self._track_new_modules:
            return
        for p in self._module_file_paths():
            if p in self._mtimes_ns:
                continue
            try:
                self._mtimes_ns[p] = p.stat().st_mtime_ns
            except OSError:
                pass

    def get_warning(self) -> str | None:
        now = time.time()
        if self._last_check_unix and (now - self._last_check_unix) < self._check_interval_sec:
            return self._cached_warning
        self._last_check_unix = now

        self._maybe_track_new_modules()

        for p, old in list(self._mtimes_ns.items()):
            try:
                cur = p.stat().st_mtime_ns
            except OSError:
                continue
            if cur != old:
                self._cached_warning = self._message
                return self._cached_warning

        self._cached_warning = None
        return None

