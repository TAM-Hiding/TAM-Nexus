# HERO-CAD reference scenario

This is a design walkthrough using the supplied placeholder actions. It makes no claim about an actual defect or test run.

> TAM, HERO-CAD breaks when I perform X followed by Y, but not when I perform X, Y, and Z. Investigate the problem, see whether you can reproduce it, and propose a fix. If it's straightforward, ask me before implementing it.

| Step | Nexus action and durable evidence |
| --- | --- |
| 1. Preserve report | TAM shows the interpretation to the operator when they submit the investigation. Nexus records the verbatim authorized message, actor and message ID, attachment permissions and work revision. Ask for missing X/Y/Z details or fixture if necessary. |
| 2. Discover repository | Resolve HERO-CAD from the allowlisted project registry to its canonical repo ID and authorized ref. Record the resolved commit. If the installed revision differs, record both and use the relevant available one. |
| 3. Assign investigation | Claim an eligible worker with repo read plus sandbox-run authority. Record worker, attempt, capability, budget and grant. The worker receives a read-only snapshot with writable output scratch. |
| 4. Reproduce | Attempt both sequences under the same environment against synthetic or specifically authorized disposable model fixtures. Record the exact sequence, application/version, commit, environment and expected oracle. Choose kernel or GUI validation as appropriate. |
| 5. Collect tests | Capture tool exits, reproducible steps, observations, candidate fixture digest, logs/screenshots as authorized, and the baseline/control results. If GL is unavailable, record the GUI evidence gap. |
| 6. Evaluate findings | Tag the original report as reported. Label observations of X→Y and X→Y→Z separately. Link plausible causes as hypotheses with confidence/applicability and evidence. Surface contradictory results and missing evidence. |
| 7. Propose fix | Record the candidate cause, affected scope, intended behavior, regression risks, dependencies and focused/full verification plan in a linked immutable proposal. Deliver an investigation report. |
| 8. Ask before editing | Park implementation in `awaiting_approval`. Present exact proposal, base SHA, scope and tests. A human decision records grant or denial. No source changes happen while waiting. |
| 9. Implement | After approval, materialize an isolated writable checkout for that base on a dedicated topic branch. Apply the approved change, add only the relevant regression coverage, record the diff and candidate SHA. Scope drift pauses for a new proposal. |
| 10. Verify and review | Freeze the candidate. Test exact regression and controls, run applicable documented suites, record counts and limits, then pass exact diff/SHA and evidence to a separate reviewer. A material correction triggers applicable checks and fresh review. |
| 11. Draft PR and report | Show exact candidate, destination, proposed public text and expected push/PR CI behavior for separate approval. Broker journals intent, conditionally pushes to an exclusively controlled topic ref, checks remote head/base, opens a draft, and validates the returned PR state. Ref drift blocks publication or, if a draft already exists, records a partial effect and blocks acceptance as the intended handoff. If offline or declined, retain a local handoff. Observed CI results are tied to a SHA and pending CI stays pending in the report. |
| 12. Human acceptance | Nexus records the completion report and sits in `awaiting_acceptance`. The operator can inspect evidence, accept that exact handoff, or ask for more work. GitHub retains draft, checks and merge state. Nexus acceptance carries no merge authority. |

| Exception | Recovery |
| --- | --- |
| Worker crashes or timeout | Fence the old attempt, record interruption and partial evidence, and retry within budget from the last verified checkpoint. A replacement gets a fresh lease and explicit inputs. |
| Evidence insufficient | Preserve attempted checks and unknowns. Ask for specific sequence, environment or fixture if needed. Stay blocked or close with an inconclusive report. |
| Approval denied or expired | Denial ends that implementation order with evidence retained. Expiry blocks sensitive action and routes to a new decision when appropriate. A denied publication allows a local-only handoff. |
| Host interruption | Reopen local records, retire previous leases, validate artifacts, base and approvals, and reconcile any external intent before resuming. |
| Cancel or uncertain publication | Stop new dispatch. Confirm any effect that reached GitHub. Ambiguous calls remain visible and paused. Leave any cleanup to the operator. |

HERO-CAD's current README documents a kernel-focused suite and the Xvfb/GL recipe. A kernel suite alone cannot verify a UI regression. Use isolated test state and versioned fixtures. [Pinned evidence](evidence.md#hero-cad).
