#!/usr/bin/env python3
"""Оставить N чатов с минимальными chat_id, остальные удалить (посты удаляются вместе с чатом).

Учитывает подчаты: повторные проходы, пока что-то удаётся удалить (иначе API: sub-chats).

По умолчанию dry-run; удаление: --yes

Если DELETE /api/chat/delete на сервере с PostgreSQL сломан (проверка sub-chats), используйте
--db-exec-trim: прямой DELETE через /api/project/exec (только localhost / private URL).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

_THIS = Path(__file__).resolve()
_MCP_TOOLS = _THIS.parents[1]
if str(_MCP_TOOLS) not in sys.path:
    sys.path.insert(0, str(_MCP_TOOLS))

from cqds_client import ColloquiumClient
import cqds_credentials as cq_cred


def _db_exec_trim_script(keep_ids: list[int]) -> str:
    """Python для выполнения внутри контейнера агента (/api/project/exec)."""
    ks = repr(set(int(x) for x in keep_ids))
    body = """
db = Database.get_database()
deleted = []
for _round in range(8000):
    rows = db.fetch_all("SELECT chat_id FROM chats ORDER BY chat_id")
    ids = [int(r[0]) for r in rows if r[0] is not None]
    victims = [i for i in ids if i not in KEEP]
    if not victims:
        break
    progressed = False
    for cid in sorted(victims, reverse=True):
        sub = db.fetch_all(
            "SELECT 1 FROM chats c INNER JOIN posts p ON p.id = c.parent_msg_id "
            "WHERE p.chat_id = :cid LIMIT 1",
            dict(cid=cid),
        )
        if sub:
            continue
        for q in (
            "DELETE FROM llm_context WHERE chat_id = :cid",
            "DELETE FROM llm_responses WHERE chat_id = :cid",
        ):
            try:
                db.execute(q, dict(cid=cid))
            except Exception:
                pass
        db.execute("DELETE FROM posts WHERE chat_id = :cid", dict(cid=cid))
        db.execute("DELETE FROM chats WHERE chat_id = :cid", dict(cid=cid))
        deleted.append(cid)
        progressed = True
    if not progressed:
        break
left = [int(r[0]) for r in db.fetch_all("SELECT chat_id FROM chats ORDER BY chat_id")]
bad = [x for x in left if x not in KEEP]
print(json.dumps({"deleted_count": len(deleted), "deleted_sample": deleted[:40], "remaining_not_in_keep": bad}))
"""
    return f"import json\nfrom managers.db import Database\nKEEP = {ks}\n" + body.lstrip("\n")


async def run(args: argparse.Namespace) -> dict[str, Any]:
    password, _ = cq_cred.resolve_password(args.password, args.password_file)
    if not password:
        raise RuntimeError("Password required.")

    client = ColloquiumClient(base_url=args.url, username=args.username, password=password)
    report: dict[str, Any] = {
        "keep_count": int(args.keep),
        "dry_run": not args.yes,
        "kept_ids": [],
        "target_delete_ids": [],
        "deleted": [],
        "failed": [],
    }
    try:
        await client.select_project(args.project_id)
        chats = await client.list_chats()
        if not isinstance(chats, list):
            chats = []
        rows = [c for c in chats if isinstance(c, dict) and c.get("chat_id") is not None]
        by_id = sorted(rows, key=lambda c: int(c["chat_id"]))
        all_ids = [int(c["chat_id"]) for c in by_id]
        keep_n = max(0, int(args.keep))
        kept = all_ids[:keep_n]
        to_del = set(all_ids[keep_n:])
        report["kept_ids"] = kept
        report["target_delete_ids"] = sorted(to_del)

        if not args.yes or not to_del:
            report["note"] = "dry-run or nothing to delete"
            return report

        if args.db_exec_trim:
            if not client.is_local_or_private_endpoint():
                report["error"] = "--db-exec-trim только для localhost / частной сети"
                return report
            py = _db_exec_trim_script(kept)
            cmd = "PYTHONPATH=/app/agent /app/venv/bin/python - <<'PY'\n" + py + "\nPY"
            ex = await client.exec_command(args.project_id, cmd, timeout=int(args.exec_timeout))
            report["db_exec_raw"] = ex
            out = str(ex.get("output") or "")
            report["db_exec_stdout_tail"] = out[-4000:]
            m = None
            for line in out.splitlines():
                line = line.strip()
                if line.startswith("{") and "deleted_count" in line:
                    try:
                        m = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    break
            if isinstance(m, dict):
                report["db_deleted_count"] = m.get("deleted_count")
                report["db_deleted_sample"] = m.get("deleted_sample")
                report["db_remaining_not_in_keep"] = m.get("remaining_not_in_keep")
            report["remaining_delete_ids"] = []
            return report

        # Несколько раундов: подчаты мешают удалить родителя, пока дети живы.
        max_rounds = max(50, len(to_del) + 5)
        for _ in range(max_rounds):
            if not to_del:
                break
            progress = False
            for cid in sorted(to_del):
                try:
                    r = await client.delete_chat(cid)
                    if isinstance(r, dict) and r.get("status") == "ok":
                        to_del.discard(cid)
                        report["deleted"].append(cid)
                        progress = True
                    else:
                        err = (r or {}).get("error", str(r))
                        # не дублировать одну и ту же ошибку каждый раунд
                        prev = [x for x in report["failed"] if x.get("chat_id") == cid]
                        if not prev or prev[-1].get("error") != err:
                            report["failed"].append({"chat_id": cid, "error": err})
                except Exception as exc:  # noqa: BLE001
                    report["failed"].append({"chat_id": cid, "error": str(exc)})
            if not progress:
                break

        report["remaining_delete_ids"] = sorted(to_del)
    finally:
        await client.aclose()

    return report


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--url", default=os.environ.get("COLLOQUIUM_URL", "http://localhost:8008"))
    ap.add_argument("--username", default=os.environ.get("COLLOQUIUM_USERNAME", "copilot"))
    ap.add_argument("--password", default=os.environ.get("COLLOQUIUM_PASSWORD", ""))
    ap.add_argument("--password-file", default=cq_cred.default_password_file_for_cli())
    ap.add_argument("--project-id", type=int, default=2)
    ap.add_argument("--keep", type=int, default=4, help="Сколько чатов с наименьшими id оставить")
    ap.add_argument("--yes", action="store_true", help="Выполнить удаление")
    ap.add_argument(
        "--db-exec-trim",
        action="store_true",
        help="Удалять через Python в контейнере агента (обход бага sub-chats в API на PG)",
    )
    ap.add_argument("--exec-timeout", type=int, default=180)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    try:
        payload = asyncio.run(run(args))
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(f"Оставляем chat_id: {payload['kept_ids']}")
        print(f"К удалению: {len(payload['target_delete_ids'])} чатов")
        if payload["dry_run"]:
            print("Dry-run. Запустите с --yes")
        else:
            if payload.get("db_deleted_count") is not None:
                print(f"DB-exec: удалено чатов (итераций): {payload['db_deleted_count']}")
                if payload.get("db_remaining_not_in_keep"):
                    print("Остались лишние id:", payload["db_remaining_not_in_keep"])
            else:
                print(f"Удалено (API): {len(payload['deleted'])}")
            if payload.get("remaining_delete_ids"):
                print(f"Не удалось (остались): {payload['remaining_delete_ids']}")
            if payload.get("failed"):
                print("Ошибки (фрагмент):", payload["failed"][:20])
            if payload.get("error"):
                print("Ошибка:", payload["error"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
