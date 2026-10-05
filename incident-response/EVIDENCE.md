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
