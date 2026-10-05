# Order Tracker

A small order tracking app for the AI Dev Tools Zoomcamp observability homework. It includes a web page, API, tests, and a Docker Compose setup. You add telemetry, alerts, and an incident responder in Homework 4.

The main user flow is creating an order and checking its status. Three sample orders are created on first startup.

## Run it

You need Docker with Compose. To run the tests, you also need Python 3.11+ and `uv`.

```bash
docker compose up --build -d --wait
```

Open <http://127.0.0.1:8000>. The API is at `/api/orders`, and the health check is at `/healthz`. Data is stored in a Docker volume and survives container recreation.

If port 8000 is occupied, set `ORDER_TRACKER_PORT`, for example:

```bash
ORDER_TRACKER_PORT=18080 docker compose up --build -d --wait
```

Run tests with `uv run --frozen pytest -q`. Stop the app with `docker compose down`. Add `-v` only if you also want to delete the order data, stored logs, and traces.

## API

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/` | Web page |
| GET | `/healthz` | Database health check |
| GET | `/api/orders` | List orders |
| POST | `/api/orders` | Create an order |
| GET | `/api/orders/{id}` | Check an order |
| PATCH | `/api/orders/{id}` | Change an order status |

## Telemetry

The app uses OpenTelemetry and sends traces, metrics, and logs over OTLP/HTTP to the `otel-collector` service, which sends traces to Tempo, exposes metrics for Prometheus, and sends logs to Loki.

- **Grafana** is at <http://127.0.0.1:3000> (set `GRAFANA_PORT` to change it) and opens on the **Order Tracker** dashboard: request counts and 5xx errors by route. Prometheus, Loki, and Tempo are provisioned as data sources; use **Explore** for logs and traces. You can view without logging in; the admin login is `admin` / `admin` (set `GRAFANA_ADMIN_PASSWORD` to change it). The dashboard is provisioned from `grafana/dashboards/order-tracker.json`, so edit that file rather than the UI.
- **Prometheus** scrapes the Collector and is at <http://127.0.0.1:9090> (set `PROMETHEUS_PORT` to change it). App series carry `exported_job="order-tracker"`.
- **Loki** receives logs over OTLP and is at <http://127.0.0.1:3100> (set `LOKI_PORT` to change it). Query them with LogQL, for example `{service_name="order-tracker"} | order_id="standard-1001"`. Log attributes such as `order_id`, `trace_id`, and `severity_text` are stored as structured metadata.
- **Tempo** receives traces over OTLP and its API is at <http://127.0.0.1:3200> (set `TEMPO_PORT` to change it). Fetch a trace with `/api/v2/traces/<trace_id>`, or search with TraceQL, for example `{ name = "order.lookup" && span.order.id = "standard-1001" }`. Lookup logs in Loki carry the same `trace_id`.

The configs are in `otel-collector.yaml`, `prometheus.yaml`, `loki.yaml`, `tempo.yaml`, and `grafana/`. When `OTEL_EXPORTER_OTLP_ENDPOINT` is not set, for example when running outside Compose, the app prints telemetry to stdout instead.

The Collector reads `otel-collector.yaml` only at startup, so after editing it run `docker compose restart otel-collector`. Until then the running Collector keeps using the earlier config.

- **Traces:** a server span per request (`GET /api/orders/{order_id}`) with an `order.lookup` child span.
- **Metrics:** `http.server.request.duration` histogram (in Prometheus: `http_server_request_duration_seconds_bucket`/`_count`/`_sum`) with `http.request.method`, `http.route`, and `http.response.status_code` attributes. Unknown paths use the route `unmatched`.
- **Logs:** order lookup results (found, not found, failed), linked to the active trace.

Metrics are exported every 60 seconds. Set `OTEL_METRIC_EXPORT_INTERVAL` (milliseconds) to change this, for example `OTEL_METRIC_EXPORT_INTERVAL=5000 docker compose up -d`.

The app uses SQLite to keep setup small. Run one app container at a time. The course exercise is about detecting and handling an incident, not scaling the database.
