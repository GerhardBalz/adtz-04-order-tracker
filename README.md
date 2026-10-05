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

Run tests with `uv run --frozen pytest -q`. Stop the app with `docker compose down`. Add `-v` only if you also want to delete the order data.

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

The app uses OpenTelemetry and prints traces, metrics, and logs to stdout (`docker compose logs app`):

- **Traces:** a server span per request (`GET /api/orders/{order_id}`) with an `order.lookup` child span.
- **Metrics:** `http.server.request.duration` histogram with `http.request.method`, `http.route`, and `http.response.status_code` attributes. Unknown paths use the route `unmatched`.
- **Logs:** order lookup results (found, not found, failed), linked to the active trace.

Metrics are exported every 60 seconds. Set `OTEL_METRIC_EXPORT_INTERVAL` (milliseconds) to change this.

The app uses SQLite to keep setup small. Run one app container at a time. The course exercise is about detecting and handling an incident, not scaling the database.
