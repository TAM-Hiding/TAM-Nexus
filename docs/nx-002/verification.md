# NX-002 verification and review

## Executed checks

NX-001 PR #1 was verified merged at `d2049b8d270e6d3fb61a71c323ab6f42df276b00`. The complete [acceptance specification](../nx-001/nx-002.md) was used as the baseline.

- `python -m unittest discover -s tests -v` passed all **29 tests**, including N01–N13 and focused regressions. No failures or skips.
- All 29 tests passed from a clean commit snapshot on Linux under Python 3.11.18, 3.12.14 and 3.13.5. A Python audit hook rejected socket operations and unexpected subprocesses during these runs, including subprocesses used by the CLI test. Kernel-level syscall tracing was unavailable in the environment.
- `python -m nexus.examples OUTPUT` regenerated eight files byte for byte across all three Python versions. The outputs show successful, interrupted, denied and ambiguous outcomes and include the corresponding event histories in JSON.
- The shell walkthrough from the operator guide opened a simulated draft and reached `awaiting_acceptance` through the ordinary CLI. The suite also exercised acceptance, rejection and multiple fresh processes sharing durable state.
- `git diff --check origin/main...HEAD` passed. Archive, Standard Interlog and HERO-CAD were untouched. The runtime made no real repository changes.

Every N01–N13 case has one or more named tests in [the suite](../../tests/test_nx002.py). The suite exercises real coordinator SQLite transactions plus a separate fake-provider ledger. Authoritative provider and sandbox behavior remain outside the tested boundary.

## Independent review

A separate read-only reviewer inspected initial candidate `12fb7a9bf651c195387a1548152c1f40f62b7532`, reproduced the issues below and verified the corrections on candidate `d961376a438ce55d94fb2d27f29cefd251c477e7`. The reviewer reran 29 tests and regenerated the example files from a clean commit snapshot. This verification record is the only addition after that candidate.

| Finding | Correction and regression |
| --- | --- |
| Medium · An exact duplicate publication command could arrive after the first caller armed an intent and before it sent the effect. The duplicate could commit a denial that prevented the first caller from delivering the effect. | Bind a durable command fingerprint to the intent. While reserved, identical callers see an in-progress result and conflicting callers cannot take over. An interleaving test checks both callers and verifies one effect. |
| Low · Opaque Archive lookups could succeed after an investigation deadline or cancellation. | Require active investigation state, the current policy and an unexpired deadline. Cover cancelled and expired work. |

Implementation checks also exposed two recovery gaps. Publication now reserves budget with the durable intent before the fake provider commits. Recovering a handoff after valid evidence is restored now reuses the accepted checkpoint. Additional regressions cover those cases and revocation between arming and dispatch.

No blockers remained after recheck. The simulation intentionally leaves unresolved targets quarantined and provides no production sandbox, GitHub integration or live Archive access. [Limits and proposed NX-003 scope](README.md#limits-and-recommended-nx-003)
