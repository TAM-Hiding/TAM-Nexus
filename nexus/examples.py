"""Generate repeatable example reports from four offline fixture workflows."""

import argparse
from pathlib import Path
from tempfile import TemporaryDirectory

from .coordinator import OPERATOR, Coordinator, render_report
from .storage import encode

SCENARIOS = ("successful", "interrupted", "denied", "ambiguous")


def _run(root, scenario):
    with Coordinator(root) as coordinator:
        index = 0

        def command(action, work=None, **args):
            nonlocal index
            index += 1
            version = coordinator.report(OPERATOR, work)["work"]["version"] if work else None
            return coordinator.execute(OPERATOR, f"example-{index}", action, work=work,
                                       version=version, **args)

        def require(action, work=None, **args):
            result = command(action, work, **args)
            if not result["ok"]:
                raise RuntimeError(result)
            return result

        def decide(work, allow):
            binding = coordinator.report(OPERATOR, work)["pending_approval"]
            return require("decide", work, binding=binding, allow=allow)

        message = require("message", text="Synthetic report: X/Y fails. X/Y/Z passes.")["message"]
        investigation = require("submit", message=message)["work"]
        require("run", investigation)
        work = require("propose", investigation)["work"]
        decide(work, allow=scenario != "denied")
        if scenario == "denied":
            return coordinator.report(OPERATOR, work)
        require("run", work)
        if scenario == "interrupted":
            require("claim", work)
            with Coordinator(root) as restarted:
                recovery = restarted.execute(OPERATOR, "example-restart", "recover")
                if not recovery["ok"]:
                    raise RuntimeError(recovery)
            return coordinator.report(OPERATOR, work)
        require("run", work)
        require("run", work)
        decide(work, allow=True)
        if scenario == "ambiguous":
            result = command("publish", work, fault="lost_before")
            if result.get("reason") != "effect_unknown":
                raise RuntimeError(result)
        else:
            require("publish", work)
            require("publish", work)
            handoff = coordinator.report(OPERATOR, work)["work"]["data"]["handoff"]
            require("accept", work, handoff=handoff)
        return coordinator.report(OPERATOR, work)


def generate(output):
    output.mkdir(parents=True, exist_ok=True)
    for scenario in SCENARIOS:
        with TemporaryDirectory(prefix=f"nx002-{scenario}-") as root:
            report = _run(root, scenario)
        (output / f"{scenario}.json").write_text(encode(report) + "\n", encoding="utf-8")
        (output / f"{scenario}.txt").write_text(render_report(report) + "\n", encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    generate(parser.parse_args().output)
