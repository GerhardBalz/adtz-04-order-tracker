# Incident responder

A small service that receives Grafana alert webhooks on `POST /alerts` (port 8001), saves the alert with endpoint, log and trace context, and asks Claude Code in headless mode to find and fix the cause. It runs on the host, not in Compose, because it uses your local `claude` CLI and edits this repository's working tree.

## Run it

Requires the root project's `uv` environment and a logged-in `claude` CLI on `PATH`. From the repository root:

```bash
uv run --frozen python incident-response/responder.py
```

It listens on `127.0.0.1:8001`. `GET /healthz` reports the queue length. Run the tests with `uv run --frozen pytest -q incident-response/tests`; they also run as part of the root `uv run --frozen pytest -q`.

Grafana is already connected: `grafana/provisioning/alerting/order-tracker-responder.yaml` provisions the `order-tracker-responder` webhook contact point (`POST http://host.docker.internal:8001/alerts`, Docker Desktop) and routes alerts labelled `service=order-tracker` to it. Grafana loads it at startup, so after editing it run `docker compose restart grafana`. On Linux, Grafana needs `extra_hosts: ["host.docker.internal:host-gateway"]` and the responder must listen on that interface (`RESPONDER_HOST=0.0.0.0`). If you set `RESPONDER_TOKEN`, the contact point must send `Authorization: Bearer <token>` (webhook settings `authorization_scheme: Bearer` and `authorization_credentials`). Do not commit the token.

To check the pipeline without starting an incident fix, send a test alert with the label `alertname=ResponderTest`. Claude Code then runs with no tools and only acknowledges it. Recorded test (Q5) and incident (Q6) runs are in [EVIDENCE.md](EVIDENCE.md).

## What it does

Each notification becomes one directory under `incident-response/data/incidents/` (git-ignored):

| File | Content |
| --- | --- |
| `alert.json` | Raw Grafana payload |
| `incident.json` | Kind (`incident`, `test`, `resolved`), action, status, endpoints, session id, cost, exit code |
| `context.json` | 5xx counts by status for each alerted endpoint (Prometheus), WARN/ERROR logs (Loki), and error traces with exception events (Tempo) |
| `prompt.md` | Prompt sent to Claude Code |
| `response.json`, `response.md` | Claude Code's JSON result and its final message |
| `claude.stderr.log` | Claude Code's stderr |

- Firing alerts are queued and handled one at a time, so two agent runs never edit the repository concurrently.
- Grafana repeats notifications for an alert that is still firing. An alert already handled (same fingerprint and `startsAt`, also across restarts) is saved but not sent to the agent again.
- Resolved notifications are saved but not sent to the agent.
- Context queries are best-effort. Failures are listed under `errors` in `context.json`, and the agent still runs.

The agent runs `claude -p --output-format json --permission-mode dontAsk` in the repository root, with the prompt on stdin. For a real incident it may read and edit files and run only `uv run --frozen pytest`, `git status` and `git diff`. `git commit`, `git push` and `docker` are denied, so fixes stay uncommitted for you to review and deploy. Continue a run with `claude --resume <session_id>`.

## Settings

| Variable | Default |
| --- | --- |
| `RESPONDER_HOST`, `RESPONDER_PORT` | `127.0.0.1`, `8001` |
| `RESPONDER_TOKEN` | unset (no auth) |
| `RESPONDER_DATA_DIR` | `incident-response/data/incidents` |
| `RESPONDER_WORKDIR` | repository root |
| `CLAUDE_BIN` | `claude` |
| `RESPONDER_CLAUDE_TIMEOUT` | `900` seconds |
| `RESPONDER_MODEL`, `RESPONDER_MAX_BUDGET_USD` | unset (CLI defaults) |
| `PROMETHEUS_URL`, `LOKI_URL`, `TEMPO_URL` | `http://127.0.0.1:9090`, `:3100`, `:3200` |
