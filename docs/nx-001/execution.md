# Work and execution lifecycle

## Work models and promotion

| Record | Purpose and required identity |
| --- | --- |
| Conversation | Messages and attachments with author, message ID and time. A draft task can cite a message span. Submitting a message alone grants no execution rights. |
| Investigation | A submitted work order of kind `investigation`, with immutable request snapshot and context manifest, project/resource IDs, bounded question, permitted observations, budget and task revision. Delivers an evidence-backed report and optional proposal. |
| Implementation proposal | Immutable revision with parent investigation, evidence IDs, repo/base SHA, permitted change surface, expected behavior, risks, excluded actions, verification and review plan, budget, and destination if publication is contemplated. A digest binds the approval to this revision. |
| Implementation work order | Linked work order of kind `implementation`. Created with the proposal as its initial revision, parked at the approval gate. Contains no authorized edit step before a valid decision. |
| Step and attempt | A step identifies a logical unit and input revisions. An attempt adds worker identity, generation, lease expiry, budget and outcome. An interrupted attempt is retained in history. |

The operator submits an investigation explicitly, either through a task control or by confirming an interpreted request through an authenticated command. That command records the authorized read and sandbox experiment scopes. Promotion copies lineage IDs and the minimum authorized excerpt into an immutable context manifest. Proposals, changed bases and scope revisions get new identities or revision IDs. Findings link to original messages, attempts and observations all the way through final acceptance.

## States and legal transitions

Work has a state and a phase. Phases are `investigate`, `implement`, `verify`, `review`, and optional `publish`. The coordinator owns transitions and the rules below. All transition commands require current revision and expected state version. Read operations need no version precondition.

| From | To | Guard or event |
| --- | --- | --- |
| New investigation | `queued` | Valid authenticated submission, bounded scope and pinned input or explicit resolution step |
| New implementation | `awaiting_approval` | Immutable proposal with linked investigation evidence |
| `awaiting_approval` | `queued` | Approval for the specific pending action and revision, still valid at subsequent dispatch |
| `awaiting_approval` | `declined` | Implementation denied. Record rationale if provided and leave investigation evidence accessible. |
| `awaiting_approval` | `awaiting_acceptance` | Publication declined or skipped by operator. Preserve the local handoff and record no publication. |
| `queued` | `running` | Current action-specific grant, exact input and role bindings, eligible worker or broker, and fresh claim. Ordinary first edits and resumed edits use the same checks. |
| `running` | `queued` | Accepted checkpoint, next phase is already authorized and runnable |
| `running` | `awaiting_approval` | Review complete and publish gate required, or implementation correction requires a revised scope or base |
| `running` | `awaiting_acceptance` | Investigation report, deliberate local-only handoff, or confirmed draft PR with completion report |
| `running` or `queued` | `blocked` | Missing inputs/capability, transient infrastructure, insufficient evidence, expired lease, revoked permission or unknown effect |
| `blocked` | `queued` or `awaiting_approval` | Resume validates checkpoint, budgets, refs, policy and side effects. Prior approval insufficient routes back to gate. |
| `running` or `blocked` | `failed` | Budget exhausted or unrecoverable failure, with evidence and known effects recorded |
| Any nonterminal state | `cancelling` | Cancel revokes new dispatch immediately, requests termination and starts reconciliation |
| `cancelling` | `cancelled` | Runner stopped and in-flight effects reconciled or still marked unknown with explicit operator disposition and no automatic retry |
| `awaiting_acceptance` | `completed` | Authenticated operator accepts the exact report and handoff revision |
| `awaiting_acceptance` | `rejected` | Authenticated operator rejects the exact handoff and can submit linked follow-up work |

`declined`, `cancelled`, `failed`, `completed` and `rejected` are terminal. New work can link to those records. Accepting an investigation report certifies receipt and acceptance of the report, including limits. A report with insufficient evidence can either stay blocked awaiting data or be handed off with explicit inconclusive findings. It cannot imply reproduction or eligibility to implement a fix whose premise requires confirmed reproduction.

Investigation runs one or more bounded observations and hands off findings. Implementation edits an isolated branch, freezes an exact candidate, verifies it, and sends that same candidate to an independent reviewer. Material edits invalidate affected results and require fresh verification and review on the revised candidate. Corrections within the original approved scope use the same gate while current. Setting a new base or widening permitted effects requires a new proposal. The reviewer and test runner supply evidence. The coordinator checks required results and candidate identity before allowing a handoff or publish gate.

## Persistence, interruption and retries

- Use a local SQLite transaction for each state change, decision, lease claim and associated append-only event. One authoritative writer serializes compare-and-set on state version, claim generation and active attempt. A command ID replay with the same authenticated principal and body returns its prior result. A different body is rejected and auditable.
- Checkpoint at validated step boundaries with exact input refs, output artifact digests, completed commands, remaining budgets, approval references and next phase. Write an artifact to a private staging file, flush it and publish its immutable digest-addressed name durably before committing its reference. A crash can leave an orphan, so recover by enumerating and validating references. Missing or corrupt required evidence blocks advancement. Cleanup is separate from execution recovery.
- Leases have a generation and expiry under coordinator control. On restart, invalidate prior live attempts and confirm their processes are stopped or their execution environments fenced before redispatch. Lease expiry blocks fresh writes from that attempt. Reject late heartbeats, checkpoints and results even if their payload is otherwise valid. Detect clock anomalies or use a process-local monotonic clock for leases within one coordinator lifetime.
- Retry transient failures within an explicit elapsed time and attempt cap. Carry forward consumed budgets durably, including reserved time or cost when actual usage is unknown. If only disposable work ran, retry from a verified checkpoint using a fresh workspace or a verified immutable candidate. A changed input, approved base or remote branch head needs revalidation, with new approval when its binding changes. Behavioral failures require a bounded correction or a blocker report. Preserve complete evidence for partial results.
- Cancel actively signals the runner, prohibits new actions and tracks shutdown. Approval revocation also stops new actions and fences affected execution when possible. Shutdown can race an effect already accepted by an external provider. Keep the effect in the report, reconcile it, and leave cleanup to a separately authorized action.
- Persist an effect intent before any publish call. Keep a durable attempt and outcome for each branch push and draft PR creation. Include stable intent ID, exact target, expected remote ref and candidate. The broker owns dispatch and serializes effects per target. Conditional ref update or first-create semantics protect the push. The draft uses a mutable topic ref, so require broker-exclusive writes to that ref and recheck head, target base and destination before create. Verify the resulting PR refs and draft status. A mismatch after a call is a known partial external effect. Record it, block the intended handoff and let the operator resolve it. Reconcile a lost acknowledgement by inspecting the ref and PR by repo/head/base, exact candidate and intent marker. Confirm the exact effect or mark it unknown. An older call might still succeed after an empty read. Retry only with a provider-enforced idempotency guarantee or positive evidence the previous attempt cannot still execute and produced no effect. Otherwise pause for operator resolution. A local lease alone cannot fence the remote provider. Keep unresolved intents quarantined by conflicting repository/ref or PR target across work orders, terminal outcomes, restarts and restores. Clearing quarantine requires durable reconciliation or an explicit operator decision that records the residual risk.
- Restore from a coordinator-consistent DB backup plus referenced artifact manifest with digest checks. Treat any effect newer than the restored record or awaiting acknowledgement as unresolved until GitHub reconciliation. If restoration may have lost durable intent history, suspend publication for all resources that might have been touched in the missing interval, including resources with no pending intent in the restored data. Resume only after reconciliation or explicit operator risk disposition.

## Evidence and reporting

Record an event ID and monotonic sequence, timestamp, work/step/attempt/command IDs when applicable, authenticated source, state and policy revision, reason code, and evidence links. Minimum event families cover submission, assignment/lease, scope decisions and denied actions, tool start/result, checkpoint, cancellation, interruption/retry, effect intent/outcome/reconciliation, review, report and acceptance. Record tool identity, sanitized arguments or digest, input revisions, exit or typed result, elapsed time, and bounded output refs. Record tests by command/environment, count, failures/skips and applicability to the exact candidate. Missing metrics are unknown.

Classify claims as reported, observed, inferred or inconclusive, with verification status and verifier separately recorded. Include conflicting observations and describe their applicability. A coordinator-observed exit code proves how a command exited. A claim about the bug also needs an adequate assertion or captured observation.

Local diagnostics include latest checkpoint, worker health, remaining budget and reason for failure. The completion report gives outcome, scope, exact refs, findings and evidence, approval decisions, verification and independent review, effects and unresolved risks. Provide a redacted reader/export path keyed by work ID. Logs exclude secrets, unnecessary raw prompts and private corpus data by default. The local history is append-only via the coordinator. A hostile host administrator can alter the store and is outside the initial trust boundary.
