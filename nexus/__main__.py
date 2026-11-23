"""Local operator entry point. No worker-facing command transport."""

import argparse
import json
import sqlite3
import sys

from .coordinator import OPERATOR, Coordinator, Rejected, render_report
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


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", default=".nexus", help="owner-only state directory")
    sub = parser.add_subparsers(dest="verb", required=True)
    command = sub.add_parser("command", help="submit an operator command")
    command.add_argument("action")
    command.add_argument("--id", required=True, help="unique command ID for retries")
    command.add_argument("--work", type=int)
    command.add_argument("--version", type=int)
    command.add_argument("--args", default="{}", help="JSON action arguments, or - for stdin")
    report = sub.add_parser("report", help="read a redacted report")
    report.add_argument("work", type=int)
    report.add_argument("--json", action="store_true")
    message = sub.add_parser("message", help="display an original private message")
    message.add_argument("message", type=int)
    options = parser.parse_args(argv)

    try:
        with Coordinator(options.state) as coordinator:
            if options.verb == "command":
                raw = sys.stdin.read(16385) if options.args == "-" else options.args
                result = coordinator.execute(OPERATOR, options.id, options.action, work=options.work,
                                             version=options.version, **_read_arguments(raw))
                print(json.dumps(result, sort_keys=True))
                return 0 if result["ok"] else 2
            if options.verb == "report":
                result = coordinator.report(OPERATOR, options.work)
                print(json.dumps(result, indent=2, sort_keys=True) if options.json else render_report(result))
            else:
                print(json.dumps(coordinator.message(OPERATOR, options.message), sort_keys=True))
    except (Rejected, ValueError, RecursionError, OSError, sqlite3.Error,
            EvidenceError, InjectedCrash) as exc:
        print(json.dumps({"ok": False, "reason": str(exc)}), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
