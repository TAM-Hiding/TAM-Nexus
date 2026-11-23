"""Deterministic worker entry point. Runs only inside the Linux sandbox."""

import json
from pathlib import Path


def solve(fixture):
    phase = fixture["phase"]
    if phase == "investigate":
        observations = [{"classification": "observed", "sequence": ["X", "Y"],
                         "result": "fixture_failure"}]
        if fixture["mode"] == "inconclusive":
            observations.append({"classification": "inconclusive", "sequence": ["X", "Y", "Z"],
                                 "result": "control_unavailable"})
            return {"observations": observations, "reproduced": False,
                    "limitation": "control result unavailable"}
        observations.append({"classification": "observed", "sequence": ["X", "Y", "Z"],
                             "result": "fixture_pass"})
        return {"observations": observations, "reproduced": True,
                "hypothesis": {"classification": "inferred", "claim": "fixture state is stale"},
                "limitation": "synthetic observations only"}
    if phase == "implement":
        return {"path": fixture["scope"], "base": fixture["base"], "iteration": fixture["iteration"],
                "change": "refresh simulated state after Y"}
    if phase == "verify":
        return {"candidate": fixture["candidate"], "command": "fixture-check-X-Y-and-control",
                "passed": 2, "failed": 0, "skipped": 0}
    if phase == "review":
        return {"candidate": fixture["candidate"], "verdict": "passed",
                "checks": ["fixture-scope", "verification-identity"]}
    raise ValueError("unknown fixture phase")


if __name__ == "__main__":
    fixture = json.loads(Path("/input/job.json").read_text())
    result = solve(fixture)
    Path("/scratch/result.json").write_text(json.dumps(result))
    print(Path("/scratch/result.json").read_text())
