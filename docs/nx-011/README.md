# Commissioning on the Dell G16-7620

Use a normal user shell. The reported Ubuntu 22.04 Bubblewrap 0.6.1 lacks `--disable-userns`. A passing namespace probe alone cannot qualify that launcher.

1. Open a fresh Bash shell in the Nexus checkout. Stop on any failed check. Use a separate worktree to preserve existing work.

```bash
set -eu -o pipefail
git fetch origin nx-011-workstation-compatibility
git worktree add --detach ../TAM-Nexus-nx011 FETCH_HEAD
cd ../TAM-Nexus-nx011
git rev-parse HEAD
python3.12 --version
```

2. Compare HEAD with the reviewed PR candidate. If build prerequisites are missing, the operator can provision them from the workstation's configured Ubuntu archive. Only this package-manager step uses administrative authority.

```bash
sudo apt-get install --no-install-recommends build-essential meson ninja-build pkg-config libcap-dev libselinux1-dev git gnupg
```

3. Build the pinned upstream v0.13.1 release. Verify the release signing fingerprint through a trusted publisher channel before importing the key. A failed signature check blocks installation. Keyserver availability is needed only for provisioning.

```bash
build=$(mktemp -d)
key=252C6FEA78A69D3BC0AD458A616C5BDC0C29AB04
mkdir -m 700 "$build/keys"
GNUPGHOME="$build/keys" gpg --keyserver hkps://keyserver.ubuntu.com --recv-keys "$key"
git clone --depth 1 --branch v0.13.1 https://github.com/containers/bubblewrap.git "$build/src"
test "$(git -C "$build/src" rev-parse v0.13.1)" = 46ec5c413a3c1da265624ddac3a715b4d1bc487d
test "$(git -C "$build/src" rev-parse HEAD)" = 362936aea0d5c6e70f7af75d2d44b5a914cb4ba2
GNUPGHOME="$build/keys" git -C "$build/src" verify-tag --raw v0.13.1 2>"$build/signature"
grep -F "[GNUPG:] VALIDSIG $key " "$build/signature"
meson setup "$build/out" "$build/src" -Dtests=false -Dman=disabled -Dbash_completion=disabled -Dzsh_completion=disabled
meson compile -C "$build/out" bwrap
```

4. Install the verified build alongside the system launcher. The prefix must be a canonical path with no symlinks or group/world-writable ancestors. Retain the approved digest separately from the executable. Any later upgrade requires a fresh review and pin.

```bash
prefix="$HOME/.local/opt/nexus-bwrap/0.13.1"
umask 077
mkdir -p "$(dirname "$prefix")"
mkdir "$prefix"
install -m 0555 "$build/out/bwrap" "$prefix/bwrap"
sha256sum "$build/out/bwrap" | cut -d' ' -f1 > "$prefix/approved.sha256"
export NEXUS_BWRAP="$prefix/bwrap"
export NEXUS_BWRAP_SHA256="$(cat "$prefix/approved.sha256")"
```

5. Keep both exports in the shell used for every Nexus command. In a fresh shell restore them from step 4 using the saved pin. Without either variable Nexus uses /usr/bin/bwrap. A partial configuration, changed bytes, unsafe path, incompatible launcher or kernel refusal blocks execution. The alternate ELF is sealed in memory before probing and execution. Its descriptor is closed before worker code starts.

```bash
python3.12 -m nexus preflight --staging-directory /tmp
NEXUS_REQUIRE_LINUX=1 python3.12 -m unittest discover -s tests -v
```

6. Follow the [runtime provisioning and manifest commands](../nx-005/README.md) using the locally installed Python 3.12. Use the [pinned baseline and disposable control walkthrough](../nx-008/README.md) and a fresh private state directory. Generate and inspect each manifest using the same local runtime digest. Run the native preflight with the actual manifest, runtime and intended staging filesystem. Authorize both exact manifests and run both investigations. Check the handoff has no evidence issues, a baseline pass, and a control exit of 1 with the expected assertion in its local capture. Follow the [mock disclosure steps](../nx-009/README.md#exercise-disclosure-with-a-mock-transport) to preview and authorize the exact payload and destination. A changed runtime requires two new manifests and fresh evidence. A successful mock run completes the disclosure check. Live inference requires separate commissioning and authorization.

## Development verification

Debian 13 x86-64, Linux 6.18.57, CPython 3.12.14, unprivileged execution and Bubblewrap 0.12.0. The unchanged base passed 121 strict tests. Reproducing the missing launcher capability on that base yielded 106 tests and four skip entries. Two class-level skips omit 8 and 7 test methods from discovery's executed count. Two individual skipped methods remain in that count. Thus 104 methods passed in the reproduced ordinary run. The physical workstation's individual skip reasons require its verbose log for direct confirmation.

The independent first review found no material issue in sealed executable identity, worker descriptor cleanup, or local manifest binding. Physical Ubuntu acceptance, the upstream build on that workstation, and live inference remain pending.

The final strict suite passed 124/124 on the default launcher (11.601 s) and on the separately pinned alternate executable (11.446 s), with zero skips. Cycle two reviewed the implementation, targeted tests and provisioning procedure at local commit f97d3b59454a71697f8c9c0b7d09d6dd582e562b and reported no material findings.
