"""HTTP API router and Lambda workflow tasks. API contract: docs/api.md."""
import logging
import os
from urllib.parse import unquote
from .common.model import Problem, Conflict, MAX_BYTES, dumps, loads
from .common.service import Service
from .common.store import make_store

logger = logging.getLogger(__name__)


def get_service():
    return Service(make_store())


def start_workflow(service, job_id, action, plan_id=None, local_start=None):
    job, claimed = service.claim(job_id, action, plan_id)
    payload = dict(jobId=job_id, attempt=job['attempt'], action=action, inventoryType=job['inventoryType'])
    if local_start:
        if claimed:
            local_start(payload)
        return job
    import boto3
    from botocore.exceptions import ClientError
    client = boto3.client('stepfunctions')
    # Same name + identical input makes a repeated start idempotent while running.
    try:
        workflow_env = {'preview': 'PREVIEW_WORKFLOW', 'execute': 'IMPORT_WORKFLOW', 'rollback': 'ROLLBACK_WORKFLOW'}[action]
        client.start_execution(stateMachineArn=os.environ[workflow_env],
                               name=f'{action}-{job_id}-{job["attempt"]}', input=dumps(payload))
    except ClientError as exc:
        if exc.response['Error']['Code'] != 'ExecutionAlreadyExists':
            # Keep the claim: caller can retry this exact named start after a
            # timeout without launching an uncontrolled second execution.
            raise
    return job


def route(service, method, path, body=None, query=None, local_start=None, base_url=''):
    body, query = {} if body is None else body, query or {}
    if not isinstance(body, dict):
        raise Problem('Request body must be a JSON object.')
    parts = [unquote(p) for p in path.strip('/').split('/')]
    if parts == ['health'] and method == 'GET':
        return 200, {'status': 'ok', 'mode': 'aws' if os.environ.get('BUCKET') else 'local-sqlite', 'tenant': service.tenant}
    if parts == ['imports']:
        if method == 'GET':
            return 200, {'items': service.list_jobs()}
        if method == 'POST':
            job = service.create_job(body)
            if os.environ.get('BUCKET'):
                # Signed ContentLength constrains the browser PUT to the declared
                # size; parsing independently enforces the limit after upload.
                url = service.store.s3.generate_presigned_url('put_object', Params={
                    'Bucket': service.store.bucket, 'Key': job['s3ObjectKey'],
                    'ContentType': 'application/octet-stream', 'ContentLength': job['size'],
                    'ServerSideEncryption': 'AES256'}, ExpiresIn=300)
            else:
                url = f'{base_url}/local/uploads/{job["jobId"]}'
            return 201, {'job': job, 'uploadUrl': url, 'uploadHeaders': {'Content-Type': 'application/octet-stream', 'x-amz-server-side-encryption': 'AES256'}}
    if parts[0] == 'imports' and len(parts) in (2, 3):
        job_id = parts[1]
        job = service.job(job_id)
        if len(parts) == 2 and method == 'GET':
            result = dict(job)
            if job.get('executionReportKey'):
                result['executionResults'] = loads(service.store.get_object(job['executionReportKey']))
            return 200, result
        if len(parts) == 3:
            action = parts[2]
            if method == 'GET' and action == 'plan':
                return 200, {'planId': job.get('planId'), 'items': service.plan(job_id)}
            if method == 'GET' and action == 'journal':
                return 200, {'items': service.store.query('journal', job_id)}
            if method == 'GET' and action == 'rollback':
                return 200, {'counts': job.get('rollbackCounts', {}), 'items': loads(service.store.get_object(job['rollbackReportKey'])) if job.get('rollbackReportKey') else [],
                             'journal': service.store.query('journal', job_id)}
            if method == 'POST' and action in ('preview', 'execute', 'rollback'):
                return 202, start_workflow(service, job_id, action, body.get('planId'), local_start)
    if parts == ['inventory'] and method == 'GET':
        return 200, {'items': service.inventory(query.get('inventoryType'))}
    if len(parts) == 3 and parts[0] == 'inventory' and method == 'PATCH':
        return 200, service.patch_inventory(parts[1], parts[2], body)
    raise Problem('Route not found.', 404)


def api(event, context):
    # The ANY proxy can forward browser preflight requests to this Lambda.
    # API Gateway supplies the configured CORS headers; no service calls are
    # needed before the browser sends the actual POST or PATCH request.
    if event['requestContext']['http']['method'] == 'OPTIONS':
        return {'statusCode': 204, 'headers': {'Cache-Control': 'no-store'}, 'body': ''}
    try:
        import base64
        raw = event.get('body') or '{}'
        if event.get('isBase64Encoded'):
            raw = base64.b64decode(raw).decode()
        if len(raw) > 16384:
            raise Problem('Request body too large.', 413)
        body = loads(raw)
        status, result = route(get_service(), event['requestContext']['http']['method'], event['rawPath'], body, event.get('queryStringParameters'))
    except (ValueError, UnicodeError):
        status, result = 400, {'error': {'message': 'Invalid JSON body.'}}
    except Problem as exc:
        status, result = exc.status, {'error': {'message': str(exc)}}
    except Exception:
        logger.exception('API request failed')
        status, result = 500, {'error': {'message': 'Request failed. Retry or inspect CloudWatch logs.'}}
    return {'statusCode': status, 'headers': {'Content-Type': 'application/json', 'Cache-Control': 'no-store'}, 'body': dumps(result)}


def validator(event, context, kind):
    service = get_service()
    if service.job(event['jobId'])['inventoryType'] != kind:
        raise Problem('Inventory validator branch mismatch.')
    try:
        service.preview(event['jobId'], event['attempt'])
    except Problem as exc:
        job = service.job(event['jobId'])
        service.fail(event['jobId'], event['attempt'], 'preview')
        failed = service.job(event['jobId'])
        failed['errorMessage'] = str(exc)
        service.save_job(failed, {'status': failed['status'], 'attempt': event['attempt']})
        return {'status': 'PREVIEW_FAILED'}
    return {'status': 'PREVIEW_READY'}


def electronics_validator(event, context):
    return validator(event, context, 'electronics')


def groceries_validator(event, context):
    return validator(event, context, 'groceries')


def worker(event, context):
    service = get_service()
    stage = event['stage']
    args = event['jobId'], event['attempt'], event['action']
    if stage == 'prepare':
        return service.prepare(*args)
    if stage == 'process':
        service.process(*args, event['ref'])
        return {'ok': True}
    if stage == 'recordFailure':
        service.record_failure(*args, event['ref'])
        return {'ok': False}
    if stage == 'finish':
        service.finish(*args, event['refs'])
    elif stage == 'fail':
        service.fail(*args)
    else:
        raise Problem('Unknown workflow stage.')
    return {'ok': True}
