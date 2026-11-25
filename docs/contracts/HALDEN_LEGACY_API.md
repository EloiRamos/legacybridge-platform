# HALDEN ERP 3.1 — Published HTTP Interface

HALDEN is a simulated legacy system written for this project. It is not a real product, was not taken from any company, and does not predate this project. This page describes the frozen contract that the Java system implements and the Python integration consumes. "Raw" means that the stored value is returned unchanged: untrimmed, in its original case, with SQL `NULL` returned as JSON `null`, an omitted XML element or an empty CSV field.

## Transport and encoding

- Base URL `http://<host>:8090`. Product paths live under `/halden/`. The simulation control plane `/_sim/*` is not part of the product.
- Requests:
  - GET parameters go in the query string.
  - POST bodies are `application/x-www-form-urlencoded; charset=UTF-8`. HALDEN never parses JSON.
  - Parameters are decoded as UTF-8.
  - A repeated parameter name is a positional list.
- Responses:
  - Bodies are UTF-8. Clients must not rely on `Content-Type`.
  - CSV and `key=value` text use `\r\n` line endings.
  - Unknown paths get the servlet container's default 404 page, and unsupported methods get its default 405. Neither is part of the contract.

## Gateway

The gateway checks every request in this order:

1. **Bypass.** `/halden/ping`, `/halden/session` and `/_sim/*` skip the checks below.
2. **Ticket.** The ticket is sent in the header `X-Halden-Ticket`.
   - Missing or blank: **401** `text/plain` `NO TICKET`.
   - Unknown or revoked: **403** HTML page.
   - Expired: **440** `{"fault":"TICKET_EXPIRED"}`.
3. **Rate limit.** A fixed window per ticket. When exceeded: **429** `THROTTLED` with header `X-Halden-Wait: <seconds, ≥1>`. A limit of 0 disables the check.
4. **Armed faults.** These are demo and test only (see the control plane).
5. **Servlet.**
6. **Uncaught errors.** **500** HTML page `HALDEN ERP - Internal Error`.

**I1 (pre-handler purity).** A 401, 403, 440 or 429 response, and a 500 produced by an armed `error_500` fault, is returned before any business logic runs. It never coincides with a state change.

## Product endpoints

| # | Method and path | Request | Success | Errors |
|---|---|---|---|---|
| E1 | `GET /halden/ping` | — | `HALDEN-ERP 3.1 READY` | — |
| E2 | `POST /halden/session` | `login`, `secret` | `STATUS=GRANTED`, `TICKET=<32 hex>`, `EXPIRES_IN=<s>` | **200** `STATUS=DENIED`, `REASON=BAD_CREDENTIALS` |
| E3 | `GET /halden/customers` | `name` (case-insensitive substring), `acct` (integer), `offset`, `limit` (≤5) | `{"rows":[{acct,nm,contact,mail,tel,hold,since,city,ctry}],"next_offset":int\|null}` | — |
| E4 | `GET /halden/items` | `page` (1-based), `q`, `cat` | XML `<ITEMS page pages>` with `<ITEM id>` children `ITMNO, DESCR, CATG, UPRICE, UOM, AVAIL_FLG, DISC_FLG, UPDTS`; 4 per page | a page beyond the end is empty |
| E5 | `GET /halden/stock` | `item` (optional) | `ITEM;WH;ON_HAND;ALLOCATED` header plus raw rows in insertion order | an unknown item returns the header only |
| E6 | `GET /halden/orders` | `acct`, `cursor` | `{"data":{"orders":[{OrderNo,Acct,Date,Stat,Total,Ref}]},"continue":str}`; 3 per page | a malformed cursor returns the first page |
| E7 | `GET /halden/orders/{no}` | path segment, case-insensitive | `{"rc":0,"order":{…,"lines":[{ln,item,qty,price}]}}` | **200** `{"rc":404,"msg":"ORDER NOT ON FILE"}` |
| E8 | `POST /halden/orders` | `acct`, `ref` (optional), `item` × 1–20, `qty` × same count | `{"rc":0,"order_no":"SO-NNNNNN","total":"0.00"}` | see below |

E2 issues a ticket, and a new grant revokes the previous ticket for that login. Only E8 changes state.

XML rules (E4):
- A `NULL` column means the element is **omitted**.
- An empty string means an **empty** element.
- Text is XML-escaped, with whitespace preserved.

E6 rules:
- `Acct` is returned without leading zeros.
- `continue` is URL-safe base64 of the next offset, or `""` on the last page.

## Write semantics (E8)

**Validation.** The first failure wins and returns **200** `{"rc":<code>,"msg":"<TEXT>"}`:

| rc | msg | Condition |
|---|---|---|
| 40 | `BAD_PAYLOAD` | Payload shape (missing `acct`, 0 or more than 20 items, `qty` count ≠ `item` count) |
| 11 | `UNKNOWN_ACCOUNT` | |
| 12 | `ACCOUNT_ON_HOLD` | |
| 21 | `UNKNOWN_ITEM` | The first item record that matches by trimmed, upper-cased number is used |
| 22 | `ITEM_DISCONTINUED` | |
| 23 | `NO_PRICE` | |
| 30 | `BAD_QTY` | Quantity outside 1..999 |

**Stock.** Quantities of the same item are summed, and free stock is pooled over warehouses. If the order needs more than is free, the response is **409** `text/plain` `SHORT:<ITEM>:<available>` for the first short item.

**Booking.** The booking runs in one JDBC transaction under one write lock:
- order number `SO-` plus the next 6-digit number;
- date `dd.MM.yyyy` (Europe/Berlin);
- status `OP`;
- total = Σ qty × price, with 2 decimals;
- `CUSTREF` = `ref` trimmed and **truncated to 12 characters** (blank becomes `NULL`).

Lines are written with upper-cased item numbers. Stock is allocated from `DUS` first, then `MAN`.

**Invariants:**

| ID | Rule |
|---|---|
| I1 | Pre-handler responses never change state |
| I2 | Every `rc ≠ 0` and every 409 leaves all tables unchanged |
| I3 | `rc 0` is sent only after commit |
| I4 | **No idempotency.** `CUSTREF` is free text, not unique, never used to deduplicate. Two posts with the same `ref` create two orders |
| I5 | Orders never update `AVAIL_FLG` |
| I6 | Order numbers are strictly sequential within a database lifetime |

## Data rules

Every business column is stored as text. The system has no foreign keys and no uniqueness on item numbers, and the 12-character reference width is enforced only in code. Seed data (8 customers, 12 item records for 11 SKUs, 17 stock rows, 10 orders) is fictional and deliberately contains:
- mixed date formats (`YYYY-MM-DD`, `DD.MM.YYYY`, `YYYYMMDD`);
- prices with currency symbols and decimal commas;
- yes/no flags spelled `J`, `Y`, `1`, `true`, `N`, `nein`, `0` or blank;
- a duplicate SKU under two item records;
- an unreadable quantity `?`, a thousands separator `1.200` and a negative on-hand quantity.

## Simulation control plane (`/_sim/*`, demo and tests only)

The control plane is present only when `HALDEN_SIM_CONTROL=true`; otherwise every `/_sim/*` request gets **404** `NOT FOUND`. It bypasses ticket checks, the rate limit and faults. Invalid parameters return **400** `BAD PARAM`.

| # | Method and path | Effect |
|---|---|---|
| C1 | `POST /_sim/reset` (`rate_limit`, `ticket_ttl_sec`, `rate_window_sec`) | Reseeds the database and clears tickets, faults and rate windows. Returns `{"reset":true,"rate_limit":R,"ticket_ttl_sec":T,"rate_window_sec":W}` |
| C2 | `POST /_sim/faults` (`method`, `path_prefix`, `mode`, `count`, `delay_ms`) | Arms `error_500`, `delay` or `commit_then_500` (the last only on `POST /halden/orders`) |
| C3 | `GET` / `DELETE /_sim/faults` | Lists or clears the armed faults |
| C4 | `POST /_sim/external-order` (`acct`, `item`, `qty`) | Books through the same order logic with `ref = EXT-NNNNNN`, simulating a counter-sales terminal |

The integration uses C1–C4 only for "Reset demo data", the setup of scenarios S6 and S7, and tests. Order submission and reconciliation never use them and never read fault state.
