# awx-host-report-error

A small Python CLI that generates a per-host CSV failure report for an AWX job.

Given a job ID, the tool queries the AWX REST API and writes a CSV with one row
per host: status (`OK`, `FAILED`, `UNREACHABLE`, ...), the failing task, and the
error message extracted from the `fatal:` event — the same data the AWX UI
shows in the job's event stream, but flattened into a spreadsheet you can sort
and filter.

## Prerequisites

- Python 3.9+
- An AWX **Personal OAuth2 Token** (AWX UI → *Users* → your user → *Tokens* → *Add*)
- Network access to the AWX server

## Install

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

## Configure

Set two environment variables (or pass `--url` / `--token` on the command line):

```powershell
$env:AWX_URL   = "https://awx.example.com"
$env:AWX_TOKEN = "your-personal-oauth2-token"
```

There's a `.env.example` you can copy as a template.

### TLS

If `awx-fullchain.crt` is present next to the script (or in the parent
directory), it's used automatically as the CA bundle. Otherwise the system
trust store is used. Pass `--ca-bundle <path>` to override, or `--insecure` to
skip verification entirely (testing only).

## Usage

```powershell
# Basic — writes awx-job-12345-report.csv
python awx_host_report.py 12345

# Custom output path
python awx_host_report.py 12345 -o reports\release-42.csv

# Only failures (FAILED + UNREACHABLE rows)
python awx_host_report.py 12345 --failures-only

# Override URL / token inline
python awx_host_report.py 12345 --url https://awx.example.com --token abc...

# Skip TLS verification
python awx_host_report.py 12345 --insecure
```

## Output

CSV columns: `play, host, status, task, message`.

- One row per **failure event** for failed hosts (a host that failed two tasks
  produces two rows — matches the AWX event stream).
- One row per **OK / CHANGED / SKIPPED** host (unless `--failures-only`).
- Sorted by `play` then `host` so failures cluster naturally.

Sample:

| play    | host       | status      | task                | message                                               |
|---------|------------|-------------|---------------------|-------------------------------------------------------|
| play-a  | host-001   | FAILED      | escalate privileges | Timeout (12s) waiting for privilege escalation prompt |
| play-a  | host-002   | FAILED      | assert OS family    | Unsupported OS family 'Unknown'. ...                  |
| play-b  | host-003   | FAILED      | ensure user exists  | User 'someuser' does not exist on host-003.           |
| play-c  | host-004   | OK          |                     |                                                       |

## Exit codes

| Code | Meaning                                        |
|------|------------------------------------------------|
| 0    | Report written successfully                    |
| 1    | API / auth / network error                     |
| 2    | Job ID not found                               |

## How it works

Two AWX endpoints, then a join:

1. `GET /api/v2/jobs/<id>/job_host_summaries/` — per-host counters
   (`ok`, `failed`, `dark`, `changed`, `skipped`). One record per host.
2. `GET /api/v2/jobs/<id>/job_events/?event__in=runner_on_failed,runner_on_unreachable,runner_on_async_failed`
   — only the failure events, with `play`, `task`, and `event_data.res`.

The script paginates both endpoints, then for each host emits either its
failure events or a single OK-style row from the summary.
