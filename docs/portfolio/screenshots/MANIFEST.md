# Screenshot capture manifest

Captures of the running console, unedited. Hashes: `SCREENSHOTS.sha256` (from the repository root:
`shasum -a 256 -c docs/portfolio/screenshots/SCREENSHOTS.sha256`). Chromium via Playwright. `01`–`08`: 1440 px wide, 2026-11-25, **current** (final images). `09`–`11`: 2026-11-24, **historical**, earlier builds. `12`–`16`: 1280 px wide at 1.5 scale, 2026-11-25, **current** (same source, before one image library update).
`01`–`08` come from one run of `capture.mjs` on the isolated demonstration stack with a fresh database; every state was produced
through the console, its API and the simulators' fault controls. `02a` and `02b` came out byte-identical to the previous capture.

```bash
make demo-up
node docs/portfolio/screenshots/capture.mjs http://127.0.0.1:8088 deploy/secrets/lbf-demo-credentials.txt 58195 58190
```

| File | Shows | Evidence class |
|---|---|---|
| `01-dashboard.png` | Overview: approvals waiting, one unknown outcome, one decision, both systems, recent outcomes | simulated systems, no model |
| `02a-customers-ambiguity.png` | Two similar HALDEN accounts kept separate | simulated systems, no model |
| `02b-customers-hold-conflict.png` | Credit-hold conflict between linked accounts | simulated systems, no model |
| `03-inventory-raw-vs-normalized.png` | Inventory per system, snapshot age, raw legacy rows | simulated systems, no model |
| `04-approval-panel.png` | Approval dialog: four eyes, exact version and content hash | simulated systems, no model |
| `05-reconciliation-unknown.png` | Reconciliation queue: an unknown outcome and an order that needs a decision | simulated systems, no model |
| `05b-unknown-order-detail.png` | SKARVIK order after a lost acknowledgement: observed, not known, permitted actions | simulated systems, no model |
| `06-skarvik-awaiting-ack.png` | SKARVIK order awaiting acknowledgement, with deadline | simulated systems, no model |
| `07-audit-verify-valid.png` | Audit chain recomputed by the server: VALID | simulated systems, no model |
| `08-assistant-refused-tool.png` | Assistant without a model: draft refused for a role without the permission | deterministic |
| `09-assistant-injection-refused.png` | **Scripted** provider (not a live model) obeys an instruction planted in a SKARVIK note; three prohibited tools refused | scripted; captured on 2026-11-24; `backend/tests/evidence/test_injection_capture.py` asserts zero state change |
| `10-assistant-live-receipt.png` | **Live OpenAI** (`gpt-5.4-mini-2026-03-17`): a draft with its receipt, wording and verified values | live campaign 4, earlier code and model |
| `11-assistant-confirmed-proposal.png` | An open request ("two boxes") became a proposal; after confirmation the application, not the model, stored exactly it | live campaign 3; the answer shown is the application's |
| `12-live-grounded-query.png` | **Live OpenAI** (`gpt-6-luna`): grounded answer with its source fields, whole page | live run of this version |
| `13-live-draft-receipt.png`, `-card.png` | **Live OpenAI** (`gpt-6-luna`): drafting command, the application's verified receipt | the same run; receipt joined to version and audit event by SQL |
| `14-live-proposal.png`, `-card.png` | **Live OpenAI** (`gpt-6-luna`): a question becomes a proposal, nothing written | the same run |
| `15-live-proposal-confirmed.png` | The confirmation of that proposal, carried out by the application without a model | the same run; not a model answer |
| `16-live-authority-boundary.png`, `-card.png` | **Live OpenAI** (`gpt-6-luna`): asked to approve, it declines | the same run; 0 approvals in the database |

`12`–`16` come from one authorized run: production provider, service, API and console in an evaluation process with fictional data and
simulated back ends (not the Compose deployment). A `-card` image is the answer element of the same page, captured at the same moment; it
includes the question, the receipt and the mode line. No image is edited.
