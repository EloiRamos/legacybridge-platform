# Legacy backend port

Source of truth: `backend/src/lbf/ports/backend.py` (`LegacyBackendPort`) and `backend/src/lbf/ports/capabilities.py`.
Workflow, domain, API and assistant code depend only on this port and on capability
predicates; they never branch on backend identity (boundary check).

| Method | Side effect | Rule |
|---|---|---|
| `capabilities()` | none | static declaration |
| `health()`, `search_customers`, `get_customer`, `list_orders`, `get_order`, `catalogue`, `stock` | read | never write (CF-08) |
| `preflight(request)` | read | HALDEN: ticket + account; SKARVIK: exchange writable, customer in latest extract |
| `render(request)` | none | pure, deterministic: same request ⇒ same bytes (CF-01) |
| `handoff(rendered)` | **write** | exactly one transport handoff; never retries; never raises (CF-02) |
| `poll_acknowledgements()` | read | async only; parsed, not yet archived |
| `archive_acknowledgement(ack)` | file move | called only **after** the acknowledgement is recorded in the database |
| `reconcile(query)` | read | rules R-1…R-3 |
| `withdraw_unclaimed(message_id)` | write (cancel unprocessed message) | capability-gated |
| `redeliver(original)` | write | capability-gated; byte-identical stored payload; one handoff |

Calling a capability-gated method on a backend that does not declare it raises
`CAPABILITY_UNSUPPORTED` before any I/O (CF-07). `archive_acknowledgement` is the explicit form of
"archive move after DB record".

`handoff`, `redeliver` and `withdraw_unclaimed` are called only from `lbf.engines.executor` and the
withdraw command handler `lbf.workflow.withdraw_unclaimed` (boundary test B-6).

## Capabilities (normative values)

| Capability | HALDEN 3.1 | SKARVIK OES 4 (default / test profile) |
|---|---|---|
| write_mode | SYNC_REQUEST_RESPONSE | ASYNC_FILE_DROP |
| dedup | NONE | MESSAGE_ID_WINDOW 259 200 s, margin 3 600 s / 120 s, 10 s |
| correlation field / max / unique | ref / 12 / false | ORDREF / 20 / false |
| customer PO / requested date / note | no / no / no | yes / yes / yes (60, Latin-1) |
| pre-processing refusal signals | yes | no |
| withdraw before pickup | no | yes |
| ack deadline | n/a | 300 s / 20 s |
| read after write | IMMEDIATE | SNAPSHOT, lag 60 s / 6 s |
| inventory | LIVE | SNAPSHOT, max age 900 s / 30 s |
| stock on accept | ATOMIC_ALLOCATION | ACCEPT_WITH_BACKORDER |
| encoding | UTF-8 form | ISO-8859-1 |
| max lines / qty | 20 / 1..999 | 50 / 1..99 999 (canonical 1..999 base units first) |

Derived predicates (properties, not declared fields): `is_async`, `supports_redelivery`,
`can_prove_absence_after_handoff` (async file drop with withdrawal).
