"""Local operator entry point. No worker-facing command transport."""

import argparse
import json
import sqlite3
import sys

from . import handoff, pilot, source
from .coordinator import OPERATOR, Coordinator, Rejected, render_report
from .linux_worker import LinuxRunner
from .storage import EvidenceError, InjectedCrash


def _read_arguments(value):
    def unique_keys(pairs):
        result = {}
        for key, item in pairs:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = item
        return result

    if len(value) > 16384:
        raise ValueError("arguments exceed 16384 characters")
    result = json.loads(value, object_pairs_hook=unique_keys)
    if not isinstance(result, dict) or set(result) & {"principal", "command", "action", "work", "version"}:
        raise ValueError("expected JSON object with action arguments")
    return result


def _reasoning_parser(sub):
    reason = sub.add_parser("reason", help="operate a bounded reasoning invocation")
    actions = reason.add_subparsers(dest="reason_action", required=True)
    start = actions.add_parser("start", help="authorize disclosure of selected handoff evidence")
    start.add_argument("work", type=int, nargs="+", help="authorized source investigation IDs")
    start.add_argument("--id", required=True, help="unique command ID, reused for identical retries")
    start.add_argument("--objective", required=True, help="objective disclosed to the stand-in")
    for action, help_text in (
        ("run", "run the fixed deterministic investigator"),
        ("show", "inspect the invocation and exact proposal"),
        ("cancel", "fence an unfinished invocation"),
        ("approve", "approve the exact proposed specification"),
        ("reject", "reject the exact proposed specification"),
        ("execute", "execute the approved fixed experiment once"),
    ):
        item = actions.add_parser(action, help=help_text)
        item.add_argument("invocation", type=int)
        if action in ("approve", "reject", "execute"):
            item.add_argument("digest", help="SHA-256 of the operator-reviewed proposal")


def _reasoning_command(coordinator, options):
    from . import reasoning_service

    action = options.reason_action
    if action == "start":
        invocation = reasoning_service.start(coordinator, OPERATOR, options.objective, options.work, options.id)
        return {"invocation": invocation}
    if action == "show":
        return reasoning_service.report(coordinator, OPERATOR, options.invocation)
    if action == "run":
        return reasoning_service.run(coordinator, OPERATOR, options.invocation)
    if action == "cancel":
        return reasoning_service.cancel(coordinator, OPERATOR, options.invocation)
    if action == "execute":
        return reasoning_service.execute(coordinator, OPERATOR, options.invocation, options.digest)
    return reasoning_service.decide(coordinator, OPERATOR, options.invocation, options.digest,
                                    approve=action == "approve")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", default=".nexus", help="owner-only state directory")
    parser.add_argument("--runner", choices=("fake", "linux"), default="fake",
                        help="fixture execution profile for run")
    sub = parser.add_subparsers(dest="verb", required=True)
    _reasoning_parser(sub)
    preflight = sub.add_parser("preflight", help="probe Linux and optional approved dependencies")
    preflight.add_argument("--environment")
    preflight.add_argument("--digest")
    preflight.add_argument("--manifest", help="use the approved manifest's dependency fingerprint")
    preflight.add_argument("--staging-directory", help="probe an existing staging filesystem directory")
    manifest = sub.add_parser("pilot-manifest", help="fingerprint a pinned git archive from stdin")
    manifest.add_argument("revision")
    manifest.add_argument("dependency_digest")
    runtime = sub.add_parser("runtime-digest", help="fingerprint a provisioned runtime")
    runtime.add_argument("environment")
    command = sub.add_parser("command", help="submit an operator command")
    command.add_argument("action")
    command.add_argument("--id", required=True, help="unique command ID for retries")
    command.add_argument("--work", type=int)
    command.add_argument("--version", type=int)
    command.add_argument("--manifest", help="read an approved pilot JSON manifest")
    command.add_argument("--args", default="{}", help="JSON action arguments, or - for stdin")
    export = sub.add_parser("handoff", help="export a versioned engineering handoff")
    export.add_argument("work", type=int, nargs="+")
    export.add_argument("--notes", help="add hypotheses and recommendations from bounded JSON")
    export.add_argument("--json", action="store_true")
    report = sub.add_parser("report", help="read a redacted report")
    report.add_argument("work", type=int)
    report.add_argument("--json", action="store_true")
    capture = sub.add_parser("source-evidence", help="read a private investigation capture")
    capture.add_argument("work", type=int)
    capture.add_argument("digest")
    message = sub.add_parser("message", help="display an original private message")
    message.add_argument("message", type=int)
    options = parser.parse_args(argv)

    try:
        if options.verb == "preflight":
            if options.manifest and options.digest:
                parser.error("preflight takes either --manifest or --digest")
            expected = (pilot.load_manifest(options.manifest)["dependency_digest"]
                        if options.manifest else options.digest)
            if bool(options.environment) != bool(expected):
                parser.error("preflight requires --environment and either --manifest or --digest")
            result = pilot.preflight(options.environment, expected, staging_directory=options.staging_directory)
            print(json.dumps(result, indent=2, sort_keys=True))
            return 0 if result["ok"] else 2
        if options.verb == "pilot-manifest":
            result = pilot.manifest_from_archive(options.revision, options.dependency_digest, sys.stdin.buffer)
            print(json.dumps(result, indent=2, sort_keys=True))
            return 0
        if options.verb == "runtime-digest":
            print(pilot.runtime_digest(options.environment))
            return 0
        runner = LinuxRunner() if options.runner == "linux" else None
        readonly = options.verb == "handoff" or (options.verb == "reason" and options.reason_action == "show")
        with Coordinator(options.state, runner=runner, readonly=readonly) as coordinator:
            if options.verb == "reason":
                result = _reasoning_command(coordinator, options)
                print(json.dumps(result, sort_keys=True))
                if options.reason_action == "execute":
                    return 0 if result["execution"]["status"] == "accepted" else 2
                return 2 if options.reason_action == "run" and result["status"] != "completed" else 0
            if options.verb == "command":
                raw = sys.stdin.read(16385) if options.args == "-" else options.args
                arguments = _read_arguments(raw)
                if options.manifest:
                    if options.action not in ("authorize-pilot", "submit-pilot") or "manifest" in arguments:
                        parser.error("--manifest is only for pilot commands and must be supplied once")
                    arguments["manifest"] = pilot.load_manifest(options.manifest)
                result = coordinator.execute(OPERATOR, options.id, options.action, work=options.work,
                                             version=options.version, **arguments)
                print(json.dumps(result, sort_keys=True))
                return 0 if result["ok"] else 2
            if options.verb == "handoff":
                notes = None
                if options.notes:
                    with open(options.notes, encoding="utf-8") as stream:
                        notes = _read_arguments(stream.read(16385))
                result = coordinator.engineering_handoff(OPERATOR, options.work, notes)
                print(json.dumps(result, indent=2, sort_keys=True) if options.json else handoff.render(result))
                return 2 if result["evidence_issues"] else 0
            if options.verb == "report":
                result = coordinator.report(OPERATOR, options.work)
                print(json.dumps(result, indent=2, sort_keys=True) if options.json else render_report(result))
            elif options.verb == "source-evidence":
                print(json.dumps(coordinator.source_evidence(OPERATOR, options.work, options.digest),
                                 sort_keys=True))
            else:
                print(json.dumps(coordinator.message(OPERATOR, options.message), sort_keys=True))
    except (source.SourceError, Rejected, ValueError, RecursionError, OSError, sqlite3.Error,
            EvidenceError, InjectedCrash) as exc:
        print(json.dumps({"ok": False, "reason": str(exc)}), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
