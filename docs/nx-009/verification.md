# NX-009 verification

GitHub reported PR #8 merged into `main` at `c3019193f26351ed5e6cd016363e2fe460542a3b`. A fresh local fetch returned the same commit. Before NX-009 changes, a clean archive passed all 104 regression tests with Linux isolation required and zero skips.

The verification environment used Debian 13 x86-64, Linux 6.18.57, CPython 3.12.14 and Bubblewrap 0.12.0. No inference endpoint or model was provisioned for this work order. The fixed local inference socket was absent, and the standard command path contained no llama-server, llama-cli or Ollama executable. No inference service was installed, no weights were downloaded, and no remote provider was contacted. The optional adapter's transport tests exercise simulations. Actual model quality and physical workstation acceptance remain open.

## Demonstration and recovery

The baseline and disposable control were rerun in the Linux sandbox. A separately approved deterministic NX-008 measurement produced the same baseline volume as the specified dimensions, 10,009.36984944 mm³. The demonstration operator assessed the hypothesis as supported for this fixture using two source captures and one follow-up capture. The optional mock consumed the original pair of observations under its own disclosure grant. The [comparison](../../examples/nx-009/comparison.json) records the remaining uncertainty, provenance, and experiment count.

A separate process test copied the genuine pilot state, granted disclosure to the in-process mock, and exited abruptly after the mock returned but before the coordinator accepted its output. A fresh coordinator found one request-sent event. Recovery marked the invocation interrupted and refused replay as `reasoning_lease_fenced`. The prior deterministic proposal and experiment remained the only proposal and experiment in that state.

## Independent security review

A read-only reviewer inspected `9c63cfc0642bbffb1e8f8f3208999c738c3e9907` and ran its 108 then-present strict tests with zero skips. Review identified that a fixed socket pathname could serve a different account after operator approval. The correction binds the expected service UID into consent and verifies the connected peer before disclosure. The service account's confinement, model provisioning, and the service's handling of disclosed data remain workstation responsibilities.
