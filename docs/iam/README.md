# Ingestra deployment IAM setup

This guide prepares scoped deployment access for IAM user **ingestra-developer**, CloudFormation stack **ingestra-dev**, and **eu-central-1**. The renderer only writes local files; an authorized IAM operator configures AWS using those files.

Use an existing authorized account owner/IAM operator to perform the Console setup below. Do not attach AdministratorAccess, IAMFullAccess, AWSCloudFormationFullAccess or service-wide FullAccess policies to the developer. The developer does not create the deployment role, its policies, its runtime boundary, or the artifact bucket.

## Files and account placeholders

The JSON under policies/ contains literal **ACCOUNT_ID** placeholders. These are templates, not IAM policy variables. **Do not paste them into IAM until rendered/replaced.** Use your own 12-digit account ID from the AWS Console account menu or an authenticated STS identity check. Account-specific output belongs in ignored `.local/`.

Run locally from the repository root, replacing the example account ID with your own:

```powershell
# Preparation only: writes local files and makes no AWS calls.
python scripts/prepare_deployment_iam.py --account-id 123456789012
```

Requires Python 3.12 and PyYAML from requirements-dev.txt. Rendered files appear in **.local/deployment-iam/**, which Git ignores. The renderer fails if the account is not 12 digits or the current seven SAM compute resources have changed.

Default artifact bucket: **ingestra-dev-sam-artifacts-ACCOUNT_ID-eu-central-1**. If an appropriate private bucket already exists in this account/region, pass `--artifact-bucket EXACT_BUCKET_NAME`. All artifact permissions then target that exact bucket and its **ingestra-dev/** prefix. The bucket is separate from the template's supplier-upload bucket.

| Rendered JSON | Create as | Attach/use where |
|---|---|---|
| deployer-user.json | Customer-managed policy `IngestraDevDeploy` | IAM user `ingestra-developer` only |
| cloudformation-trust.json | Trust relationship, not a managed permissions policy | Role `ingestra-dev-cloudformation-execution` |
| cloudformation-compute.json | Customer-managed policy `IngestraDevCfnCompute` | CloudFormation execution role |
| cloudformation-storage.json | Customer-managed policy `IngestraDevCfnStorage` | CloudFormation execution role |
| cloudformation-iam.json | Customer-managed policy `IngestraDevCfnIam` | CloudFormation execution role |
| runtime-boundary.json | Customer-managed policy `IngestraDevRuntimeBoundary` | Permissions boundary on the seven SAM-generated runtime roles |
| artifact-bucket-policy.json | S3 bucket policy | The separate deployment artifact bucket |

The three execution policies are split to remain below IAM's 6,144-character managed-policy limit. They include scoped delete operations for automatic CloudFormation rollback/replacement; the developer policy does **not** grant DeleteStack or permission to empty buckets.

## What the repository actually expands into

The current SAM template expands into **29 CloudFormation resources**, including seven generated runtime roles. The renderer and tests inspect the existing logical resources and required policy scope.

| Resource | Logical IDs / generated resources | Policy scope before physical IDs exist |
|---|---|---|
| Four Lambda functions | ApiFunction, WorkerFunction, ElectronicsFunction, GroceriesFunction | `arn:aws:lambda:eu-central-1:ACCOUNT_ID:function:ingestra-dev-<logical-id>-*` |
| Four Lambda roles | ApiFunctionRole, WorkerFunctionRole, ElectronicsFunctionRole, GroceriesFunctionRole | `arn:aws:iam::ACCOUNT_ID:role/ingestra-dev-<role-logical-id>-*` |
| Lambda permission | ApiFunctionProxyPermission | AddPermission / RemovePermission / GetPolicy only on ApiFunction |
| Three state machines | PreviewWorkflow, ImportWorkflow, RollbackWorkflow | `arn:aws:states:eu-central-1:ACCOUNT_ID:stateMachine:<logical-id>-*` |
| Three workflow roles | PreviewWorkflowRole, ImportWorkflowRole, RollbackWorkflowRole | `arn:aws:iam::ACCOUNT_ID:role/ingestra-dev-<role-logical-id>-*` |
| HTTP API + default stage | HttpApi, HttpApiApiGatewayDefaultStage | Regional `/apis` collection and `/apis/*`; generated API ID is unknown |
| Three on-demand tables | InventoryTable, JobsTable, JournalTable | `arn:aws:dynamodb:eu-central-1:ACCOUNT_ID:table/ingestra-dev-<logical-id>-*` |
| Private upload bucket + policy | UploadBucket, BucketPolicy | `arn:aws:s3:::ingestra-dev-uploadbucket-*` |
| Four Lambda log groups | ApiLogGroup, WorkerLogGroup, ElectronicsLogGroup, GroceriesLogGroup | `/aws/lambda/ingestra-dev-<function-logical-id>-*` |
| Three workflow log groups | PreviewWorkflowLogs, ImportWorkflowLogs, RollbackWorkflowLogs | Exact `/aws/vendedlogs/states/ingestra-dev-preview`, `-import`, `-rollback` |

Step Functions default names are a significant detail: the current provider uses **logical ID plus suffix**, not necessarily stack name plus logical ID. AWS's [SAM example](https://docs.aws.amazon.com/serverless-application-model/latest/developerguide/using-sam-cli-remote-invoke.html) shows that distinction. A blanket `stateMachine:ingestra-dev-*` policy would not match this template's generated state machines. Other stacks using identical workflow logical IDs can match these initial patterns; see tightening below.

There are no Lambda aliases/versions, layers, event-source mappings, API Gateway integration roles, REST APIs, VPCs, ECR repositories, customer-managed KMS keys, CloudFront resources or hosted frontend resources in this template. Permissions for those are deliberately absent.

## Exact permission split

The JSON files are the authoritative action lists; these are the reasons for each group.

**Developer / CloudFormation control plane.** CreateChangeSet only for `stack/ingestra-dev/*`, requiring `cloudformation:RoleArn` to equal the dedicated role. A separate CreateChangeSet statement permits only the AWS Serverless transform ARN in eu-central-1. Describe/read/list-this-stack, ExecuteChangeSet and DeleteChangeSet are scoped to that stack ARN. These change-set IAM actions authorize against the **stack ARN**, not a guessed change-set ARN. ValidateTemplate and inline GetTemplateSummary require Resource `*`, limited to eu-central-1. The developer can PassRole only for the dedicated CloudFormation role, only to `cloudformation.amazonaws.com`. No direct Lambda, API Gateway, DynamoDB, Step Functions or IAM role-management privileges are granted.

**CloudFormation / SAM transform.** The execution role also needs `cloudformation:CreateChangeSet` on the exact AWS-owned ARN `arn:aws:cloudformation:eu-central-1:aws:transform/Serverless-2016-10-31`. This statement is in IngestraDevCfnCompute. Granting it only to the developer is insufficient when CloudFormation assumes the execution role for macro expansion. This does not grant the execution role general stack/change-set management. See [AWS transform access requirements](https://docs.aws.amazon.com/AWSCloudFormation/latest/UserGuide/control-access-with-iam.html).

**Developer / artifact S3.** GetBucketLocation on the exact artifact bucket, prefix-conditioned ListBucket, and GetObject/GetObjectVersion/PutObject/AbortMultipartUpload/ListMultipartUploadParts only under its `ingestra-dev/*` object prefix. GetObject covers SAM's HeadObject deduplication check; multipart rights cover Python dependency ZIP uploads. There is no bucket creation, bucket policy editing, artifact deletion or supplier-data access.

**CloudFormation / Lambda.** Create/Get/UpdateFunctionCode/UpdateFunctionConfiguration/DeleteFunction and tag management on four generated function-name patterns. The read-only code-signing/recursion/runtime/scaling configuration getters cover current provider read handlers even when those optional features are unconfigured. AddPermission/RemovePermission/GetPolicy are restricted to ApiFunction, the only function with an HttpApi event. The role may pass only the four corresponding runtime roles to Lambda. It is not granted InvokeFunction, version publication, layer management, VPC or ECR access.

**CloudFormation / HTTP API.** Regional API Gateway management verbs GET/POST/PUT/PATCH/DELETE cover HTTP API resources and tagging. POST/PUT on `/apis` cover Create/Import; `/apis/*` covers the generated API, OpenAPI reimport and `$default` stage lifecycle. The Stage provider additionally requires literal TagResource/UntagResource actions as described below. SDK operation names such as apigateway:CreateApi are not substituted for the HTTP management verbs. There is no `execute-api:Invoke`, REST API `/restapis/*`, custom-domain or API Gateway IAM execution-role PassRole grant.

The separate `ManageHttpApiTags` statement must include GET/**POST**/PUT/DELETE on `arn:aws:apigateway:eu-central-1::/tags/*`. API creation checks POST authorization against an encoded target such as `/tags/arn%3Aaws%3Aapigateway%3Aeu-central-1%3A%3A%2Fv2%2Fapis%2F*`. Permission to POST `/apis` alone is insufficient. This provider requirement is covered by a regression test. AWS's [tagging policy example](https://docs.aws.amazon.com/apigateway/latest/developerguide/apigateway-tagging-iam-policy.html) includes these tagging verbs.

**HTTP API Stage exception:** the actual Stage create/update provider also enforces **apigateway:TagResource** and **apigateway:UntagResource**. `ManageDefaultStageProviderTags` permits these only on `arn:aws:apigateway:eu-central-1::/apis/*/stages` and `.../stages/$default`, with a eu-central-1 condition. Creating the tagged stage checks the **collection ARN**, before the named stage exists. The collection form `/apis/API_ID/stages` is required before the stage has a name; broad HTTP verbs on `/apis/*` do not substitute for that literal action.

The published [eu-central-1 CloudFormation resource schema](https://schema.cloudformation.eu-central-1.amazonaws.com/CloudformationSchema.zip), aws-apigatewayv2-stage.json, lists both actions. Its relevant metadata is preserved in [evidence/apigatewayv2-stage-permissions.json](evidence/apigatewayv2-stage-permissions.json); regression tests cover every listed Stage handler action. The generic action catalog and IAM Access Analyzer may still label the literal action invalid, a mismatch also [reported and tested on AWS re:Post](https://www.repost.aws/questions/QUMw9zvPe8RQ6SiE7YDEKmRQ/http-api-createstage-denies-tagresource-but-iam-access-analyzer-says-the-action-is-invalid). The live service and provider schema are the evidence for including it. Do not replace it with AdministratorAccess or an action wildcard. If the Console blocks saving the policy because of this validation mismatch, use an authorized owner's IAM API/CLI to create a new default policy version from the exact JSON, or contact AWS Support; do not broaden all API Gateway access to work around the validator.

For that Console-validation case, the owner can open AWS Console **CloudShell**, choose **Actions → Upload file**, and upload the rendered `.local/deployment-iam/cloudformation-compute.json`. In the owner-authenticated CloudShell session (not the restricted ingestra profile), run the following after substituting the account ID if needed. This updates the existing managed policy; it does not deploy or delete a stack:

```bash
aws iam create-policy-version --policy-arn arn:aws:iam::ACCOUNT_ID:policy/IngestraDevCfnCompute --policy-document file://cloudformation-compute.json --set-as-default
```

If an initial deployment has already rolled back to **ROLLBACK_COMPLETE**, update the owner-managed IngestraDevCfnCompute policy from the newly rendered JSON. Then an authorized owner must delete the failed stack record in **CloudFormation → eu-central-1 → ingestra-dev → Delete**, wait for DELETE_COMPLETE, and rerun the same deployment command. Check rollback/resource states before deleting anything. AWS permits only deletion in [ROLLBACK_COMPLETE](https://docs.aws.amazon.com/AWSCloudFormation/latest/UserGuide/view-stack-events.html). The restricted developer policy intentionally lacks DeleteStack; do this cleanup through the owner's Console session. Keep the separately created artifact bucket, execution role and five IAM policies for the retry. No application rebuild is required for these external IAM policy fixes.

**CloudFormation / S3.** Create the generated supplier bucket only in eu-central-1 via `s3:LocationConstraint`, configure its existing encryption/CORS/lifecycle/public-access-block/tags/TLS bucket policy, read bucket configuration and delete the empty bucket for rollback. Configuration read includes other bucket-feature getters used by the provider's read handler; it does not permit enabling those extra features. Artifact GetObject/GetObjectVersion is limited to the separate bucket's stack prefix so Lambda code and Step Functions definitions can be read. CloudFormation cannot read/write supplier objects or empty the supplier bucket with these policies.

**CloudFormation / DynamoDB.** Create/Describe/Update/DeleteTable, PITR Describe/UpdateContinuousBackups, tags, and read-only TTL/contributor-insights/Kinesis-destination/resource-policy metadata on the three table-name patterns. There are no item reads/writes, scans, exports/imports, backups/restores, TTL mutations, global-table replicas, streams or DynamoDB resource-policy edits granted to this role.

**CloudFormation / Step Functions.** Create/Describe/Update/DeleteStateMachine and tag management on the three generated-name patterns, plus definition reads from the artifact prefix. PassRole is limited to the three workflow roles and `states.amazonaws.com`. It cannot StartExecution, redrive or manipulate runtime executions.

**CloudFormation / CloudWatch Logs.** Manage the seven log groups and retention/tags, plus read-only GetDataProtectionPolicy for provider reads. DescribeLogGroups, DescribeIndexPolicies and DescribeResourcePolicies require Resource `*`; they are restricted to eu-central-1. There are no log-event reads or alarm, metric, dashboard or monitoring-resource creation permissions. The Step Functions runtime roles already hold the log-delivery permissions; those mutation permissions are not duplicated onto the CloudFormation role.

**CloudFormation / IAM.** Create/Get/Update/DeleteRole, inline-policy lifecycle and tags only for the seven generated-role patterns. CreateRole and PutRolePermissionsBoundary require the exact runtime boundary ARN. Boundary removal and policy version management are absent. AttachRolePolicy/DetachRolePolicy can reference only AWSLambdaBasicExecutionRole and only the four Lambda roles. SAM attaches that AWS policy automatically; the three workflow roles use inline policies. The role cannot create arbitrary managed policies, users, access keys or service-linked roles, or change itself, the developer, or the boundary policy.

**Runtime boundary.** This maximum permission envelope preserves the union of the existing runtime operations on application tables, four S3 prefixes, worker/validator invokes, three workflow starts and Lambda logs. Existing SAM identity policies still narrow each individual role; the boundary grants nothing by itself. Transaction-only DynamoDB write conditions are preserved. The existing wildcard Logs delivery APIs remain regional.

**DynamoDB encryption detail.** `SSEEnabled: true` without a key ID selects the AWS-managed `alias/aws/dynamodb` KMS key, not the AWS-owned default. Its AWS-managed key policy authorizes service-mediated use; the CloudFormation role needs no unrestricted KMS administration. The runtime boundary explicitly permits the required key use only through `dynamodb.eu-central-1.amazonaws.com`, in this account, on a key carrying that exact AWS-managed alias. The key UUID is unknown, hence `key/*` with these conditions. No CreateKey, PutKeyPolicy, alias mutation, key deletion or direct general-purpose decrypt grant is included. AWS documents this [SSE behavior](https://docs.aws.amazon.com/AWSCloudFormation/latest/TemplateReference/aws-properties-dynamodb-table-ssespecification.html) and [AWS-managed key policy](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/encryption.usagenotes.html).

## Why the separate SAM template copy is required

IAM cannot constrain the JSON contents of `PutRolePolicy`. Allowing CloudFormation to write arbitrary inline policies without a runtime boundary would leave an escalation path, even with scoped role names. Therefore the generated execution policy **requires a boundary on creation** and does not permit its removal.

The renderer writes **.local/deployment-iam/template.yaml**, adding `PermissionsBoundary` to the current four AWS::Serverless::Function and three AWS::Serverless::StateMachine resources. It also makes CodeUri and DefinitionUri paths absolute so that the copied template builds from its new directory. **The repository's template.yaml, workflow definitions, physical-name settings, application policies and samconfig.toml are not changed.** There are no renamed resources. Do not deploy the original unbounded template with this execution policy: its generated-role creation will fail, intentionally.

SAM supports PermissionsBoundary on both [functions](https://docs.aws.amazon.com/serverless-application-model/latest/developerguide/sam-resource-function.html) and [state machines](https://docs.aws.amazon.com/serverless-application-model/latest/developerguide/sam-resource-statemachine.html). It propagates the property to their generated roles. The runtime boundary is an owner-created prerequisite outside the application stack; it must not be attached as an ordinary permissions policy to the developer or CloudFormation role.

## AWS Console setup — authorized owner/IAM operator

These are instructions for you; they have **not** been executed.

1. Sign in with an existing identity authorized to manage these IAM prerequisites. Open the account menu and verify the intended 12-digit account ID. Render the local files for that account. Select **Europe (Frankfurt), eu-central-1** for regional consoles; IAM itself is global.
2. Open **IAM → Policies → Create policy → JSON**. Paste the rendered `runtime-boundary.json`. Choose Next, name it **IngestraDevRuntimeBoundary**, review findings, and choose Create policy. Leave path `/`. Do not attach it to any existing user/role as a normal permissions policy.
3. Repeat **Policies → Create policy → JSON** for `cloudformation-compute.json` as **IngestraDevCfnCompute**, `cloudformation-storage.json` as **IngestraDevCfnStorage**, and `cloudformation-iam.json` as **IngestraDevCfnIam**. Review IAM's validation/security findings. All files must contain your real account ID, not ACCOUNT_ID.
4. Open **IAM → Roles → Create role → Custom trust policy**. Paste rendered `cloudformation-trust.json`; only `cloudformation.amazonaws.com` is trusted. Choose Next. Select exactly the three **IngestraDevCfnCompute / Storage / Iam** policies. Do not set the runtime boundary on this deployment role. Name it **ingestra-dev-cloudformation-execution**, leave path `/`, review and create. Its ARN must be `arn:aws:iam::YOUR_ACCOUNT_ID:role/ingestra-dev-cloudformation-execution`.
5. Open **IAM → Policies → Create policy → JSON**, paste rendered `deployer-user.json`, and create **IngestraDevDeploy**.
6. Open **IAM → Users → ingestra-developer → Permissions → Add permissions → Attach policies directly**. Select **IngestraDevDeploy**, then add permissions. Review the user's existing direct, inline and group policies. Additional grants remain effective; this setup is least privilege only if unrelated broad grants are removed by the owner as appropriate. An existing permissions boundary, SCP or explicit deny can still block these grants. Do not grant the developer rights to edit these setup policies or manage the deployment role.
7. Open **S3 → Create bucket** in Frankfurt. Choose a general-purpose bucket named exactly **ingestra-dev-sam-artifacts-YOUR_ACCOUNT_ID-eu-central-1** (or the exact name passed to the renderer). Keep **Bucket owner enforced**, all four **Block Public Access** settings enabled, and default encryption **SSE-S3**. Bucket versioning is optional and not needed by this template; if enabled, old versions need separate cleanup. Create the bucket as the owner.
8. Open that bucket → **Permissions → Bucket policy → Edit**. Paste rendered `artifact-bucket-policy.json`, verify its two bucket ARNs, and save. This adds a TLS-only deny, not a public Allow. No additional cross-account/Lambda service bucket grant is required for this same-account ZIP deployment; the execution role reads the artifact prefix. Keep the bucket private.
9. Configure the developer's existing AWS CLI **ingestra** profile using your approved authentication method. No access key or console password belongs in the repository. Authenticate as **ingestra-developer** and verify the account/ARN with `aws sts get-caller-identity --profile ingestra`. Authentication setup is separate from provisioning rights; this policy does not let the user create access keys or IAM identities.

If you choose browser-based `aws login --profile ingestra`, the IAM operator can additionally attach the AWS-managed **SignInLocalDevelopmentAccess** policy to this user through the same Add permissions screen. It grants only `signin:AuthorizeOAuth2Access` and `signin:CreateOAuth2Token` on AWS Sign-in public-client resources, across regions/accounts; it grants no deployment permissions. This is optional when the existing profile already authenticates by another approved method. See the [exact AWS-managed policy](https://docs.aws.amazon.com/aws-managed-policy/latest/reference/SignInLocalDevelopmentAccess.html). It is not AdministratorAccess.

These instructions follow AWS's [custom policy creation](https://docs.aws.amazon.com/IAM/latest/UserGuide/access_policies_create-console.html), [custom trust-role creation](https://docs.aws.amazon.com/IAM/latest/UserGuide/id_roles_create_for-custom.html), and [user policy attachment](https://docs.aws.amazon.com/IAM/latest/UserGuide/id_users_change-permissions.html) workflows. The IAM operator needs its own existing authorization for these setup actions; no new administrator privileges are granted here.

The developer policy is CLI-focused. It deliberately omits account-wide IAM/S3/CloudFormation listing rights and direct service-console access; some Console panels will show AccessDenied for this user. Do not add FullAccess just to populate those panels. The Console setup above is performed by the authorized IAM operator. If this stack already exists, have that operator inspect its actual physical resources and attach the approved boundary to any existing matching runtime roles before delegating role-policy management; the CreateRole boundary condition cannot retroactively constrain an existing unbounded role. Review existing physical resources before applying these policies to an existing deployment.

## Future deployment commands — do not run without approval

The generated minimal samconfig.toml is deliberate: the repository's existing config sets `resolve_s3 = true`. Combining that with an explicit bucket can fail and can trigger an unnecessary SAM-managed bootstrap stack. This setup uses an **owner-created explicit bucket**, not `aws-sam-cli-managed-default`. Do not use `--resolve-s3`, do not omit the explicit bucket, and do not use the original guided deployment defaults for this policy set.

After rendering the real account files, owner setup, and deployment approval:

```powershell
$IngestraAccountId = '123456789012' # replace with your actual account ID
$IngestraArtifactBucket = "ingestra-dev-sam-artifacts-$IngestraAccountId-eu-central-1"
$IngestraExecutionRole = "arn:aws:iam::${IngestraAccountId}:role/ingestra-dev-cloudformation-execution"

aws sts get-caller-identity --profile ingestra
sam validate --lint --template-file .local/deployment-iam/template.yaml --region eu-central-1
sam build --template-file .local/deployment-iam/template.yaml --config-file samconfig.toml
sam deploy --template-file .aws-sam/build/template.yaml --config-file ../../.local/deployment-iam/samconfig.toml --stack-name ingestra-dev --region eu-central-1 --profile ingestra --s3-bucket $IngestraArtifactBucket --s3-prefix ingestra-dev --role-arn $IngestraExecutionRole --capabilities CAPABILITY_IAM --confirm-changeset --parameter-overrides FrontendOrigin=https://YOUR-FRONTEND-ORIGIN DemoTenant=demo-retail
```

SAM resolves `--config-file` relative to the selected template file. For `sam build`, use `samconfig.toml` beside the boundary-enabled template; for deployment from .aws-sam/build/template.yaml, `../../.local/deployment-iam/samconfig.toml` points to that same generated file. These commands specify an existing bucket and a service role. CAPABILITY_IAM is sufficient because the generated roles have no explicit RoleName; they still have generated names. No CAPABILITY_NAMED_IAM, AUTO_EXPAND, ECR or API Gateway execution role is needed. `--confirm-changeset` requests final review before execution. A change set itself is still an AWS action; none was created during this task.

The default developer policy intentionally lacks `DeleteStack` and S3 object-deletion permissions. An authorized owner handles cleanup or grants a separate, temporary cleanup policy later. Keep the deployment role and runtime boundary until the stack is fully removed; CloudFormation needs scoped role/service deletion permissions for rollback/cleanup. The artifact bucket and owner-created IAM setup are outside the stack and remain after stack deletion.

## Broader permissions and what to tighten

* **Generated physical-name suffixes:** account/region/logical-resource-specific wildcards are required before first deployment. They permit matching names, not a cryptographic proof of stack membership. IAM/other services have global or regional name uniqueness constraints. Step Functions' logical-ID-only prefix can overlap with other stacks using identical IDs. After the first approved deployment, replace generated-name patterns with actual physical ARNs from CloudFormation Resources, allowing for replacement resources when updating the template.
* **HTTP API:** its API ID is allocated by AWS, and API Gateway ARNs have an empty account component. Before creation `/apis/*` and tag management cover HTTP APIs in this account's eu-central-1 control plane. This is broader than one API. After deployment, render with `--api-id ACTUAL_API_ID` and replace the owner-created execution policy; this narrows `/apis/*` to that API and descendants. API collection creation and generic tag-path access remain broader bootstrap rights and can be removed after first creation if no replacement is planned. Tag-path access may affect other API Gateway tags in the region. No unsupported stack-tag condition is used to pretend these rights are narrower than they are.
* **CloudFormation template validation:** ValidateTemplate and inline template-summary operations cannot be constrained to the future stack ARN. Their Resource `*` grant has eu-central-1 enforced. The Serverless transform has a specific AWS-owned ARN; it is not a wildcard transform grant.
* **Logs:** the CloudFormation role has regional Resource `*` only for read-only describe APIs. The existing workflow runtime roles, and therefore their boundary envelope, require Resource `*` for legacy log-delivery/resource-policy APIs, including PutResourcePolicy. Those runtime rights can affect logging outside this stack; do not treat the runtime boundary as a sandbox for malicious templates. The developer cannot directly call them. Splitting identities does not remove permissions the application's logging integration needs.
* **IAM:** inline-policy contents and role trust-policy contents cannot be validated by IAM conditions. Mandatory runtime boundaries cap identity-policy privilege expansion, but they are not a complete sandbox against every resource-based-policy/session-principal edge case. Review templates as trusted deployment code. The developer cannot change the boundary or execution policies.
* **Existing service role association:** CloudFormation continues to use its associated service role for later stack operations. Anyone else who can operate this stack may be able to use that role without a fresh PassRole check. Restrict all stack operators, not only this user's policy. AWS explains this in [CloudFormation service roles](https://docs.aws.amazon.com/AWSCloudFormation/latest/UserGuide/using-iam-servicerole.html).

## Validation limits

Prepared from this repository's SAM template, the locally expanded 29-resource CloudFormation representation, installed SAM packaging/deploy code, current public eu-central-1 CloudFormation provider schemas, and AWS's [service authorization reference](https://docs.aws.amazon.com/service-authorization/latest/reference/list_cloudformation.html). Provider schema permissions were filtered for this template: for example, no ECR/EC2/EFS, S3 replication, global tables, custom KMS key management or future unconfigured bucket features were granted just because they appear in generic provider schemas.

All JSON parses locally and managed policies fit size limits. Action names were compared with AWS's public service-reference metadata; the literal Stage TagResource/UntagResource actions are documented exceptions supported by the official provider schema and live denial. The boundary-enabled template was checked locally with SAM and its seven generated roles inspected offline. Those preparation checks are not IAM Policy Simulator, Access Analyzer, SCP or successful deployment verification. See [repository verification](../verification.md) for local checks. Existing public deployment status does not replace IAM review for a different account or organization. An IAM operator should review Console policy findings and each change set; if an actual denial appears, inspect the denied action/resource and adjust only that documented requirement, not attach FullAccess.
