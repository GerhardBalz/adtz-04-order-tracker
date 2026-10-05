"""Incident responder: receives Grafana alert webhooks and hands them to Claude Code.

POST /alerts stores the Grafana notification, collects endpoint, log and trace
context from Prometheus, Loki and Tempo, then runs `claude -p` (headless mode)
in the repository and stores its response. Each notification becomes one
incident directory under RESPONDER_DATA_DIR:

    alert.json      the raw Grafana payload
    incident.json   status, kind, alert keys, timestamps, agent exit code
    context.json    endpoint metrics, error logs and traces (real incidents only)
    prompt.md       the prompt sent to Claude Code
    response.json   Claude Code's JSON result (stdout)
    response.md     the agent's final message
    claude.stderr.log

Alerts labelled alertname=ResponderTest are test alerts: Claude Code runs with
no tools and is told only to acknowledge, so no incident fix is attempted.
"""

import json
import logging
import os
import queue
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI, Header, HTTPException, Request


HERE = Path(__file__).resolve().parent
TEST_ALERT_NAME = "ResponderTest"

# Tools the agent may use on a real incident. In dontAsk mode anything not
# listed here is denied instead of prompting, which would hang a headless run.
INCIDENT_TOOLS = "Read,Grep,Glob,Edit,Write,Bash"
INCIDENT_ALLOWED = [
    "Read", "Grep", "Glob", "Edit", "Write",
    "Bash(uv run --frozen pytest:*)",
    "Bash(git status:*)",
    "Bash(git diff:*)",
]
INCIDENT_DISALLOWED = ["Bash(git commit:*)", "Bash(git push:*)", "Bash(docker:*)"]

logger = logging.getLogger("incident-response")


@dataclass
class Config:
    data_dir: Path = field(default_factory=lambda: Path(
        os.getenv("RESPONDER_DATA_DIR", HERE / "data" / "incidents")))
    workdir: Path = field(default_factory=lambda: Path(
        os.getenv("RESPONDER_WORKDIR", HERE.parent)))
    claude_bin: str = field(default_factory=lambda: os.getenv("CLAUDE_BIN", "claude"))
    claude_timeout: float = field(default_factory=lambda: float(
        os.getenv("RESPONDER_CLAUDE_TIMEOUT", "900")))
    claude_model: str | None = field(default_factory=lambda: os.getenv("RESPONDER_MODEL"))
    max_budget_usd: str | None = field(default_factory=lambda: os.getenv("RESPONDER_MAX_BUDGET_USD"))
    token: str | None = field(default_factory=lambda: os.getenv("RESPONDER_TOKEN"))
    prometheus_url: str = field(default_factory=lambda: os.getenv(
        "PROMETHEUS_URL", "http://127.0.0.1:9090"))
    loki_url: str = field(default_factory=lambda: os.getenv("LOKI_URL", "http://127.0.0.1:3100"))
    tempo_url: str = field(default_factory=lambda: os.getenv("TEMPO_URL", "http://127.0.0.1:3200"))
    http_timeout: float = 5.0


# --- Grafana payload helpers -------------------------------------------------

def is_test_alert(alert):
    return alert.get("labels", {}).get("alertname") == TEST_ALERT_NAME


def alert_key(alert):
    """Grafana re-sends a still-firing alert with the same fingerprint and startsAt."""
    labels = alert.get("labels", {})
    fingerprint = alert.get("fingerprint") or json.dumps(labels, sort_keys=True)
    return f"{fingerprint}@{alert.get('startsAt', '')}"


def endpoints(alerts):
    """Return the distinct (method, route) pairs named by the alerts' labels."""
    found = []
    for alert in alerts:
        labels = alert.get("labels", {})
        pair = (labels.get("http_request_method"), labels.get("http_route"))
        if pair[1] and pair not in found:
            found.append(pair)
    return found


def parse_time(value):
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        return None
    # Grafana sends 0001-01-01T00:00:00Z for "not set".
    return parsed if parsed.year > 1970 else None


# --- Context collection ------------------------------------------------------

def http_json(url, params, timeout):
    full_url = f"{url}?{urllib.parse.urlencode(params)}" if params else url
    with urllib.request.urlopen(full_url, timeout=timeout) as response:
        return json.load(response)


def otlp_value(value):
    for kind in ("stringValue", "intValue", "doubleValue", "boolValue"):
        if kind in value:
            return value[kind]
    return value


def otlp_attributes(items):
    return {item["key"]: otlp_value(item.get("value", {})) for item in items or []}


def summarize_trace(trace):
    """Flatten a Tempo /api/v2/traces response into the spans an agent needs."""
    trace = trace.get("trace", trace)
    spans = []
    for resource_spans in trace.get("resourceSpans", []):
        for scope_spans in resource_spans.get("scopeSpans", []):
            for span in scope_spans.get("spans", []):
                spans.append({
                    "name": span.get("name"),
                    "span_id": span.get("spanId"),
                    "parent_span_id": span.get("parentSpanId"),
                    "status": span.get("status", {}),
                    "attributes": otlp_attributes(span.get("attributes")),
                    "events": [
                        {"name": event.get("name"),
                         "attributes": otlp_attributes(event.get("attributes"))}
                        for event in span.get("events", [])
                    ],
                })
    return spans


class ContextCollector:
    """Best-effort queries; each failure is recorded rather than raised."""

    def __init__(self, config, fetch=http_json):
        self.config = config
        self.fetch = fetch

    def _get(self, errors, source, url, params=None):
        try:
            return self.fetch(url, params, self.config.http_timeout)
        except (urllib.error.URLError, OSError, ValueError) as exc:
            errors.append(f"{source}: {exc}")
            return None

    def collect(self, alerts, now=None):
        now = now or datetime.now(timezone.utc)
        starts = [t for t in (parse_time(a.get("startsAt")) for a in alerts) if t]
        start = min(starts, default=now) - timedelta(minutes=10)
        errors = []
        context = {
            "window": {"start": start.isoformat(), "end": now.isoformat()},
            "endpoints": [],
            "error_logs": [],
            "traces": [],
            "errors": errors,
        }

        for method, route in endpoints(alerts):
            selector = f'exported_job="order-tracker", http_route="{route}"'
            if method:
                selector += f', http_request_method="{method}"'
            query = ("sum by (http_request_method, http_route, http_response_status_code) "
                     f"(increase(http_server_request_duration_seconds_count{{{selector}}}[15m]))")
            result = self._get(errors, "prometheus", f"{self.config.prometheus_url}/api/v1/query",
                               {"query": query, "time": now.timestamp()})
            context["endpoints"].append({
                "method": method,
                "route": route,
                "query": query,
                "responses_by_status_15m": [
                    {"status": r["metric"].get("http_response_status_code"),
                     "count": float(r["value"][1])}
                    for r in (result or {}).get("data", {}).get("result", [])
                ],
            })

        logs = self._get(errors, "loki", f"{self.config.loki_url}/loki/api/v1/query_range", {
            "query": '{service_name="order-tracker"} | severity_text=~"ERROR|WARN|WARNING"',
            "start": int(start.timestamp() * 1e9),
            "end": int(now.timestamp() * 1e9),
            "limit": 50,
            "direction": "backward",
        })
        trace_ids = []
        for stream in (logs or {}).get("data", {}).get("result", []):
            labels = stream.get("stream", {})
            for _ts, line in stream.get("values", []):
                context["error_logs"].append({
                    "line": line,
                    "severity": labels.get("severity_text"),
                    "order_id": labels.get("order_id"),
                    "trace_id": labels.get("trace_id"),
                })
                if labels.get("trace_id") and labels["trace_id"] not in trace_ids:
                    trace_ids.append(labels["trace_id"])

        for method, route in endpoints(alerts):
            traceql = f'{{ span.http.route = "{route}" && status = error }}'
            if method:
                traceql = (f'{{ span.http.route = "{route}" && '
                           f'span.http.request.method = "{method}" && status = error }}')
            found = self._get(errors, "tempo", f"{self.config.tempo_url}/api/search", {
                "q": traceql, "start": int(start.timestamp()), "end": int(now.timestamp()) + 1,
                "limit": 5,
            })
            for trace in (found or {}).get("traces", []):
                if trace.get("traceID") and trace["traceID"] not in trace_ids:
                    trace_ids.append(trace["traceID"])

        for trace_id in trace_ids[:3]:
            trace = self._get(errors, "tempo", f"{self.config.tempo_url}/api/v2/traces/{trace_id}")
            if trace:
                context["traces"].append({"trace_id": trace_id, "spans": summarize_trace(trace)})
        return context


# --- Claude Code invocation ---------------------------------------------------

def ack_prompt(payload):
    return (
        f"This is a {TEST_ALERT_NAME} test notification sent to the Order Tracker incident "
        "responder to check the alert pipeline. It is not an incident. Do not investigate, "
        "change files or attempt a fix. Reply with one line that starts with "
        f"'{TEST_ALERT_NAME} acknowledged' and repeats the alert's summary.\n\n"
        f"Alert payload:\n```json\n{json.dumps(payload, indent=2)[:4000]}\n```\n"
    )


def incident_prompt(payload, alerts, context):
    return f"""You are the incident responder for the Order Tracker app in this repository.
Grafana fired the alert below. Find the root cause and fix it in the code.

Rules:
- Use the alert and the collected telemetry context as evidence; read the code in app/ and tests/.
- Make the smallest correct fix and add a regression test in tests/.
- Run the tests with `uv run --frozen pytest -q`.
- Do not commit, push, run docker, or change running services, volumes or Grafana config.
  The fix stays uncommitted for a human to review and deploy.
- If the evidence does not support a code fix, change nothing and say why.

Finish with a short report: root cause, evidence, files changed, test result, follow-ups.

Firing alerts:
```json
{json.dumps(alerts, indent=2)}
```

Notification summary: {payload.get("title") or payload.get("message", "")[:500]}

Collected context (Prometheus, Loki, Tempo):
```json
{json.dumps(context, indent=2)[:30000]}
```
"""


def claude_command(config, test):
    """Build the headless command; the prompt is passed on stdin."""
    command = [shutil.which(config.claude_bin) or config.claude_bin,
               "-p", "--output-format", "json", "--permission-mode", "dontAsk"]
    if test:
        command += ["--tools", ""]
    else:
        command += ["--tools", INCIDENT_TOOLS,
                    "--allowedTools", *INCIDENT_ALLOWED,
                    "--disallowedTools", *INCIDENT_DISALLOWED]
    if config.claude_model:
        command += ["--model", config.claude_model]
    if config.max_budget_usd:
        command += ["--max-budget-usd", config.max_budget_usd]
    return command


def run_claude(command, prompt, cwd, timeout):
    return subprocess.run(command, input=prompt, cwd=cwd, capture_output=True,
                          text=True, encoding="utf-8", timeout=timeout)


# --- Incident store and processing --------------------------------------------

def now_iso():
    return datetime.now(timezone.utc).isoformat()


class Responder:
    def __init__(self, config=None, collector=None, runner=run_claude):
        self.config = config or Config()
        self.collector = collector or ContextCollector(self.config)
        self.runner = runner
        self.handled_keys = set()
        self.lock = threading.Lock()
        self.queue = queue.Queue()
        self.config.data_dir.mkdir(parents=True, exist_ok=True)
        self._load_handled_keys()

    def _load_handled_keys(self):
        for path in self.config.data_dir.glob("*/incident.json"):
            incident = json.loads(path.read_text(encoding="utf-8"))
            if incident.get("action") == "queued":
                self.handled_keys.update(incident.get("alert_keys", []))

    def incident_dir(self, incident_id):
        return self.config.data_dir / incident_id

    def write(self, incident_id, name, data):
        path = self.incident_dir(incident_id) / name
        text = data if isinstance(data, str) else json.dumps(data, indent=2)
        path.write_text(text, encoding="utf-8")

    def read_incident(self, incident_id):
        path = self.incident_dir(incident_id) / "incident.json"
        return json.loads(path.read_text(encoding="utf-8"))

    def update_incident(self, incident_id, **changes):
        incident = self.read_incident(incident_id)
        incident.update(changes)
        self.write(incident_id, "incident.json", incident)
        return incident

    def receive(self, payload):
        """Save the notification and decide what to do with it. Returns the incident record."""
        alerts = payload.get("alerts") or []
        firing = [a for a in alerts if a.get("status", payload.get("status")) == "firing"]
        test_alerts = [a for a in firing if is_test_alert(a)]
        real_alerts = [a for a in firing if not is_test_alert(a)]
        keys = [alert_key(a) for a in real_alerts]

        with self.lock:
            if real_alerts:
                kind = "incident"
                new = [k for k in keys if k not in self.handled_keys]
                action = "queued" if new else "skipped_duplicate"
                if new:
                    self.handled_keys.update(keys)
            elif test_alerts:
                kind, action = "test", "queued"
            else:
                kind, action = "resolved" if alerts else "empty", "skipped_not_firing"

        incident_id = f"{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{uuid4().hex[:8]}"
        self.incident_dir(incident_id).mkdir(parents=True)
        self.write(incident_id, "alert.json", payload)
        incident = {
            "id": incident_id,
            "kind": kind,
            "action": action,
            "status": "queued" if action == "queued" else "skipped",
            "received_at": now_iso(),
            "alertnames": sorted({a.get("labels", {}).get("alertname", "") for a in alerts}),
            "alert_keys": keys,
            "endpoints": [{"method": m, "route": r} for m, r in endpoints(real_alerts)],
        }
        self.write(incident_id, "incident.json", incident)
        logger.info("Alert %s: kind=%s action=%s", incident_id, kind, action)
        if action == "queued":
            self.queue.put(incident_id)
        return incident

    def process(self, incident_id):
        """Collect context, run Claude Code and save its response."""
        incident = self.update_incident(incident_id, status="running", started_at=now_iso())
        payload = json.loads((self.incident_dir(incident_id) / "alert.json").read_text("utf-8"))
        test = incident["kind"] == "test"
        try:
            if test:
                prompt = ack_prompt(payload)
            else:
                alerts = [a for a in payload.get("alerts", [])
                          if a.get("status", payload.get("status")) == "firing"
                          and not is_test_alert(a)]
                context = self.collector.collect(alerts)
                self.write(incident_id, "context.json", context)
                prompt = incident_prompt(payload, alerts, context)
            self.write(incident_id, "prompt.md", prompt)

            command = claude_command(self.config, test)
            started = time.monotonic()
            result = self.runner(command, prompt, self.config.workdir, self.config.claude_timeout)
            self.write(incident_id, "response.json", result.stdout or "")
            self.write(incident_id, "claude.stderr.log", result.stderr or "")
            changes = {"exit_code": result.returncode,
                       "duration_s": round(time.monotonic() - started, 1),
                       "command": command}
            try:
                output = json.loads(result.stdout)
                self.write(incident_id, "response.md", str(output.get("result", "")))
                changes.update(session_id=output.get("session_id"),
                               cost_usd=output.get("total_cost_usd"),
                               agent_error=output.get("is_error"))
                ok = result.returncode == 0 and not output.get("is_error")
            except (TypeError, ValueError):
                ok = False
            self.update_incident(incident_id, status="completed" if ok else "failed",
                                 finished_at=now_iso(), **changes)
        except subprocess.TimeoutExpired:
            self.update_incident(incident_id, status="failed", finished_at=now_iso(),
                                 error=f"Claude Code timed out after {self.config.claude_timeout}s")
        except Exception as exc:
            logger.exception("Processing %s failed", incident_id)
            self.update_incident(incident_id, status="failed", finished_at=now_iso(),
                                 error=repr(exc))

    def work_forever(self):
        """One agent run at a time, so two incidents never edit the repo concurrently."""
        while True:
            incident_id = self.queue.get()
            if incident_id is None:
                return
            self.process(incident_id)

    def start_worker(self):
        worker = threading.Thread(target=self.work_forever, name="responder", daemon=True)
        worker.start()
        return worker

    def stop_worker(self, worker):
        self.queue.put(None)
        worker.join(timeout=5)


def create_app(responder=None, start_worker=True):
    responder = responder or Responder()

    @asynccontextmanager
    async def lifespan(_app):
        worker = responder.start_worker() if start_worker else None
        yield
        if worker:
            responder.stop_worker(worker)

    app = FastAPI(title="Order Tracker incident responder", lifespan=lifespan)
    app.state.responder = responder

    @app.get("/healthz")
    def health():
        return {"status": "ok", "queued": responder.queue.qsize()}

    @app.post("/alerts", status_code=202)
    async def alerts(request: Request, authorization: str | None = Header(default=None)):
        if responder.config.token and authorization != f"Bearer {responder.config.token}":
            raise HTTPException(401, "Invalid or missing bearer token")
        try:
            payload = await request.json()
        except ValueError:
            raise HTTPException(400, "Body must be JSON")
        if not isinstance(payload, dict) or not isinstance(payload.get("alerts"), list):
            raise HTTPException(422, "Expected a Grafana webhook payload with an alerts list")
        incident = responder.receive(payload)
        return {key: incident[key] for key in ("id", "kind", "action", "status")}

    return app


if __name__ == "__main__":
    import uvicorn

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    uvicorn.run(create_app(), host=os.getenv("RESPONDER_HOST", "127.0.0.1"),
                port=int(os.getenv("RESPONDER_PORT", "8001")))
