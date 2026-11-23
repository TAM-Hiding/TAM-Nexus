# NX-002 operator guide

This guide describes the deterministic in-process fixture path. For the Linux subprocess profile and security tests, see the [NX-003 worker guide](../nx-003/README.md).

NX-002 runs one synthetic HERO-CAD workflow on Linux using Python 3.11+ and SQLite. All workers, source revisions, Archive references and provider operations are fixtures. The runtime uses the Python standard library and opens no network connections.

## Run and inspect

From the repository root:

```sh
python -m unittest discover -s tests -v
python -m nexus --help
python -m nexus.examples /tmp/nexus-fixture-reports
```

The example command generates four human reports with matching JSON reports and audit traces. Use a fresh output directory to avoid replacing examples.

| Scenario | Human report | JSON report and trace |
| --- | --- | --- |
| Successful draft and accepted handoff | [Text](../../examples/nx-002/successful.txt) | [JSON](../../examples/nx-002/successful.json) |
| Interrupted verification at its previous checkpoint | [Text](../../examples/nx-002/interrupted.txt) | [JSON](../../examples/nx-002/interrupted.json) |
| Denied implementation | [Text](../../examples/nx-002/denied.txt) | [JSON](../../examples/nx-002/denied.json) |
| Ambiguous publication with durable quarantine | [Text](../../examples/nx-002/ambiguous.txt) | [JSON](../../examples/nx-002/ambiguous.json) |

## Operator commands

Each mutation takes a unique command ID. Every work command also takes the current version from `report WORK --json`. An exact replay returns the original committed result. A concurrent replay of an armed effect returns an in-progress response. If the coordinator crashed before completing the command, inspect the report and reconcile the intent. Start with a fresh private directory:

```sh
nexus_state=$(mktemp -d)
nexus() { python -m nexus --state "$nexus_state" "$@"; }
nexus command message --id message-1 --args '{"text":"Synthetic X then Y fails. X/Y/Z passes."}'
nexus command submit --id submit-1 --args '{"message":1}'
nexus command run --id investigate-1 --work 1 --version 1
nexus command propose --id propose-1 --work 1 --version 3
nexus report 2
```

For subsequent mutations this shell helper gets the current version. The coordinator still rejects stale requests when another command wins the race.

```sh
nexus_version() {
  nexus report "$1" --json | python -c 'import json,sys; print(json.load(sys.stdin)["work"]["version"])'
}
nexus_decision() {
  nexus report "$1" --json | python -c 'import json,sys; r=json.load(sys.stdin); print(json.dumps(dict(binding=r["pending_approval"], allow=True)))'
}
nexus command decide --id approve-edit --work 2 --version "$(nexus_version 2)" --args "$(nexus_decision 2)"
for phase in implement verify review; do
  nexus command run --id "$phase-1" --work 2 --version "$(nexus_version 2)"
done
nexus report 2
```

Review the binding before using `decide`. The second decision binds the reviewed candidate and target. Simulated publication takes two effects. The first creates a fixture ref and the second creates a fixture draft.

```sh
nexus command decide --id approve-publish --work 2 --version "$(nexus_version 2)" --args "$(nexus_decision 2)"
nexus command publish --id push-1 --work 2 --version "$(nexus_version 2)"
nexus command publish --id draft-1 --work 2 --version "$(nexus_version 2)"
nexus report 2 --json
```

After reading the report, pass its handoff digest as `{"handoff":"...","allow":true}` to `command accept`, with a fresh ID and current work version. Use `allow:false` to reject that handoff and retain its record. `decide` with `allow:false` at the implementation gate declines the order. At the publication gate it delivers a local-only handoff with any already confirmed effects visible in the report.

| Command | Additional arguments and behavior |
| --- | --- |
| `message` / `submit` | Store private `text`, then explicitly submit by `message` ID. Optional fixture `mode` is `reproduced` or `inconclusive`. |
| `propose` | After confirmed reproduction, create a linked proposal. Optional `prior_work` identifies a terminal implementation for the same investigation. |
| `decide` / `revoke` | Pass exact `binding`, `allow`, optional expiry `ttl`, or a `grant` ID to revoke. |
| `claim` / `run` | Claim one fixture lease, or run one worker step. Splitting the calls permits interruption before completion. |
| `recover` / `resume` | After interruption, run global `recover` explicitly. It fences outstanding attempts and inspects armed effects. `resume` resumes one blocked work order when its prerequisites permit. |
| `advance` / `policy` | Advance the durable simulation clock by positive `ticks`, or increment the policy revision and invalidate prior authorization. |
| `cancel` / `reconcile` | Stop further dispatch, or inspect a pending publication outcome. Pass `acknowledge_unknown:true` to finish cancellation while retaining target quarantine. |
| `publish` | Optional `fault` accepts `lost_before`, `lost_after`, `mismatch`, or `commit_gap`. These inject fixture failures. |
| `rework` | Before a publication effect exists, restart bounded fixture implementation under its current edit grant. Old verification, review and publication bindings are stale. |
| `archive` | During an active investigation, resolve opaque fixture `reference` and `revision` values. Examples include `example` at `r1`, `restricted` at `r2` and `offline` at `r1`. |

To inspect original private text, use the separate `message MESSAGE_ID` read command. Pass sensitive-looking synthetic input via `--args -` and standard input if you want to keep it off the process command line. The prototype's local owner can read all data in the state directory.

## Architecture and failure boundaries

- The entry point binds the local operator principal. The coordinator serializes policy, work transitions, durable claims, grants and audit events with SQLite transactions. Work/proposal bodies are immutable records identified by content digests. Candidate revisions are fixture artifacts.
- Worker selection is deterministic by priority and ID after filtering by capability, contract version, policy and role separation. The runner binds worker identity to an attempt, generation, expiry and exact input. The coordinator validates the result against the closed fixture contract. A review by the implementer cannot satisfy the gate.
- A separate effect ledger simulates provider commits that outlive lost responses or a local rollback. Each armed effect has a durable command reservation and a budget charge. Acknowledgement loss resolves only when the provider ledger and refs prove the intended effect. An absent receipt is ambiguous. Work targeting an ambiguous or mismatched ref remains blocked even after cancellation. Known ref drift stops publication and requires operator disposition.
- Checkpoints reference owner-only digest-addressed artifacts. An artifact is durably written before its SQLite reference. An interrupted transaction can leave an unreferenced blob, which is ineligible as evidence. Missing or corrupt evidence blocks work advancement. Restoring valid bytes lets the operator resume the handoff without rerunning an accepted worker step.
- The CLI bootstraps schema version 1 on first use and rejects unsupported versions. The coordinator and fake provider use separate SQLite databases. Work and audit writes are atomic within the coordinator, while provider reconciliation is deliberate across the two ledgers.
- Dispatches reserve a unit of work budget before starting. The simulated clock advances only on command. Time deadlines cover queued work and approvals as well as execution. Budget and clock survive reopen. `recover --args '{"restore":true}'` freezes all publication in that state directory when intent history may be incomplete.

## Limits and recommended NX-003

The prototype uses same-user local trust, an in-process deterministic runner and a modeled provider. It does not validate Linux sandbox containment, hostile processes, real credential scope, GitHub guarantees, shared data policy or automatic backup restoration. Default reports suppress conversation text and arbitrary callback payloads. They retain fixture metadata, selected labels and hashes. Use synthetic input. Transcripts remain in the private database. There is no automatic release of ambiguous target quarantine in this prototype.

Recommended NX-003 scope is one isolated Linux subprocess runner using the same synthetic fixture and contracts. Verify read-only input mounts, process-tree termination, restricted filesystem and credential access, no egress, timeout enforcement, and restart fencing under a real killed process. Keep the publication broker mocked and defer live Archive, models, transport adapters and repository writes.
