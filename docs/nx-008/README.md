# Approved HERO-CAD experiment

Nexus can run one follow-up experiment, `hero-cad-volume-crosscheck-v1`, against the pinned baseline box. The reasoning worker receives redacted evidence and proposes the experiment. The coordinator chooses the source, runtime, command and resource limits. An operator reviews and approves the exact plan. Completion produces evidence for human inspection and ends the cycle.

## Reproduce the two source investigations

Use the [NX-005 procedure](../nx-005/README.md) to provision an offline runtime, inspect its digest and check Linux sandbox capabilities. Use a local detached baseline at `b053d529c8cd32d07c5d37d060372b8bb0d85741` and [recreate the disposable control](../nx-007/control.md) at `ff43cdd9ab36c24aa1ae4ff3cb42f7e35782bdea`. These steps require a compatible native CAD runtime and Linux sandbox. Run the manifest-bound NX-006 preflight before authorizing the pilot. Physical workstation verification is separate.

From the Nexus root, set `baseline`, `control`, and `runtime` to the canonical absolute directories you have reviewed. Generate the two baseline-test manifests in a fresh directory.

```sh
state=$(mktemp -d)
manifests=$(mktemp -d)
deps=$(python -m nexus runtime-digest "$runtime")
nexus() { python -m nexus --state "$state" "$@"; }
idx=0
for tree in "$baseline" "$control"; do
    idx=$((idx + 1))
    pin=$(git -C "$tree" rev-parse HEAD)
    git -C "$tree" archive --format=tar "$pin" src/hero_cad tests/test_document.py |
        python -m nexus pilot-manifest "$pin" "$deps" > "$manifests/$idx.json"
done
```

Review both manifests before authorizing either. The following commands assume fresh state. The first work ID targets the baseline. The second targets the disposable control.

```sh
idx=0
for tree in "$baseline" "$control"; do
    idx=$((idx + 1))
    paths=$(python -c 'import json,sys; print(json.dumps(dict(checkout=sys.argv[1], environment=sys.argv[2])))' "$tree" "$runtime")
    nexus command message --id "note-$idx" --args '{"text":"Observe pinned box regeneration."}'
    nexus command authorize-pilot --id "auth-$idx" --manifest "$manifests/$idx.json" --args "$paths"
    nexus command submit-pilot --id "work-$idx" --manifest "$manifests/$idx.json" \
        --args "{\"message\":$idx,\"authorization\":$idx}"
    nexus --runner linux command run --id "run-$idx" --work "$idx" --version 1
done
nexus handoff 1 2 --json
```

Expect two observations from the existing box-regeneration test and no evidence issues. The pinned baseline passes. The control deliberately halves HERO-CAD's reported volume and fails the original assertion.

## Review and approve the follow-up

```sh
nexus reason start 1 2 --id compare-box --objective 'Compare the pinned box baseline with its disposable negative control.'
nexus reason run 1
nexus reason show 1
```

Review the unverified hypothesis and its evidence references, the requested independent measurement and the coordinator's executable plan. Check the exact source, invocation, profile, command, runtime digest, limits and target before copying the displayed proposal digest into `spec`. Previously approved NX-007 proposals remain informational. A fresh proposal and approval are needed to bind executable authority.

```sh
spec=PASTE_REVIEWED_PROPOSAL_SHA256
# Expected refusals. Run these two checks individually.
nexus reason execute 1 "$spec"
nexus reason approve 1 "$(python -c 'print("0" * 64)')"

# Explicit operator approval, followed by a separate execution request.
nexus reason approve 1 "$spec"
nexus reason execute 1 "$spec"
nexus reason show 1
nexus handoff 1 2 --json
```

Review the exact execution attempt and accepted evidence digest before treating the experiment as complete. A successful repeat of the same completed request retrieves its result. If a coordinator was interrupted, use `nexus command recover --id UNIQUE_RECOVERY_ID` and inspect `nexus reason show 1` before making any decision. An uncertain or abandoned attempt requires operator disposition. Follow-up execution cannot initiate another reasoning invocation.

Run the complete offline regression suite from the Nexus checkout with `NEXUS_REQUIRE_LINUX=1 python -m unittest discover -s tests -v`. This requires actual Linux kernel isolation. For native workstation acceptance, also run the two source investigations, decision and measurement above.

## Recorded result

| Run | Observation |
| --- | --- |
| Pinned baseline test | The sandbox observed exit 0. |
| Disposable control test | The sandbox observed exit 1. Its captured assertion text reported 5,004.684924719999 mm³ against an expected 10,009.36984944 mm³. |
| Approved baseline cross-check | The sandbox observed exit 0. The worker reported 10,009.36984944 mm³ from oriented mesh triangles and 10,009.369849439998 mm³ from HERO-CAD's helper. The specified box dimensions give 10,009.36984944 mm³. |

The cross-check regenerated the baseline document and used its actual shape for both measurements. The independent calculation sums signed tetrahedral volumes from OCCT's oriented surface triangles. The comparison calls HERO-CAD's mass-properties helper separately. Both share HERO-CAD document regeneration, the shape, OCCT and the same dependency environment. The cross-check targets this planar rectangular box. The negative-control value comes from its earlier test capture. Only the baseline was approved for a mesh measurement.

The supervisor verified the pinned inputs, process outcome and captured bytes. Measurement values remain identified as worker reports inside the accepted evidence. The agreement supports the expected baseline volume within this fixture. It does not independently prove a cause for a different project failure.

A second process replayed the exact execution request and retrieved the original accepted attempt and artifact. The database contained one reasoning invocation, one proposal and one follow-up attempt. Both checkouts were clean afterward. The runtime fingerprint matched both original manifests. The [decision trace](../../examples/nx-008/experiment-trace.json) includes the unverified hypothesis, refusal records, approval and replay. The [engineering handoff](../../examples/nx-008/engineering-handoff.json) includes the redacted original and follow-up observations. Re-running the process produces new timing-dependent evidence digests.

If the operator restores an older database, run `nexus command recover --id RESTORE_ID --args '{"restore":true}'`. Restore recovery revokes existing experiment grants because the backup can precede a dispatch. A recorded completion remains historical evidence. An unaccepted timeout or oversized stream retains the typed run status only. Its partial output is unavailable in the engineering handoff.

For NX-009, add an operator decision on the original hypothesis that cites this follow-up evidence and records whether it supports, contradicts, or leaves the hypothesis unresolved. Keep the decision separate from execution authority.
