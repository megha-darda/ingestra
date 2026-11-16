# Verification

Verification covers local correctness, buildability, deployment-file consistency, and publication hygiene. It does not replace testing real AWS permissions, browser-to-S3 transfers, or Step Functions execution histories in the target account.

## Reproduce the checks

With Python 3.12 and development dependencies installed:

```powershell
python -m pytest -q
npm --prefix frontend run build
python -m samcli validate --lint --region eu-central-1
```

Build the boundary-enabled template following [the deployment guide](deployment.md). The IAM renderer writes account-specific files only under ignored `.local/`. Do not replace a real deployment build with a fictitious-account build.

The tests cover parsing and size limits; electronics and groceries schemas; duplicate SKUs; preview operations; exact-plan approval; per-record transactions; concurrent edits; retry idempotency; partial imports and recovery; version/revision protection; stale-worker fences; Lambda routing and preflight; local HTTP integration; and deployment-policy scope. SQLite and Moto exercise the shared service and AWS adapters. They do not run managed Step Functions executions or test organizational SCPs.

## Latest repository verification

Checked on **16 November 2026**:

| Check | Result |
| --- | --- |
| Full backend suite | **118 passed**, 0 failed (58.80 seconds) |
| Frontend production build | **Passed**, TypeScript and Vite 7.3.8 |
| SAM validation with lint | **Passed**, original and boundary-enabled templates |
| SAM build | **Passed**, isolated local output; exit 0 |
| Packaged source and boundaries | All four function packages match source; seven runtime boundaries preserved |
| README and documentation references | Local links, anchors, required files, and Markdown fences checked |
| Public application link | HTTP HEAD returned **200** |
| Secret scan | Gitleaks **v8.30.1**, redacted scan: **0 findings** in 71 publication candidates; additional credential patterns also found none |
| Local Git history and staging | **0 commit objects**, **0 reachable commits**, **0 staged files**; history scan exited 0; all **109 stored blobs** also scanned with **0 findings** |
| Ignore rules | Local environment, AWS configuration, rendered policies, tools, dependencies, builds, logs, and caches excluded; examples remain publishable |
| Functional preservation | **50 application, test, deployment-script, infrastructure, IAM-policy, and provider-evidence files unchanged** |

The scanner was installed only as ignored local tooling from its official release, with its published checksum verified. No application dependencies were added or changed. Account-specific deployment history was removed from the publication tree and retained privately under ignored `.local/`; it is not a runtime dependency. The remaining public AWS policy JSON uses account placeholders.

These results describe this local publication candidate. No commits, pushes, Git history rewrites, or AWS resource changes were made. Remote GitHub history was not fetched or audited. Secret scanners are not a guarantee against all possible sensitive information.

## Publication and security checks

Publication candidates are the tracked files plus non-ignored untracked files. Environment files, AWS configuration, rendered policies, local data, tool installations, dependency trees, build outputs, caches, and logs must remain excluded. Safe `.env.example` files contain examples rather than secrets.

A secret audit should scan the publication candidates and all reachable Git history, with values redacted. Scan staged content again before publishing any future changes. Pattern-based scanning can miss credentials; manually review generated files and new configuration too. Do not publish presigned URLs, tokens, private keys, or account-specific operational reports.

The current public deployment is reported operational by its maintainer. No live AWS mutations or workflow executions are part of repository cleanup. Use [the live verification steps](deployment.md#live-verification) and [the recovery walkthrough](walkthrough.md) to verify a target deployment separately. Application job status and counts, not only Step Functions execution status, determine the business result.
