# ADR-F08 — Bounded assistant; OpenAI API as the exclusive live inference provider

- Status: Accepted
- Decision: the assistant is optional (`LBF_ASSISTANT_PROVIDER = disabled | scripted | openai`, default `disabled`).
  It runs with the invoking human's principal intersected with a fixed allowlist of nine read/draft tools; it has
  no tool that approves, submits, redelivers, withdraws, resolves or administers. The tool-call loop is owned by
  `lbf.assistant.service`; providers implement a framework-neutral interface. The only live provider is the
  official `openai` Python SDK using the Responses API, imported only in
  `lbf.assistant.providers.openai_provider`; live calls require double opt-in (`openai` + `LBF_LIVE_INFERENCE=true`),
  a key from a Docker secret, a model (default `gpt-6-luna`) and complete owner-entered prices. A scripted provider and a
  deterministic guided-intent router work without a key.
- Alternatives rejected: other providers (owner decision), LangChain or SDK agent runners (extra orchestration over
  tool execution), provider-hosted tools.
- Consequences: prompts are not a security boundary; the workflow's server-side controls are. Live behaviour needs a
  separately authorized run; what has been observed is listed in [KNOWN_LIMITATIONS](../KNOWN_LIMITATIONS.md) (L-02).

## Addendum (remediation of the assistant findings)

- **Intent gate** (`lbf.assistant.intent`). Validating arguments is not enough: a request for minus three units became
  a schema-valid draft of three. A drafting call is now executed only when the person's own words of that turn establish
  every argument under a small explicit grammar. Explicitly invalid quantities are refused. Everything else becomes a
  proposal that the person confirms (`confirm <code>`); the application then executes exactly the stored arguments, once,
  in the same session, with the person's current permissions, without a model. The gate is an allowlist, not general
  language understanding: what it cannot read it does not write.
- **Receipts** (`lbf.assistant.receipts`). Every stored draft yields a receipt made by the application and read back from
  the database (operation, version, content hash, audit event). It does not depend on the model's answer and survives
  every fallback. An unobserved outcome is reported as unknown.
- **Typed facts** (`lbf.assistant.answer`). The model names one field and copies its value; the application compares them
  exactly and renders the fact from the observed value. Free text is checked for exact numbers and identifiers,
  credit-hold polarity and claims of actions not performed; it is not semantically verified and is labelled as the
  assistant's wording.
- **API**: `TurnOut` gains the additive fields `receipts` and `proposal`; `tool_calls[]` entries gain `outcome`,
  `error_code`, `refused_reason` and, for drafting calls, `receipt`. No field was removed or changed.
- Alternatives rejected: confirming every draft (a needless step for explicit requests); pattern denylists (fail open);
  a second model as judge (not an enforcement mechanism).

## Update (2026-11-24): model authority
The model is no longer a configurable override: only `gpt-6-luna` is accepted, as configuration and as reported by the
provider, and anything else fails closed ([ADR-F10](ADR-F10-openai-provider.md)). The provider interface is unchanged.
