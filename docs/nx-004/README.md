# NX-004 read-only engineering investigation

The pilot runs one fixed Python test command against an operator-provisioned synthetic Git checkout. It reuses the [NX-003 Linux runner](../nx-003/README.md) and stores workflow, authorization and evidence in the local Nexus state directory.

## Run the fixture

The [fixture generator](../../tests/fixtures/provision_engineering.py) requires local Git. It makes an ordinary repository in a new directory with two reproducible loose-object commits. The defect multiplies millimetres by 100 when converting to micrometres. The test expects 2 mm to produce 2,000 µm and reports 200 on the defect revision. The passing control corrects the factor to 1,000. The generator prints both commit IDs and leaves the defect checked out.

From the Nexus root:

```sh
state=$(mktemp -d)
workspace=$(mktemp -d)
checkout="$workspace/engineering"
pins=$(python tests/fixtures/provision_engineering.py "$checkout")
pin=$(printf '%s' "$pins" | python -c 'import json,sys; print(json.load(sys.stdin)["defect"])')
source_args=$(python -c 'import json,sys; print(json.dumps(dict(checkout=sys.argv[1],commit=sys.argv[2])))' "$checkout" "$pin")
python -m nexus --state "$state" command message --id report-1 --args '{"text":"Investigate the isolated unit conversion failure."}'
python -m nexus --state "$state" command authorize-source --id authorize-1 --args "$source_args"
submit_args=$(printf '%s' "$source_args" | python -c 'import json,sys; print(json.dumps(dict(json.load(sys.stdin),message=1,authorization=1)))')
python -m nexus --state "$state" command submit-source --id submit-1 --args "$submit_args"
python -m nexus --state "$state" --runner linux command run --id run-1 --work 1 --version 1
python -m nexus --state "$state" report 1 --json
```

The CLI success means the investigation reached its handoff. The redacted evidence records the supervised command exit, conventionally 1 for unittest failure or error and 0 for success. Each test is code supplied by the source. Its output can be misleading even when the runner measured its exit accurately. The checked-in fixture and the NX-004 regression suite independently check the expected assertion and passing control.

Use the report's work version and exact handoff digest to accept the result with the [existing accept command](../nx-002/README.md#operator-commands). To examine the control, check out the printed control commit in the **synthetic fixture**, then authorize and submit a new investigation with fresh command IDs and the control pin. A previously authorized defect investigation will refuse to run while the control is checked out.

The report includes the checkout identity hash, revision, snapshot digest, work and authorization IDs, worker and sandbox identifiers, profile, observed exit and durations, stream lengths and hashes, and evidence artifact digests. It omits host paths, conversation and raw test text. The local operator can inspect a capture separately with `python -m nexus --state "$state" source-evidence WORK DIGEST`. That command returns private base64 streams. Treat them as untrusted data and keep them out of shared reports.

## Fixture manifest and scope

| Item | Contract |
| --- | --- |
| `NEXUS_FIXTURE.json` | Exact single-line manifest with fixture ID `nx-004` and a lowercase dependency token. The checked-in manifest specifies `python-stdlib`. Other recognized dependency tokens are recorded as unavailable and block dispatch. |
| `model.py` | The pinned source module. |
| `test_model.py` | The only discovery target. |
| Git format | Ordinary SHA-1 loose objects, detached HEAD or a simple local branch, and exactly these three regular files in the root tree. Both commit and worktree must match on the three provisioned files. Git config, index and other paths do not participate. |

The operator authorizes an exact absolute checkout path, Git revision and snapshot. The coordinator binds the authorization to the policy revision and local checkout and Git directory device/inode identities. Each path component is opened through a directory descriptor without following symlinks. The reader verifies Git object type and SHA-1 digest, rejects nonregular files and hard links, compares the worktree to the pinned blobs, and checks HEAD again. It repeats the identity and digest checks at submission, dispatch, and resumption that would rerun a test. The SHA-256 snapshot binds all three file names and content digests. Only verified bytes cross a private descriptor into read-only sandbox mounts. The sandbox receives neither a checkout path nor Git metadata. The reader never executes Git against the supplied source. Git is used only by the synthetic fixture generator.

The command is fixed to `/usr/bin/python3 -I -S -B -m unittest discover -s /source -p test_model.py -v`. The working directory is private scratch. The NX-003 network namespace, seccomp policy and resource limits apply. No shell, dependency install or environment from the checkout is provided. An unavailable declared dependency blocks before launch. A traceback containing `ModuleNotFoundError` and exit 1 blocks with a separately labeled, unverified dependency report.

## Recovery and limitations

Run `recover` after interruption, inspect the report, then use `resume` and a fresh run command ID when authorized. Before another test is launched, the current checkout must still match the authorized identity, revision and snapshot. Existing accepted evidence remains evidence of its original snapshot even if the checkout subsequently changes. A failure between observation and checkpoint leaves a private capture and a partial redacted record. A crash before durable observation leaves the exit unknown. The NX-003 dispatch lock, attempt fencing and reserved budgets apply across restart. Oversized streams and timeouts retain only bounded observations and cannot create an accepted checkpoint.

This pilot supports a three-file, standard-library fixture. Packed repositories, alternates, submodules, linked worktrees, dependencies outside the standard library, and production checkouts are outside its supported profile. File bytes are capped at 12,000 each and output is capped at 8 KiB combined. The operator state remains a trusted local boundary. A mutable checkout can change after a check. The running worker still receives the immutable verified bytes for the pinned snapshot. The full [NX-003 residual risks](../nx-003/README.md#residual-risks-and-proposed-nx-004-scope) apply. There is no proposal, publication or Archive route for this work kind.

## Verification and next milestone

```sh
NEXUS_REQUIRE_LINUX=1 python -m unittest discover -s tests -v
```

See the [verification and review record](verification.md).

Recommended NX-005 scope is one owner-approved, read-only local engineering checkout with an explicit file and command manifest. Validate a practical checkout packaging method, reproducible offline dependencies and independently reviewable baseline-versus-control evidence on the intended Linux workstation. Keep publication, live models, and repository edits outside that milestone.
