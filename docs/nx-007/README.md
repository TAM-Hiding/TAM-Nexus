# Bounded reasoning and experiment requests

An operator selects completed investigation evidence for a single objective. Nexus rebuilds the redacted NX-006 handoff from the local evidence store. The deterministic investigator receives that handoff and can return hypotheses and one proposed experiment. Nexus validates the response and records the proposal for a separate operator decision. This interface accepts no provider credentials or worker-supplied commands.

## Operator acceptance

Run these commands from the Nexus root. Use existing private state containing the NX-006 baseline and disposable control. To recreate those inputs, see the fixture procedure below. The example assumes work IDs 1 and 2. Supply the baseline first because it is the target of the proposed measurement.

```sh
nexus() { python -m nexus --state "$state" "$@"; }
nexus reason start 1 2 --id box-compare-1 --objective 'Compare the pinned box baseline with its disposable negative control.'
nexus handoff 1 2 --json
# Read the invocation ID returned by start.
id=1
nexus reason run "$id"
nexus reason show "$id"
```

If the start acknowledgment is lost, repeat the identical start command with the same ID. Nexus returns the original invocation and preserves the remaining budget. Changing the objective or work selection while reusing the ID produces a conflict.

Review the proposal target, pinned source, evidence references, permission, limits, purpose and expected informational value. Copy the exact `proposal_digest` from the reviewed record into `spec`. The following calls exercise the state transitions. Each command marked “expected refusal” returns exit code 2.

```sh
spec=PASTE_REVIEWED_SHA256_HERE
nexus reason execute "$id" "$spec"  # expected refusal before approval
nexus reason approve "$id" "$(python -c 'print("0" * 64)')"  # expected refusal
nexus reason approve "$id" "$spec"  # explicit operator decision
nexus reason execute "$id" "$spec"  # expected refusal: profile has no executor
nexus reason show "$id"
```

The independent volume cross-check would need a separate, reviewed measurement profile. The existing box-regeneration profile measures the original assertion. Rerunning it would answer a different question. An approved cross-check therefore remains informational, with no checkout access, test execution or new observations. A real future execution adapter would need to pass the full source, manifest, dependency and sandbox gates.

An approval expires ten coordinator ticks after issue. Revocation, policy changes or a changed input can make a recorded approval unusable.

Use `nexus reason reject "$id" "$spec"` in place of approval to decline a proposed experiment. Use `nexus reason cancel "$id"` for an unfinished invocation. After an actual coordinator interruption, run the existing `nexus command recover --id UNIQUE_RECOVERY_ID`, then inspect the invocation using `reason show`. Recovery applies to the coordinator state as a whole. An interrupted invocation requires a fresh operator request within the remaining reasoning budget.

## Reasoning interface

| Record | Contents and authority |
| --- | --- |
| Input v1, `nexus.reasoning-input` | Operator objective, invocation and primary investigation IDs, redacted handoff, allowed evidence tuples, requestable experiment profile IDs and coordinator limits. The stand-in receives observations and references. Optional NX-006 notes are omitted. |
| Output v1, `nexus.reasoning-output` | Echoed invocation, hypotheses with supporting and contradictory evidence tuples, uncertainty, proposed verification, and one optional experiment request. Worker prose remains unverified throughout. |
| Durable proposal | Nexus binds the validated request to the invocation, worker and exact source/evidence context. The operator addresses a decision by invocation ID and proposal digest. A requestable profile can lack an execution adapter. |

The stand-in runs inside the existing Bubblewrap sandbox with a read-only JSON input and a bounded JSON response. It has no access to the coordinator process, raw captures, source checkout or dependency environment. Sandbox startup failures leave a typed, incomplete result.

The coordinator selects the authorized handoff from its database. There is no upload path for an externally supplied JSON export to become verified evidence. For each reference, the work ID, attempt ID and capture SHA-256 must match the supplied evidence set. A matching digest identifies captured bytes. The supervised exit status describes what the process did. The investigator's suggested reason for that outcome remains a hypothesis.

Version 1 caps input at 64 KiB, output at 8 KiB, worker runtime at two seconds, hypotheses at three, references per list at eight and experiment requests at one. Each selected work may participate in three invocations total, including cancelled and interrupted invocations. The contract includes no operation for recursively invoking another investigator. Treat the objective and returned prose as private local data when choosing what to export.

## Reproduce the HERO-CAD fixture

Recreate the pinned baseline and the deliberately modified disposable [control checkout](control.md). Use the [NX-005 procedure](../nx-005/README.md) to create a manifest for each revision, authorize the two investigations separately and execute both pinned tests in the same state directory. Give each command a unique ID, and use the message, grant, work and version IDs returned by the coordinator. Use the [NX-006 handoff](../nx-006/README.md) to inspect both records. Actual capture hashes and proposal digests can change across reruns because timings and local source identities are part of provenance.

The [recorded demonstration](../../examples/nx-007/hero-cad-reasoning.json) includes the supervised test outcomes, validated unverified interpretation, exact proposal, approval and boundary decisions. Run the complete acceptance suite with `NEXUS_REQUIRE_LINUX=1 python -m unittest discover -s tests -v`. Physical workstation acceptance requires running that suite and the two real investigations on the intended Linux host.

## Suggested NX-008

Implement a single independently specified read-only volume cross-check, with an operator-reviewed fixed profile, reproducible expected result and exact manifest-to-proposal binding. Reuse the existing sandbox, pinned inputs, attempt fencing and evidence verification. Its result would let an operator judge the NX-007 hypothesis against an independent measurement.
