# Acceptance plan

> **Fictional engagement.** Brightwater Industrial Supply Group, Norrvik Industrial AB, all people, accounts, figures
> and pre-2026-11 dates are invented. HALDEN ERP 3.1 and SKARVIK OES 4 are simulators. No real customer, deployment
> or outcome exists.

## Scope and criteria

Each [requirement](REQUIREMENTS.md) is tied to tests and, for side effects, to a source outside the application. Release criterion: every
critical and high-severity check passes; the rest pass or carry a documented limitation; checks that need a live model are reported as not
executed unless such a run was authorized.

## Procedure

Anyone can run the public part, without private material or paid calls:

```bash
make verify
```

The author's campaign adds two resilience runs with a determinism diff (`make resilience`), the end-to-end suite (`make e2e`),
default-profile security checks, backup and restore and a clean-clone start. What each part covers: [VERIFICATION](../evidence/VERIFICATION.md).

## Roles in acceptance (fictional)

| Activity | Who |
|---|---|
| Runs the acceptance suite and records evidence | implementing team |
| Walks through the signature scenarios with the order desk | operations manager, sales operations |
| Verifies an audit export independently | compliance reviewer |
| Repeats the clean-clone deployment | system administrator |
| Independent review of code and evidence | separate from implementation; scope in [VERIFICATION](../evidence/VERIFICATION.md#who-executed-what) |

Results: [ACCEPTANCE_REPORT.md](ACCEPTANCE_REPORT.md).
