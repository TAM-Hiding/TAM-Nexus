# Architecture decision records

All seven decisions are proposed in NX-001. They are scoped to the first local coordinator and its evolution path.

## ADR-001 · Keep application and coordination ownership independent

**Decision.** TAM hosts conversational interaction. Nexus owns coordination and execution evidence. Archive is authoritative for knowledge and access. GitHub owns shared engineering history. Workers and application runtimes use explicit boundaries.

**Alternatives.** A single assistant database or application plugins can simplify the first connection, but create duplicate source truth and runtime coupling. A stateless chat-only coordinator leaves approvals and recovery ambiguous.

**Rationale and tradeoffs.** References preserve independence and permit offline engineering with provisioned snapshots. Nexus must handle incomplete context and version drift explicitly. Revisit the interface when multiple authorized consumers encounter the same unresolved integration friction. Ownership changes require separate approval.

## ADR-002 · Deploy one local coordinator with isolated workers

**Decision.** Use a modular monolith on Linux with a local command interface. Workers that execute untrusted code or models run through a separate constrained runner boundary. Simulated workers can be in-process.

**Alternatives.** Multiple services offer independent scaling and trust domains at the cost of distributed operations. One process for trusted policy and real worker execution saves deployment work while exposing coordinator state and credentials to workers.

**Rationale and tradeoffs.** A single coordinator simplifies durable local coordination and offline testing. It is a single availability boundary. Revisit based on measured contention, distinct trust domains or demonstrable multi-host need.

## ADR-003 · Store Nexus state in SQLite with referenced artifacts

**Decision.** Commit current state, command deduplication and corresponding event in one SQLite transaction. Use a protected local directory for larger immutable artifacts, with digests and coordinated backup.

**Alternatives.** JSON files are readable but make atomic multi-record updates and concurrent claims difficult. Pure event sourcing requires projection and replay policy from the start. A hosted database adds provisioning and a possible cloud dependency.

**Rationale and tradeoffs.** Transactions cover local state and audit coherence. Artifacts have a separate durability boundary handled by staging and reconciliation. Revisit if representative workload or replication needs defeat single-host storage and recovery.

## ADR-004 · Use versioned domain contracts and replaceable adapters

**Decision.** Keep capability IDs, jobs, findings and receipts in Nexus contracts. Models, backends and transport are adapter details. Start with local calls.

**Alternatives.** A2A first accelerates native remote interoperability. MCP first standardizes tools. SIL first explores semantic reuse. Freeform messages reduce schema work but cannot validate transitions or authorization.

**Rationale and tradeoffs.** Domain records keep required semantics inspectable. External compatibility requires explicit translation and version testing. Revisit A2A or MCP with a second concrete integration, and SIL if preregistered results demonstrate adoption value.

## ADR-005 · Bind human grants to immutable scope and separate effects

**Decision.** Enforce authenticated, scoped investigation, approval-gated implementation, and separately approved publication. Agent and tool credentials cannot confer human policy authority. Keep merge and deployment outside initial agent controls.

**Alternatives.** Standing repository write access avoids prompts but grants authority beyond the proposal. Approval for every shell command makes bounded independent work impractical. One approval for edit and publication cannot bind the final reviewed candidate.

**Rationale and tradeoffs.** Gate consequential effects while allowing routine actions inside a grant. A second human decision precedes publication. Revisit operator interaction based on evidence of review friction while preserving scope binding and human authority.

## ADR-006 · Resolve Archive references through Archive policy

**Decision.** Bind Nexus activity to opaque Archive references and revisions where supported. Start with a fake. Use an authorized, versioned source contract before accessing real corpora.

**Alternatives.** Mount or index the corpus for fast local access, use the current Flask routes for convenience, or duplicate knowledge in Nexus.

**Rationale and tradeoffs.** These alternatives bypass Archive's future ACL/revision contract or duplicate curation and deletion. Reference resolution can be missing, revoked or stale and must be explicit. Revisit excerpt caching only after live contract readiness, measured need, and owner-approved revocation and retention behavior.

## ADR-007 · Checkpoint local work and reconcile external effects

**Decision.** Use explicit states, version checks, fenced attempts, checkpoints and bounded retries. Persist external intent before dispatch and reconcile uncertain effects. Block conflicting targets when an effect may still land.

**Alternatives.** Always retry simplifies recovery but can duplicate writes. Restart entire work from scratch wastes evidence. Distributed transactions would require cooperation from every provider and add infrastructure.

**Rationale and tradeoffs.** Bounded replay of disposable work plus conservative effect reconciliation limits surprises. Some ambiguous operations require human resolution. Revisit with measured recovery friction or verified provider idempotency and conditional write support.
