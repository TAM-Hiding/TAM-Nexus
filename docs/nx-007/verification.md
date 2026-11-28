# NX-007 verification

GitHub reported PR #6 merged into `main` at `23e8d983f355d52d403351832944a1a907fee486`. A fresh fetch returned the same commit. Before NX-007 changes, the strict suite ran 73 tests with zero skips or failures.

## Environment and evidence

Verification used Debian 13 x86-64, Linux 6.18.57 and CPython 3.12.14. The pinned baseline and disposable control were run again in the existing real Linux sandbox. The baseline source was `b053d529c8cd32d07c5d37d060372b8bb0d85741`, and the locally modified control was `ff43cdd9ab36c24aa1ae4ff3cb42f7e35782bdea`. Both manifests bound runtime digest `d3386f3f11e2d75f3c3ebdfd5b4a9af65cfe51943232ae30d2cb45a466edcba0`. NX-006 validated the resulting handoff with zero evidence issues before the reasoning demonstration. Physical workstation acceptance remains pending.

To run the complete offline regression suite with mandatory kernel isolation checks, use `NEXUS_REQUIRE_LINUX=1 python -m unittest discover -s tests -v`.

## Independent security review

A separate read-only reviewer inspected local revision `2fc429c59a42737d00661100bc6dd5e9b3dcb1c7` and ran its then-current 79-test strict suite. All passed. Review identified the following issues beyond those tests.

| Finding | Remediation |
| --- | --- |
| Coordinator death after process creation could roll back the running claim and allow another dispatch on the same budget. | Commit the claim before process creation, then revalidate the input and lease inside a short launch transaction under the existing dispatch lock. |
| Retrying a reasoning submission after losing its acknowledgment could consume another budget slot. | Require a stable operator command ID and reuse the existing transactional command ledger. |
| An approved proposal could look executable after its grant expired. | Distinguish the historical decision from grant validity and execution support in the operator report. |
