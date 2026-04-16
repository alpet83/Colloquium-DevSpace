#!/usr/bin/env python3
"""
Случайная ротация пароля PostgreSQL (роли postgres и cqds) и файла secrets/cqds_db_password.

Перед сменой пароля (если запущен colloquium-core): расшифровка users.llm_token текущим
секретом, после ALTER и записи файла — повторное шифрование новым секретом
(agent/lib/token_crypto.py, скрипт в контейнере rekey_llm_tokens_for_password_rotation.py).

Дополнительно: замена старого пароля на новый в текстовых файлах ./secrets/ и .env* в корне.

Требования:
  - postgres и colloquium-core в docker compose должны быть запущены (для миграции LLM).
  - Локальный psql в контейнере postgres обычно без пароля (иначе см. логику _postgres_psql_sql).

Опция --skip-llm-tokens: не трогать users.llm_token (после смены пароля enc:v1 станут недействительны).
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import string
import subprocess
import sys
from pathlib import Path


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _generate_password(length: int) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def _write_secret_file(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(value, encoding="utf-8", newline="")
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _docker_compose_cmd(root: Path, compose_files: list[str] | None) -> list[str]:
    cmd = ["docker", "compose"]
    if compose_files:
        for f in compose_files:
            cmd.extend(["-f", f])
    return cmd


def _run(
    cmd: list[str],
    *,
    cwd: Path,
    check: bool = True,
    capture: bool = False,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        cwd=cwd,
        check=check,
        text=True,
        capture_output=capture,
    )


def _postgres_psql_sql(root: Path, compose_files: list[str] | None, sql: str) -> None:
    base = _docker_compose_cmd(root, compose_files)
    cmd = [
        *base,
        "exec",
        "-T",
        "postgres",
        "psql",
        "-U",
        "postgres",
        "-d",
        "postgres",
        "-v",
        "ON_ERROR_STOP=1",
        "-c",
        sql,
    ]
    proc = subprocess.run(cmd, cwd=root, text=True, capture_output=True)
    if proc.returncode != 0:
        # повтор с паролем из файла (если локальный trust отключён)
        pw_file = root / "secrets" / "cqds_db_password"
        old_pw = pw_file.read_text(encoding="utf-8", errors="replace").strip() if pw_file.is_file() else ""
        if not old_pw:
            sys.stderr.write(proc.stderr or proc.stdout or "psql failed\n")
            proc.check_returncode()
        cmd2 = [
            *base,
            "exec",
            "-T",
            "-e",
            f"PGPASSWORD={old_pw}",
            "postgres",
            "psql",
            "-U",
            "postgres",
            "-d",
            "postgres",
            "-h",
            "127.0.0.1",
            "-v",
            "ON_ERROR_STOP=1",
            "-c",
            sql,
        ]
        subprocess.run(cmd2, cwd=root, check=True)


def _alter_roles_sql(new_pw: str) -> str:
    """Dollar-quoting: безопасно для любых символов в пароле (кроме вхождения тега)."""
    for _ in range(5):
        tag = "cqds" + secrets.token_hex(10)
        if tag not in new_pw:
            break
    else:
        tag = "cqds" + secrets.token_hex(20)
    return (
        f"ALTER ROLE postgres PASSWORD ${tag}${new_pw}${tag}; "
        f"ALTER ROLE cqds PASSWORD ${tag}${new_pw}${tag};"
    )


def _collect_env_files(root: Path) -> list[Path]:
    out: list[Path] = []
    for p in sorted(root.glob(".env*")):
        if p.is_file():
            out.append(p)
    return out


def _collect_secret_files(root: Path) -> list[Path]:
    d = root / "secrets"
    if not d.is_dir():
        return []
    files: list[Path] = []
    for p in sorted(d.rglob("*")):
        if p.is_file() and p.name != ".gitkeep":
            files.append(p)
    return files


def _exec_colloquium_core_rekey(root: Path, compose_files: list[str] | None, payload: dict) -> dict:
    """Запуск agent/scripts/rekey_llm_tokens_for_password_rotation.py внутри colloquium-core; JSON на stdin/stdout."""
    base = _docker_compose_cmd(root, compose_files)
    cmd = base + [
        "exec",
        "-T",
        "colloquium-core",
        "python",
        "/app/agent/scripts/rekey_llm_tokens_for_password_rotation.py",
    ]
    proc = subprocess.run(
        cmd,
        cwd=str(root),
        input=json.dumps(payload),
        text=True,
        capture_output=True,
    )
    raw = (proc.stdout or "").strip()
    if not raw:
        raise RuntimeError((proc.stderr or "").strip() or "empty stdout from LLM rekey script")
    try:
        out = json.loads(raw)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"invalid JSON from LLM rekey script: {e}: {raw[:300]!r}") from e
    if not out.get("ok"):
        err = out.get("error", "unknown")
        uid = out.get("user_id")
        if uid is not None:
            raise RuntimeError(f"LLM token rekey: user_id={uid}: {err}")
        raise RuntimeError(f"LLM token rekey: {err}")
    return out


def _replace_in_file(path: Path, root: Path, old: str, new: str, dry_run: bool) -> bool:
    if not old or old == new:
        return False
    try:
        text = _read_text(path)
    except OSError:
        return False
    if old not in text:
        return False
    rel = path.relative_to(root)
    if dry_run:
        print(f"  [dry-run] would replace password substring in {rel}")
        return True
    path.write_text(text.replace(old, new), encoding="utf-8", newline="\n")
    print(f"  updated {rel}")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="Rotate CQDS PostgreSQL password (postgres + cqds roles).")
    parser.add_argument(
        "--length",
        type=int,
        default=20,
        help="New password length (default 20, [A-Za-z0-9] only).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List files that would get substring replace; do not touch DB or files.",
    )
    parser.add_argument(
        "--skip-db",
        action="store_true",
        help="Skip ALTER ROLE (files only). Dangerous: can desync from real DB.",
    )
    parser.add_argument(
        "--skip-restart",
        action="store_true",
        help="Do not restart colloquium-core / pg-backup-scheduler after writing secret.",
    )
    parser.add_argument(
        "--skip-llm-tokens",
        action="store_true",
        help="Do not re-encrypt users.llm_token (enc:v1 will break after rotation).",
    )
    parser.add_argument(
        "--compose-file",
        action="append",
        dest="compose_files",
        metavar="FILE",
        help="Extra compose file (repeatable). Default: docker compose auto-discovery.",
    )
    args = parser.parse_args()
    root = _repo_root()
    os.chdir(root)

    pw_path = root / "secrets" / "cqds_db_password"
    old_pw = ""
    if pw_path.is_file():
        old_pw = pw_path.read_text(encoding="utf-8", errors="replace").strip().replace("\r\n", "\n").strip()

    new_pw = _generate_password(max(8, args.length))

    compose_files: list[str] | None = args.compose_files if args.compose_files else None

    print("=== rotate_cqds_db_password ===")
    print(f"Repo: {root}")
    if old_pw:
        print(f"Previous password file: present ({len(old_pw)} chars)")
    else:
        print("Previous password file: missing or empty")

    if args.dry_run:
        print(f"[dry-run] would set new password ({len(new_pw)} chars) and run DB ALTER unless --skip-db")
        if not args.skip_llm_tokens and not args.skip_db:
            print("[dry-run] would pull/push users.llm_token via colloquium-core if running")
        for f in _collect_secret_files(root) + _collect_env_files(root):
            if f.resolve() == pw_path.resolve():
                continue
            _replace_in_file(f, root, old_pw, new_pw, dry_run=True)
        return 0

    if not args.skip_db:
        try:
            _run(_docker_compose_cmd(root, compose_files) + ["ps", "-q", "postgres"], capture=True)
        except (subprocess.CalledProcessError, FileNotFoundError) as e:
            print(f"ERROR: docker compose not available or postgres service missing: {e}", file=sys.stderr)
            return 1
        r = _run(
            _docker_compose_cmd(root, compose_files) + ["ps", "-q", "--status", "running", "postgres"],
            capture=True,
            check=False,
        )
        if not (r.stdout or "").strip():
            print("ERROR: postgres container is not running. Start the stack first.", file=sys.stderr)
            return 1

        token_rows: list | None = None
        do_llm = not args.skip_llm_tokens
        if do_llm:
            cr = _run(
                _docker_compose_cmd(root, compose_files)
                + ["ps", "-q", "--status", "running", "colloquium-core"],
                capture=True,
                check=False,
            )
            if not (cr.stdout or "").strip():
                print(
                    "ERROR: colloquium-core is not running (needed to re-key users.llm_token). "
                    "Start it or pass --skip-llm-tokens.",
                    file=sys.stderr,
                )
                return 1
            if not old_pw:
                print(
                    "ERROR: empty secrets/cqds_db_password; cannot pull LLM tokens.",
                    file=sys.stderr,
                )
                return 1
            print("LLM tokens: pull + decrypt with current secret …")
            try:
                pull_out = _exec_colloquium_core_rekey(
                    root, compose_files, {"phase": "pull", "old_pw": old_pw}
                )
            except RuntimeError as e:
                print(f"ERROR: {e}", file=sys.stderr)
                return 1
            token_rows = pull_out.get("rows") or []
            print(f"  staged {len(token_rows)} user row(s) with non-empty llm_token")

        sql = _alter_roles_sql(new_pw)
        print("Applying ALTER ROLE postgres / cqds inside container …")
        try:
            _postgres_psql_sql(root, compose_files, sql)
        except subprocess.CalledProcessError as e:
            print(f"ERROR: psql failed: {e}", file=sys.stderr)
            return 1
    else:
        print("WARNING: --skip-db: database passwords not changed.", file=sys.stderr)
        token_rows = None

    print("Writing secrets/cqds_db_password …")
    _write_secret_file(pw_path, new_pw)

    if not args.skip_db and not args.skip_llm_tokens and token_rows is not None and len(token_rows) > 0:
        print("LLM tokens: push + encrypt with new secret …")
        try:
            _exec_colloquium_core_rekey(
                root,
                compose_files,
                {"phase": "push", "new_pw": new_pw, "rows": token_rows},
            )
        except RuntimeError as e:
            print(f"ERROR: {e}", file=sys.stderr)
            print(
                "CRITICAL: пароль PostgreSQL и файл секрета уже обновлены; восстановите БД из бэкапа "
                "или исправьте users.llm_token вручную.",
                file=sys.stderr,
            )
            return 1
        print("  push OK")

    if old_pw:
        print("Replacing old password substring in secrets/ and .env* …")
        touched = False
        for f in _collect_secret_files(root):
            if f.resolve() == pw_path.resolve():
                continue
            if _replace_in_file(f, root, old_pw, new_pw, dry_run=False):
                touched = True
        for f in _collect_env_files(root):
            if _replace_in_file(f, root, old_pw, new_pw, dry_run=False):
                touched = True
        if not touched:
            print("  (no other files contained the old substring)")
    else:
        print("No old password to search for in other files.")

    if not args.skip_restart:
        base = _docker_compose_cmd(root, compose_files)
        for svc in ("colloquium-core", "pg-backup-scheduler"):
            try:
                print(f"Restarting {svc} …")
                _run(base + ["restart", svc], check=False)
            except FileNotFoundError:
                break
    else:
        print("WARNING: --skip-restart: перезапустите colloquium-core вручную, иначе старый процесс держит старый пароль/ключ.", file=sys.stderr)

    print("")
    print("Готово.")
    if args.skip_llm_tokens and not args.skip_db:
        print(
            "ВНИМАНИЕ: --skip-llm-tokens — существующие enc:v1 в users.llm_token после смены пароля "
            "расшифровать будет нельзя; задайте ключи заново или восстановите из бэкапа."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
