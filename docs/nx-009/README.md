# Evidence closure and bounded inference

An operator can assess one existing hypothesis against accepted evidence and record it as `supported`, `contradicted`, or `unresolved`. The assessment retains the original hypothesis, its investigation, the evidence references, the rationale and the remaining uncertainty. Recording a conclusion grants no execution authority.

The deterministic investigator remains the default. NX-009 adds a local inference adapter and a simulated transport so the same coordinator contract can be exercised offline. The pilot limits adapter input to the two redacted HERO-CAD investigations and a fixed objective. Worker text can describe an action. Only the coordinator interprets typed fields and evaluates permissions.

## Operator acceptance

Start with private state from the [NX-008 operator walkthrough](../nx-008/README.md) on a machine where that walkthrough and sandbox preflight succeeded. Use its two work IDs in baseline/control order. The examples assume IDs 1 and 2, a completed deterministic invocation 1 and an accepted baseline cross-check. Keeping the database and its private artifacts together allows Nexus to revalidate the original observations. A copied public JSON handoff alone is insufficient.

```sh
nexus() { python -m nexus --state "$state" "$@"; }
# With a prior schema, opening the existing state through the ordinary report upgrades it.
nexus report 1 --json > /dev/null
nexus reason show 1
nexus handoff 1 2 --json
```

Inspect the exact hypothesis digest, source observation references, follow-up reference and measurement limitations. For this controlled example, the following prepares an assessment from the displayed records. Review the file contents and choose your own disposition, rationale and uncertainty before submission. Use a private temporary directory.

```sh
packet=$(mktemp -d)
nexus reason show 1 > "$packet/reason.json"
nexus handoff 1 2 --json > "$packet/handoff.json"
python - "$packet" <<'PY'
import json
import sys
from pathlib import Path

d = Path(sys.argv[1])
reason = json.loads((d / "reason.json").read_text())
handoff = json.loads((d / "handoff.json").read_text())
assert not handoff["evidence_issues"]
evidence = [o["reference"] for o in handoff["observations"]]
evidence += [handoff["followup_observations"][0]["reference"]]
assessment = {
    "invocation": reason["invocation"],
    "hypothesis_digest": reason["hypothesis_digests"][0],
    "evidence": evidence,
    "disposition": "supported",
    "rationale": "The baseline passed and its independent mesh volume matched the specified box. The disposable control failed its original volume assertion.",
    "uncertainty": "The cross-check measured only the baseline. Both calculations share document regeneration and the OCCT kernel.",
}
(d / "assessment.json").write_text(json.dumps(assessment, indent=2))
PY
cat "$packet/assessment.json"
```

The following command records the reviewed assessment. Reuse its command ID for an identical retry. Inspect the durable record in a fresh CLI process.

```sh
nexus command conclude --id assess-box-1 --args - < "$packet/assessment.json"
nexus reason show 1
```

## Exercise disclosure with a mock transport

The fixture returns simulated reasoning. These steps use no model, network service or credentials. Start a distinct invocation so that the reasoning provenance and disclosure approval are independent of the deterministic result.

```sh
objective='Compare the pinned box baseline with its disposable negative control.'
nexus reason start 1 2 --id compare-mock --implementation mock-v1 --objective "$objective"
# Use the invocation ID returned above.
mock=2
nexus reason preview "$mock"
```

Read the payload and destination. Copy the disclosure digest from that preview. This decision permits one inference request with those bytes at that destination. Keep the preview local. An approval for an experiment grants different authority.

```sh
nexus reason disclose "$mock" REVIEWED_DISCLOSURE_SHA256
nexus reason run "$mock"
nexus reason show "$mock"
```

Inspect the evidence references, unverified hypothesis and optional experiment proposal. No subsequent reasoning or experiment starts on completion. After an interrupted invocation, inspect its recorded status, use the existing `command recover` procedure where needed, and review the uncertainty before considering a new invocation. The shared per-work budget includes interrupted invocations.

Run the mandatory regression suite from the repository root with `NEXUS_REQUIRE_LINUX=1 python -m unittest discover -s tests -v`. Physical workstation acceptance remains pending.

## Recorded exercise

The baseline and disposable control were rerun locally with the pinned native runtime. The baseline test exited 0. The deliberately modified control exited 1. The separately approved NX-008 baseline cross-check reported 10,009.36984944 mm³ from oriented triangles and 10,009.369849439998 mm³ from HERO-CAD's volume helper. Its accepted capture and both source captures were available to the operator for closure. The reasoning invocations consumed equivalent selected source observations. The post-experiment measurement was available separately to the operator. [Recorded comparison and assessment](../../examples/nx-009/comparison.json)

| Evaluation | Result in the recorded exercise |
| --- | --- |
| Observation and citation | Both validated outputs cite the exact work, attempt and capture digest for each original test and identify the pass and failure. |
| Hypothesis and uncertainty | Both describe the half-volume behavior as a possible explanation in the deliberately modified control. They acknowledge that exit status alone cannot locate a faulty calculation. |
| Verification proposal | Both propose the fixed, read-only baseline volume cross-check. A measured baseline helps establish a reference. Establishing the control geometry independently would require separate evidence. |
| Authority | The mock invocation required a new disclosure approval. Nexus rejected running before disclosure and rejected an altered disclosure digest. Even an explicitly approved mock proposal had no executable plan. The final state contained two reasoning invocations and exactly one experiment run, the earlier deterministic follow-up. |
| Operator decision | The demonstration recorded `supported` for the exact deterministic hypothesis with all three evidence references and explicit residual uncertainty. Replaying the conclusion command returned its original receipt. A fresh read revalidated the assessment, and the source handoff remained identical. |
| Live-model reasoning quality | Unmeasured. The mock intentionally reuses the deterministic answer inside a simulated provider response. Their matching text verifies the contract and authorization workflow. |

## Optional local model

This host had no explicitly provisioned compatible inference endpoint. No live call, model download, paid API request or workstation connection was used in the demonstration. Nexus supports one optional local destination with the implementation ID `llama-unix-v1`. Its fixed Unix socket is `/run/tam-nexus-inference/inference.sock`. See the [adapter contract](adapter.md) for compatible server behavior, service identity, budgets and limitations.

After operator workstation acceptance and explicit provisioning of an authorized zero-cost endpoint, select `--implementation llama-unix-v1` on a fresh invocation using the same bounded objective and evidence selection. Inspect `reason preview` and decide whether to grant disclosure of the exact request to that destination. Use `reason show` to inspect provenance, timing, validated output, evidence references and uncertainty. The adapter exposes no tool-call, experiment-execution or remote network interface. No installer or server startup is included.
