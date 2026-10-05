# Q5 test evidence

End-to-end check of the responder with a test alert (`alertname=ResponderTest`), sent to the running responder on 2026-10-05.

| Field | Value |
| --- | --- |
| Incident id | `20261005T133532Z-f5f33247` |
| `POST /alerts` | HTTP 202 |
| Kind | `test` |
| Status | `completed` |
| Claude Code exit code | 0 |
| Agent error | `false` |
| Tools | none (`--tools ""`) |
| Response | `ResponderTest acknowledged: Test notification; no incident to fix` |

The raw incident files stay under the git-ignored `incident-response/data/incidents/` and are not committed.

# Q6 incident evidence

End-to-end run of a real incident through Grafana and the responder on 2026-10-05 (times in UTC). Grafana used the `order-tracker-responder` webhook contact point and the `service=order-tracker` route, now provisioned in `grafana/provisioning/alerting/order-tracker-responder.yaml`.

| Step | Observation |
| --- | --- |
| 14:03:09 | `GET /api/orders/{order_id}` for an express order returned HTTP 500 |
| Alert | **Order Tracker 5xx responses** firing for `GET /api/orders/{order_id}` |
| 14:05:00 | Webhook `POST /alerts` returned HTTP 202; incident `20261005T140500Z-5f9552a7` queued |
| Agent | Kind `incident`, status `completed`, exit code 0, agent error `false`; no telemetry query errors in `context.json` |
| Fix | Root cause: `placed_at.replace(day=placed_at.day + 2)` raises `ValueError` for orders placed in a month's last two days. Changed to `placed_at + timedelta(days=2)` in `app/main.py`, with the regression test `test_express_order_placed_at_month_end` in `tests/test_api.py`. Both reviewed. |
| Tests | `uv run --frozen pytest -q`: 21 passed |
| Deploy | Only the app was rebuilt and recreated; the observability stack kept running |
| 14:11:35 | The same request returned HTTP 200 with `estimated_delivery` `2026-10-02` |
| Alert | Grafana subsequently showed the alert as Normal |

As for Q5, the raw incident files are not committed.
