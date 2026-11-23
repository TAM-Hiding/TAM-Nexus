# Verification and independent review

NX-001 is a documentation milestone. The checks below validate the proposed design and cited repository state. Runtime security and behavior require later implementation and testing.

## Verification results

| Check | Result |
| --- | --- |
| Documentation inventory | 10 Markdown files, seven ADRs, 12 HERO-CAD scenario steps and 13 NX-002 acceptance cases. Required responsibilities, interfaces, execution, trust, knowledge, communication, observability and operator controls are covered. |
| Document parsing and links | Marked parsed all 10 files. Seventeen relative links and anchors resolved. Twenty-five GitHub evidence links resolved to objects in the pinned local clones via `git cat-file -e`. Five standards references point to the reviewed primary sources. |
| Diagrams | Mermaid parsed both component/responsibility flowcharts using a DOM-backed local check. Browser visual rendering was unavailable in the verification environment. |
| Git scope and whitespace | `git diff --check main...HEAD` passed. The diff contains Markdown only. Archive, Standard Interlog and HERO-CAD working copies were clean and their HEADs matched the inspected main refs. No writes were made to those GitHub repositories. |
| Trust and failure walkthrough | Reviewed the full HERO-CAD trace against the state machine and action matrix. Mapped worker crash, inconclusive evidence, rejection/denial, interruption and uncertain effects to explicit outcomes and to NX-002 fixtures. |

The document checks used a temporary validator outside the repository. No application or simulated worker code was added or run. No live isolation, real GitHub effect handling or live Archive authorization has been validated by NX-001.

## Independent review

A separate read-only reviewer (architecture_review) reviewed the full local candidate `8fd5bbf57e854fd3e874704684c3afcf867acb6e` against the work order and prerequisites, then rechecked corrected design candidate `a1c5350d41599a03d43347d8a89e467f6e7e470c`. The published specification adds this verification record. The reviewer examined ownership and coupling, deployment complexity, Archive roadmap compatibility, incomplete/duplicate effects, approval enforcement, role separation and the scenario trace.

| Finding | Resolution |
| --- | --- |
| Medium · Lost-ack ambiguity could outlive a single attempt and conflict with another work order. | Record target quarantine across work orders, terminal states, restart and restore. Added N09 cross-work-order case. |
| Medium · Normal and resumed edit dispatch both need explicit current-grant checks. | Specify the same action-specific guard for first dispatch, subsequent actions and resumption. Added stale/revoked grant coverage in N04/N05. |
| Low · Final rejection needed an explicit outcome. | Added terminal rejection of the exact handoff, with provenance retained and linked follow-up available. |
| Medium · GitHub draft creation reads a mutable head ref after push. | Require an exclusively controlled topic ref, check head/base before and after creation, block intended handoff on mismatch, and exercise drift in N09. |
| Low · A stale backup can omit all evidence of later external intents. | Suspend publication for all potentially affected resources until the missing interval is resolved or the operator records a risk disposition. |

No blockers remained after the independent recheck. Live isolation and provider credential/ref behavior remain validation gates. Real Archive access stays gated on Archive's own contracts and operator-approved policy.
