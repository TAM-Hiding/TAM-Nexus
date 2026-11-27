#!/usr/bin/env bash
# Run explicitly with a trusted CPython 3.12 and an already populated wheelhouse.
set -euo pipefail
if [[ $# != 3 ]]; then
    echo 'usage: provision-runtime.sh /path/to/python3.12 WHEELHOUSE NEW_RUNTIME' >&2
    exit 2
fi
python=$1
wheelhouse=$2
runtime=$3
here=$(cd -- "$(dirname -- "$0")" && pwd)
prefix=$("$python" -I -S -c 'import sys; assert sys.version_info[:2] == (3,12); print(sys.base_prefix)')
stdlib=$("$python" -I -S -c 'import sysconfig; print(sysconfig.get_path("stdlib"))')
mkdir -m 700 -- "$runtime"
mkdir -- "$runtime/bin" "$runtime/lib" "$runtime/native"
cp -L -- "$python" "$runtime/bin/python3"
cp -a -- "$stdlib" "$runtime/lib/python3.12"
rm -rf -- "$runtime/lib/python3.12/site-packages" "$runtime/lib/python3.12/dist-packages"
find "$runtime/lib" -name __pycache__ -type d -prune -exec rm -rf -- {} +
for shared in "$prefix/lib/libpython3.12.so.1.0" "$prefix/lib/x86_64-linux-gnu/libpython3.12.so.1.0"; do
    if [[ -f "$shared" ]]; then
        cp -L -- "$shared" "$runtime/lib/libpython3.12.so.1.0"
        break
    fi
done
for library in libGL.so.1 libGLX.so.0 libGLdispatch.so.0 libX11.so.6 libXau.so.6 libXdmcp.so.6 \
    libxcb.so.1 libgcc_s.so.1 libstdc++.so.6 libbsd.so.0 libmd.so.0 \
    libpthread.so.0 libdl.so.2 libutil.so.1 librt.so.1; do
    cp -L -- "/usr/lib/x86_64-linux-gnu/$library" "$runtime/native/$library"
done
"$python" -I -m pip --isolated install --disable-pip-version-check --no-index --no-deps \
    --only-binary=:all: --require-hashes --no-compile --find-links "$wheelhouse" \
    --requirement "$here/hero-cad-wheels.lock" --target "$runtime/packages"
