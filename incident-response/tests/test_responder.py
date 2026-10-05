import json
import subprocess
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

import responder
from responder import Config, ContextCollector, Responder, claude_command, create_app


def grafana_payload(*alerts, status="firing"):
    """A Grafana webhook notification in the shape Grafana 12 sends."""
    return {
        "receiver": "incident-responder",
        "status": status,
        "orgId": 1,
        "alerts": list(alerts),
        "groupLabels": {"alertname": alerts[0]["labels"]["alertname"]} if alerts else {},
        "commonLabels": {},
        "commonAnnotations": {},
        "externalURL": "http://127.0.0.1:3000/",
        "version": "1",
        "groupKey": "{}:{}",
        "truncatedAlerts": 0,
        "title": f"[{status.upper()}:{len(alerts)}]",
        "state": "alerting",
        "message": "",
    }


def alert_5xx(status="firing", fingerprint="abc123", starts_at="2026-10-05T10:00:00Z"):
    return {
        "status": status,
        "labels": {
            "alertname": "Order Tracker 5xx responses",
            "http_request_method": "GET",
            "http_route": "/api/orders/{order_id}",
            "service": "order-tracker",
            "severity": "critical",
        },
        "annotations": {"summary": "5xx responses on GET /api/orders/{order_id}"},
        "startsAt": starts_at,
        "endsAt": "0001-01-01T00:00:00Z",
        "fingerprint": fingerprint,
        "values": {"A": 3, "B": 1},
    }


def responder_test_alert():
    return {
        "status": "firing",
        "labels": {"alertname": "ResponderTest", "instance": "Grafana"},
        "annotations": {"summary": "Responder pipeline test"},
        "startsAt": "2026-10-05T10:00:00Z",
        "endsAt": "0001-01-01T00:00:00Z",
        "fingerprint": "test0001",
    }


class FakeCollector:
    def __init__(self):
        self.calls = []

    def collect(self, alerts):
        self.calls.append(alerts)
        return {"endpoints": [{"route": "/api/orders/{order_id}"}], "error_logs": [],
                "traces": [], "errors": []}


class FakeRunner:
    def __init__(self, stdout=None, returncode=0, raises=None):
        self.calls = []
        self.stdout = stdout if stdout is not None else json.dumps({
            "type": "result", "subtype": "success", "is_error": False,
            "result": "Root cause: day overflow. Fixed.", "session_id": "sess-1",
            "total_cost_usd": 0.12,
        })
        self.returncode = returncode
        self.raises = raises

    def __call__(self, command, prompt, cwd, timeout):
        self.calls.append({"command": command, "prompt": prompt, "cwd": cwd})
        if self.raises:
            raise self.raises
        return subprocess.CompletedProcess(command, self.returncode, self.stdout, "")


@pytest.fixture
def setup(tmp_path):
    config = Config(data_dir=tmp_path / "incidents", workdir=tmp_path, claude_bin="claude",
                    token=None)
    collector, runner = FakeCollector(), FakeRunner()
    resp = Responder(config, collector=collector, runner=runner)
    with TestClient(create_app(resp, start_worker=False)) as client:
        yield client, resp, collector, runner


def files(resp, incident_id):
    return {p.name for p in resp.incident_dir(incident_id).iterdir()}


def test_incident_alert_saves_context_and_agent_response(setup):
    client, resp, collector, runner = setup
    payload = grafana_payload(alert_5xx())

    response = client.post("/alerts", json=payload)

    assert response.status_code == 202
    body = response.json()
    assert body["kind"] == "incident" and body["action"] == "queued"
    incident_id = resp.queue.get_nowait()
    assert incident_id == body["id"]
    resp.process(incident_id)

    incident = resp.read_incident(incident_id)
    assert incident["status"] == "completed"
    assert incident["session_id"] == "sess-1"
    assert incident["endpoints"] == [{"method": "GET", "route": "/api/orders/{order_id}"}]
    assert files(resp, incident_id) >= {"alert.json", "incident.json", "context.json",
                                        "prompt.md", "response.json", "response.md"}
    saved = json.loads((resp.incident_dir(incident_id) / "alert.json").read_text("utf-8"))
    assert saved == payload
    assert "day overflow" in (resp.incident_dir(incident_id) / "response.md").read_text("utf-8")

    assert len(collector.calls) == 1
    command = runner.calls[0]["command"]
    assert command[1:5] == ["-p", "--output-format", "json", "--permission-mode"]
    assert "Bash(uv run --frozen pytest:*)" in command
    assert "Bash(git commit:*)" in command
    prompt = runner.calls[0]["prompt"]
    assert "/api/orders/{order_id}" in prompt and "regression test" in prompt


def test_responder_test_alert_is_acknowledged_without_fix(setup):
    client, resp, collector, runner = setup

    body = client.post("/alerts", json=grafana_payload(responder_test_alert())).json()

    assert body["kind"] == "test" and body["action"] == "queued"
    resp.process(resp.queue.get_nowait())
    incident = resp.read_incident(body["id"])
    assert incident["status"] == "completed"
    assert collector.calls == []
    assert "context.json" not in files(resp, body["id"])
    command = runner.calls[0]["command"]
    # No tools at all, so the agent cannot read, edit or run anything.
    assert command[command.index("--tools") + 1] == ""
    assert "--allowedTools" not in command
    prompt = runner.calls[0]["prompt"]
    assert "not an incident" in prompt and "ResponderTest acknowledged" in prompt


def test_repeated_notification_does_not_rerun_agent(setup):
    client, resp, _collector, _runner = setup
    first = client.post("/alerts", json=grafana_payload(alert_5xx())).json()
    repeat = client.post("/alerts", json=grafana_payload(alert_5xx())).json()
    new_firing = client.post(
        "/alerts", json=grafana_payload(alert_5xx(starts_at="2026-10-05T11:00:00Z"))).json()

    assert first["action"] == "queued"
    assert repeat["action"] == "skipped_duplicate"
    assert new_firing["action"] == "queued"
    assert resp.queue.qsize() == 2
    # The repeat is still saved for the record.
    assert "alert.json" in files(resp, repeat["id"])


def test_handled_alerts_survive_restart(setup, tmp_path):
    client, resp, collector, runner = setup
    client.post("/alerts", json=grafana_payload(alert_5xx()))

    restarted = Responder(resp.config, collector=collector, runner=runner)

    assert restarted.receive(grafana_payload(alert_5xx()))["action"] == "skipped_duplicate"


def test_resolved_notification_is_saved_but_not_sent_to_agent(setup):
    client, resp, _collector, runner = setup
    body = client.post("/alerts", json=grafana_payload(alert_5xx(status="resolved"),
                                                       status="resolved")).json()
    assert body["action"] == "skipped_not_firing"
    assert resp.queue.empty()
    assert "alert.json" in files(resp, body["id"])
    assert runner.calls == []


def test_agent_failure_and_timeout_are_recorded(setup):
    _client, resp, _collector, _runner = setup
    resp.runner = FakeRunner(stdout="not json", returncode=1)
    failed = resp.receive(grafana_payload(alert_5xx(fingerprint="f1")))
    resp.process(failed["id"])
    assert resp.read_incident(failed["id"])["status"] == "failed"
    assert resp.read_incident(failed["id"])["exit_code"] == 1

    resp.runner = FakeRunner(raises=subprocess.TimeoutExpired("claude", 900))
    timed_out = resp.receive(grafana_payload(alert_5xx(fingerprint="f2")))
    resp.process(timed_out["id"])
    assert "timed out" in resp.read_incident(timed_out["id"])["error"]


def test_rejects_bad_payloads_and_tokens(setup):
    client, resp, _collector, _runner = setup
    assert client.post("/alerts", content="nope").status_code == 400
    assert client.post("/alerts", json={"status": "firing"}).status_code == 422

    resp.config.token = "s3cret"
    payload = grafana_payload(responder_test_alert())
    assert client.post("/alerts", json=payload).status_code == 401
    ok = client.post("/alerts", json=payload, headers={"Authorization": "Bearer s3cret"})
    assert ok.status_code == 202


def test_worker_processes_queue(tmp_path):
    runner = FakeRunner()
    resp = Responder(Config(data_dir=tmp_path, workdir=tmp_path), collector=FakeCollector(),
                     runner=runner)
    worker = resp.start_worker()
    incident = resp.receive(grafana_payload(responder_test_alert()))
    resp.stop_worker(worker)
    assert resp.read_incident(incident["id"])["status"] == "completed"


def test_claude_command_options():
    config = Config(claude_bin="definitely-not-on-path", claude_model="opus",
                    max_budget_usd="2")
    command = claude_command(config, test=False)
    assert command[0] == "definitely-not-on-path"
    assert command[command.index("--permission-mode") + 1] == "dontAsk"
    assert command[command.index("--model") + 1] == "opus"
    assert command[command.index("--max-budget-usd") + 1] == "2"


def test_context_collector_queries_metrics_logs_and_traces():
    trace = {"trace": {"resourceSpans": [{"scopeSpans": [{"spans": [{
        "name": "order.lookup", "spanId": "s1",
        "status": {"code": "STATUS_CODE_ERROR"},
        "attributes": [{"key": "order.id", "value": {"stringValue": "express-1002"}}],
        "events": [{"name": "exception", "attributes": [
            {"key": "exception.message", "value": {"stringValue": "day is out of range"}}]}],
    }]}]}]}}
    responses = {
        "/api/v1/query": {"data": {"result": [
            {"metric": {"http_response_status_code": "500"}, "value": [0, "3"]}]}},
        "/loki/api/v1/query_range": {"data": {"result": [{
            "stream": {"severity_text": "ERROR", "order_id": "express-1002", "trace_id": "t1"},
            "values": [["1", "Order lookup failed"]]}]}},
        "/api/search": {"traces": [{"traceID": "t2"}]},
        "/api/v2/traces/t1": trace,
    }
    queried = []

    def fetch(url, params, timeout):
        path = url.split(":", 2)[2].split("/", 1)[1]
        queried.append((path, params))
        if f"/{path}" not in responses:
            raise OSError("connection refused")
        return responses[f"/{path}"]

    collector = ContextCollector(Config(), fetch=fetch)
    context = collector.collect([alert_5xx()], now=datetime(2026, 10, 5, 10, 5, tzinfo=timezone.utc))

    assert context["endpoints"][0]["responses_by_status_15m"] == [{"status": "500", "count": 3.0}]
    assert 'http_route="/api/orders/{order_id}"' in context["endpoints"][0]["query"]
    assert context["error_logs"][0]["trace_id"] == "t1"
    assert [t["trace_id"] for t in context["traces"]] == ["t1"]
    span = context["traces"][0]["spans"][0]
    assert span["events"][0]["attributes"]["exception.message"] == "day is out of range"
    # Trace t2 was found by TraceQL but fetching it failed: recorded, not raised.
    assert any("tempo" in error for error in context["errors"])
    assert context["window"]["start"] == "2026-10-05T09:50:00+00:00"


def test_parse_time_ignores_unset_grafana_times():
    assert responder.parse_time("0001-01-01T00:00:00Z") is None
    assert responder.parse_time("2026-10-05T10:00:00Z").hour == 10
