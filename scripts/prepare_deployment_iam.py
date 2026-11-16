"""Prepare IAM JSON and a boundary-enabled SAM copy. Strictly local; no AWS calls.

The checked-in JSON uses ACCOUNT_ID placeholders. Render account-specific copies
with --account-id. Keep template.yaml and samconfig.toml untouched.
"""
import argparse
import copy
import json
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
REGION = 'eu-central-1'
STACK = 'ingestra-dev'
ROLE_NAME = 'ingestra-dev-cloudformation-execution'
BOUNDARY_NAME = 'IngestraDevRuntimeBoundary'
FUNCTIONS = ('ApiFunction', 'WorkerFunction', 'ElectronicsFunction', 'GroceriesFunction')
WORKFLOWS = ('PreviewWorkflow', 'ImportWorkflow', 'RollbackWorkflow')
TABLES = ('InventoryTable', 'JobsTable', 'JournalTable')
DELIVERY_ACTIONS = ['logs:CreateLogDelivery', 'logs:GetLogDelivery', 'logs:UpdateLogDelivery',
                    'logs:DeleteLogDelivery', 'logs:ListLogDeliveries', 'logs:PutResourcePolicy',
                    'logs:DescribeResourcePolicies', 'logs:DescribeLogGroups']


def statement(sid, actions, resources, condition=None):
    item = dict(Sid=sid, Effect='Allow', Action=actions, Resource=resources)
    if condition:
        item['Condition'] = condition
    return item


def policy(*statements):
    return {'Version': '2012-10-17', 'Statement': list(statements)}


def generate(account='ACCOUNT_ID', artifact_bucket=None, api_id=None):
    bucket = artifact_bucket or f'{STACK}-sam-artifacts-{account}-{REGION}'
    cfn_role = f'arn:aws:iam::{account}:role/{ROLE_NAME}'
    boundary = f'arn:aws:iam::{account}:policy/{BOUNDARY_NAME}'
    stack = f'arn:aws:cloudformation:{REGION}:{account}:stack/{STACK}/*'
    functions = [f'arn:aws:lambda:{REGION}:{account}:function:{STACK}-{name}-*' for name in FUNCTIONS]
    function_roles = [f'arn:aws:iam::{account}:role/{STACK}-{name}Role-*' for name in FUNCTIONS]
    workflow_roles = [f'arn:aws:iam::{account}:role/{STACK}-{name}Role-*' for name in WORKFLOWS]
    # Unlike IAM roles, the current Step Functions provider's generated names
    # start with the logical ID, not the stack name (see AWS SAM remote-invoke docs).
    workflows = [f'arn:aws:states:{REGION}:{account}:stateMachine:{name}-*' for name in WORKFLOWS]
    tables = [f'arn:aws:dynamodb:{REGION}:{account}:table/{STACK}-{name}-*' for name in TABLES]
    uploads = f'arn:aws:s3:::{STACK}-uploadbucket-*'
    artifact = f'arn:aws:s3:::{bucket}'
    artifact_objects = artifact + f'/{STACK}/*'
    lambda_groups = [f'arn:aws:logs:{REGION}:{account}:log-group:/aws/lambda/{STACK}-{name}-*' for name in FUNCTIONS]
    workflow_groups = [f'arn:aws:logs:{REGION}:{account}:log-group:/aws/vendedlogs/states/{STACK}-{name}' for name in ('preview', 'import', 'rollback')]
    all_groups = lambda_groups + workflow_groups
    regional = {'StringEquals': {'aws:RequestedRegion': REGION}}
    api = f'arn:aws:apigateway:{REGION}::/apis/{api_id or "*"}'
    api_resources = [api, api + '/*'] if api_id else [api]
    transaction_only = {'ForAnyValue:StringEquals': {'dynamodb:EnclosingOperation': ['TransactWriteItems']}}
    basic = 'arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole'

    deployer = policy(
        statement('CreateOnlyThisStacksChangeSetsWithApprovedRole', ['cloudformation:CreateChangeSet'], stack,
                  {'StringEquals': {'cloudformation:RoleArn': cfn_role, 'aws:RequestedRegion': REGION}}),
        statement('AllowOnlyServerlessTransform', ['cloudformation:CreateChangeSet'],
                  f'arn:aws:cloudformation:{REGION}:aws:transform/Serverless-2016-10-31', regional),
        statement('ReviewAndExecuteThisStacksChangeSets', [
            'cloudformation:DescribeStacks', 'cloudformation:DescribeStackEvents',
            'cloudformation:DescribeStackResource', 'cloudformation:DescribeStackResources',
            'cloudformation:ListStackResources', 'cloudformation:ListChangeSets',
            'cloudformation:DescribeChangeSet', 'cloudformation:ExecuteChangeSet',
            'cloudformation:DeleteChangeSet', 'cloudformation:GetTemplate',
            'cloudformation:TagResource', 'cloudformation:UntagResource'], stack, regional),
        statement('ValidateInlineTemplates', ['cloudformation:ValidateTemplate', 'cloudformation:GetTemplateSummary'], '*', regional),
        statement('PassOnlyTheCloudFormationExecutionRole', ['iam:PassRole'], cfn_role,
                  {'StringEquals': {'iam:PassedToService': 'cloudformation.amazonaws.com'}}),
        statement('ReadOnlyTheArtifactBucketLocation', ['s3:GetBucketLocation'], artifact),
        statement('ListOnlyThisStacksArtifactPrefix', ['s3:ListBucket'], artifact,
                  {'StringLike': {'s3:prefix': [STACK + '/', STACK + '/*']}}),
        statement('UploadAndDeduplicateThisStacksArtifacts', [
            's3:GetObject', 's3:GetObjectVersion', 's3:PutObject',
            's3:AbortMultipartUpload', 's3:ListMultipartUploadParts'], artifact_objects),
    )

    compute = policy(
        # With a service role, CloudFormation authorizes SAM macro expansion
        # using that role as well as the initiating deployer. This ARN is the
        # AWS-owned transform, not permission to create arbitrary stack changes.
        statement('ExpandOnlyTheRegionalServerlessTransform', ['cloudformation:CreateChangeSet'],
                  f'arn:aws:cloudformation:{REGION}:aws:transform/Serverless-2016-10-31', regional),
        statement('ManageOnlyTheFourGeneratedFunctions', [
            'lambda:CreateFunction', 'lambda:GetFunction', 'lambda:UpdateFunctionCode',
            'lambda:UpdateFunctionConfiguration', 'lambda:DeleteFunction',
            'lambda:GetFunctionCodeSigningConfig', 'lambda:GetFunctionRecursionConfig',
            'lambda:GetRuntimeManagementConfig', 'lambda:GetFunctionScalingConfig',
            'lambda:TagResource', 'lambda:UntagResource'], functions, regional),
        statement('ManageOnlyTheApiGatewayInvokePermission', [
            'lambda:AddPermission', 'lambda:RemovePermission', 'lambda:GetPolicy'], functions[0], regional),
        statement('ManageOnlyTheThreeGeneratedStateMachines', [
            'states:CreateStateMachine', 'states:DescribeStateMachine', 'states:UpdateStateMachine',
            'states:DeleteStateMachine', 'states:TagResource', 'states:UntagResource',
            'states:ListTagsForResource'], workflows, regional),
        statement('CreateTheHttpApiCollection', ['apigateway:POST', 'apigateway:PUT'],
                  f'arn:aws:apigateway:{REGION}::/apis', regional),
        statement('ManageHttpApiAndDefaultStage', [
            'apigateway:GET', 'apigateway:POST', 'apigateway:PUT',
            'apigateway:PATCH', 'apigateway:DELETE'], api_resources, regional),
        statement('ManageHttpApiTags', ['apigateway:GET', 'apigateway:POST', 'apigateway:PUT', 'apigateway:DELETE'],
                  f'arn:aws:apigateway:{REGION}::/tags/*', regional),
        # The Stage provider and live service additionally require these literal
        # actions, despite their omission from the generic IAM action catalog.
        # Creation checks the collection ARN; updates may check the stage ARN.
        statement('ManageDefaultStageProviderTags', ['apigateway:TagResource', 'apigateway:UntagResource'],
                  [api + '/stages', api + '/stages/$default'], regional),
        statement('ManageOnlyApplicationLogGroups', [
            'logs:CreateLogGroup', 'logs:DeleteLogGroup', 'logs:PutRetentionPolicy',
            'logs:DeleteRetentionPolicy', 'logs:ListTagsForResource', 'logs:TagResource',
            'logs:UntagResource', 'logs:TagLogGroup', 'logs:UntagLogGroup',
            'logs:GetDataProtectionPolicy'], all_groups + [g + ':*' for g in all_groups], regional),
        statement('DescribeLogIndexesForProviderRead', ['logs:DescribeIndexPolicies'], '*', regional),
        statement('DescribeLogConfigurationForProviderRead',
                  ['logs:DescribeLogGroups', 'logs:DescribeResourcePolicies'], '*', regional),
    )

    storage = policy(
        statement('ManageOnlyTheThreeApplicationTables', [
            'dynamodb:CreateTable', 'dynamodb:DescribeTable', 'dynamodb:UpdateTable',
            'dynamodb:DeleteTable', 'dynamodb:DescribeContinuousBackups', 'dynamodb:UpdateContinuousBackups',
            'dynamodb:DescribeTimeToLive', 'dynamodb:DescribeContributorInsights',
            'dynamodb:DescribeKinesisStreamingDestination', 'dynamodb:GetResourcePolicy',
            'dynamodb:ListTagsOfResource', 'dynamodb:TagResource', 'dynamodb:UntagResource'], tables, regional),
        statement('CreateOnlyTheGeneratedUploadsBucketInFrankfurt', ['s3:CreateBucket'], uploads,
                  {'StringEquals': {'s3:LocationConstraint': REGION}}),
        statement('ConfigureOnlyUploadsBucketFeaturesInTemplate', [
            's3:PutEncryptionConfiguration', 's3:PutBucketCORS', 's3:PutLifecycleConfiguration',
            's3:PutBucketPublicAccessBlock', 's3:PutBucketTagging', 's3:TagResource', 's3:UntagResource',
            's3:PutBucketPolicy', 's3:DeleteBucketPolicy', 's3:DeleteBucket'], uploads),
        statement('ReadUploadsBucketConfigurationForCloudFormation', [
            's3:GetBucketLocation', 's3:ListBucket', 's3:GetBucketPolicy', 's3:GetBucketAcl',
            's3:GetAccelerateConfiguration', 's3:GetLifecycleConfiguration', 's3:GetBucketPublicAccessBlock',
            's3:GetAnalyticsConfiguration', 's3:GetBucketCORS', 's3:GetEncryptionConfiguration',
            's3:GetInventoryConfiguration', 's3:GetBucketLogging', 's3:GetMetricsConfiguration',
            's3:GetBucketNotification', 's3:GetBucketVersioning', 's3:GetReplicationConfiguration',
            's3:GetBucketWebsite', 's3:GetBucketObjectLockConfiguration', 's3:GetBucketTagging',
            's3:ListTagsForResource', 's3:GetBucketAbac', 's3:GetBucketOwnershipControls',
            's3:GetIntelligentTieringConfiguration', 's3:GetBucketMetadataTableConfiguration'], uploads),
        statement('ReadOnlyThisStacksDeploymentArtifacts', ['s3:GetObject', 's3:GetObjectVersion'], artifact_objects),
    )

    roles = function_roles + workflow_roles
    iam = policy(
        statement('CreateOnlySevenRuntimeRolesWithMandatoryBoundary', ['iam:CreateRole'], roles,
                  {'ArnEquals': {'iam:PermissionsBoundary': boundary}}),
        statement('ManageOnlySevenRuntimeRoles', [
            'iam:GetRole', 'iam:UpdateRole', 'iam:UpdateRoleDescription', 'iam:DeleteRole',
            'iam:UpdateAssumeRolePolicy', 'iam:PutRolePolicy', 'iam:DeleteRolePolicy',
            'iam:GetRolePolicy', 'iam:ListRolePolicies', 'iam:ListAttachedRolePolicies',
            'iam:TagRole', 'iam:UntagRole'], roles),
        statement('KeepOnlyTheApprovedBoundaryOnRuntimeRoles', ['iam:PutRolePermissionsBoundary'], roles,
                  {'ArnEquals': {'iam:PermissionsBoundary': boundary}}),
        statement('AttachOnlyBasicLambdaLogsPolicy', ['iam:AttachRolePolicy', 'iam:DetachRolePolicy'], function_roles,
                  {'ArnEquals': {'iam:PolicyARN': basic}}),
        statement('PassOnlyFunctionRolesToLambda', ['iam:PassRole'], function_roles,
                  {'StringEquals': {'iam:PassedToService': 'lambda.amazonaws.com'}}),
        statement('PassOnlyWorkflowRolesToStepFunctions', ['iam:PassRole'], workflow_roles,
                  {'StringEquals': {'iam:PassedToService': 'states.amazonaws.com'}}),
    )

    # Maximum permission envelope, not a replacement for the narrower SAM
    # per-function/per-workflow identity policies.
    runtime = policy(
        statement('ReadApplicationInventoryJobsAndJournal', ['dynamodb:GetItem', 'dynamodb:Query'], tables),
        statement('TransactionalApplicationPuts', ['dynamodb:PutItem'], tables, transaction_only),
        statement('TransactionalInventoryDeletes', ['dynamodb:DeleteItem'], tables[0], transaction_only),
        statement('TransactionalJobFences', ['dynamodb:ConditionCheckItem'], tables[1], transaction_only),
        statement('ReadAndWriteOnlyApplicationObjectPrefixes', ['s3:GetObject', 's3:PutObject'],
                  [uploads + '/' + p + '/*' for p in ('uploads', 'plans', 'outcomes', 'reports')]),
        statement('InvokeOnlyWorkerAndValidators', ['lambda:InvokeFunction'], functions[1:]),
        statement('StartOnlyApplicationWorkflows', ['states:StartExecution'], workflows),
        statement('WriteOnlyApplicationLambdaLogs', ['logs:CreateLogGroup', 'logs:CreateLogStream', 'logs:PutLogEvents'],
                  lambda_groups + [g + ':*' for g in lambda_groups]),
        statement('ExistingWorkflowLogDeliveryApis', DELIVERY_ACTIONS, '*', regional),
        # SSEEnabled:true selects alias/aws/dynamodb. Keep its service-mediated
        # key-policy authorizations inside the boundary; permit no key admin.
        statement('AllowOnlyDynamoDbManagedKeyUseThroughDynamoDb', [
            'kms:Encrypt', 'kms:Decrypt', 'kms:ReEncryptFrom', 'kms:ReEncryptTo',
            'kms:GenerateDataKey', 'kms:GenerateDataKeyWithoutPlaintext', 'kms:CreateGrant', 'kms:DescribeKey'],
            f'arn:aws:kms:{REGION}:{account}:key/*', {
                'StringEquals': {'kms:ViaService': f'dynamodb.{REGION}.amazonaws.com', 'kms:CallerAccount': account},
                'ForAnyValue:StringEquals': {'kms:ResourceAliases': 'alias/aws/dynamodb'}}),
    )
    trust = {'Version': '2012-10-17', 'Statement': [{
        'Effect': 'Allow', 'Principal': {'Service': 'cloudformation.amazonaws.com'}, 'Action': 'sts:AssumeRole'}]}
    bucket_policy = {'Version': '2012-10-17', 'Statement': [{
        'Sid': 'DenyNonTlsRequests', 'Effect': 'Deny', 'Principal': '*', 'Action': 's3:*',
        'Resource': [artifact, artifact + '/*'], 'Condition': {'Bool': {'aws:SecureTransport': 'false'}}}]}
    return {
        'deployer-user.json': deployer, 'cloudformation-trust.json': trust,
        'cloudformation-compute.json': compute, 'cloudformation-storage.json': storage,
        'cloudformation-iam.json': iam, 'runtime-boundary.json': runtime,
        'artifact-bucket-policy.json': bucket_policy,
    }


def deployment_template(account, output):
    template = yaml.safe_load((ROOT / 'template.yaml').read_text())
    template = copy.deepcopy(template)
    found = set()
    for name, resource in template['Resources'].items():
        props = resource.get('Properties', {})
        if resource['Type'] in ('AWS::Serverless::Function', 'AWS::Serverless::StateMachine'):
            props['PermissionsBoundary'] = f'arn:aws:iam::{account}:policy/{BOUNDARY_NAME}'
            found.add(name)
        if resource['Type'] == 'AWS::Serverless::Function':
            props['CodeUri'] = str((ROOT / props['CodeUri']).resolve())
        if resource['Type'] == 'AWS::Serverless::StateMachine':
            props['DefinitionUri'] = str((ROOT / props['DefinitionUri']).resolve())
    if found != set(FUNCTIONS + WORKFLOWS):
        raise ValueError('Template resources changed; review IAM scope before generating.')
    (output / 'template.yaml').write_text(yaml.safe_dump(template, sort_keys=False))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--account-id', help='Your 12-digit account ID; never a credential.')
    parser.add_argument('--artifact-bucket', help='Use a specific, already-created same-account bucket.')
    parser.add_argument('--api-id', help='After first deployment, restrict HTTP API management to its actual ID.')
    parser.add_argument('--output', type=Path, default=ROOT / '.local/deployment-iam')
    parser.add_argument('--write-policy-templates', action='store_true', help='Refresh checked-in ACCOUNT_ID policy templates only.')
    args = parser.parse_args()
    if not args.write_policy_templates and not re.fullmatch(r'\d{12}', args.account_id or ''):
        parser.error('--account-id must be your 12-digit AWS account ID.')
    if args.artifact_bucket and not re.fullmatch(r'[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]', args.artifact_bucket):
        parser.error('Invalid S3 bucket name.')
    if args.api_id and not re.fullmatch(r'[a-z0-9]+', args.api_id):
        parser.error('Invalid HTTP API ID.')
    output = ROOT / 'docs/iam/policies' if args.write_policy_templates else args.output.resolve()
    if not output.is_relative_to(ROOT):
        parser.error('Write generated setup inside this repository.')
    output.mkdir(parents=True, exist_ok=True)
    account = 'ACCOUNT_ID' if args.write_policy_templates else args.account_id
    for name, document in generate(account, args.artifact_bucket, args.api_id).items():
        serialized = json.dumps(document, separators=(',', ':'))
        if name not in ('artifact-bucket-policy.json', 'cloudformation-trust.json') and len(serialized) > 6144:
            raise ValueError(f'{name} exceeds managed-policy size limit; split it before use.')
        (output / name).write_text(json.dumps(document, indent=2) + '\n')
    if not args.write_policy_templates:
        deployment_template(account, output)
        # No resolve_s3 key: root samconfig.toml has resolve_s3=true, which must
        # not be combined with an explicit --s3-bucket.
        (output / 'samconfig.toml').write_text('version = 0.1\n')
    print(f'Prepared local IAM files: {output}. No AWS API calls were made.')


if __name__ == '__main__':
    main()
