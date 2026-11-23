# Risk register

| Risk | Consequence | Control and validation gate |
| --- | --- | --- |
| Untrusted content influences authority | Unauthorized changes or egress | Authenticated operator path, default deny, output validation, and approved arguments. NX-002 negative tests. Live runner escape tests are separate. |
| Test process escapes containment | Workstation, credentials or files exposed | OS/process isolation with read-only mounts, separate scratch, restricted egress, and no ambient credentials. Validate threat model and isolation on the intended Linux host before live code. |
| Approval is replayed or stale | Different revision edited or published | Bind decisions to identity, immutable proposal and candidate, refs and destination. Check at dispatch/effect and during resume. NX-002 stale/revoked cases. |
| External call times out after acceptance | Duplicate effect or overwrite | Journal branch/PR intents individually, conditional writes, reconcile and quarantine target when ambiguous. NX-002 fake lost-ack test. Verify GitHub provider controls before live write. |
| Cancel or crash leaves an active worker | Stale commits, artifacts or effects | Revoke, fence, verify process termination, reject stale generation. Test prior attempts and cancel races. |
| Archive lacks usable revision/access contract | Broken references or data leak | Fixture-only integration now. Gate on Archive roadmap and owner-approved corpus policy. |
| Evidence rendered or shared beyond scope | Sensitive workspace, model or knowledge leakage | Protect private artifacts, filter reports and recheck at export. Show exact intended publication data and destinations. |
| Publication triggers CI or other automation | Unapproved compute or privileged effects | Review repository triggers and credentials before publication. Require scope for expected CI. Block uncontrolled deploys or privileged downstream effects. |
| Candidate changes after tests or review | Misleading confidence or stale review | Pin exact refs, evidence and reviewer identity. Repeat impacted verification and review. |
| Missing runtime or misleading reproduction oracle | False confirmation | Record environment prerequisites, mark inconclusive and verify X/Y/Z controls. |
| Premature generalization | Maintenance cost and brittle APIs | One deterministic workflow, small contracts, no real model or scheduling fleet. Revisit seams when a second consumer needs them. |
| Host loss or partial artifact durability | Unverifiable state or accidentally repeated external work | Backup/recovery evidence checks. Validate durability assumptions on actual host before real use. |

## Operator decisions

The proposal assumes one local human principal and local synthetic fixtures. NX-002 can be reviewed on that basis. Live rollout requires these decisions.

- Which Linux host and isolation profile may execute untrusted repository code, and how should local runtime and dependency provisioning work?
- Which repositories and branch namespaces allow source reads, topic pushes and draft PRs? Which CI effects and spending limits may those operations trigger?
- What evidence retention, redaction and sharing rules apply to locally attached `.hcad` files, private Archive data and any later remote worker?
- Which Archive collections, identities and access policy are in scope after the read API and authorization prerequisites?

Language, SDK and model choices are routine implementation decisions within an authorized scope. No protocol or real provider selection is needed to approve the simulated work order.
