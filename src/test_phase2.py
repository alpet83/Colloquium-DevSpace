"""test_phase2.py — smoke-тест фазы 2 (cq_smart_grep, cq_replace).

Тестирует прямые API маршруты:
  POST /api/project/smart_grep
  POST /api/project/replace

Запуск:
  python src/test_phase2.py
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.parse
import urllib.request
import http.cookiejar

BASE = "http://localhost:8008"
USER = "copilot"
PASSWD = "devspace"


def api(cj: http.cookiejar.CookieJar, method: str, path: str, body: dict | None = None) -> tuple[int, str]:
    url = BASE.rstrip('/') + path
    data = json.dumps(body).encode() if body is not None else None
    headers = {'Content-Type': 'application/json'} if data else {}
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
    try:
        with opener.open(req, timeout=25) as resp:
            return resp.status, resp.read().decode(errors='replace')
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(errors='replace')


def ok(msg: str) -> None:
    print(f"[PASS] {msg}")


def fail(msg: str, body: str = "") -> None:
    print(f"[FAIL] {msg}")
    if body:
        print(body[:300])


def main() -> int:
    cj = http.cookiejar.CookieJar()

    code, body = api(cj, 'POST', '/api/login', {'username': USER, 'password': PASSWD})
    if code != 200:
        fail('login', body)
        return 1
    ok('login')

    code, body = api(cj, 'GET', '/api/project/list')
    if code != 200:
        fail('project/list', body)
        return 1
    projects = json.loads(body)
    if not projects:
        fail('project/list empty')
        return 1
    pid = projects[0]['id']
    print(f"Using project_id={pid}")

    code, body = api(cj, 'POST', '/api/project/smart_grep', {
        'project_id': pid,
        'query': 'project',
        'mode': 'all',
        'profile': 'all',
        'is_regex': False,
        'case_sensitive': False,
        'max_results': 10,
        'context_lines': 0,
    })
    if code != 200:
        fail('project/smart_grep HTTP', body)
        return 1
    data = json.loads(body)
    if data.get('status') != 'ok':
        fail('project/smart_grep status', json.dumps(data, ensure_ascii=False))
        return 1
    if data.get('total', 0) < 1:
        fail('project/smart_grep no hits', json.dumps(data, ensure_ascii=False))
        return 1
    ok(f"project/smart_grep total={data.get('total')}")

    # profile + time_strict sanity check
    code, body = api(cj, 'POST', '/api/project/smart_grep', {
        'project_id': pid,
        'query': 'project',
        'mode': 'all',
        'profile': 'backend',
        'time_strict': 'mtime>2000-01-01',
        'is_regex': False,
        'case_sensitive': False,
        'max_results': 5,
    })
    if code != 200:
        fail('project/smart_grep profile+time HTTP', body)
        return 1
    data2 = json.loads(body)
    if data2.get('status') != 'ok' or data2.get('profile') != 'backend':
        fail('project/smart_grep profile+time status', json.dumps(data2, ensure_ascii=False))
        return 1
    ok(f"project/smart_grep profile+time total={data2.get('total')}")

    code, body = api(cj, 'GET', f'/api/project/file_index?project_id={pid}')
    if code != 200:
        fail('project/file_index', body)
        return 1
    files = json.loads(body)
    if not files:
        fail('project/file_index empty')
        return 1
    file_id = files[0]['id']

    code, body = api(cj, 'POST', '/api/project/replace', {
        'project_id': pid,
        'file_id': file_id,
        'old': '__THIS_PATTERN_SHOULD_NOT_EXIST__',
        'new': '__THIS_PATTERN_SHOULD_NOT_EXIST__',
        'is_regex': False,
        'case_sensitive': True,
    })
    if code != 200:
        fail('project/replace HTTP', body)
        return 1
    rep = json.loads(body)
    if rep.get('status') not in ('ok', 'no_changes'):
        fail('project/replace status', json.dumps(rep, ensure_ascii=False))
        return 1
    if rep.get('status') != 'no_changes':
        ok(f"project/replace status={rep.get('status')} replaced={rep.get('replaced')}")
    else:
        ok('project/replace no_changes')

    code, body = api(cj, 'POST', '/api/project/smart_grep', {
        'project_id': pid,
        'query': '([',
        'mode': 'code',
        'is_regex': True,
    })
    if code != 400:
        fail('project/smart_grep invalid regex should return 400', body)
        return 1
    ok('project/smart_grep invalid regex returns 400')

    code, body = api(cj, 'POST', '/api/project/smart_grep', {
        'project_id': pid,
        'query': 'project',
        'mode': 'all',
        'time_strict': 'mtime>>2026-01-01',
    })
    if code != 400:
        fail('project/smart_grep invalid time_strict should return 400', body)
        return 1
    ok('project/smart_grep invalid time_strict returns 400')

    api(cj, 'POST', '/api/logout')
    ok('logout')

    print('All phase-2 checks passed')
    return 0


if __name__ == "__main__":
    sys.exit(main())
