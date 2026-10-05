import pytest
from fastapi.testclient import TestClient
from opentelemetry.trace import StatusCode

from app import main


@pytest.fixture
def client(tmp_path, monkeypatch, telemetry):
    monkeypatch.setattr(main, "DB_PATH", tmp_path / "orders.db")
    with TestClient(main.app, raise_server_exceptions=False) as test_client:
        yield test_client


def request_points(metric_reader):
    data = metric_reader.get_metrics_data()
    return [
        point
        for resource in data.resource_metrics
        for scope in resource.scope_metrics
        for metric in scope.metrics
        if metric.name == "http.server.request.duration"
        for point in metric.data.data_points
    ]


def has_request_point(metric_reader, route, status_code):
    return any(
        point.attributes.get("http.route") == route
        and point.attributes.get("http.response.status_code") == status_code
        and point.count >= 1
        for point in request_points(metric_reader)
    )


def test_successful_lookup_emits_trace_metric_and_log(client, telemetry):
    spans, metric_reader, logs = telemetry
    assert client.get("/api/orders/standard-1001").status_code == 200

    finished = {span.name: span for span in spans.get_finished_spans()}
    server = finished["GET /api/orders/{order_id}"]
    lookup = finished["order.lookup"]
    assert server.attributes["http.route"] == "/api/orders/{order_id}"
    assert server.attributes["http.response.status_code"] == 200
    assert lookup.parent.span_id == server.context.span_id
    assert lookup.attributes["order.id"] == "standard-1001"
    assert lookup.attributes["order.found"] is True

    assert has_request_point(metric_reader, "/api/orders/{order_id}", 200)

    [log] = [r.log_record for r in logs.get_finished_logs() if r.log_record.body == "Order found"]
    assert log.attributes["order.id"] == "standard-1001"
    assert log.trace_id == server.context.trace_id


def test_missing_lookup_records_404(client, telemetry):
    spans, metric_reader, logs = telemetry
    assert client.get("/api/orders/missing").status_code == 404

    lookup = next(s for s in spans.get_finished_spans() if s.name == "order.lookup")
    assert lookup.attributes["order.found"] is False
    assert has_request_point(metric_reader, "/api/orders/{order_id}", 404)
    assert any(
        r.log_record.body == "Order not found" and r.log_record.severity_text == "WARN"
        for r in logs.get_finished_logs()
    )


def test_failed_lookup_records_500_and_exception(client, telemetry, monkeypatch):
    spans, metric_reader, logs = telemetry

    def broken_detail(_row):
        raise ValueError("day is out of range for month")

    monkeypatch.setattr(main, "order_detail", broken_detail)
    assert client.get("/api/orders/standard-1001").status_code == 500

    finished = {span.name: span for span in spans.get_finished_spans()}
    for name in ("GET /api/orders/{order_id}", "order.lookup"):
        assert finished[name].status.status_code == StatusCode.ERROR
        assert any(event.name == "exception" for event in finished[name].events)
    assert finished["GET /api/orders/{order_id}"].attributes["http.response.status_code"] == 500
    assert has_request_point(metric_reader, "/api/orders/{order_id}", 500)
    assert any(
        r.log_record.body == "Order lookup failed" and r.log_record.severity_text == "ERROR"
        for r in logs.get_finished_logs()
    )


def test_unknown_paths_share_one_route_value(client, telemetry):
    _spans, metric_reader, _logs = telemetry
    assert client.get("/no/such/path").status_code == 404
    assert has_request_point(metric_reader, "unmatched", 404)
    assert not any("/no/such/path" in str(p.attributes) for p in request_points(metric_reader))
