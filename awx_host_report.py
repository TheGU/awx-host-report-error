#!/usr/bin/env python3
"""Generate a per-host CSV report for an AWX job.

For a given AWX job ID, queries the AWX REST API and writes a CSV listing
every host the job touched, its status, and (for failures) the failing
task and error message extracted from the playbook event.

Usage:
    python awx_host_report.py <job-id> [-o report.csv] [--failures-only] ...

Environment:
    AWX_URL    Base URL of the AWX server (e.g. https://awx.example.com)
    AWX_TOKEN  Personal OAuth2 token (AWX UI -> Users -> Tokens)
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import urljoin, urlparse

import requests

FAILURE_EVENTS = "runner_on_failed,runner_on_unreachable,runner_on_async_failed"
PAGE_SIZE = 200
CSV_COLUMNS = ["play", "host", "status", "task", "message"]


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Write a per-host CSV report for an AWX job."
    )
    p.add_argument("job_id", type=int, help="AWX job ID")
    p.add_argument(
        "-o", "--output",
        help="Output CSV path (default: awx-job-<id>-report.csv)",
    )
    p.add_argument("--url", help="AWX base URL (overrides AWX_URL)")
    p.add_argument("--token", help="AWX OAuth2 token (overrides AWX_TOKEN)")
    p.add_argument(
        "--ca-bundle",
        help="Path to CA bundle for TLS verification. "
             "Defaults to ./awx-fullchain.crt or ../awx-fullchain.crt if present.",
    )
    p.add_argument(
        "--insecure", action="store_true",
        help="Disable TLS verification (use only for ad-hoc testing).",
    )
    g = p.add_mutually_exclusive_group()
    g.add_argument(
        "--failures-only", action="store_true",
        help="Only include FAILED and UNREACHABLE rows.",
    )
    g.add_argument(
        "--include-ok", action="store_true", default=True,
        help="Include OK/CHANGED/SKIPPED rows (default).",
    )
    return p.parse_args()


def resolve_ca_bundle(explicit: str | None) -> str | bool:
    """Return a value suitable for requests' `verify=` parameter."""
    if explicit:
        return explicit
    script_dir = Path(__file__).resolve().parent
    for candidate in (
        script_dir / "awx-fullchain.crt",
        script_dir.parent / "awx-fullchain.crt",
    ):
        if candidate.is_file():
            return str(candidate)
    return True  # fall back to system CA store


class AwxClient:
    def __init__(self, base_url: str, token: str, verify: str | bool):
        if not base_url:
            raise SystemExit("error: AWX URL not set (use --url or AWX_URL env var)")
        if not token:
            raise SystemExit("error: AWX token not set (use --token or AWX_TOKEN env var)")
        self.base_url = base_url.rstrip("/")
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        })
        self.session.verify = verify

    def _full_url(self, url: str) -> str:
        if urlparse(url).netloc:
            return url
        return urljoin(self.base_url + "/", url.lstrip("/"))

    def get(self, path: str, **params: Any) -> dict:
        url = self._full_url(path)
        resp = self.session.get(url, params=params or None, timeout=60)
        if resp.status_code == 401:
            raise SystemExit("error: 401 Unauthorized — check AWX_TOKEN")
        if resp.status_code == 404:
            raise NotFound(url)
        resp.raise_for_status()
        return resp.json()

    def paginated(self, path: str, **params: Any) -> Iterator[dict]:
        """Yield every `result` across all pages of a list endpoint."""
        data = self.get(path, **params)
        while True:
            yield from data.get("results", [])
            next_url = data.get("next")
            if not next_url:
                return
            data = self.get(next_url)


class NotFound(Exception):
    pass


def extract_message(event_data: dict) -> str:
    """Pull the most useful human-readable message out of an event payload.

    Matches the precedence the AWX UI uses: `res.msg` first, then stderr
    variants, then the whole `res` dict as a last resort.
    """
    res = (event_data or {}).get("res") or {}
    for key in ("msg", "stderr", "module_stderr", "reason"):
        val = res.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
        if val:  # non-string truthy (list, dict) — stringify
            return json.dumps(val, ensure_ascii=False)
    if res:
        return json.dumps(res, ensure_ascii=False)
    # unreachable events sometimes put it at the top level
    top_msg = (event_data or {}).get("msg")
    return top_msg.strip() if isinstance(top_msg, str) else ""


def status_from_summary(s: dict) -> str:
    """Map a job_host_summary record to a single status label."""
    if s.get("failed") or s.get("failures", 0) > 0:
        return "FAILED"
    if s.get("dark", 0) > 0:  # 'dark' = unreachable in AWX
        return "UNREACHABLE"
    if s.get("changed", 0) > 0:
        return "CHANGED"
    if s.get("ok", 0) > 0:
        return "OK"
    if s.get("skipped", 0) > 0:
        return "SKIPPED"
    return "NO-OP"


def event_status(event_name: str) -> str:
    if event_name == "runner_on_unreachable":
        return "UNREACHABLE"
    return "FAILED"


def build_rows(
    summaries: list[dict],
    failure_events: list[dict],
    failures_only: bool,
) -> list[dict]:
    # Index failure events by host so we can attach them to summary rows.
    fails_by_host: dict[str, list[dict]] = {}
    for ev in failure_events:
        host = ev.get("host_name") or ""
        if not host:
            continue
        fails_by_host.setdefault(host, []).append(ev)

    rows: list[dict] = []
    seen_hosts: set[str] = set()

    for s in summaries:
        host = s.get("host_name") or ""
        if not host:
            continue
        seen_hosts.add(host)
        host_fails = fails_by_host.get(host, [])
        if host_fails:
            for ev in host_fails:
                rows.append({
                    "play": ev.get("play") or "",
                    "host": host,
                    "status": event_status(ev.get("event") or ""),
                    "task": ev.get("task") or "",
                    "message": extract_message(ev.get("event_data") or {}),
                })
        else:
            status = status_from_summary(s)
            if failures_only:
                continue
            rows.append({
                "play": "",
                "host": host,
                "status": status,
                "task": "",
                "message": "",
            })

    # Failure events for hosts not present in summaries (rare, but possible
    # if AWX is mid-write). Include them so we never silently drop failures.
    for host, evs in fails_by_host.items():
        if host in seen_hosts:
            continue
        for ev in evs:
            rows.append({
                "play": ev.get("play") or "",
                "host": host,
                "status": event_status(ev.get("event") or ""),
                "task": ev.get("task") or "",
                "message": extract_message(ev.get("event_data") or {}),
            })

    rows.sort(key=lambda r: (r["play"], r["host"]))
    return rows


def main() -> int:
    args = parse_args()
    base_url = args.url or os.environ.get("AWX_URL", "")
    token = args.token or os.environ.get("AWX_TOKEN", "")

    if args.insecure:
        verify: str | bool = False
        # Silence the single warning per run — user opted in.
        from urllib3.exceptions import InsecureRequestWarning
        requests.packages.urllib3.disable_warnings(InsecureRequestWarning)  # type: ignore[attr-defined]
    else:
        verify = resolve_ca_bundle(args.ca_bundle)

    client = AwxClient(base_url, token, verify)

    try:
        job = client.get(f"/api/v2/jobs/{args.job_id}/")
    except NotFound:
        print(f"error: job {args.job_id} not found", file=sys.stderr)
        return 2
    except requests.RequestException as e:
        print(f"error: failed to reach AWX: {e}", file=sys.stderr)
        return 1

    job_name = job.get("name") or f"job-{args.job_id}"
    print(f"Job {args.job_id}: {job_name}  (status: {job.get('status')})", file=sys.stderr)

    try:
        summaries = list(client.paginated(
            f"/api/v2/jobs/{args.job_id}/job_host_summaries/",
            page_size=PAGE_SIZE,
        ))
        failure_events = list(client.paginated(
            f"/api/v2/jobs/{args.job_id}/job_events/",
            page_size=PAGE_SIZE,
            **{"event__in": FAILURE_EVENTS},
        ))
    except requests.RequestException as e:
        print(f"error: AWX API call failed: {e}", file=sys.stderr)
        return 1

    rows = build_rows(summaries, failure_events, args.failures_only)

    output = args.output or f"awx-job-{args.job_id}-report.csv"
    with open(output, "w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)

    failed = sum(1 for r in rows if r["status"] in ("FAILED", "UNREACHABLE"))
    print(
        f"Wrote {len(rows)} row(s) to {output}  "
        f"({failed} failure row(s), {len(summaries)} host(s) in job)",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
