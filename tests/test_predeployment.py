"""Public-demo upload bounds and duplicate requests through the actual router."""
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
import yaml

from backend.ingestra import handler
from backend.ingestra.common.model import MAX_BYTES, Problem, dumps
from test_ingestra import BASE, preview


@pytest.mark.parametrize('filename', ['supplier.csv', 'supplier.json'])
def test_oversized_upload_request_rejected_before_job_creation(service, filename):
    with pytest.raises(Problem) as error:
        handler.route(service, 'POST', '/imports', {'inventoryType': 'electronics', 'filename': filename, 'size': MAX_BYTES + 1})
    assert error.value.status == 413
    assert not service.list_jobs()


@pytest.mark.parametrize('filename', ['supplier.exe', 'supplier.csv.exe', 'supplier.xlsx'])
def test_unsupported_upload_request_rejected(service, filename):
    with pytest.raises(Problem) as error:
        handler.route(service, 'POST', '/imports', {'inventoryType': 'electronics', 'filename': filename, 'size': 10})
    assert error.value.status == 400
    assert not service.list_jobs()


def test_actual_oversized_object_is_rejected_even_with_small_declared_size(service):
    job = service.create_job({'inventoryType': 'electronics', 'filename': 'supplier.csv', 'size': 10})
    # Simulate an object written outside the signed browser upload path.
    service.store.put_object(job['s3ObjectKey'], b'x' * (MAX_BYTES + 1))
    job, _ = service.claim(job['jobId'], 'preview')
    with pytest.raises(Problem) as error:
        service.preview(job['jobId'], job['attempt'])
    assert error.value.status == 413
    assert not service.inventory()
    assert 'planKey' not in service.job(job['jobId'])


@pytest.mark.parametrize('filename', ['supplier.csv', 'supplier.json'])
def test_more_than_500_records_never_produces_plan_or_inventory(service, filename):
    if filename.endswith('.json'):
        content = dumps([dict(BASE, sku=f'E{i}') for i in range(501)]).encode()
    else:
        content = ('sku,name,quantity,price\n' + ''.join(f'E{i},Product,1,2.00\n' for i in range(501))).encode()
    job = service.create_job({'inventoryType': 'electronics', 'filename': filename, 'size': len(content)})
    service.store.put_object(job['s3ObjectKey'], content)
    job, _ = service.claim(job['jobId'], 'preview')
    with pytest.raises(Problem, match='500'):
        service.preview(job['jobId'], job['attempt'])
    assert not service.inventory()
    assert 'planKey' not in service.job(job['jobId'])


def test_put_authorization_signs_exact_server_validated_size(service, monkeypatch):
    if not hasattr(service.store, 's3'):
        return
    captured = {}
    sign = service.store.s3.generate_presigned_url
    def capture(operation, **kwargs):
        captured.update(operation=operation, **kwargs)
        return sign(operation, **kwargs)
    monkeypatch.setattr(service.store.s3, 'generate_presigned_url', capture)
    _, result = handler.route(service, 'POST', '/imports', {'inventoryType': 'electronics', 'filename': 'supplier.csv', 'size': MAX_BYTES})
    assert captured['operation'] == 'put_object'
    assert captured['Params']['ContentLength'] == MAX_BYTES
    assert captured['Params']['Key'] == result['job']['s3ObjectKey']
    assert captured['Params']['ServerSideEncryption'] == 'AES256'
    assert result['job']['jobId'] in captured['Params']['Key']
    headers = parse_qs(urlparse(result['uploadUrl']).query)['X-Amz-SignedHeaders'][0].split(';')
    assert 'content-length' in headers


def test_api_duplicate_approval_starts_one_workflow_and_one_commit(service):
    job = preview(service, [BASE])
    launches = []
    path = f'/imports/{job["jobId"]}/execute'
    body = {'planId': job['planId']}
    _, first = handler.route(service, 'POST', path, body, local_start=launches.append)
    _, second = handler.route(service, 'POST', path, body, local_start=launches.append)
    assert first['attempt'] == second['attempt'] and len(launches) == 1
    service.import_record(job['jobId'], first['attempt'], 0)
    service.import_record(job['jobId'], first['attempt'], 0)
    service.finish(job['jobId'], first['attempt'], 'execute', [0])
    with pytest.raises(Problem) as error:
        handler.route(service, 'POST', path, body, local_start=launches.append)
    assert error.value.status == 409
    assert len(launches) == 1 and service.inventory()[0]['version'] == 1
    assert len(service.store.query('journal', job['jobId'])) == 1


def test_api_repeated_rollback_preserves_original_journal_and_no_double_write(service):
    from test_ingestra import execute
    job, _ = execute(service, preview(service, [BASE]))
    original = service.store.query('journal', job['jobId'])[0]
    launches = []
    path = f'/imports/{job["jobId"]}/rollback'
    _, first = handler.route(service, 'POST', path, {}, local_start=launches.append)
    _, second = handler.route(service, 'POST', path, {}, local_start=launches.append)
    assert first['attempt'] == second['attempt'] and len(launches) == 1
    service.rollback_record(job['jobId'], first['attempt'], 'E100')
    service.finish(job['jobId'], first['attempt'], 'rollback', ['E100'])
    _, retry = handler.route(service, 'POST', path, {}, local_start=launches.append)
    assert service.rollback_record(job['jobId'], retry['attempt'], 'E100')['status'] == 'ALREADY_REVERSED'
    service.finish(job['jobId'], retry['attempt'], 'rollback', ['E100'])
    journal = service.store.query('journal', job['jobId'])[0]
    assert not service.inventory()
    assert journal['beforeImage'] == original['beforeImage']
    assert journal['afterImage'] == original['afterImage']


def test_execute_uses_workflow_variable_from_actual_sam_template(service, monkeypatch):
    template = yaml.safe_load(Path('template.yaml').read_text())
    variables = template['Resources']['ApiFunction']['Properties']['Environment']['Variables']
    import_arn = 'arn:aws:states:eu-central-1:123456789012:stateMachine:Import'
    for name in ('PREVIEW_WORKFLOW', 'IMPORT_WORKFLOW', 'ROLLBACK_WORKFLOW'):
        assert name in variables
        monkeypatch.setenv(name, import_arn if name == 'IMPORT_WORKFLOW' else 'unused')
    monkeypatch.delenv('EXECUTE_WORKFLOW', raising=False)
    import boto3
    calls = []
    class StepFunctions:
        def start_execution(self, **kwargs):
            calls.append(kwargs)
    monkeypatch.setattr(boto3, 'client', lambda name: StepFunctions())
    job = preview(service, [BASE])
    handler.start_workflow(service, job['jobId'], 'execute', job['planId'])
    assert len(calls) == 1 and calls[0]['stateMachineArn'] == import_arn
