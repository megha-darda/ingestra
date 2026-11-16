import json
from urllib.parse import urlparse, parse_qs
import pytest
from backend.ingestra import handler
from backend.ingestra.common.model import dumps
from test_ingestra import BASE, preview


def test_lambda_workflow_dispatch(service, monkeypatch):
    monkeypatch.setattr(handler,'get_service',lambda:service)
    job=preview(service,[BASE])
    job,_=service.claim(job['jobId'],'execute',job['planId'])
    payload={'jobId':job['jobId'],'attempt':job['attempt'],'action':'execute'}
    prepared=handler.worker(dict(payload,stage='prepare'),None)
    assert prepared['refs']==[0]
    assert handler.worker(dict(payload,stage='process',ref=0),None)=={'ok':True}
    handler.worker(dict(payload,stage='finish',refs=[0]),None)
    assert service.job(job['jobId'])['status']=='COMPLETED'
    job,_=service.claim(job['jobId'],'rollback')
    payload.update(attempt=job['attempt'],action='rollback')
    handler.worker(dict(payload,stage='process',ref='E100'),None)
    handler.worker(dict(payload,stage='finish',refs=['E100']),None)
    assert not service.inventory()


def test_api_lambda_errors_are_json(service, monkeypatch):
    monkeypatch.setattr(handler,'get_service',lambda:service)
    event={'requestContext':{'http':{'method':'GET'}},'rawPath':'/health'}
    assert handler.api(event,None)['statusCode']==200
    result=handler.api(dict(event,body='broken'),None)
    assert result['statusCode']==400 and json.loads(result['body'])['error']['message']=='Invalid JSON body.'
    result=handler.api(dict(event,rawPath='/imports/absent'),None)
    assert result['statusCode']==404
    monkeypatch.setattr(handler,'get_service',lambda:(_ for _ in ()).throw(RuntimeError('private traceback detail')))
    result=handler.api(event,None)
    assert result['statusCode']==500 and 'private traceback detail' not in result['body']


@pytest.mark.parametrize('path,method', [
    ('/imports', 'POST'),
    ('/imports/example/preview', 'POST'),
    ('/imports/example/execute', 'POST'),
    ('/imports/example/rollback', 'POST'),
    ('/inventory/electronics/E100', 'PATCH'),
])
def test_api_preflight_succeeds_without_accessing_services(monkeypatch, path, method):
    monkeypatch.setattr(handler, 'get_service', lambda: pytest.fail('Preflight must not access AWS services'))
    event = {
        'requestContext': {'http': {'method': 'OPTIONS'}},
        'rawPath': path,
        'headers': {'origin': 'http://localhost:5173',
                    'access-control-request-method': method,
                    'access-control-request-headers': 'content-type'},
    }
    result = handler.api(event, None)
    assert result['statusCode'] == 204
    assert result['body'] == ''
    # Origin restrictions remain in API Gateway, rather than being widened here.
    assert not any(key.lower().startswith('access-control-') for key in result['headers'])


def test_aws_upload_signature(service):
    if not hasattr(service.store,'s3'):
        return
    _,result=handler.route(service,'POST','/imports',{'inventoryType':'electronics','filename':'inventory.csv','size':100})
    url=urlparse(result['uploadUrl'])
    params=parse_qs(url.query)
    assert url.hostname.endswith('s3.eu-central-1.amazonaws.com')
    assert params['X-Amz-Expires']==['300']
    headers=params['X-Amz-SignedHeaders'][0]
    assert 'content-length' in headers and 'x-amz-server-side-encryption' in headers


def test_malformed_preview_safely_fails(service,monkeypatch):
    monkeypatch.setattr(handler,'get_service',lambda:service)
    job=service.create_job({'filename':'bad.json','size':3,'inventoryType':'electronics'})
    service.store.put_object(job['s3ObjectKey'],b'bad')
    job,_=service.claim(job['jobId'],'preview')
    assert handler.electronics_validator({'jobId':job['jobId'],'attempt':job['attempt']},None)['status']=='PREVIEW_FAILED'
    assert 'Malformed' in service.job(job['jobId'])['errorMessage']
    assert not service.inventory()


def test_lost_task_response_reconciles_committed_journal(service):
    job=preview(service,[BASE])
    job,_=service.claim(job['jobId'],'execute',job['planId'])
    service.import_record(job['jobId'],job['attempt'],0)
    result=service.record_failure(job['jobId'],job['attempt'],'execute',0)
    assert result['status']=='IMPORTED'
    job=service.finish(job['jobId'],job['attempt'],'execute',[0])
    job,_=service.claim(job['jobId'],'rollback')
    service.rollback_record(job['jobId'],job['attempt'],'E100')
    assert service.record_failure(job['jobId'],job['attempt'],'rollback','E100')['status']=='REVERSED'
    job=service.finish(job['jobId'],job['attempt'],'rollback',['E100'])
    job,_=service.claim(job['jobId'],'rollback')
    assert service.record_failure(job['jobId'],job['attempt'],'rollback','E100')['status']=='ALREADY_REVERSED'


def test_repeated_api_start_uses_identical_execution_name(service,monkeypatch):
    import boto3
    calls=[]
    class StepFunctions:
        def start_execution(self,**kwargs):
            calls.append(kwargs)
    monkeypatch.setattr(boto3,'client',lambda name:StepFunctions())
    monkeypatch.setenv('IMPORT_WORKFLOW','arn:aws:states:eu-central-1:123456789012:stateMachine:Import')
    job=preview(service,[BASE])
    first=handler.start_workflow(service,job['jobId'],'execute',job['planId'])
    second=handler.start_workflow(service,job['jobId'],'execute',job['planId'])
    assert first['attempt']==second['attempt'] and calls[0]==calls[1]
