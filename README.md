# TAM Nexus

TAM Nexus is the proposed local-first coordination layer for TAM, a conversational engineering and research assistant. It coordinates scoped work, approvals, workers, and evidence on Linux. Archive owns knowledge records. GitHub owns repository history and pull requests.

NX-002 implements an offline coordination prototype using deterministic fixtures. See the [operator guide](docs/nx-002/README.md) and [verification record](docs/nx-002/verification.md).

NX-003 adds an optional Linux subprocess runner for those fixtures. See the [Linux worker guide](docs/nx-003/README.md) for the sandbox profile and strict security test command.

NX-004 adds a [read-only engineering investigation](docs/nx-004/README.md) for a pinned, synthetic Git checkout.

NX-005 adds an approved [HERO-CAD workstation pilot](docs/nx-005/README.md) with an offline dependency environment and Linux preflight.

Run the offline suite with `python -m unittest discover -s tests -v` from the repository root. Linux and Python 3.11+ are required. All runtime modules use the Python standard library.

| Document | Contents |
| --- | --- |
| [Architecture](docs/nx-001/architecture.md) | Responsibilities, deployment, interfaces, knowledge, communication, and operator controls |
| [Execution lifecycle](docs/nx-001/execution.md) | Work models, state transitions, evidence, retries, and recovery |
| [Trust and authorization](docs/nx-001/trust.md) | Scope, isolation, approvals, publication, and credential boundaries |
| [HERO-CAD reference scenario](docs/nx-001/hero-cad.md) | Trace from the original report through human acceptance |
| [Architecture decisions](docs/nx-001/decisions.md) | Seven proposed ADRs with alternatives and revisit conditions |
| [Repository evidence](docs/nx-001/evidence.md) | Pinned sources, existing behavior, proposals, and assumptions |
| [Risk register](docs/nx-001/risks.md) | Risks, controls, gates, and outstanding decisions |
| [Proposed NX-002](docs/nx-001/nx-002.md) | Deterministic simulator scope and acceptance tests |
| [Verification and review](docs/nx-001/verification.md) | Checks and independent review findings |
