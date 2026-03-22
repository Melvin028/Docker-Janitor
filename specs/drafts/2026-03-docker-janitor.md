# Docker Janitor

| Field       | Value                        |
|-------------|------------------------------|
| Author      | TBD                          |
| Status      | Draft                        |
| Created     | Mar 14, 2026                 |
| Feature     | Docker Janitor               |

---

## Problem statement

Docker hosts accumulate unused images, stopped containers, dangling volumes, and orphaned networks over time. Manual cleanup is tedious, error-prone, and rarely done consistently. Docker Janitor automates this housekeeping using configurable policies, giving teams confidence that resources are cleaned safely and predictably.

---

## Proposed solution

A Python CLI tool with four pipeline stages, each communicating with the Docker Engine API via Unix socket or TCP:

1. **Scanner Core** — discovers all images, containers, volumes, and networks; calculates usage and dependency relationships.
2. **Policy Engine** — evaluates user-defined rules (age, name patterns, version count) and marks each resource as safe or unsafe to delete.
3. **Cleanup Engine** — supports dry-run (preview) and live (execute) modes; logs every action taken.
4. **Notifier / Reporter** — delivers results via CLI output, Slack, webhook, or email.

---

## Data model changes

No database required. State is ephemeral per run. Configuration is file-based (YAML).

**Policy config example (`janitor.yaml`):**
```yaml
docker:
  host: unix:///var/run/docker.sock

policy:
  retention_days: 30
  min_versions: 2
  keep_patterns:
    - "production-*"
    - "latest"
  protect_running: true
  protect_named: true

notifications:
  cli: true
  slack:
    enabled: false
  webhook:
    enabled: false
  email:
    enabled: false
```

---

## API / CLI interface

```
docker-janitor scan            # List what would be cleaned (no deletions)
docker-janitor clean --dry-run # Preview deletions with counts and reasons
docker-janitor clean --live    # Execute deletions
```

---

## UI changes

CLI output only for v1. Notifier channels (Slack, webhook, email) are secondary outputs.

---

## Acceptance criteria

- [ ] `scan` command connects to Docker and lists unused images, stopped containers, unused volumes, and orphaned networks
- [ ] `clean --dry-run` produces a report of what would be deleted without removing anything
- [ ] `clean --live` deletes only resources marked safe by the Policy Engine
- [ ] Policy rules are loaded from `janitor.yaml`
- [ ] Running containers and named resources are never deleted unless explicitly configured
- [ ] All actions are logged with timestamps and resource IDs
- [ ] CLI reporter renders a human-readable summary after each run
- [ ] At least one non-CLI notifier (Slack, webhook, or email) works end-to-end

---

## Out of scope

- Kubernetes / container orchestration support
- Multi-host management
- Web UI or dashboard
- Registry-side image cleanup

---

## Open questions

- Should we support cron scheduling natively, or rely on external schedulers (cron, Kubernetes CronJob)?
- Should the Cleanup Engine support rollback or undo?

---

## Dependencies

- `docker` (Python SDK)
- `click` (CLI)
- `pyyaml` (config)
- `requests` (Slack / webhook notifications)
