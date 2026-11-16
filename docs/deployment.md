# AWS deployment and operations

The application backend is defined by `template.yaml` and `workflows/`. The public frontend is hosted separately with AWS Amplify Hosting. Local development does not require AWS resources.

## Prerequisites and credentials

Use Python 3.12, Node.js 20.19+ or 22.12+, AWS CLI v2, and the SAM CLI installed with the development requirements. Docker is optional for native Python builds and required for container-based builds or local Lambda emulation.

Authenticate a named AWS CLI profile through your approved method, then verify its identity:

```powershell
aws --version
python -m samcli --version
aws sts get-caller-identity --profile ingestra
```

Where supported, `aws login --profile ingestra --region eu-central-1` uses browser-based temporary credentials. IAM Identity Center users can use `aws configure sso --profile ingestra` and `aws sso login --profile ingestra`. See [AWS CLI authentication](https://docs.aws.amazon.com/cli/latest/userguide/cli-configure-sign-in.html) and [IAM Identity Center configuration](https://docs.aws.amazon.com/cli/latest/userguide/cli-configure-sso.html).

Do not copy AWS credential files, tokens, passwords, or access keys into this repository or the frontend. The public API URL is configuration, not a credential. `VITE_` values are embedded in browser-delivered JavaScript.

## Prepare scoped deployment access

Follow [the IAM setup guide](iam/README.md) before deploying as `ingestra-developer`. It preserves:

- A dedicated `ingestra-dev-cloudformation-execution` service role.
- Separate scoped compute, storage, and IAM execution policies.
- A runtime permissions boundary on all seven SAM-generated roles.
- A private, explicitly named SAM artifact bucket outside the application stack.
- PassRole limited to the intended service roles and destinations.

Use your own account ID to render policies and the boundary-enabled template. This preparation makes no AWS calls:

```powershell
$IngestraAccountId = (aws sts get-caller-identity --profile ingestra --query Account --output text)
python scripts/prepare_deployment_iam.py --account-id $IngestraAccountId
```

An authorized IAM operator creates the policies, execution role, boundary, and artifact bucket using those rendered files. Do not deploy the original unbounded template with the restricted execution policy. Account-specific outputs remain in ignored `.local/deployment-iam/`.

## Validate, build, and deploy the backend

From the repository root, after IAM setup:

```powershell
$IngestraArtifactBucket = "ingestra-dev-sam-artifacts-$IngestraAccountId-eu-central-1"
$IngestraExecutionRole = "arn:aws:iam::${IngestraAccountId}:role/ingestra-dev-cloudformation-execution"
$IngestraFrontendOrigin = 'http://localhost:5173'
python -m samcli validate --lint --template-file .local/deployment-iam/template.yaml --region eu-central-1
python -m samcli build --template-file .local/deployment-iam/template.yaml --config-file samconfig.toml
python -m samcli deploy `
  --template-file .aws-sam/build/template.yaml `
  --config-file ../../.local/deployment-iam/samconfig.toml `
  --stack-name ingestra-dev --region eu-central-1 --profile ingestra `
  --s3-bucket $IngestraArtifactBucket --s3-prefix ingestra-dev `
  --role-arn $IngestraExecutionRole --capabilities CAPABILITY_IAM --confirm-changeset `
  --parameter-overrides FrontendOrigin=$IngestraFrontendOrigin DemoTenant=demo-retail
```

Replace `FrontendOrigin` with your exact hosted HTTPS origin when publishing the frontend. If a custom artifact bucket was passed to the renderer, use that same bucket here. Inspect the change set before executing it.

SAM resolves `--config-file` relative to the selected template. The generated minimal configuration avoids the root `samconfig.toml` setting `resolve_s3 = true`, which conflicts with an explicit bucket and may otherwise create a SAM-managed bootstrap stack. Do not combine the restricted setup with generic `sam deploy --guided` or `--resolve-s3` defaults.

Retrieve outputs after deployment and keep any account-specific records in ignored `.local/`:

```powershell
aws cloudformation describe-stacks --stack-name ingestra-dev --profile ingestra --region eu-central-1 --query 'Stacks[0].Outputs'
```

Use the `ApiUrl` output as frontend `VITE_API_BASE_URL`. Changing that variable requires restarting Vite or rebuilding the static frontend. Backend deployment does not publish the frontend; follow [Amplify Hosting instructions](frontend-hosting.md).

## Resources and access

The backend contains one HTTP API, four Python Lambdas and their four runtime roles, three Standard workflows and their three roles, three on-demand DynamoDB tables, one private application S3 bucket and policy, and seven log groups with 14-day retention. SAM expands the current template into 29 CloudFormation resources. The deployment artifact bucket and Amplify hosting are separate.

- API Lambda reads tables and plans/reports, writes jobs and inventory edits transactionally, signs uploads, and starts the three workflows.
- Validators read jobs/inventory and uploaded objects, conditionally save plans, and update job state.
- Worker Lambda reads the approved plan and journal, writes per-record inventory/journal transactions, and saves outcomes and reports.
- State machines invoke their permitted functions. CloudWatch log-delivery management requires selected resource-wide permissions documented in the IAM guide.
- DynamoDB tables use AWS managed KMS encryption and PITR. S3 uses SSE-S3 encryption, Block Public Access, exact-origin PUT CORS, and a TLS-only policy.

See [architecture](architecture.md), [correctness guarantees](correctness.md), and [the IAM action inventory](iam/README.md). PITR is a table-level safeguard; application rollback uses the journal rather than restoring a table.

## Live verification

Local and mocked tests do not establish deployed IAM or complete browser integration. After an authorized deployment:

1. Verify GET `/health` reports AWS mode and GET `/inventory` works.
2. Seed a disposable working dataset by importing both baseline CSVs through the UI. There is no AWS seed endpoint.
3. Upload the supplier file using the browser's presigned PUT; explicitly start preview. Inspect the Preview execution and selected inventory branch.
4. Review and approve the exact plan. Inspect the Import Map, inventory updates, journal entries, counts, and partial results.
5. Edit E100 to quantity 98, request rollback, and verify two reversed operations and one preserved conflict using [the walkthrough](walkthrough.md).
6. Retry recovery and verify already-reversed counts. Inspect job status and reports as well as workflow history: a Step Functions `SUCCEEDED` label does not necessarily mean the business operation completed successfully.
7. Use `python scripts/smoke.py --api <ApiUrl>` for authorized read-only endpoint checks. Keep execution ARNs and operational records locally; do not publish presigned URLs or secrets.

## Security, retention, and cost

The API is public and unauthenticated with a shared configured tenant. CORS is not access control. Use only non-sensitive sample data. Add authentication, authorization, tenant isolation, API pagination, and operational recovery before considering production use.

Default API throttling is 10 requests/second with burst 20; record Map concurrency is five. These are not visitor quotas or spending caps. The template does not provision a WAF, watchdog, budget alert, or automatic kill switch. Configure billing alerts separately if desired; alerts can be delayed.

All `uploads/` objects expire after seven days. Plans, outcomes, reports, and table records have no automatic deletion configured. On-demand tables, PITR, storage, workflow transitions, hosting, and logs may incur charges; serverless does not mean zero idle cost.

## Resource cleanup

Export any required reports first. Stop active workflows and review the target account and region. An authorized operator can discard application data and remove the stack with:

```powershell
aws s3 rm s3://<UploadBucketName> --recursive --profile ingestra --region eu-central-1
python -m samcli delete --stack-name ingestra-dev --profile ingestra --region eu-central-1
```

These commands delete data. The restricted developer policy intentionally does not grant stack deletion or object cleanup; an authorized operator must perform them or grant narrowly scoped temporary cleanup permissions. Versioned buckets require cleanup of object versions too.

Verify stack resources are removed. The separate SAM artifact bucket, IAM setup, and Amplify app remain until removed independently. Keep the execution role and runtime boundary until CloudFormation cleanup is complete. Local SQLite data is separate and remains in ignored `.local/ingestra.db`.

Deleting a PITR-enabled table leaves an AWS-managed system backup for 35 days at no additional cost; this differs from chargeable retained or user-created backups. See [DynamoDB deletion and PITR](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/PointInTimeRecovery_Howitworks.html).
