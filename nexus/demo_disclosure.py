"""Disclosure scope for the optional NX-009 pilot, after local evidence verification."""

from pathlib import Path

from . import pilot, reasoning
from .storage import digest

OBJECTIVE = "Compare the pinned box baseline with its disposable negative control."

# Exact path set shared by the two pinned git archives. This is coordinator-owned
# policy, never a list supplied by the optional worker.
PATHS = Path(__file__).with_name("demo_source_paths.txt").read_text().splitlines()
DEMO = (
    ("b053d529c8cd32d07c5d37d060372b8bb0d85741",
     "9021d107d0c3fe570c6ec50d6873ed5355076d960cd9c8cabb1d270b950dc87b",
     0, "command_passed"),
    ("ff43cdd9ab36c24aa1ae4ff3cb42f7e35782bdea",
     "dd2ccb8df91403928bb5cae6a1036414c962f6780f441b6c4e14c556aae35e4e",
     1, "command_exit_one"),
)


def validate(request):
    """Accept only a coordinator-built, redacted handoff for the original comparison."""
    reasoning.validate_input(request)
    packet = request["handoff"]
    if request["objective"] != OBJECTIVE or len(packet["investigations"]) != len(DEMO):
        raise ValueError("outside_demo_disclosure_scope")
    observations = {o["reference"]["work"]: o for o in packet["observations"]}
    dependency = packet["investigations"][0]["dependency_sha256"]
    if not isinstance(dependency, str) or not pilot.SHA256.fullmatch(dependency):
        raise ValueError("outside_demo_disclosure_scope")
    for subject, (pin, snapshot, code, outcome) in zip(packet["investigations"], DEMO, strict=True):
        manifest = digest(pilot.pinned_manifest(pin, snapshot, PATHS, dependency))
        observed = observations[subject["work"]]
        if (subject["repository"] != "TAM-Hiding/hero-cad"
                or subject["source_revision"] != pin or subject["manifest_sha256"] != manifest
                or subject["snapshot_sha256"] != snapshot or subject["dependency_sha256"] != dependency
                or subject["execution_profile"] != "hero-cad-box-v1"
                or subject["approved_scope"] != ["read_pinned_source", "hero-cad-box-v1"]
                or observed["test"] != "tests/test_document.py::test_box_regenerates_exact_solid"
                or observed["sandbox"] != "nx-003-bubblewrap" or observed["source_unchanged"] is not True
                or type(observed["exit_status"]) is not int or observed["exit_status"] != code
                or observed["outcome"] != outcome):
            raise ValueError("outside_demo_disclosure_scope")
