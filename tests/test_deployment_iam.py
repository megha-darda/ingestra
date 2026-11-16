"""Offline checks for the reviewable IAM package, not a live IAM simulator."""
import copy
import fnmatch
import json
from pathlib import Path

import boto3
import pytest
import yaml
from samtranslator.parser.parser import Parser
from samtranslator.translator.translator import Translator

from scripts.prepare_deployment_iam import (
    ROOT, BOUNDARY_NAME, FUNCTIONS, WORKFLOWS, deployment_template, generate,
)

ACCOUNT = '111122223333'


def by_sid(document, name):
    return next(s for s in document['Statement'] if s['Sid'] == name)


def expand(template):
    template = copy.deepcopy(template)
    # Public translator is used locally, with fake credentials. No IAM, S3,
    # CloudFormation, or STS client calls occur.
    for resource in template['Resources'].values():
        if resource['Type'] == 'AWS::Serverless::Function':
            resource['Properties']['CodeUri'] = 's3://offline-artifact-bucket/code.zip'
        elif resource['Type'] == 'AWS::Serverless::StateMachine':
            resource['Properties']['DefinitionUri'] = 's3://offline-artifact-bucket/definition.json'
    session = boto3.Session(region_name='eu-central-1', aws_access_key_id='offline', aws_secret_access_key='offline')
    translator = Translator({'AWSLambdaBasicExecutionRole': 'arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole'}, Parser(), boto_session=session)
    return translator.translate(template, {'AWS::AccountId': ACCOUNT, 'AWS::Region': 'eu-central-1',
                                          'AWS::Partition': 'aws', 'AWS::StackName': 'ingestra-dev'})


def test_checked_in_json_matches_generator_and_fits_managed_policy_limits():
    for name, document in generate().items():
        assert json.loads((ROOT / 'docs/iam/policies' / name).read_text()) == document
        if name not in ('cloudformation-trust.json', 'artifact-bucket-policy.json'):
            assert len(json.dumps(document, separators=(',', ':'))) <= 6144
        assert 'AdministratorAccess' not in json.dumps(document)


def test_developer_has_only_stack_control_scoped_artifacts_and_exact_passrole():
    document = generate(ACCOUNT)['deployer-user.json']
    for item in document['Statement']:
        assert all(a.split(':')[0] in ('cloudformation', 'iam', 's3') for a in item['Action'])
        assert 'cloudformation:DeleteStack' not in item['Action']
        assert 's3:CreateBucket' not in item['Action']
        assert 's3:DeleteObject' not in item['Action']
        if item['Resource'] == '*':
            assert item['Action'] == ['cloudformation:ValidateTemplate', 'cloudformation:GetTemplateSummary']
    passes = by_sid(document, 'PassOnlyTheCloudFormationExecutionRole')
    assert passes['Resource'] == f'arn:aws:iam::{ACCOUNT}:role/ingestra-dev-cloudformation-execution'
    assert passes['Condition']['StringEquals']['iam:PassedToService'] == 'cloudformation.amazonaws.com'
    create = by_sid(document, 'CreateOnlyThisStacksChangeSetsWithApprovedRole')
    assert create['Condition']['StringEquals']['cloudformation:RoleArn'] == passes['Resource']
    assert create['Resource'] == f'arn:aws:cloudformation:eu-central-1:{ACCOUNT}:stack/ingestra-dev/*'


def test_cloudformation_role_cannot_remove_boundary_or_modify_setup_policies():
    document = generate(ACCOUNT)['cloudformation-iam.json']
    actions = {a for s in document['Statement'] for a in s['Action']}
    assert not actions & {'iam:DeleteRolePermissionsBoundary', 'iam:CreatePolicy', 'iam:CreatePolicyVersion',
                           'iam:SetDefaultPolicyVersion', 'iam:CreateUser', 'iam:CreateAccessKey', 'iam:PutUserPolicy'}
    create = by_sid(document, 'CreateOnlySevenRuntimeRolesWithMandatoryBoundary')
    assert len(create['Resource']) == 7
    assert create['Condition']['ArnEquals']['iam:PermissionsBoundary'] == f'arn:aws:iam::{ACCOUNT}:policy/{BOUNDARY_NAME}'
    attach = by_sid(document, 'AttachOnlyBasicLambdaLogsPolicy')
    assert len(attach['Resource']) == 4
    assert attach['Condition']['ArnEquals']['iam:PolicyARN'] == 'arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole'


def test_sam_transform_permission_covers_both_deployer_and_execution_role():
    documents = generate(ACCOUNT)
    transform = 'arn:aws:cloudformation:eu-central-1:aws:transform/Serverless-2016-10-31'
    for name in ('deployer-user.json', 'cloudformation-compute.json'):
        statements = [s for s in documents[name]['Statement']
                      if 'cloudformation:CreateChangeSet' in s['Action'] and s['Resource'] == transform]
        assert len(statements) == 1
        assert statements[0]['Effect'] == 'Allow'
        assert statements[0]['Condition']['StringEquals']['aws:RequestedRegion'] == 'eu-central-1'
    # Execution-role transform permission must not also grant stack creation or
    # generic change-set management. Only this one AWS-owned macro is allowed.
    for item in documents['cloudformation-compute.json']['Statement']:
        if any(a.startswith('cloudformation:') for a in item['Action']):
            assert item['Action'] == ['cloudformation:CreateChangeSet']
            assert item['Resource'] == transform


def test_cloudformation_passrole_targets_match_the_two_actual_runtime_services():
    document = generate(ACCOUNT)['cloudformation-iam.json']
    function_pass = by_sid(document, 'PassOnlyFunctionRolesToLambda')
    workflow_pass = by_sid(document, 'PassOnlyWorkflowRolesToStepFunctions')
    assert len(function_pass['Resource']) == 4 and len(workflow_pass['Resource']) == 3
    assert function_pass['Condition']['StringEquals']['iam:PassedToService'] == 'lambda.amazonaws.com'
    assert workflow_pass['Condition']['StringEquals']['iam:PassedToService'] == 'states.amazonaws.com'
    assert 'cloudformation-execution' not in json.dumps(function_pass['Resource'] + workflow_pass['Resource'])


def test_boundary_copy_preserves_source_and_covers_all_seven_generated_roles(tmp_path):
    original = (ROOT / 'template.yaml').read_bytes()
    deployment_template(ACCOUNT, tmp_path)
    assert (ROOT / 'template.yaml').read_bytes() == original
    prepared = yaml.safe_load((tmp_path / 'template.yaml').read_text())
    source = yaml.safe_load(original)
    for name in FUNCTIONS + WORKFLOWS:
        props = prepared['Resources'][name]['Properties']
        assert props['PermissionsBoundary'] == f'arn:aws:iam::{ACCOUNT}:policy/{BOUNDARY_NAME}'
        assert 'Role' not in props and 'FunctionName' not in props and 'Name' not in props
        for key in ('CodeUri', 'DefinitionUri'):
            if key in props:
                assert Path(props[key]).resolve() == (ROOT / source['Resources'][name]['Properties'][key]).resolve()
    expanded = expand(prepared)
    roles = {name: r for name, r in expanded['Resources'].items() if r['Type'] == 'AWS::IAM::Role'}
    assert set(roles) == {name + 'Role' for name in FUNCTIONS + WORKFLOWS}
    assert len(expanded['Resources']) == 29
    for role in roles.values():
        assert role['Properties']['PermissionsBoundary'] == f'arn:aws:iam::{ACCOUNT}:policy/{BOUNDARY_NAME}'
        assert 'RoleName' not in role['Properties']


def test_artifact_bucket_and_prefix_override_are_used_consistently():
    documents = generate(ACCOUNT, 'owner-existing-artifacts')
    user = documents['deployer-user.json']
    assert by_sid(user, 'UploadAndDeduplicateThisStacksArtifacts')['Resource'] == 'arn:aws:s3:::owner-existing-artifacts/ingestra-dev/*'
    assert by_sid(documents['cloudformation-storage.json'], 'ReadOnlyThisStacksDeploymentArtifacts')['Resource'] == 'arn:aws:s3:::owner-existing-artifacts/ingestra-dev/*'
    assert 'sam-artifacts-' not in json.dumps(documents)


def test_optional_api_id_narrows_management_without_claiming_to_scope_collection_creation():
    documents = generate(ACCOUNT, api_id='a1b2c3d4e5')
    compute = documents['cloudformation-compute.json']
    assert by_sid(compute, 'ManageHttpApiAndDefaultStage')['Resource'] == [
        'arn:aws:apigateway:eu-central-1::/apis/a1b2c3d4e5',
        'arn:aws:apigateway:eu-central-1::/apis/a1b2c3d4e5/*']
    assert by_sid(compute, 'CreateTheHttpApiCollection')['Resource'] == 'arn:aws:apigateway:eu-central-1::/apis'


def test_http_api_create_can_authorize_encoded_tagging_endpoint():
    # API Gateway performs a separate tagging authorization while creating an
    # HTTP API. A POST grant on /apis does not cover this /tags/... resource.
    denied_resource = 'arn:aws:apigateway:eu-central-1::/tags/arn%3Aaws%3Aapigateway%3Aeu-central-1%3A%3A%2Fv2%2Fapis%2F*'
    compute = generate(ACCOUNT)['cloudformation-compute.json']
    tags = by_sid(compute, 'ManageHttpApiTags')
    assert set(tags['Action']) == {'apigateway:GET', 'apigateway:POST', 'apigateway:PUT', 'apigateway:DELETE'}
    assert fnmatch.fnmatchcase(denied_resource, tags['Resource'])
    assert tags['Condition']['StringEquals']['aws:RequestedRegion'] == 'eu-central-1'
    assert not fnmatch.fnmatchcase(denied_resource.replace('eu-central-1', 'us-east-1'), tags['Resource'])


def test_stage_literal_tag_permissions_cover_denied_collection_and_default_stage():
    for api_id in (None, '5bpy400smg'):
        compute = generate(ACCOUNT, api_id=api_id)['cloudformation-compute.json']
        tags = by_sid(compute, 'ManageDefaultStageProviderTags')
        assert tags['Action'] == ['apigateway:TagResource', 'apigateway:UntagResource']
        assert tags['Resource'] == [
            f'arn:aws:apigateway:eu-central-1::/apis/{api_id or "*"}/stages',
            f'arn:aws:apigateway:eu-central-1::/apis/{api_id or "*"}/stages/$default']
        assert tags['Condition']['StringEquals']['aws:RequestedRegion'] == 'eu-central-1'
        for target in ('arn:aws:apigateway:eu-central-1::/apis/5bpy400smg/stages',
                       'arn:aws:apigateway:eu-central-1::/apis/5bpy400smg/stages/$default'):
            assert any(fnmatch.fnmatchcase(target, pattern) for pattern in tags['Resource'])
        assert not any(fnmatch.fnmatchcase('arn:aws:apigateway:eu-central-1::/apis/5bpy400smg/stages/other', pattern)
                       for pattern in tags['Resource'])


def test_all_published_stage_handler_permissions_have_a_scoped_grant():
    evidence = json.loads((ROOT / 'docs/iam/evidence/apigatewayv2-stage-permissions.json').read_text())
    assert evidence['typeName'] == 'AWS::ApiGatewayV2::Stage'
    required = {a for h in evidence['handlers'].values() for a in h['permissions']}
    assert {'apigateway:TagResource', 'apigateway:UntagResource'} <= required
    compute = generate(ACCOUNT)['cloudformation-compute.json']
    for target in ('arn:aws:apigateway:eu-central-1::/apis/5bpy400smg/stages',
                   'arn:aws:apigateway:eu-central-1::/apis/5bpy400smg/stages/$default'):
        for action in required:
            assert any(action in s['Action'] and any(fnmatch.fnmatchcase(target, pattern)
                       for pattern in (s['Resource'] if isinstance(s['Resource'], list) else [s['Resource']]))
                       for s in compute['Statement']), (action, target)


def test_runtime_boundary_does_not_grant_identity_admin_or_provisioning():
    document = generate(ACCOUNT)['runtime-boundary.json']
    for item in document['Statement']:
        assert all(not a.startswith(('iam:', 'cloudformation:', 'apigateway:')) for a in item['Action'])
        assert '*' not in item['Action']
    kms = by_sid(document, 'AllowOnlyDynamoDbManagedKeyUseThroughDynamoDb')
    assert kms['Condition']['StringEquals']['kms:ViaService'] == 'dynamodb.eu-central-1.amazonaws.com'
    assert kms['Condition']['ForAnyValue:StringEquals']['kms:ResourceAliases'] == 'alias/aws/dynamodb'


def test_logs_wildcards_are_regional_and_storage_does_not_grant_supplier_object_access():
    documents = generate(ACCOUNT)
    for document in (documents['cloudformation-compute.json'], documents['runtime-boundary.json']):
        for item in document['Statement']:
            if item['Resource'] == '*':
                assert item['Condition']['StringEquals']['aws:RequestedRegion'] == 'eu-central-1'
                assert all(a.startswith('logs:') for a in item['Action'])
    storage = documents['cloudformation-storage.json']
    object_reads = [item for item in storage['Statement'] if 's3:GetObject' in item['Action']]
    assert len(object_reads) == 1 and '/ingestra-dev/*' in object_reads[0]['Resource']
    assert 'uploadbucket' not in object_reads[0]['Resource']
    cfn_actions = {a for s in documents['cloudformation-compute.json']['Statement'] for a in s['Action']}
    assert 'logs:PutResourcePolicy' not in cfn_actions
    assert 'logs:CreateLogDelivery' not in cfn_actions
