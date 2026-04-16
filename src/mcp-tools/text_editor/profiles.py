from __future__ import annotations

import json
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Profile:
    profile_id: str
    extensions: tuple[str, ...]
    indent_mode: str = "off"
    tab_width: int = 4
    syntax_check_cmd: tuple[str, ...] = ()
    syntax_timeout_sec: int = 20
    format_cmd: tuple[str, ...] = ()
    priority: int = 0


def builtin_profiles() -> list[Profile]:
    return [
        Profile(profile_id="plain", extensions=(), indent_mode="off", tab_width=4, priority=-100),
        Profile(
            profile_id="python",
            extensions=("py", "pyw"),
            indent_mode="strict",
            tab_width=4,
            syntax_check_cmd=("python", "-m", "py_compile", "{path}"),
            syntax_timeout_sec=30,
            priority=10,
        ),
    ]


class ProfileRegistry:
    def __init__(self, profiles_dir: Path):
        self.profiles_dir = profiles_dir
        self._profiles = builtin_profiles()
        self._profiles.extend(self._load_external_profiles())
        self._profiles.sort(key=lambda p: p.priority, reverse=True)

    def get(self, profile_id: str) -> Profile | None:
        for p in self._profiles:
            if p.profile_id == profile_id:
                return p
        return None

    def resolve(self, path: Path, *, profile_id: str | None, profile_auto: bool) -> Profile:
        if profile_id:
            found = self.get(profile_id)
            if found is not None:
                return found
        if profile_auto:
            ext = path.suffix.lower().lstrip(".")
            for p in self._profiles:
                if ext and ext in p.extensions:
                    return p
        return self.get("plain") or self._profiles[0]

    def _load_external_profiles(self) -> list[Profile]:
        if not self.profiles_dir.exists():
            return []
        out: list[Profile] = []
        for path in sorted(self.profiles_dir.glob("*.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                out.append(_profile_from_payload(payload))
            except Exception:
                continue
        for path in sorted(self.profiles_dir.glob("*.yml")) + sorted(self.profiles_dir.glob("*.yaml")):
            out.extend(_load_yaml_profiles(path))
        return out


def _profile_from_payload(payload: dict[str, Any]) -> Profile:
    return Profile(
        profile_id=str(payload.get("id") or payload.get("profile_id") or "unknown"),
        extensions=tuple(str(x).lower().lstrip(".") for x in payload.get("extensions", [])),
        indent_mode=str(payload.get("indent_mode", "off")),
        tab_width=int(payload.get("tab_width", 4)),
        syntax_check_cmd=tuple(str(x) for x in payload.get("syntax_check_cmd", [])),
        syntax_timeout_sec=int(payload.get("syntax_timeout_sec", 20)),
        format_cmd=tuple(str(x) for x in payload.get("format_cmd", [])),
        priority=int(payload.get("priority", 0)),
    )


def run_syntax_check(profile: Profile, content: str, suffix: str) -> tuple[bool, str]:
    if not profile.syntax_check_cmd:
        return True, ""
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False, suffix=suffix) as tf:
        tf.write(content)
        temp_path = Path(tf.name)
    try:
        cmd = [part.replace("{path}", str(temp_path)) for part in profile.syntax_check_cmd]
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=max(1, profile.syntax_timeout_sec),
            check=False,
        )
        if proc.returncode == 0:
            return True, ""
        msg = (proc.stderr or proc.stdout or "").strip()
        return False, msg[:2000]
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)
    finally:
        try:
            temp_path.unlink(missing_ok=True)
        except Exception:
            pass


def run_formatter(profile: Profile, content: str, suffix: str) -> tuple[bool, str, str]:
    if not profile.format_cmd:
        return True, content, ""
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False, suffix=suffix) as tf:
        tf.write(content)
        temp_path = Path(tf.name)
    try:
        cmd = [part.replace("{path}", str(temp_path)) for part in profile.format_cmd]
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=max(1, profile.syntax_timeout_sec),
            check=False,
        )
        if proc.returncode != 0:
            msg = (proc.stderr or proc.stdout or "").strip()
            return False, content, msg[:2000]
        new_content = temp_path.read_text(encoding="utf-8")
        return True, new_content, ""
    except Exception as exc:  # noqa: BLE001
        return False, content, str(exc)
    finally:
        try:
            temp_path.unlink(missing_ok=True)
        except Exception:
            pass


def _load_yaml_profiles(path: Path) -> list[Profile]:
    try:
        import yaml  # type: ignore[import-not-found]
    except Exception:
        return []
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    if raw is None:
        return []
    items: list[dict[str, Any]]
    if isinstance(raw, list):
        items = [x for x in raw if isinstance(x, dict)]
    elif isinstance(raw, dict):
        items = [raw]
    else:
        return []
    out: list[Profile] = []
    for payload in items:
        try:
            out.append(_profile_from_payload(payload))
        except Exception:
            continue
    return out

