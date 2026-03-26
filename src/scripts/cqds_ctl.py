#!/usr/bin/env python3
import argparse
import json
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path


FAIL_PATTERNS = [
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r'traceback',
        r'exception(?![./-])',
        r'failed',
        r'fatal',
        r'panic',
        r'crash',
        r'refused',
        r'unhealthy',
        r'module not found',
        r'no such file',
    )
]

STABLE_LOG_PATTERNS = {
    'colloquium-core': [re.compile(r'uvicorn', re.IGNORECASE)],
    'postgres': [re.compile(r'database system is ready to accept connections', re.IGNORECASE)],
    'frontend': [re.compile(r'ready', re.IGNORECASE)],
    'nginx-router': [re.compile(r'start worker process', re.IGNORECASE)],
}

BENIGN_LOG_PATTERNS = [
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r'uvicorn\.error:started server process',
        r'uvicorn\.error:waiting for application startup',
        r'uvicorn\.error:application startup complete',
        r'uvicorn\.error:uvicorn running on',
        r'front_errors\.log',
        r'mcp_errors\.log',
        r'exception\.td',
        r'exception\.log',
        r'\|\s+[d-][rwx-]{9}\s',
        r'failed_cmds',   # debug metric counters e.g. failed_cmds=0
    )
]

# Direct HTTP probes from the host (only services with ports exposed to the host)
HTTP_PROBES: dict[str, tuple[str, int]] = {
    'nginx-router': ('http://localhost:8008/', 5),
}

# Minimal image used for privileged log operations (must be present or pullable)
_LOG_HELPER_IMAGE = 'alpine'


class CommandError(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def is_benign_log_line(line: str) -> bool:
    return any(pattern.search(line) for pattern in BENIGN_LOG_PATTERNS)


def unique_preserve(items: list[str]) -> list[str]:
    result = []
    seen = set()
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        result.append(item)
    return result


class CqdsCtl:
    def __init__(self, root: Path):
        self.root = root

    def _run(self, args: list[str], check: bool = True) -> subprocess.CompletedProcess:
        result = subprocess.run(
            args,
            cwd=self.root,
            capture_output=True,
            text=True,
            encoding='utf-8',
            errors='replace',
        )
        if check and result.returncode != 0:
            stderr = (result.stderr or result.stdout).strip()
            raise CommandError(f'command failed: {" ".join(args)} :: {stderr}')
        return result

    def compose(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        return self._run(['docker', 'compose', *args], check=check)

    def docker(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        return self._run(['docker', *args], check=check)

    def list_services(self) -> list[str]:
        result = self.compose('config', '--services')
        return [line.strip() for line in result.stdout.splitlines() if line.strip()]

    def container_id(self, service: str) -> str | None:
        result = self.compose('ps', '-q', service, check=False)
        value = (result.stdout or '').strip()
        return value or None

    def inspect_container(self, container_id: str) -> dict:
        result = self.docker('inspect', container_id)
        payload = json.loads(result.stdout)
        return payload[0] if payload else {}

    def get_recent_logs(self, service: str, since: str | None = None, tail: int = 120) -> list[str]:
        extra = ['--since', since] if since else []
        result = self.compose('logs', '--no-color', f'--tail={tail}', *extra, service, check=False)
        content = (result.stdout or '') + ('\n' + result.stderr if result.stderr else '')
        return [line.rstrip() for line in content.splitlines() if line.strip()]

    def summarize_logs(self, service: str, lines: list[str]) -> dict:
        failures = [
            line for line in lines
            if any(pattern.search(line) for pattern in FAIL_PATTERNS) and not is_benign_log_line(line)
        ]
        stable_markers = [line for line in lines if any(pattern.search(line) for pattern in STABLE_LOG_PATTERNS.get(service, []))]
        return {
            'failure_lines': failures[-8:],
            'stable_lines': stable_markers[-5:],
        }

    def http_probe(self, url: str, timeout: int = 5) -> dict:
        """GET the url from the host and report outcome."""
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'cqds-ctl/1.0'})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return {'ok': resp.status < 400, 'status_code': resp.status, 'error': None}
        except urllib.error.HTTPError as exc:
            return {'ok': exc.code < 500, 'status_code': exc.code, 'error': None}
        except Exception as exc:
            return {'ok': False, 'status_code': None, 'error': str(exc)[:120]}

    def healthcheck_probe(self, container: str, inspect_data: dict) -> dict | None:
        """Replay the container's own healthcheck test and report exit code."""
        hc = (inspect_data.get('Config') or {}).get('Healthcheck') or {}
        test_cmd = hc.get('Test') or []
        if not test_cmd or test_cmd[0] == 'NONE':
            return None
        if test_cmd[0] == 'CMD-SHELL' and len(test_cmd) >= 2:
            args = ['docker', 'exec', container, 'sh', '-c', test_cmd[1]]
        elif test_cmd[0] == 'CMD' and len(test_cmd) >= 2:
            args = ['docker', 'exec', container, *test_cmd[1:]]
        else:
            return None
        try:
            result = subprocess.run(args, capture_output=True, timeout=15)
            return {'ok': result.returncode == 0, 'exit_code': result.returncode}
        except subprocess.TimeoutExpired:
            return {'ok': False, 'exit_code': -1, 'error': 'timeout'}
        except Exception as exc:
            return {'ok': False, 'exit_code': -1, 'error': str(exc)[:120]}

    def get_log_path(self, container_id: str) -> str | None:
        result = self.docker('inspect', '--format={{.LogPath}}', container_id, check=False)
        path = (result.stdout or '').strip()
        return path or None

    def _stat_log_size_via_container(self, log_dir: str, log_file: str) -> tuple[int | None, str | None]:
        """Return log file size from inside Docker VM via privileged helper container."""
        try:
            r = subprocess.run(
                ['docker', 'run', '--rm', '--privileged',
                 '-v', f'{log_dir}:/ld:ro',
                 _LOG_HELPER_IMAGE, 'sh', '-c', f"stat -c '%s' /ld/{log_file}"],
                capture_output=True, text=True, timeout=30,
            )
            if r.returncode != 0:
                return None, (r.stderr or r.stdout).strip()[:200]
            return int((r.stdout or '').strip()), None
        except ValueError:
            return None, 'stat output is not numeric'
        except Exception as exc:
            return None, str(exc)[:120]

    def _count_log_lines_via_container(self, log_dir: str, log_file: str) -> tuple[int | None, str | None]:
        """Return log file line count from inside Docker VM via privileged helper container."""
        try:
            r = subprocess.run(
                ['docker', 'run', '--rm', '--privileged',
                 '-v', f'{log_dir}:/ld:ro',
                 _LOG_HELPER_IMAGE, 'sh', '-c', f"wc -l < /ld/{log_file}"],
                capture_output=True, text=True, timeout=30,
            )
            if r.returncode != 0:
                return None, (r.stderr or r.stdout).strip()[:200]
            return int((r.stdout or '').strip()), None
        except ValueError:
            return None, 'wc output is not numeric'
        except Exception as exc:
            return None, str(exc)[:120]

    def clear_log_via_container(self, log_path: str) -> tuple[int | None, int | None, int | None, bool, str | None]:
        """Stat + count lines + truncate a Docker json-file log by running short-lived privileged containers.

        Works on Docker Desktop (WSL2/VM) because the bind-mount path is resolved inside
        the Docker engine's VM, where /var/lib/docker/containers/ is always accessible.
        Returns (lines_before, bytes_before, bytes_after, ok, error_message).
        """
        log_dir, log_file = log_path.rsplit('/', 1)
        bytes_before, err = self._stat_log_size_via_container(log_dir, log_file)
        if err:
            return None, None, None, False, err

        lines_before, err_lines = self._count_log_lines_via_container(log_dir, log_file)
        if err_lines:
            return None, bytes_before, None, False, err_lines

        script = f"truncate -s 0 /ld/{log_file}"
        try:
            r = subprocess.run(
                ['docker', 'run', '--rm', '--privileged',
                 '-v', f'{log_dir}:/ld',
                 _LOG_HELPER_IMAGE, 'sh', '-c', script],
                capture_output=True, text=True, timeout=30,
            )
            if r.returncode != 0:
                return lines_before, bytes_before, None, False, (r.stderr or r.stdout).strip()[:200]

            bytes_after, err_after = self._stat_log_size_via_container(log_dir, log_file)
            if err_after:
                return lines_before, bytes_before, None, False, err_after
            return lines_before, bytes_before, bytes_after, bytes_after == 0, None
        except Exception as exc:
            return lines_before, bytes_before, None, False, str(exc)[:120]

    def service_status(self, service: str) -> dict:
        container_id = self.container_id(service)
        status = {
            'service': service,
            'container_id': container_id,
            'exists': bool(container_id),
            'state': 'missing',
            'health': None,
            'started_at': None,
            'finished_at': None,
            'exit_code': None,
            'restart_count': None,
            'status': 'missing',
            'stable': False,
            'http_probe': None,
            'hc_probe': None,
            'problems': [],
            'warnings': [],
            'log_summary': {
                'failure_lines': [],
                'stable_lines': [],
            },
        }
        if not container_id:
            status['problems'].append('Container is not created')
            return status

        info = self.inspect_container(container_id)
        state = info.get('State') or {}
        health = (state.get('Health') or {}).get('Status')
        raw_state = state.get('Status') or 'unknown'
        started_at = state.get('StartedAt')
        status.update({
            'state': raw_state,
            'health': health,
            'started_at': started_at,
            'finished_at': state.get('FinishedAt'),
            'exit_code': state.get('ExitCode'),
            'restart_count': info.get('RestartCount'),
        })

        # Only fetch logs from the current container run to avoid stale failure lines
        since = started_at if raw_state == 'running' else None
        logs = self.get_recent_logs(service, since=since)
        log_summary = self.summarize_logs(service, logs)
        status['log_summary'] = log_summary

        # Active live probes (only for running containers)
        if raw_state == 'running':
            status['hc_probe'] = self.healthcheck_probe(container_id, info)
            if service in HTTP_PROBES:
                url, timeout = HTTP_PROBES[service]
                status['http_probe'] = self.http_probe(url, timeout)

        if raw_state in ('exited', 'dead'):
            status['status'] = 'failed'
            status['problems'].append(f'Container state is {raw_state}')
        elif raw_state == 'restarting':
            status['status'] = 'starting'
            status['problems'].append('Container is restarting')
        elif health == 'unhealthy':
            status['status'] = 'failed'
            status['problems'].append('Healthcheck is unhealthy')
        elif health == 'starting':
            status['status'] = 'starting'
        elif raw_state == 'running' and health == 'healthy':
            status['status'] = 'healthy'
            status['stable'] = True
        elif raw_state == 'running' and health is None:
            status['status'] = 'running'
            stable_lines = log_summary['stable_lines']
            failure_lines = log_summary['failure_lines']
            status['stable'] = True
            if stable_lines:
                status['status'] = 'healthy'
            if failure_lines:
                status['warnings'].extend(failure_lines[-3:])
        elif raw_state == 'running':
            status['status'] = 'running'

        # Let live healthcheck probe override the Docker engine's cached health state
        hc = status['hc_probe']
        if hc is not None and not hc['ok'] and status['stable']:
            status['stable'] = False
            status['status'] = 'starting'
            status['warnings'].append(f'Live healthcheck probe failed (exit {hc["exit_code"]})')

        http = status['http_probe']
        if http is not None and not http['ok']:
            target = HTTP_PROBES[service][0]
            detail = http.get('error') or http.get('status_code')
            status['warnings'].append(f'HTTP probe {target} failed: {detail}')

        if log_summary['failure_lines'] and not status['stable']:
            status['problems'].extend(log_summary['failure_lines'][-3:])
        elif log_summary['failure_lines'] and status['stable']:
            status['warnings'].extend(log_summary['failure_lines'][-3:])

        status['problems'] = unique_preserve(status['problems'])
        status['warnings'] = unique_preserve(status['warnings'])

        if raw_state == 'running' and health is None and not status['stable'] and not status['problems']:
            status['problems'].append('Running without healthcheck and without stable log markers yet')
        return status

    def gather_status(self, services: list[str] | None = None) -> dict:
        selected = services or self.list_services()
        service_rows = [self.service_status(service) for service in selected]
        if all(row['stable'] for row in service_rows):
            overall = 'stable'
        elif any(row['status'] == 'failed' for row in service_rows):
            overall = 'failed'
        else:
            overall = 'starting'
        return {
            'timestamp': utc_now(),
            'root': str(self.root),
            'overall_status': overall,
            'services': service_rows,
        }

    def wait_for_stable(self, services: list[str] | None = None, timeout: int = 90, interval: float = 2.0) -> dict:
        deadline = time.monotonic() + timeout
        last_snapshot = self.gather_status(services)
        while time.monotonic() < deadline:
            if last_snapshot['overall_status'] in ('stable', 'failed'):
                return last_snapshot
            time.sleep(interval)
            last_snapshot = self.gather_status(services)
        last_snapshot['overall_status'] = 'timeout' if last_snapshot['overall_status'] != 'failed' else 'failed'
        return last_snapshot

    def restart(self, services: list[str] | None, timeout: int) -> dict:
        selected = services or self.list_services()
        self.compose('restart', *selected)
        return self.wait_for_stable(selected, timeout=timeout)

    def rebuild(self, services: list[str] | None, timeout: int) -> dict:
        selected = services or self.list_services()
        self.compose('up', '-d', '--build', *selected)
        return self.wait_for_stable(selected, timeout=timeout)

    def clear_logs(self, services: list[str] | None = None) -> dict:
        """Truncate Docker json-file log via the Docker Desktop WSL VM."""
        selected = services or self.list_services()
        cleared = []
        for service in selected:
            cid = self.container_id(service)
            entry: dict = {
                'service': service,
                'container_id': cid,
                'log_path': None,
                'lines_before': None,
                'bytes_before': None,
                'bytes_after': None,
                'ok': False,
                'error': None,
            }
            if not cid:
                entry['error'] = 'container not found'
                cleared.append(entry)
                continue
            log_path = self.get_log_path(cid)
            if not log_path:
                entry['error'] = 'could not determine log path'
                cleared.append(entry)
                continue
            entry['log_path'] = log_path
            lines_before, bytes_before, bytes_after, ok, err = self.clear_log_via_container(log_path)
            entry['lines_before'] = lines_before
            entry['bytes_before'] = bytes_before
            entry['bytes_after'] = bytes_after
            entry['ok'] = ok
            if not ok:
                entry['error'] = err or 'truncate via privileged container failed'
            cleared.append(entry)
        all_ok = all(e['ok'] for e in cleared)
        return {'timestamp': utc_now(), 'root': str(self.root), 'all_ok': all_ok, 'cleared': cleared}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description='CQDS docker compose helper with JSON diagnostics')
    parser.add_argument('--root', default=str(Path(__file__).resolve().parents[1]), help='CQDS runtime root with docker-compose.yml')

    subparsers = parser.add_subparsers(dest='command', required=True)

    def add_common_options(subparser: argparse.ArgumentParser) -> None:
        subparser.add_argument('--timeout', type=int, default=90, help='Seconds to wait for stable/failed state')
        subparser.add_argument('--pretty', action='store_true', help='Pretty-print JSON')

    status_parser = subparsers.add_parser('status', help='Show current container status')
    add_common_options(status_parser)
    status_parser.add_argument('--wait', action='store_true', help='Wait for a stable or failed state before printing JSON')
    status_parser.add_argument('services', nargs='*', help='Optional compose service names')

    restart_parser = subparsers.add_parser('restart', help='Restart one or more services and wait for result')
    add_common_options(restart_parser)
    restart_parser.add_argument('services', nargs='*', help='Optional compose service names')

    rebuild_parser = subparsers.add_parser('rebuild', help='Rebuild and restart one or more services and wait for result')
    add_common_options(rebuild_parser)
    rebuild_parser.add_argument('services', nargs='*', help='Optional compose service names')

    clear_logs_parser = subparsers.add_parser('clear-logs', help='Truncate container log files via Docker Desktop WSL VM')
    clear_logs_parser.add_argument('--pretty', action='store_true', help='Pretty-print JSON')
    clear_logs_parser.add_argument('services', nargs='*', help='Optional compose service names (default: all)')

    return parser.parse_args()


def emit(payload: dict, pretty: bool) -> None:
    json.dump(payload, sys.stdout, indent=2 if pretty else None, ensure_ascii=False)
    sys.stdout.write('\n')


def main() -> int:
    args = parse_args()
    ctl = CqdsCtl(Path(args.root).resolve())
    try:
        if args.command == 'status':
            payload = ctl.wait_for_stable(args.services, timeout=args.timeout) if args.wait else ctl.gather_status(args.services)
        elif args.command == 'restart':
            payload = ctl.restart(args.services, timeout=args.timeout)
        elif args.command == 'rebuild':
            payload = ctl.rebuild(args.services, timeout=args.timeout)
        elif args.command == 'clear-logs':
            payload = ctl.clear_logs(args.services)
        else:
            raise CommandError(f'Unsupported command: {args.command}')
        emit(payload, args.pretty)
        if args.command == 'clear-logs':
            return 0 if payload.get('all_ok') else 1
        return 0 if payload.get('overall_status') in ('stable', 'starting') else 1
    except CommandError as exc:
        emit({'timestamp': utc_now(), 'overall_status': 'error', 'error': str(exc)}, args.pretty)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())