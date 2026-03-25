"""test_phase1.py — smoke-тест для фазы 1 новых MCP-инструментов.

Проверяет напрямую через HTTP (без MCP-протокола):
  1. POST /api/project/exec  → cq_exec
  2. GET  /api/chat/file_contents → cq_read_file
  3. Логику sync-mode cq_send_message (опционально, только если LLM отвечает)

Запуск:
  python src/test_phase1.py
  python src/test_phase1.py --url http://localhost:8008 --username copilot --password devspace
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
import urllib.error
import urllib.parse
import http.cookiejar

_config = {
    "url": "http://localhost:8008",
    "username": "copilot",
    "password": "devspace",
}

PASS = "\033[32mPASS\033[0m"
FAIL = "\033[31mFAIL\033[0m"
SKIP = "\033[33mSKIP\033[0m"


def api(cj: http.cookiejar.CookieJar, method: str, path: str,
        body: dict | None = None, timeout: float = 30.0) -> tuple[int, str]:
    url = _config["url"].rstrip("/") + path
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"} if data else {}
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
    try:
        with opener.open(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode(errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(errors="replace")


def step(label: str, ok: bool, detail: str = "") -> bool:
    status = PASS if ok else FAIL
    print(f"  [{status}] {label}" + (f": {detail}" if detail else ""))
    return ok


def main() -> None:
    global _config
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default=_config["url"])
    parser.add_argument("--username", default=_config["username"])
    parser.add_argument("--password", default=_config["password"])
    args = parser.parse_args()

    _config["url"] = args.url.rstrip("/")

    cj = http.cookiejar.CookieJar()
    passed = failed = 0

    # -----------------------------------------------------------------------
    print("\n=== [1] Логин ===")
    code, body = api(cj, "POST", "/api/login",
                     {"username": args.username, "password": args.password})
    ok = step("POST /api/login → 200", code == 200, f"HTTP {code}")
    if not ok:
        print(f"\nОтвет: {body[:200]}")
        sys.exit(1)
    passed += 1
    print(f"    Ответ: {body[:80]}")

    # -----------------------------------------------------------------------
    print("\n=== [2] Получить список проектов ===")
    code, body = api(cj, "GET", "/api/project/list")
    ok = step("GET /api/project/list → 200", code == 200, f"HTTP {code}")
    passed += ok; failed += (not ok)
    projects = json.loads(body) if code == 200 else []
    if projects:
        pid = projects[0]["id"]
        pname = projects[0]["project_name"]
        print(f"    Будем использовать project_id={pid} ({pname})")
    else:
        print(f"    {SKIP} Нет проектов — пропускаем тесты exec и read_file")
        pid = None

    # -----------------------------------------------------------------------
    print("\n=== [3] cq_exec — POST /api/project/exec ===")
    if pid is not None:
        t0 = time.monotonic()
        code, body = api(cj, "POST", "/api/project/exec",
                         {"project_id": pid, "command": "echo CQEXEC_OK && pwd", "timeout": 10},
                         timeout=20.0)
        elapsed = time.monotonic() - t0

        ok = step("HTTP 200", code == 200, f"HTTP {code}")
        passed += ok; failed += (not ok)

        if code == 200:
            result = json.loads(body)
            ok2 = step("status=success", result.get("status") == "success", repr(result.get("status")))
            ok3 = step("output содержит CQEXEC_OK", "CQEXEC_OK" in result.get("output", ""),
                       repr(result.get("output", "")[:80]))
            ok4 = step("project в ответе", bool(result.get("project")), repr(result.get("project")))
            passed += ok2 + ok3 + ok4
            failed += (not ok2) + (not ok3) + (not ok4)
            print(f"    Время выполнения: {elapsed:.2f}s")
            print(f"    output (первые 120 байт): {result.get('output','')[:120]!r}")
        else:
            print(f"    Тело ответа: {body[:200]}")
    else:
        print(f"  [{SKIP}] project_id недоступен — тест пропущен")

    # -----------------------------------------------------------------------
    print("\n=== [4] cq_exec — граничные случаи ===")
    if pid is not None:
        # Пустая команда → 400
        code, body = api(cj, "POST", "/api/project/exec",
                         {"project_id": pid, "command": "", "timeout": 5})
        ok = step("Пустая команда → 400", code == 400, f"HTTP {code}")
        passed += ok; failed += (not ok)

        # Несуществующий проект → 404
        code, body = api(cj, "POST", "/api/project/exec",
                         {"project_id": 999999, "command": "echo hi", "timeout": 5})
        ok = step("Несуществующий project_id → 404", code == 404, f"HTTP {code}")
        passed += ok; failed += (not ok)

    # -----------------------------------------------------------------------
    print("\n=== [5] cq_read_file — GET /api/chat/file_contents ===")
    if pid is not None:
        # Получаем хотя бы один file_id из file_index
        code, body = api(cj, "GET", f"/api/project/file_index?project_id={pid}")
        if code == 200:
            files = json.loads(body)
            if files:
                fid = files[0]["id"]
                fname = files[0]["file_name"]
                t0 = time.monotonic()
                code2, content = api(cj, "GET", f"/api/chat/file_contents?file_id={fid}", timeout=15.0)
                elapsed = time.monotonic() - t0
                ok = step(f"GET /api/chat/file_contents?file_id={fid} → 200",
                          code2 == 200, f"HTTP {code2}")
                passed += ok; failed += (not ok)
                ok2 = step("Непустое содержимое", len(content) > 0, f"{len(content)} байт")
                passed += ok2; failed += (not ok2)
                print(f"    Файл: {fname}  |  {len(content)} байт  |  {elapsed:.2f}s")
                print(f"    Первые 80 байт: {content[:80]!r}")
            else:
                print(f"  [{SKIP}] В проекте нет файлов")
        else:
            print(f"  [{SKIP}] file_index вернул HTTP {code}")

        # Несуществующий file_id → 404
        code, body = api(cj, "GET", "/api/chat/file_contents?file_id=999999")
        ok = step("Несуществующий file_id → 404", code == 404, f"HTTP {code}")
        passed += ok; failed += (not ok)
    else:
        print(f"  [{SKIP}] project_id недоступен — тест пропущен")

    # -----------------------------------------------------------------------
    print("\n=== [6] Логаут ===")
    code, _ = api(cj, "POST", "/api/logout")
    ok = step("POST /api/logout → 200", code == 200, f"HTTP {code}")
    passed += ok; failed += (not ok)

    # -----------------------------------------------------------------------
    print(f"\n{'='*40}")
    color = "\033[32m" if failed == 0 else "\033[31m"
    print(f"{color}Итог: {passed} passed, {failed} failed\033[0m")
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
