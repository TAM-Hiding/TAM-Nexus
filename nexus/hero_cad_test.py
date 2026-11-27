"""Trusted entry point copied into the sandbox for the fixed pilot command."""

import os
import sys

if sys.version_info[:2] != (3, 12):
    raise SystemExit(78)
sys.path.insert(0, "/runtime/packages")
try:
    import pytest
    from OCP.BRepCheck import BRepCheck_Analyzer
except (ImportError, OSError):
    raise SystemExit(78) from None

if sys.argv[1:] == ["preflight"]:
    raise SystemExit(0)

sys.path.insert(0, "/source/src")
os.environ["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
try:
    code = pytest.main([
        "-s", "-c", "/input/pytest.ini", "--noconftest", "-p", "no:cacheprovider", "-p", "no:logging",
        "--basetemp=/scratch/pytest", "-q", "/source/tests/test_document.py::test_box_regenerates_exact_solid",
    ])
except Exception:
    import traceback
    traceback.print_exc()
    code = 79
raise SystemExit(code)
