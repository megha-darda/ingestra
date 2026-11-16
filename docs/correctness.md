# Transaction and recovery design

Inventory keys: PK=`tenantId#inventoryType`, SK=`sku`. Job keys: PK=`tenantId`, SK=`jobId`. Journal keys: PK=`jobId`, SK=`sku`. Queries paginate internally with strongly consistent reads; the implementation returns the accumulated results rather than exposing pagination. Keep the dataset small.

Preview reads inventory without writing it. Each row saves a full before image and an expected `(version, revision)` pair, or expected absence. SHA-256 identifies the exact persisted plan. The execution request must echo that identifier. Workers verify both planId and approvedPlanId when reading the S3 plan.

Every CREATE / UPDATE uses a DynamoDB transaction containing:

1. A condition check for the active job attempt and IMPORTING status.
2. A conditional inventory Put: absent for CREATE, exact version **and revision** for UPDATE.
3. A journal Put conditioned on absence, with deterministic SHA-256(jobId + ':' + sku) operationId.

On a transaction response timeout, retry first reads the strongly consistent journal. An existing entry proves that inventory and journal committed atomically. Versions are not incremented again. Two concurrent retries can both read no journal; exactly one transaction can create that journal item. The other observes the committed journal and returns IMPORTED. Different jobs touching the same SKU compete through inventory conditions, independently of the Map concurrency setting.

Decimal prices are normalized before comparison and passed to DynamoDB's TypeSerializer as Decimal. No binary float is sent to DynamoDB. AWS's numeric versions and counts serialize back as JSON integers.

Rollback queries committed journal entries even when the import failed partway. Every reversal uses a transaction containing:

1. A condition check for active ROLLING_BACK job attempt.
2. Conditional inventory Delete for an imported CREATE, or Put of original business content for an UPDATE, matching the **imported** version and revision.
3. Conditional journal recovery-status update using the status observed before reversal.

Restoring an UPDATE creates a fresh revision and increments the current version. It does not restore the older version number. A UUID revision also distinguishes a deleted/recreated SKU with the same numeric version (ABA). Earlier imports therefore cannot mistake restored or recreated content for their untouched imported resource.

Missing products and changed revisions are conflicts. They are never overwritten. Reversals and journal recovery status commit together. Retrying after a lost response observes REVERSED and does no inventory write. Conflicts may be retried, but later legitimate edits cannot become eligible just by reducing the numeric version: the revision must also match.

Only recovery metadata (rollbackStatus, rollbackMessage, rolledBackAt and rollbackAttempt) changes on a journal entry. The original before/after images, operationId, versions and importStatus remain untouched. This is application-level immutable import history, **not** a compliance/WORM guarantee: privileged AWS administrators can alter underlying records.

Each record outcome is kept in private S3; finalization produces a report and aggregate counts. Map concurrency is five; state inputs contain job/attempt references and at most 500 small row identifiers. Large supplier data, plans and reports never pass through state payloads. After exhausted record retries, the failure handler checks the journal before reporting FAILED, so a lost successful transaction response is still counted as committed/reversed. A job-level error leaves journals available for rollback. The job condition checks fence delayed workers when workflow state or attempt changes.

Failure limits: a service outage can prevent even the failure handler from updating job status. Inspect Step Functions history and retry the failed execution using AWS's supported redrive after resolving the outage; record writes remain idempotent. If redrive is unavailable, an operator must reconcile the stopped execution before conditional job-state repair. Do not blindly reset IMPORTING jobs while an execution is active. This implementation has no automatic watchdog or production operations console.

Tests execute the same service against real SQLite transactions and moto's DynamoDB/S3 mocks. They verify transaction conditions and serialization, but do not verify deployed AWS IAM, signed browser uploads, service quotas, or Step Functions execution histories. Those require the deployed integration checks in deployment.md.
