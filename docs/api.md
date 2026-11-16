# API contract

The configured configured tenant is assigned server-side. There is **no authentication**. All callers with access to the public API can read, edit, import and roll back the shared inventory. CORS is a browser constraint, not access control. Do not use confidential data.

Successful responses contain the resource itself or `{ "items": [...] }`. Errors are `{ "error": { "message": "..." } }`. Prices are exact decimal amounts (JSON integer or decimal string); versions and counts are JSON integers. Timestamps are UTC ISO 8601. Inventory types are `electronics` and `groceries`.

| Method | Route | Request / response |
|---|---|---|
| OPTIONS | Any API route | 204 empty response for browser preflight; API Gateway applies the configured origin, method, and header restrictions |
| GET | `/health` | Backend mode and configured configured tenant |
| POST | `/imports` | `{inventoryType, filename, size}` → 201 `{job, uploadUrl, uploadHeaders}` |
| POST | `/imports/{jobId}/preview` | `{}` → 202 job; explicitly starts validation after upload |
| GET | `/imports` | `{items: Job[]}`, newest first |
| GET | `/imports/{jobId}` | Job, counts, and executionResults when available |
| GET | `/imports/{jobId}/plan` | `{planId, items: PlanRow[]}` |
| POST | `/imports/{jobId}/execute` | `{planId}` → 202; exact approved SHA-256 required |
| POST | `/imports/{jobId}/rollback` | `{}` → 202; safe retry supported |
| GET | `/imports/{jobId}/rollback` | `{counts, items: RecoveryResult[], journal: JournalEntry[]}` |
| GET | `/imports/{jobId}/journal` | `{items: JournalEntry[]}`, original before/after images |
| GET | `/inventory?inventoryType=electronics` | `{items: Product[]}`; filter optional |
| PATCH | `/inventory/{inventoryType}/{sku}` | `{version, revision, quantity, ...optionalProductFields}` → updated product |

Upload with `PUT uploadUrl`, sending the binary file and returned uploadHeaders. The signed URL expires after five minutes and signs the declared ContentLength and encryption/content-type headers. Browsers supply Content-Length automatically. Upload validation rechecks the actual size and file content. Objects remain private. A presigned upload URL can be reused until expiry; the approved plan is a separate immutable object with its own verified digest, so changing an upload cannot change an approved import.

The PUT path is retained because Content-Length is included in the SigV4 signed headers with an exact server-validated value of at most 2 MiB. Changing that length invalidates the signature; this is not an unsigned browser-only limit. The backend also checks GetObject's actual ContentLength before reading the response body and checks parsed bytes/record count before producing a plan. Oversized upload requests and actual oversized objects return 413; files with more than 500 rows return 400. Content-Type and extension do not replace CSV/JSON parsing. No multipart POST upload or new quota subsystem is required for this path. Real browser-to-S3 signature enforcement still needs the post-deployment live check.

CSV requires unique headers and consistent column counts. JSON requires an array of objects. Both require UTF-8, 1–500 records and at most 2 MiB. Unknown columns are invalid. Required product fields: `sku`, `name`, `quantity`, `price`; optional `category`, plus `serialNumber`, `warrantyMonths` for electronics or ISO `expiryDate` for groceries. Quantity and warranty are integers. Prices allow up to two decimal places. Duplicate SKUs invalidate every occurrence. Data is a full product replacement: omitted optional supplier attributes are removed on UPDATE. The PATCH route merges supplied attributes into current product content.

Plan rows contain `index`, `row`, `operation` (CREATE / UPDATE / UNCHANGED / INVALID), normalized `data`, `errors`, `beforeImage`, and `expected` (version/revision or null for absence). Execution skips INVALID and UNCHANGED; approval imports only valid changes. The UI disables approval for a plan with no actionable changes. A plan containing invalid records finishes PARTIAL so they remain visible for review.

Job progression:

```
UPLOADING → PREVIEWING → PREVIEW_READY → IMPORTING → COMPLETED | PARTIAL | FAILED
                  ↘ PREVIEW_FAILED (preview retry allowed)
COMPLETED | PARTIAL | FAILED → ROLLING_BACK → ROLLED_BACK | ROLLBACK_PARTIAL | ROLLBACK_FAILED
ROLLED_BACK | ROLLBACK_PARTIAL | ROLLBACK_FAILED → ROLLING_BACK (safe retry)
```

Running action retries reuse the same attempt and named Standard execution. Execute is single-use per approved plan; after partial import failure, recover committed records and upload a new file for another import. There is no second import execution against the same old approval. Rollback reports are per attempt: previously reversed entries count as ALREADY_REVERSED on subsequent attempts. Original journal images and operation metadata are preserved; recovery status and timestamps are appended to the journal item.

400 malformed input, 404 missing resource, 409 optimistic concurrency or state conflict, 413 oversized local upload/API body, 500 unexpected infrastructure error. Errors do not contain stack traces. For conditional-write conflicts, refresh and review the current resource before retrying a PATCH or creating a new preview.

Local-only binary upload route: `PUT /local/uploads/{jobId}`. It is not deployed to API Gateway. Local HTTP binds to loopback. Local workflows execute with bounded Python threads and SQLite transactions; they are not AWS Step Functions executions.
