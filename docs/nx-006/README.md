# Engineering investigation handoff

NX-006 exports a versioned record from the existing coordinator database and evidence store. A handoff is read-only and carries no execution authority. Use it to compare authorized experiments and make inconclusive or missing evidence visible.

## Export

With the state directory from the [HERO-CAD pilot](../nx-005/README.md), run:

```sh
python -m nexus --state "$state" handoff 1
python -m nexus --state "$state" handoff 1 2 --json
python -m nexus --state "$state" handoff 1 2 --notes ./hypotheses.json --json
```

Specify the local work IDs to compare explicitly. Up to eight distinct source investigations of the same supported profile can be selected. A selected work ID does not grant access to any other work. Use the same database and artifact store together. A work record awaiting human acceptance can already contain a coordinator-accepted execution checkpoint. That state does not confer human approval.

| Field | Version 1 meaning |
| --- | --- |
| `schema`, `version`, `authority` | `nexus.investigation-handoff`, `1`, `informational_only`. Tool consumers should reject unsupported versions. |
| `investigations` | Work revision, state and source authorization ID, approved read scope, repository label, source identity and pinned revision, source and manifest digests, execution profile and dependency digest. |
| `observations` | Measured process outcome, elapsed time, scoped source-unchanged result where available, test profile, worker, observed host platform where captured, sandbox, evidence reference, stream fingerprints and limitations. Raw streams are omitted. |
| `evidence_issues` | Missing, corrupt, unsafe, unmatched or unaccepted evidence and work without a current checkpoint. An interrupted result stays in this section. |
| `hypotheses` | Unverified explanations with supporting and contradictory evidence references, uncertainty and a suggested verification experiment. |
| `recommendations` | Proposed read-only next actions with required permissions, informational value, risks, dependencies and evidence references. They carry `authorization: not_granted`. |

Each reference is an exact `{work, attempt, sha256}` tuple. The exporter verifies the current work revision, accepted generation and matching checkpoint and capture records, then reads and verifies the artifact and reconstructs its capture against the approved request. It also verifies the coordinator acceptance packet, its recorded readiness event, and the operator decision for completed work. Evidence hashes identify bytes. Supervisor observations establish the recorded process behavior. Arbitrary test output can still make unsupported claims.

Limit a notes file to 16 KiB of JSON with exactly the `hypotheses` and `recommendations` arrays. Each hypothesis has `explanation`, `uncertainty`, `verification`, `supporting`, and `contradictory`. Each recommendation has `action`, `proposal`, `permissions`, `value`, `risks`, `dependencies`, and `evidence`. Arrays can be empty. Evidence references must match verified observations in the selected handoff. Action `inspect_evidence` requires `["read_approved_evidence"]`. Action `request_experiment` requires `["approve_read_only_experiment"]`. These are declarative labels. JSON marks notes as `operator_supplied_unverified`. Operator notes are included as supplied, so omit any private information you do not want in the export. There is no observation input field.

A report with integrity issues is emitted with exit 2. If supplied notes cite evidence that fails validation, the annotated export is refused. Export without notes to inspect the issues. The CLI opens existing state without creating or upgrading it. If an older database needs migration, open it normally before exporting. A frozen export describes its source state when created. Use the local exporter to validate the references again later. Sharing the export alone does not share the private bytes or independently authenticate their origin.

See the real pilot [JSON](../../examples/nx-006/hero-cad-handoff.json), its [human rendering](../../examples/nx-006/hero-cad-handoff.txt), and the [verification record](verification.md).

## Workstation acceptance

1. Run `python -m nexus preflight --staging-directory "$scratch_parent"` from the Nexus root. Use an existing canonical directory on the filesystem where the Nexus state will live. This verifies coordinator Python, Bubblewrap flags, the actual namespace/seccomp path, and writable host and sandbox scratch. The output includes available staging space. It creates no durable state.
2. Follow the [NX-005 runtime and manifest procedure](../nx-005/README.md). Then run `python -m nexus preflight --environment "$runtime" --manifest "$manifest" --staging-directory "$scratch_parent"` with the manifest you intend to authorize. This checks the approved digest against a real staged runtime and executes the native CAD import probe. Only a successful dependency probe sets `ready_for_hero_cad`.
3. Run the pinned investigation using that manifest and export the handoff. A passing preflight does not replace an actual test run.

Preflight never installs packages or changes policy. If it fails, use the reported missing capability or dependency to choose a supported host or provision a compatible runtime. Ubuntu 22.04 and Arch Linux are not acceptance assumptions. See the verification record for tested host details.

## Proposed reasoning worker boundary for NX-007

A future worker can receive an objective ID and text, a versioned redacted handoff, explicitly authorized evidence references, and a list of available experiment profile IDs. The coordinator resolves those inputs and enforces disclosure, budgets, and policy. The worker can return the validated hypothesis and recommendation arrays and propose a typed read-only experiment request. A request should identify its evidence, approved profile or requested profile, purpose, and expected information. The coordinator constructs any executable plan and routes it through explicit operator approval. With no approval the request remains informational.

The future worker may suggest how to interpret an observation. Its outputs cannot assert a checkpoint, grant permissions, access unrestricted raw evidence, or publish. The coordinator continues to construct the observation section. Review that interface with a deterministic stand-in and one approved evidence set in NX-007 before enabling any bounded model execution. Provider choice, transport and UI can evolve independently of the record.
