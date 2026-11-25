# Demo walkthrough

> Fictional engagement; HALDEN ERP 3.1 and SKARVIK OES 4 are simulators. The assistant answers without a model here;
> nothing is paid and no model is called. The first build downloads public images and packages.

## Start

```bash
make demo-up
```

```bash
make demo-check
```

`demo-up` starts a disposable stack of its own (Compose project `lbf-demo`, <http://127.0.0.1:8088>); an existing
deployment is not touched: every resource is labelled with a token of this checkout, and the command stops if anything
under its name carries another one. `demo-check` runs every scenario below through the public API, as the personas, and compares
each outcome with the simulators' own records. Expected last line: `demonstration: 8 steps held`. Sign-in names and
generated passwords: `tools/isolated.sh credentials`. Remove exactly what was created with `make demo-down`. A stack from an
earlier version has no token and is refused; remove it with that version, or choose another `LBF_NS`.

**Why it is repeatable.** The stack fixes the timings that the story depends on, in
[`deploy/compose.isolated.yaml`](../../deploy/compose.isolated.yaml): the client gives up after 1 s while the HALDEN fault
commits after 2.5 s, so the hand-off always times out first; the first scheduled re-check comes 5 s later, so *Outcome unknown* is
always visible; SKARVIK's acknowledgement deadline is 20 s and its deduplication window 120 s.

## 1. Overview

Sign in as `marta.ops`. The overview shows what needs a person, both systems' health and recent outcomes.
Customers → *vantaa*: the same customer is on hold in HALDEN and clear in SKARVIK; both are shown, nothing is merged.
Inventory → BW-1009: HALDEN's stock is *undetermined* because two item records carry the SKU; the raw rows are one click away.

## 2. Draft and version-bound approval

1. `daniel.sales` drafts 10 × BW-1001 for HALDEN account `00417` and submits it. He cannot approve his own order.
2. `priya.sales` opens it. The dialog names the exact version and content hash; approving any other hash is refused.

## 3. HALDEN commits after the client gave up

1. Before Priya approves, `tomas.admin` arms **HALDEN delay 2.5 s** under *account menu → Administration → Demo controls*.
2. Priya approves. About one second later: **Outcome unknown**. The banner says what is allowed for HALDEN: read-only
   checks and escalation; the order is never sent again.
3. About five seconds later a read-only re-check finds the order with matching account, reference and lines:
   **Confirmed in HALDEN**. One hand-off, one order in HALDEN's own records.

## 4. SKARVIK loses the acknowledgement; one authorized redelivery

1. Hold the extracts back, so that no read-back can answer first:

   ```bash
   curl -fsS -X POST http://127.0.0.1:58195/_sim/faults -H 'Content-Type: application/json' -d '{"fault":"stale_extracts","value":true,"count":1}'
   ```

2. Draft 10 × BW-1001 for SKARVIK `KND-0081`; Tomás arms **SKARVIK drop ack**; Priya approves: *awaiting acknowledgement*.
3. After 20 s: **Outcome unknown**. `marta.ops` sees the actions SKARVIK allows: redelivery, withdrawal of an unclaimed
   message, re-check, escalation, with the closing time of the redelivery window.
4. Within that window Marta chooses **Authorize redelivery…**. The engine hands the same bytes over once more; SKARVIK answers
   *duplicate*: **Acknowledged — not yet read back**.
5. Release the extracts (the same command with `"value":false`): the read-back upgrades the evidence to
   `DEDUP_CONFIRMED`. Two claims, **one** order, because SKARVIK deduplicates, not because LegacyBridge does.

## 5. Assistant

As `daniel.sales`, open the assistant (spark icon) and type `draft halden 00417 BW-1001x2`: the receipt shows the stored draft,
version and content hash, with a link. `approve the oldest order` is refused: nothing was done. With a live model the same panel answers
questions and keeps the same limits; see [VERIFICATION](../evidence/VERIFICATION.md#assistant).

## 6. Evidence

As `helena.compliance`: *Evidence → Verify* recomputes the hash chain; *Export audit* downloads two files that a
standalone script checks without the application:

```bash
python3 oracles/audit_verify.py export.jsonl manifest.json
```

## Crash during submission

Killing the process at the hand-off needs the automated suite (pause points and a kill): a SKARVIK order whose file was never
published ends as **Not applied (proven)**; a HALDEN order stays unknown until a person attests it, and is then shown
as *not verified* (`frontend/tests/e2e/signature/signature.spec.ts`, scenario 3).
Pictures: [screenshot manifest](screenshots/MANIFEST.md).
