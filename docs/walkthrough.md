# Recovery walkthrough

This scenario demonstrates a supplier import followed by an inventory adjustment and selective recovery. Use a fresh local dataset or a separately prepared AWS dataset to reproduce the counts. A previous run leaves E100 at quantity 98 and changes the next preview.

## Prepare baseline inventory

For local development, start the API with `python -m backend.local --seed` and run the frontend as described in [the README](../README.md#local-development). On AWS, upload and execute `samples/baseline-electronics.csv` and `samples/baseline-groceries.csv` through the application. There is no automatic AWS seed endpoint. Baseline imports have their own genuine journals.

## Preview the supplier file

Choose Electronics and upload [electronics.csv](../samples/electronics.csv). Explicitly start validation and preview. With the baseline present, the plan contains **1 CREATE, 2 UPDATE, 1 UNCHANGED, and 3 INVALID** rows, including duplicate SKUs. Preview saves expected versions and revisions without writing inventory.

When using AWS, the Preview Standard workflow selects the Electronics validator. Local mode runs background tasks and does not create Step Functions execution histories.

## Import the approved plan

Approve the exact plan. Three writes commit; invalid and unchanged rows do not modify inventory. The result is `PARTIAL`, with `IMPORTED=3`, `UNCHANGED=1`, and `INVALID=3`.

- E100 changes from quantity 10 to 100.
- E300 changes from 15 to 40.
- E400 is created.

Inspect the original before/after images in the recovery journal. Each imported write and its journal entry committed in one transaction.

## Make a subsequent adjustment

Open Inventory and change E100's quantity to **98**. The PATCH uses its current version and revision, then generates fresh concurrency tokens. This edit does not create a new import journal entry.

## Recover the import

Select the supplier import in Recovery center and request rollback. Expect **2 REVERSED / 1 CONFLICT**:

- E100 remains at 98; its later modification requires manual reconciliation.
- E300 returns to 15 with a fresh version and revision.
- E400 is deleted because it still matches the imported CREATE.

The resulting inventory is E100=98, E200=24, E300=15, with E400 absent. E100 and E300 have version 3; E200 remains at version 1. Original journal images remain unchanged while recovery metadata records the outcome.

Retry recovery. Expect **2 ALREADY_REVERSED / 1 CONFLICT** with no second reversal. The same scenario is asserted in [sample-data tests](../tests/test_ingestra.py) and [the local HTTP integration test](../tests/test_http.py).

[Groceries CSV](../samples/groceries.csv) and both JSON samples exercise the other schema and input format. Groceries expiry validation checks date format, not whether stock has expired. The UI displays prices in EUR.
