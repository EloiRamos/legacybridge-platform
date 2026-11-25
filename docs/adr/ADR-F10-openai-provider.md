# ADR-F10 — OpenAI provider (Responses API) and its SDK binding

Status: accepted. Context: owner decision (OpenAI API is the exclusive
inference provider). Fictional engagement; the assistant is optional and disabled by default.

## Decision

`lbf.assistant.providers.openai_provider.OpenAIProvider` is the only module that imports `openai` (boundary test
B-8). The tool-call loop belongs to `lbf.assistant.service` — no SDK agent/runner abstraction, no orchestration
framework. The provider maps one `ProviderRequest` to one Responses API call per attempt, decides and budgets every
retry itself (SDK retries are 0), sends only the official endpoint, never enables provider-hosted tools and never
uses server-side storage or response chaining. Live inference needs double opt-in (`LBF_ASSISTANT_PROVIDER=openai`
**and** `LBF_LIVE_INFERENCE=true`), a key file, a model (default `gpt-6-luna`, see the 2026-11-24 update) and complete prices; otherwise no client is
constructed (fail closed, `CONFIG_REJECTED`, router fallback, `/readyz` `assistant: misconfigured`).

## Binding table — `openai` 3.57.0 (exact pin in `backend/uv.lock`)

Each row was confirmed by reading the installed SDK source (`.venv/lib/python3.12/site-packages/openai/…`), not by
recollection. The SDK builds on `httpx2` (resolves alongside the v1 bridge's `httpx==0.28.1`).

| Need | Binding used | Source confirming it |
|---|---|---|
| Client | `openai.OpenAI(api_key=…, base_url="https://api.openai.com/v1", timeout=LBF_OPENAI_TIMEOUT_SEC, max_retries=0[, http_client=httpx2.Client(...) in tests only])` | `_client.py` `OpenAI.__init__` (`api_key`, `base_url`, `timeout`, `max_retries`, `http_client: httpx2.Client`) |
| Official endpoint only | `base_url` passed explicitly: without it the SDK falls back to the `OPENAI_BASE_URL` environment variable | `lib/_client_auth.py` (`os.environ.get("OPENAI_BASE_URL")`) — test AI-OAI-09 sets that variable and observes `api.openai.com` |
| Request | `client.responses.create(model, instructions, input, tools, tool_choice="auto", parallel_tool_calls=False, text, max_output_tokens, store=False)` | `resources/responses/responses.py` `Responses.create` parameters |
| Function tools | `{"type": "function", "name", "description", "parameters": <strict JSON Schema>, "strict": True}` | `types/responses/function_tool_param.py` (`name`, `parameters`, `strict`, `type`, `description`) |
| Structured output | `text={"format": {"type": "json_schema", "name": "AssistantAnswer", "schema": …, "strict": True}}` | `types/responses/response_format_text_json_schema_config_param.py` |
| Conversation state | Client-side: full input each request; prior calls echoed as `{"type": "function_call", "call_id", "name", "arguments"}` and results as `{"type": "function_call_output", "call_id", "output"}`; no `previous_response_id` | `types/responses/response_function_tool_call_param.py`, `response_input_item_param.py` (`FunctionCallOutput`) |
| Tool calls in the response | output item `type == "function_call"` with `call_id`, `name`, `arguments` (JSON string) | `types/responses/response_function_tool_call.py` |
| Final answer / refusal | output item `type == "message"`, content parts `output_text` (`text`) or `refusal` (`refusal`) | `response_output_message.py`, `response_output_text.py`, `response_output_refusal.py` |
| Incomplete | `response.status == "incomplete"` (`incomplete_details.reason`) ⇒ `INCOMPLETE`, no continuation | `types/responses/response.py` (`status`, `incomplete_details`) |
| Usage | `response.usage.input_tokens`, `.output_tokens`, `.input_tokens_details.cached_tokens` | `types/responses/response_usage.py` |
| Reported model / request id | `response.model`, `response._request_id` (from `x-request-id`) | `response.py`, `_models.py` (`_request_id`) |
| Errors | `APITimeoutError` ⇒ TIMEOUT (never retried); `APIConnectionError` ⇒ CONNECTION (retried only when the cause is a connect failure, i.e. nothing was sent); `APIStatusError.status_code` 429 ⇒ RATE_LIMITED, 401 ⇒ AUTHENTICATION, 403 ⇒ PERMISSION, 404 ⇒ NOT_FOUND, 400/422 ⇒ BAD_REQUEST, 5xx ⇒ SERVER_ERROR; retries only for 429/500/502/503/504 with `min(Retry-After, LBF_OPENAI_RETRY_MAX_WAIT_SEC)` | `_exceptions.py` class hierarchy |

## Budgets and telemetry
Reservation before each request (`input bytes × input price + max_output_tokens × output price`, per million),
per-request/turn/daily/evaluation ceilings, append-only `inference_budget_ledger` (RESERVE then SETTLE) and
`inference_calls` (no key, header, prompt or output content). Money is always labelled an **estimate**; the OpenAI
dashboard and invoice are authoritative.

## Verification (offline)
`backend/tests/assistant/test_assistant.py` (AI-SEC-01…07, AI-OAI-01…12, AI-COST-01/02/04) and
`test_eval_harness.py` (AI-EV-01, AI-COST-03, AI-COST-05) run against an injected `httpx2.MockTransport` serving
hand-written Responses-API payloads with the constant key `test-key-not-a-secret`; the session network guard
records zero non-loopback attempts. No live request had been made at that
stage; live runs need an owner authorization and approval of the specific run.

## Consequences
SDK or API drift is caught by these contract tests only for the shapes they encode; a live smoke (owner-approved)
would be the only confirmation against the real service. Prompt text is a mitigation, not a control.

## Update (2026-11-23)
The smoke ran on 2026-11-22 (`gpt-6.1-terra`, 3 requests): structured output and a strict tool call worked; the final answer
after the tool call was rejected. A bounded two-turn run passed on 2026-11-23
([summary](../evidence/VERIFICATION.md#assistant)). The binding table is unchanged.

## Update (2026-11-24): default model
By owner decision the default configured model of the OpenAI provider is `gpt-6-luna`. There was no default before. The default applies only when
`LBF_ASSISTANT_PROVIDER=openai`; `OPENAI_MODEL` overrides it, and the provider stays off, live inference stays double
opt-in, and key, prices and budgets are still required. Telemetry keeps the configured and the reported model
separately. Earlier runs keep the identifiers they used (`gpt-6.1-terra`, `gpt-5.4-mini-2026-03-17`); none of their
results says anything about `gpt-6-luna`.

Verified offline on a mocked transport: request shape, strict function tools, strict structured output, nine tools,
ignored reasoning items and exact model telemetry (`backend/tests/assistant/test_default_model.py`). **Not verified:**
availability to an account, live compatibility and answer quality.

## Update (2026-11-24): one authorized model
A later owner decision supersedes the override described above. `gpt-6-luna` is the only model this provider may use:
the identifier is one constant (`AUTHORIZED_OPENAI_MODEL`), no environment variable, file or authorization record extends
it, and every request names it. Any other `OPENAI_MODEL` (another model, a dated snapshot, another case, a fine-tune) is
`CONFIG_REJECTED`: no client, no reservation, no ledger slot, no request. The provider enforces this for any configuration
object. Smoke, live-validation and evaluation runs also need the variable set explicitly, and an authorization that names
the same model.

A response is used only if it **reports** `gpt-6-luna`, or that name with a valid date appended (a validation grammar; no
dated identifier is known to exist). Otherwise nothing of it is mapped, executed, shown, repaired or retried: the call is
recorded `FAILED` / `MODEL_MISMATCH` with the verbatim identifier and settled at no less than its worst case, the turn
falls back to the deterministic router, and the process sends no further request until it is restarted. A campaign also
pins the first accepted identifier, ends `ABORTED_MODEL_MISMATCH` and cannot be resumed under its authorization (ledger
schema 2; earlier ledgers are kept as they are and not opened again). Tests: `backend/tests/assistant/test_luna_contract.py`.
