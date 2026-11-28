# Reproduce the disposable HERO-CAD control

The second commit in the pilot is a local fixture. Start with the operator's verified local HERO-CAD checkout and create a separate disposable clone. These commands have no remote write step. Use a fresh directory on each run.

```sh
set -eu
pin=b053d529c8cd32d07c5d37d060372b8bb0d85741
control=$(mktemp -d)
git clone --quiet --no-local --no-checkout "$checkout" "$control"
git -C "$control" checkout --quiet --detach "$pin"
python - "$control/src/hero_cad/geometry.py" <<'PY'
from pathlib import Path
import sys

path = Path(sys.argv[1])
original = path.read_bytes()
old = b"    return properties.Mass()"
assert original.count(old) == 1
path.write_bytes(original.replace(old, b"    return 0.5 * properties.Mass()"))
PY
git -C "$control" add -- src/hero_cad/geometry.py
GIT_AUTHOR_DATE='1795828479 +0000' GIT_COMMITTER_DATE='1795828479 +0000' \
    git -C "$control" -c core.hooksPath=/dev/null -c commit.gpgSign=false \
    -c user.name='Nexus fixture' -c user.email=fixture@example.invalid \
    commit --quiet -m 'NX-006 negative control: halve computed solid volume'
test "$(git -C "$control" rev-parse HEAD)" = ff43cdd9ab36c24aa1ae4ff3cb42f7e35782bdea
```

Follow the [NX-005 pilot procedure](../nx-005/README.md) for each detached checkout, using one state directory, separate manifest approvals, and distinct command IDs. Supply the baseline work ID first and the control work ID second in the [reasoning demonstration](README.md).
