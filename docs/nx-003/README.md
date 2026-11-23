# NX-003 Linux worker guide

NX-003 adds one isolated subprocess adapter. It runs the built-in deterministic HERO-CAD fixture and writes the same evidence and handoffs as NX-002. The adapter receives no conversation text, private evidence, live repository, credential, publication authority, or user-provided command.

## Run the profile

Use an unprivileged account on x86-64 Linux with Python 3.11+ for Nexus. The sandbox uses the distro CPython at `/usr/bin/python3`, its matching standard library, the Debian-style glibc x86-64 loader and four runtime libraries, Bubblewrap, util-linux `unshare` and `prlimit`, and `libseccomp.so.2`. For Debian or Ubuntu use the distribution packages `python3`, `bubblewrap`, `util-linux`, and `libseccomp2`. Use maintained versions with the flags in [sandbox.py](../../nexus/sandbox.py). The host must allow unprivileged and nested user namespaces, network, mount, PID, IPC and UTS namespaces, seccomp filters, bounded tmpfs mounts, memfds, pidfds and local `flock`. Python exposing pidfds and a kernel with pidfd signaling are required.

The environment must already permit those protections. Unsupported hosts, missing tools or setup failure refuse execution. For a typed failure, inspect the blocked work report and the `linux_observed` event. `sandbox_unavailable` includes a fixed reason for missing or unsupported primitives. `sandbox_setup_failed` means the required launcher, namespace, mount, seccomp or resource setup failed. Validate the host profile without weakening host policy. Run this suite to require actual sandbox coverage:

```sh
NEXUS_REQUIRE_LINUX=1 python -m unittest discover -s tests -v
```

When the profile cannot start, the ordinary suite explicitly skips the kernel test class while still testing coordinator and fail-closed behavior. The strict command above fails instead.

From the repository root:

```sh
nexus_state=$(mktemp -d)
python -m nexus --state "$nexus_state" command message --id report-1 --args '{"text":"Synthetic X then Y fails."}'
python -m nexus --state "$nexus_state" command submit --id submit-1 --args '{"message":1}'
python -m nexus --state "$nexus_state" --runner linux command run --id run-1 --work 1 --version 1
python -m nexus --state "$nexus_state" report 1 --json
```

Use the [NX-002 command guide](../nx-002/README.md#operator-commands) for proposal, approval, implementation, verification and review. Pass `--runner linux` for each `run` on Linux-selected work. That selection is durable and inherited by a linked implementation. It is part of the command replay identity. The default fake runner remains available for separate fixture workflows. The ordinary `publish` command still writes only to the NX-002 fake provider and requires separate human approval.

## Protection boundaries

| Boundary | Enforcement |
| --- | --- |
| Network and credentials | `unshare --user --map-current-user --net` creates a fresh network namespace without bringing up loopback. Seccomp independently denies the socket and socketpair syscalls, io_uring entry points, kernel keyring calls and selected process introspection syscalls. This prevents the worker from using socket families outside the normal network namespace and from bypassing the filter through io_uring. There are no inherited sockets or mounted service endpoints. |
| Filesystem and identity | Bubblewrap builds its own mount namespace, sets up new user, IPC, PID and UTS namespaces, disables further user namespaces and drops all capabilities. The root, fixture memfds, system Python, standard library and selected loader and libraries are read-only. Home, host `/proc`, `/sys`, `/dev`, source checkout and the coordinator state directory are absent. The only writable filesystem is a private 4 MiB tmpfs. |
| Descriptor and environment hygiene | The trusted launcher supplies a fixed environment, closes ambient descriptors and passes only setup memfds and the Bubblewrap status pipe. Bubblewrap closes those before worker execution. Worker stdin and stderr are backed by the null device. Its stdout goes to a bounded private pipe. Supervisor liveness and dispatch-lock descriptors never enter the sandbox. |
| Resume and tree termination | The worker runs as PID 1 of its sandbox namespace. A trusted supervisor consumes Bubblewrap status on a private channel, checks the direct child relationship and uses a pidfd to signal that PID 1 on timeout or cancellation. Linux tears down its descendants, including descendants in different sessions. Bubblewrap parent-death handling covers loss of the supervisor, with worker `prctl` blocked to protect that setting. |
| Resource limits | `prlimit` sets hard and soft ceilings of 256 MiB virtual address space per process, three CPU seconds per process, 64 open descriptors per process, 64 threads/processes charged to the real UID, 4 MiB for each regular file and `RLIMIT_CORE=0` for ordinary core files. Host-configured piped crash handlers can ignore that limit. The supervisor adds a two-second wall deadline and an 8 KiB stdout limit. The filesystem limit is independent of regular-file limits. |
| Result and authority | The runner observes launch, exit and byte limits outside the worker and parses a single bounded UTF-8 JSON object. Duplicate fields and non-integer numbers are rejected. The coordinator checks the exact expected result, attempt, generation, revision, scope and current grant. Sandbox output cannot approve work or supply trusted execution metadata. |

The combined util-linux and Bubblewrap setup is mandatory. Bubblewrap's own `--unshare-net` tries to configure loopback. The chosen launcher leaves loopback down and also works where the enclosing runtime blocks that configuration call. The isolated namespace is created before Bubblewrap, and failure of either program prevents the worker from running.

The seccomp filter is focused on egress and protecting setup or supervisor privileges. It is not a general syscall allowlist. Only packaged runtime files should exist in the selected system Python installation. The memory and CPU ceilings are per process. No cgroup delegation or aggregate memory or CPU limit is assumed. Real-UID process headroom depends on other jobs charged to that UID. Test under a dedicated unprivileged account if its normal load approaches the process cap.

## Recovery and reported states

The CLI first commits the attempt, budget reservation and command fingerprint to SQLite. The supervisor launches only after the coordinator rechecks the attempt and authorization. It holds a local dispatch lock until reaping the sandbox. Coordinator exit closes a liveness pipe. A recovering coordinator can fence an abandoned attempt immediately, and dispatch stays blocked while that supervisor still owns the lock. A bounded outer wait can report `worker_cleanup_pending`; the supervisor retains the lock during cleanup. `cancelled` records durable cancellation. It does not assert kernel teardown has already finished.

The `linux_launched` event records the supervisor launch, `linux_observed` records the bounded response, and a checkpoint records durable acceptance. A crash may leave a launched or observed result without a checkpoint. On restart run global `recover`, inspect the report and use `resume` before retrying disposable fixture work with a fresh command ID. An old result cannot restore the fenced attempt. A published artifact can be orphaned if the database transaction rolls back. It is never served as accepted evidence. Existing fake publication reconciliation and quarantine rules still apply.

State uses a local owner-only directory and SQLite schema version 2. Existing version 1 databases migrate transactionally. Keep the state directory on a local filesystem with reliable SQLite and `flock` locking. Trusted processes sharing its OS identity and local administrators are outside this boundary.

## Residual risks and proposed NX-004 scope

NX-003 covers the packaged x86-64 Linux profile, local trust assumptions and synthetic process executions. It does not establish containment against kernel or sandbox vulnerabilities, arbitrary privileged host configuration, compromised coordinator code or host administrators. It has no real provider credentials, local repository execution contract, multi-user access policy, or production resource guarantees. Host policy may block unprivileged namespaces, pidfds or seccomp. Tests report that as unavailable.

Proposed NX-004 is a read-only investigation pilot against one operator-provisioned, pinned local checkout of a synthetic engineering project. Specify a minimal fixture manifest, explicit dependency provisioning, digest-pinned input capture and redacted evidence export. Exercise the runner with one real test command and an ordinary interrupted investigation. Keep real GitHub writes, live Archive data, model workers and production credentials outside that milestone.

See [verification and review](verification.md). References include the [Bubblewrap manual](https://github.com/containers/bubblewrap/blob/main/bwrap.xml), [Linux PID namespaces](https://man7.org/linux/man-pages/man7/pid_namespaces.7.html), [Linux resource limits](https://man7.org/linux/man-pages/man2/getrlimit.2.html), and [Linux core-dump behavior](https://man7.org/linux/man-pages/man5/core.5.html).
