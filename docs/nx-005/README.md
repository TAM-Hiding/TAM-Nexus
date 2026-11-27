# NX-005 workstation and HERO-CAD pilot

This pilot runs one existing HERO-CAD test through the NX-003 Linux sandbox and the NX-004 investigation lifecycle. The selected node is `tests/test_document.py::test_box_regenerates_exact_solid`. It exercises native Open CASCADE shape validity, volume and bounds after document regeneration. Nexus supplies its own pytest harness with plugin autoload, the project Qt conftest, the logging plugin and the pytest cache disabled. The harness measures a kernel geometry test. Full application and GUI verification remain separate.

## 1. Provision a trusted dependency environment

Run `python -m nexus preflight` first from the Nexus root using Python 3.11+. It checks launcher capabilities, enters the real sandbox and verifies writable scratch. This probe leaves no Nexus state. A full dependency probe checks the same kernel import path used by the pilot.

Use x86-64 Linux with a trusted CPython 3.12 for HERO-CAD, a compatible glibc, Bubblewrap with `--ro-bind-fd`, `--ro-bind-data`, `--json-status-fd` and `--disable-userns`, plus util-linux, libseccomp and the kernel capabilities in the [NX-003 guide](../nx-003/README.md). Ubuntu 22.04's distro Python 3.10 can serve the small Linux sandbox probe. Nexus requires a separate 3.11+ coordinator Python and HERO-CAD a 3.12 interpreter. Check the Bubblewrap feature flags rather than assuming the stock Jammy package is sufficient. No workstation package changes are made by Nexus. [Ubuntu package details](https://packages.ubuntu.com/en/jammy/bubblewrap)

Collect the [locked wheels](hero-cad-wheels.lock) through your approved download process before provisioning. The lock pins the x86-64 CPython 3.12 wheel hashes for the kernel-only subset of the inspected upstream lock. The following optional first command contacts the configured package index, so run it only during explicit provisioning on a machine allowed to download. Once the wheelhouse is available, the provisioner installs only approved wheels offline. It copies the selected interpreter, standard library, and explicit native libraries into a dedicated runtime directory. A new installation cannot overwrite an existing runtime.

```sh
# Set these to a trusted Python 3.12 and new/approved absolute directories.
python312=/absolute/path/to/python3.12
wheelhouse=/absolute/path/to/wheelhouse
runtime=/absolute/path/to/new-runtime
"$python312" -m pip --isolated download --only-binary=:all: --no-deps --require-hashes \
    --dest "$wheelhouse" -r docs/nx-005/hero-cad-wheels.lock
bash docs/nx-005/provision-runtime.sh "$python312" "$wheelhouse" "$runtime"
deps=$(python -m nexus runtime-digest "$runtime")
python -m nexus preflight --environment "$runtime" --digest "$deps"
```

Only use a dedicated runtime with trusted packaged files. Do not place secrets or unrelated files inside it. The runtime must fit within 2 GiB and local staging needs comparable free space. The tree fingerprint includes each regular file's contents and executable bit. Symlinks, special files and hard links are refused. A particular interpreter packaging can include symlinked stdlib pieces or additional native dependencies. Treat a failed preflight as a blocker and provision a compatible runtime. Host glibc/loader remain part of the NX-003 host trust boundary.

## 2. Create and authorize a manifest

Use a local, operator-verified HERO-CAD checkout with detached HEAD. The verified baseline is `b053d529c8cd32d07c5d37d060372b8bb0d85741`. Prepare the manifest from a trusted local Git export. The command verifies the commit stamp in Git's tar and hashes only the permitted source files, without extracting or executing the archive. The archive stamp is producer-supplied provenance. Establish the checkout's upstream origin and revision before producing it. Nexus checks the pinned HEAD, checkout and Git directory identities and exact file bytes against operator approval. Independent GitHub authentication and arbitrary Git history parsing are outside the pilot.

```sh
checkout=/absolute/path/to/detached/hero-cad
pin=b053d529c8cd32d07c5d37d060372b8bb0d85741
git -C "$checkout" archive --format=tar "$pin" src/hero_cad tests/test_document.py |
    python -m nexus pilot-manifest "$pin" "$deps" > hero-cad-pilot.json
```

Review the manifest before approval. v1 declares repository label, commit, source digest, explicit file paths, script and argument vector, dependency digest, resource and scratch limits, and bounded evidence streams. Only the single HERO-CAD profile is supported. Unknown/duplicate fields, unexpected commands, path escapes, unsupported limits and oversize inputs are refused. Any material change requires a new approval. The private checkout and runtime paths are passed separately and never exported in the report.

```sh
state=$(mktemp -d)
nexus() { python -m nexus --state "$state" "$@"; }
paths=$(python -c 'import json,sys; print(json.dumps(dict(checkout=sys.argv[1],environment=sys.argv[2])))' "$checkout" "$runtime")
nexus command message --id note --args '{"text":"Check kernel box regeneration."}'
nexus command authorize-pilot --id auth --manifest hero-cad-pilot.json --args "$paths"
nexus command submit-pilot --id work --manifest hero-cad-pilot.json --args '{"message":1,"authorization":1}'
nexus --runner linux command run --id run --work 1 --version 1
nexus report 1 --json
```

Approval records the claimed environment digest. The runner verifies it against the actual bytes before launching. The job checks the pinned source before staging, immediately before launch and after observation. All staged source and dependency bytes are mounted read-only by descriptor. Only private, 4 MiB scratch is writable. The fixed worker has 15 seconds wall time, 12 CPU seconds per process, 2 GiB virtual memory per process, the NX-003 UID process limit and 8 KiB combined output. The worker sees no credentials, unrelated files or network. Output and test behavior remain untrusted. The redacted report exposes observed exit, source and manifest digests, dependency identity, scoped source unchanged status, evidence digests and relevant limitations. `source_unchanged` refers to the approved checkout identity, HEAD and listed files. Raw streams remain in the existing operator-only, digest-checked capture.

Exit 1 is a supervised non-success observation. Inspect the bounded capture to distinguish an assertion failure from other causes of exit 1. Known dependency/setup outcomes, timeout and stale/policy failures have separate codes. No source investigation can be promoted to implementation, publication, or Archive work. Use the existing `accept` command to accept an exact handoff.

After a crash, use `recover`, inspect the report, and `resume` with a fresh run ID if the approved inputs still match. The coordinator stages outside the SQLite writer lock and rechecks permission before launch. A new pilot dispatch can reclaim abandoned private staging directories only after the prior supervisor releases the dispatch lock. A hard stop before subsequent dispatch can leave a staged copy consuming disk space. Do not remove it while a worker or supervisor may still be active.

## Limits and recommended NX-006

The pilot verifies one local project and fixed command. It leaves production integration, application-wide verification, aggregate cgroups and workstation acceptance to later work. Run the full suite with `NEXUS_REQUIRE_LINUX=1 python -m unittest discover -s tests -v`. See [verification evidence](verification.md).

For NX-006, first execute this exact preflight and repeat the pinned box test on the intended workstation. Establish an approved evidence bundle for one owner-selected HERO-CAD defect, then add a human-readable investigation handoff that separates measured baseline/control evidence from hypotheses. Defer model-directed execution and repository writes.
