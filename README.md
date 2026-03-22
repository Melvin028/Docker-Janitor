# Zafin Docker Janitor

> Automated Docker resource lifecycle management — scan, evaluate, clean up, and recover Docker resources through a policy-driven engine with a modern web dashboard and full CLI support.

---

## Table of contents

1. [Overview](#overview)
2. [Architecture](#architecture)
3. [Project structure](#project-structure)
4. [Workflow](#workflow)
5. [Features](#features)
6. [Installation](#installation)
7. [Configuration](#configuration)
8. [CLI usage](#cli-usage)
9. [Web dashboard](#web-dashboard)
10. [Policy engine](#policy-engine)
11. [Cleanup engine](#cleanup-engine)
12. [Audit log & recovery](#audit-log--recovery)
13. [Notifications](#notifications)
14. [Environment variables](#environment-variables)

---

## Overview

Docker hosts accumulate unused images, stopped containers, dangling volumes, and stale networks over time. Left unmanaged, these resources consume gigabytes of disk space and make it difficult to reason about what is actually running.

**Zafin Docker Janitor** provides:

- A **Scanner** that inventories every Docker resource and calculates disk usage.
- A **Policy Engine** with a configurable guard chain that decides what is safe to delete.
- A **Cleanup Engine** that executes (or simulates) deletions and writes every action to an audit log.
- A **Web dashboard** for point-and-click management, trend charts, and resource exploration.
- A **CLI** for scripted and pipeline-based usage.
- **Notifications** via application log, Slack, generic webhook, or email after every cleanup run.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                        Docker Host                              │
│   Images · Containers · Volumes · Networks · Build Cache        │
└────────────────────────┬────────────────────────────────────────┘
                         │  Docker SDK (docker-py)
                         ▼
┌─────────────────────────────────────────────────────────────────┐
│                       Scanner Layer                             │
│  core.py — orchestrates full scan, attaches container↔image     │
│  images.py · models.py · docker_client.py                       │
└────────────────────────┬────────────────────────────────────────┘
                         │  ScanResult
                         ▼
┌─────────────────────────────────────────────────────────────────┐
│                    Policy Engine                                 │
│  Guard chain: protect_running → protect_named → keep_patterns   │
│               → min_versions → retention_days                   │
│  Also evaluates: stopped containers, orphaned volumes           │
└────────────────────────┬────────────────────────────────────────┘
                         │  PolicyResult (ResourceDecision[])
                         ▼
┌────────────────┬────────────────────────────────────────────────┐
│  Cleanup Engine│             Web UI / CLI                       │
│  dry-run or    │  Dashboard · Images · Containers · Volumes     │
│  live delete   │  Networks · Cleanup · Audit · Policy · Settings│
└───────┬────────┴────────────────────────────────────────────────┘
        │  audit entry (JSONL)          │  scan snapshot (JSONL)
        ▼                               ▼
  logs/audit.jsonl              logs/scan_history.jsonl
        │
        ▼
  Notifier (CLI log / Slack / Webhook / Email)
```

---

## Project structure

```
Docker-Janitor/
├── configs/
│   └── janitor.yaml              # Active policy + notification config
├── janitor/
│   ├── cli.py                    # Click CLI entry point
│   ├── config.py                 # YAML config loader / saver
│   ├── audit/
│   │   └── logger.py             # Append-only JSONL audit log
│   ├── cleanup/
│   │   └── engine.py             # Dry-run & live deletion engine
│   ├── history/
│   │   └── store.py              # Scan history snapshots for trend chart
│   ├── notifier/
│   │   ├── __init__.py           # Dispatcher — build_payload, send_notifications
│   │   ├── base.py               # Abstract BaseNotifier
│   │   ├── cli_reporter.py       # Logs cleanup summary to stdout
│   │   ├── slack.py              # Slack Block Kit webhook
│   │   ├── webhook.py            # Generic HTTP webhook
│   │   └── email.py              # SMTP email report
│   ├── policy/
│   │   ├── engine.py             # PolicyEngine + guard chain
│   │   └── rules.py              # PolicyRules dataclasses
│   ├── scanner/
│   │   ├── core.py               # Scanner — full resource inventory
│   │   ├── images.py             # Image listing helpers
│   │   ├── models.py             # ImageInfo, ContainerInfo, VolumeInfo, etc.
│   │   └── docker_client.py      # Docker SDK connection helper
│   ├── utils/
│   │   └── logger.py             # Structured logging helper
│   └── web/
│       ├── __init__.py           # Flask app factory
│       ├── routes.py             # All route handlers + API endpoints
│       ├── static/
│       │   └── img/logo.png      # Zafin | Docker Janitor brand logo
│       └── templates/
│           ├── base.html         # Layout: navbar, sidebar, flash messages
│           ├── dashboard.html    # Overview cards + disk usage trend chart
│           ├── images.html       # Image table with drawer + layer visualizer
│           ├── containers.html   # Container table with details drawer
│           ├── volumes.html      # Volume table with details drawer
│           ├── networks.html     # Network table with details drawer
│           ├── cleanup.html      # Cleanup planner (Plan / Action tabs)
│           ├── policy.html       # Live policy evaluation view
│           ├── audit.html        # Audit log with export + recovery
│           └── settings.html     # Policy and notification settings form
├── logs/
│   ├── audit.jsonl               # Persistent audit trail (auto-created)
│   └── scan_history.jsonl        # Scan snapshots for trend chart (auto-created)
├── .env.example                  # Template for environment variables
├── Dockerfile                    # Container image for the web UI
├── docker-compose.yml            # One-command local setup
└── pyproject.toml                # Package metadata + CLI entry point
```

---

## Workflow

The complete lifecycle from first run to cleanup looks like this:

```
1. Configure
   Edit configs/janitor.yaml (or use the Settings page in the UI)
   Set retention_days, keep_patterns, protect_running, etc.

2. Scan
   CLI:  docker-janitor scan
   UI:   Click "Run scan" in the top navbar
   → Scanner connects to Docker Desktop / Engine
   → Inventories images, containers, volumes, networks, build cache
   → Calculates total disk usage and reclaimable space
   → Stores a lightweight snapshot to logs/scan_history.jsonl

3. Evaluate (Policy Engine)
   Each resource is run through the guard chain:
   Guard 1  protect_running    — keep images used by running containers
   Guard 2  protect_named      — keep any image with a named tag
   Guard 3  keep_patterns      — keep images matching glob patterns
   Guard 4  min_versions       — keep the N newest images per repo
   Guard 5  retention_days     — keep images younger than N days
   → Output: ResourceDecision (safe_to_delete: true/false + reason)

4. Plan (dry-run)
   CLI:  docker-janitor clean           (default: dry-run)
   UI:   Cleanup → "Plan" tab
   → Shows every resource that would be removed and the space to free
   → No changes are made

5. Execute (live)
   CLI:  docker-janitor clean --live    (prompts for confirmation)
   UI:   Cleanup → "Action" tab → select resources → "Execute cleanup"
   → Deletes selected resources via Docker API
   → Writes every action to logs/audit.jsonl
   → Sends cleanup notification (if configured)

6. Recover
   UI:  Audit → click "Recover" on any deleted image
   → Pulls the image back from its registry source
   → Logged as a recovery action in the audit trail

7. Prune build cache (optional)
   UI:  Cleanup → "Prune build cache" button
   → Calls docker system prune --filter until=0 for build layers only
   → Logs freed space to the audit trail
```

---

## Features

### Scanner
- Inventories all **images** (including dangling), **containers** (running + stopped), **volumes**, and **networks**
- Attaches container references to their parent images to determine "in use" status
- Calls `docker system df` to calculate total and reclaimable disk usage across all resource types including **build cache**
- Stores lightweight scan snapshots for historical trend analysis

### Dashboard
- Summary cards: total counts + unused/stopped counts per resource type
- Disk usage breakdown with reclaimable amounts for images, containers, volumes, and build cache
- **Interactive line chart** (Chart.js) showing disk usage trend over the last 30 scans
- Trend indicator badge (↑ up / ↓ down / → stable) with percentage change
- Animated "Run scan" button with spinner feedback

### Resource pages — Images, Containers, Volumes, Networks
- Sortable tables (sort by name, size, or age)
- **Live search** — filters rows instantly as you type
- **Status filter pills** — All / In use / Unused / Dangling (images); Running / Stopped (containers); Mounted / Orphaned (volumes)
- **Details drawer** — click any row to open a right-side panel with full metadata:
  - **Images**: all tags, labels, size, age, full SHA, parent image ID, dependent containers
  - **Containers**: name, status badge, age, created date, image, ports, mounted volumes, networks, labels
  - **Volumes**: name, driver, scope, mount point, labels, in-use status
  - **Networks**: name, driver, scope, internal flag, full ID, labels

### Image layer visualizer
- **Layers tab** inside the image details drawer (lazy-loaded on demand)
- Shows the full `docker history`-style layer tree from newest to oldest
- Each layer displays: command (cleaned of `/bin/sh -c` noise), layer ID (short), size
- **Shared layer detection** — cross-references all images in the last scan to find layers used by more than one image
- Shared layers highlighted with a blue **"shared ×N"** badge; hover to see which images share the layer
- Summary banner: "N layers shared with other images"

### Policy engine
Configurable guard chain evaluated in priority order:

| Guard | Setting | Behaviour |
|---|---|---|
| 1 | `protect_running` | Never delete images used by a running container |
| 2 | `protect_named` | Never delete images with a named `repo:tag` |
| 3 | `keep_patterns` | Never delete images whose tag matches a glob (e.g. `production-*`) |
| 4 | `min_versions` | Keep the N newest images per repository |
| 5 | `retention_days` | Keep images younger than N days |
| — | `container_retention_days` | Flag stopped containers older than N days (opt-in) |
| — | `cleanup_orphaned_volumes` | Flag volumes not mounted by any container (opt-in) |

### Cleanup engine
- **Plan tab (dry-run)**: simulates every deletion, shows resource name, type, size, and age — no changes made
- **Action tab (live)**: per-resource checkboxes let you select exactly what to delete
- Confirmation required before execution
- Captures image tags *before* deletion so they can be recovered later
- Every action written to the audit log

### Build cache cleanup
- Dedicated "Prune build cache" section in Cleanup showing unused layer count and total size
- One-click prune of all unused build layers
- Freed space reported and logged to audit trail

### Audit log
- Append-only JSONL log at `logs/audit.jsonl`
- Records every delete, dry-run, prune, and recovery action with: timestamp, resource type, display name, size, success flag, reason, tags, and pull commands
- Newest-first display with total space freed summary
- **Export** — download as CSV or JSON for sharing cleanup reports
- **Clear** — wipe the log (with confirmation)

### Image recovery
- Images marked `recoverable: true` (have at least one registry-style tag) show a **"Recover"** button in the audit log
- Clicking "Recover" opens a confirmation modal and pulls the image back via `docker pull`
- Recovery is logged as a new audit entry
- Animated spinner on the Pull button during the network request

### Scan history & trend chart
- Every scan appends a snapshot (resource counts + byte usage) to `logs/scan_history.jsonl`
- Dashboard renders up to 30 snapshots as a multi-dataset **line chart**
- Datasets: Images, Build cache, Volumes, Containers (in GB)
- Combined tooltip on hover shows all datasets for that point in time
- `GET /api/scan-history` JSON endpoint for external tooling

### Notifications
Post-cleanup summaries dispatched to configured channels:

| Channel | Enabled by | Credentials |
|---|---|---|
| Application log | Always available | — |
| Slack | `notifications.slack.enabled: true` | `SLACK_WEBHOOK_URL` env var |
| Webhook | `notifications.webhook.enabled: true` | `JANITOR_WEBHOOK_URL`, optional `JANITOR_WEBHOOK_TOKEN` |
| Email (SMTP) | `notifications.email.enabled: true` | `SMTP_HOST`, `SMTP_SENDER`, `SMTP_RECIPIENTS`, etc. |

### Settings UI
- Edit all policy rules from the browser — no file editing required
- Toggle notification channels on/off (credentials always loaded from environment variables, never stored in config)
- Changes saved instantly to `configs/janitor.yaml`

### CLI
Full-featured terminal interface with colored output:

```
docker-janitor scan                # Scan and print resource summary
docker-janitor scan --json         # Raw JSON output (scriptable)
docker-janitor clean               # Dry-run: preview deletions
docker-janitor clean --live        # Execute deletions (confirmation prompt)
docker-janitor clean --live --yes  # Non-interactive (CI/CD)
docker-janitor audit               # Show recent audit entries
docker-janitor audit -n 50 --json  # Last 50 entries as JSON
docker-janitor ui                  # Launch the web dashboard
docker-janitor ui --port 8080
```

---

## Installation

### Prerequisites

- Python 3.11+
- Docker Desktop (Windows/macOS) or Docker Engine (Linux) running

### Local setup

```bash
# Clone the repository
git clone https://github.com/your-org/docker-janitor.git
cd docker-janitor

# Create and activate a virtual environment
python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS / Linux
source .venv/bin/activate

# Install the package (installs the docker-janitor CLI too)
pip install -e .

# Copy the example env file
cp .env.example .env
# Edit .env to set any notification credentials you need

# Launch the web UI
docker-janitor ui
# → http://127.0.0.1:5000
```

### Docker Compose

```bash
docker compose up
# → http://127.0.0.1:5000
```

---

## Configuration

Edit `configs/janitor.yaml`:

```yaml
policy:
  # Delete images older than this many days (null = disabled)
  retention_days: 30

  # Keep at least this many versions per repository (0 = disabled)
  min_versions: 1

  # Glob patterns — images whose tag matches are always kept
  keep_patterns:
    - production-*
    - stable

  # Never delete images used by a running container
  protect_running: true

  # Never delete images that have a named tag (repo:tag)
  protect_named: false

  # Opt-in: remove stopped containers older than N days (0 = disabled)
  container_retention_days: 0

  # Opt-in: remove volumes not mounted by any container
  cleanup_orphaned_volumes: false

notifications:
  cli:
    enabled: true       # Always logs to stdout
  slack:
    enabled: false      # Requires SLACK_WEBHOOK_URL env var
  webhook:
    enabled: false      # Requires JANITOR_WEBHOOK_URL env var
  email:
    enabled: false      # Requires SMTP_* env vars
```

All policy fields can also be edited from the **Settings** page in the web UI.

---

## CLI usage

### Scan

```bash
docker-janitor scan
```

```
  Docker Janitor  —  Scan results
  ──────────────────────────────────────────────────────────

  Resources
    Images:              45 total  ·  12 unused  ·  3 dangling
    Containers:           8 total  ·   3 stopped
    Volumes:             15 total  ·   5 orphaned
    Networks:            10 total  ·   2 unused

  Disk usage
    Images:        2.30 GB       450 MB reclaimable
    Containers:      12 MB         5 MB reclaimable
    Volumes:        340 MB       120 MB reclaimable
    Build cache:   1.20 GB       800 MB reclaimable
  ──────────────────────────────────────────────────────────
    Total:         3.85 GB      1.37 GB reclaimable

  Largest unused images  (12 total)
    nginx:latest                               150.3 MB   45d  [unused]
    python:3.11-slim                           125.1 MB  120d  [unused]
    ...

  ✓  Run 'docker-janitor clean --live' to free 1.37 GB.
```

### Clean (dry-run)

```bash
docker-janitor clean
```

```
  Docker Janitor  —  Cleanup  [DRY-RUN]
  ──────────────────────────────────────────────────────────
  Resources that would be removed  (12)
    nginx:latest                         image        150.3 MB
    python:3.11-slim                     image        125.1 MB
    ...
  ──────────────────────────────────────────────────────────
  Space to free: 1.20 GB

  This is a simulation — no changes were made.
  Run with --live to execute.
```

### Audit

```bash
docker-janitor audit -n 10
```

```
  Docker Janitor  —  Audit log  (last 10 entries)
  ──────────────────────────────────────────────────────────
  Summary
    Total entries:          10
    Space freed:          1.20 GB
    Failed actions:           0

  Entries
    Date                Resource                                 Type        Mode      Result
    ────────────────────────────────────────────────────────────────────────────
    2026-03-14 09:12    nginx:latest                             image       live      ✓ ok
    2026-03-14 09:12    python:3.11-slim                         image       live      ✓ ok
    ...
```

---

## Web dashboard

Navigate to `http://127.0.0.1:5000` after running `docker-janitor ui`.

| Page | URL | Description |
|---|---|---|
| Overview | `/` | Summary cards, disk usage breakdown, trend chart |
| Images | `/images` | Sortable table, search, filter, details drawer, layer visualizer |
| Containers | `/containers` | Sortable table, search, filter, details drawer |
| Volumes | `/volumes` | Sortable table, search, filter, details drawer |
| Networks | `/networks` | Sortable table, search, filter, details drawer |
| Policy | `/policy` | Live evaluation results — see exactly why each resource is kept or flagged |
| Cleanup | `/cleanup` | Plan (dry-run preview) and Action (live deletion with checkboxes) |
| Audit | `/audit` | Full action history, CSV/JSON export, image recovery |
| Settings | `/settings` | Edit policy rules and notification toggles |

---

## Policy engine

The guard chain is evaluated left-to-right. **The first guard that fires returns a KEEP decision**; if an image passes all guards it is marked safe to delete.

```
Image
  │
  ├─ protect_running?  ──YES──▶  KEEP  (used by running container)
  │
  ├─ protect_named?    ──YES──▶  KEEP  (has named repo:tag)
  │
  ├─ keep_patterns?    ──YES──▶  KEEP  (tag matches glob)
  │
  ├─ min_versions?     ──YES──▶  KEEP  (within N newest for its repo)
  │
  ├─ retention_days?   ──YES──▶  KEEP  (younger than threshold)
  │
  └─ (all guards pass) ────────▶  DELETE
```

Stopped containers and orphaned volumes are evaluated separately and are **opt-in** via `container_retention_days` and `cleanup_orphaned_volumes`.

---

## Cleanup engine

| Mode | How to trigger | Effect |
|---|---|---|
| Dry-run | `docker-janitor clean` or Cleanup → Plan tab | Logs would-delete decisions; zero Docker API mutations |
| Live | `docker-janitor clean --live` or Cleanup → Action tab | Calls `images.remove()`, `containers.remove()`, `volumes.remove()`, `networks.remove()` |
| Cache prune | Cleanup → "Prune build cache" | Calls `docker system prune` scoped to build layers |

In live mode, image tags are captured *before* deletion so they can be re-pulled later via the recovery feature.

---

## Audit log & recovery

Every action — delete, dry-run, prune, recover — is appended to `logs/audit.jsonl` as a structured JSON line:

```json
{
  "timestamp": "2026-03-14T09:12:00+00:00",
  "resource_id": "sha256:abc123...",
  "resource_type": "image",
  "display_name": "nginx:latest",
  "size_bytes": 157286400,
  "action": "delete",
  "dry_run": false,
  "success": true,
  "message": "Deleted successfully",
  "reason": "unused, 45d old, no matching keep rules",
  "tags": ["nginx:latest", "nginx:1.25.3"],
  "pull_commands": ["docker pull nginx:latest", "docker pull nginx:1.25.3"],
  "recoverable": true
}
```

Images with `recoverable: true` show a **Recover** button in the audit UI. Clicking it pulls the image back from its registry and logs the recovery.

**Export formats:**
- `GET /audit/export?fmt=csv` — CSV with key fields
- `GET /audit/export?fmt=json` — Full JSON array

---

## Notifications

After every live cleanup run, Docker Janitor dispatches a summary to all enabled channels.

### Slack

Set `notifications.slack.enabled: true` in `janitor.yaml` and export:

```bash
export SLACK_WEBHOOK_URL=https://hooks.slack.com/services/...
```

### Generic webhook

Set `notifications.webhook.enabled: true` and export:

```bash
export JANITOR_WEBHOOK_URL=https://your-server.example.com/hook
export JANITOR_WEBHOOK_TOKEN=optional-bearer-token   # optional
```

The full cleanup payload is POSTed as JSON.

### Email (SMTP)

Set `notifications.email.enabled: true` and export:

```bash
export SMTP_HOST=smtp.example.com
export SMTP_PORT=587                   # defaults to 587
export SMTP_SENDER=janitor@example.com
export SMTP_RECIPIENTS=team@example.com,ops@example.com
export SMTP_USERNAME=janitor@example.com
export SMTP_PASSWORD=your-smtp-password
export SMTP_USE_SSL=false              # true for port 465
```

---

## Environment variables

| Variable | Required | Description |
|---|---|---|
| `FLASK_SECRET_KEY` | No | Session secret for the web UI (auto-generated if not set) |
| `SLACK_WEBHOOK_URL` | Slack only | Incoming webhook URL from your Slack app |
| `JANITOR_WEBHOOK_URL` | Webhook only | URL to POST cleanup payloads to |
| `JANITOR_WEBHOOK_TOKEN` | No | Bearer token added to webhook `Authorization` header |
| `SMTP_HOST` | Email only | SMTP server hostname |
| `SMTP_PORT` | No | SMTP port (default: `587`) |
| `SMTP_SENDER` | Email only | From address |
| `SMTP_RECIPIENTS` | Email only | Comma-separated recipient list |
| `SMTP_USERNAME` | No | SMTP auth username |
| `SMTP_PASSWORD` | No | SMTP auth password |
| `SMTP_USE_SSL` | No | `true` for port-465 SSL (default: `false`, uses STARTTLS) |

All credentials are read from environment variables at runtime and are **never stored** in `janitor.yaml` or committed to source control.

---

*Built by Zafin · Docker Janitor*
