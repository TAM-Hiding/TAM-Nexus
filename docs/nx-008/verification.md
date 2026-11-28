# NX-008 verification

PR #7 merged into `main` at `299cbe8f4f45fc893b014ae372c9a1e340b93650`. The strict baseline ran from a clean archive of that commit. All 87 tests passed, with zero skips.

The verification host ran Debian 13 on x86-64, Linux 6.18.57, Bubblewrap 0.12.0 and CPython 3.12.14. The pinned HERO-CAD runtime had fingerprint `d3386f3f11e2d75f3c3ebdfd5b4a9af65cfe51943232ae30d2cb45a466edcba0`. Physical workstation acceptance remains pending.

## Real execution and recovery checks

The operator walkthrough produced one durable reasoning invocation, one proposal and one follow-up attempt. The baseline mesh result was 10,009.36984944 mm³. A fresh CLI process replayed the same request and retrieved the same evidence digest. See the [redacted trace](../../examples/nx-008/experiment-trace.json).

Two additional checks used private copies of actual coordinator state.

- A saved NX-007 approval was migrated from SQLite schema 5 to 6. Executing its original digest returned `no_compatible_execution_profile`. The experiment attempt count remained zero.
- A separate process executed the actual approved cross-check in the Linux sandbox and exited abruptly after the supervisor returned exit 0, before accepting evidence. A fresh coordinator found one launched attempt. Recovery and replay reported that same attempt as uncertain with zero accepted artifacts. This exercises the distinction between observing an exit and durably accepting it.

The strict kernel mount test exercised both the original pilot and cross-check launcher profiles with read-only dependencies and source, private scratch, no network sockets and no inherited worker descriptors.

## Independent security review

The first read-only review inspected `ec3183ac17eb6872b158ea491efe1a5a242fe080`. All 89 tests at that revision passed. Review identified a second read of the worker script between checking its digest and sending bytes to the supervisor. The corrected launcher checks the digest of the immutable bytes it actually sends. Additional corrections enforce operator identity directly at the dispatch entry point, classify lost supervisor responses as uncertain, and identify dependency exit 78 in the measurement result. The operator guide documents partial-capture limits for failed attempts.
