from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from pathlib import Path
import json
import pytest
from backend.ingestra.common.model import Problem, Conflict, dumps, resource_key
from backend.ingestra.common.service import version_of
from backend.ingestra.demo import seed
from backend.ingestra.handler import route
from backend.ingestra.validators.common import parse_file
from backend.ingestra.validators.electronics import validate as electronics
from backend.ingestra.validators.groceries import validate as groceries

BASE = {'sku':'E100','name':'Headphones','quantity':10,'price':'89.00'}


def preview(service, rows, kind='electronics'):
    content = dumps(rows).encode()
    job = service.create_job({'inventoryType':kind,'filename':'test.json','size':len(content)})
    service.store.put_object(job['s3ObjectKey'],content)
    job, _ = service.claim(job['jobId'],'preview')
    return service.preview(job['jobId'],job['attempt'])


def execute(service, job, finish=True):
    job, _ = service.claim(job['jobId'],'execute',job['planId'])
    refs = service.prepare(job['jobId'],job['attempt'],'execute')['refs']
    results = [service.import_record(job['jobId'],job['attempt'],i) for i in refs]
    if finish:
        job = service.finish(job['jobId'],job['attempt'],'execute',refs)
    return job, results


def rollback(service, job):
    job, _ = service.claim(job['jobId'],'rollback')
    refs = service.prepare(job['jobId'],job['attempt'],'rollback')['refs']
    results = [service.rollback_record(job['jobId'],job['attempt'],i) for i in refs]
    return service.finish(job['jobId'],job['attempt'],'rollback',refs), results


def baseline(service):
    return execute(service, preview(service,[BASE]))[0]


def test_csv_and_json():
    rows = parse_file(b'\xef\xbb\xbfsku,name,quantity,price\r\nE1,"Cable, USB",2,3.50\r\n','DATA.CSV')
    assert rows[0]['name']=='Cable, USB'
    assert parse_file(b'[{"sku":"E1","price":3.50}]','a.json')[0]['price']==Decimal('3.50')


@pytest.mark.parametrize('content,name', [(b'{oops','a.json'),(b'{}','a.json'),(b'[]','a.json'),(b'[1]','a.json'),(b'a,a\n1,2','a.csv'),(b'a,b\n1,2,3','a.csv'),(b'[]','a.txt'),(b'\xff','a.csv'),(b'[{"price":NaN}]','a.json'),(b'[]'*1048577,'a.json')])
def test_bad_files(content,name):
    with pytest.raises(Problem):parse_file(content,name)


def test_record_limit():
    with pytest.raises(Problem):parse_file(dumps([BASE]*501).encode(),'a.json')


@pytest.mark.parametrize('field,value',[('sku',''),('name',''),('quantity',-1),('quantity',1.5),('quantity',True),('price','NaN'),('price','Infinity'),('price','1.234'),('warrantyMonths',-1),('warrantyMonths',True),('warrantyMonths',1.5),('serialNumber',23)])
def test_electronics_validation(field,value):
    assert electronics(dict(BASE,**{field:value}))[1]


def test_grocery_validation():
    assert not groceries(dict(BASE,expiryDate='2028-02-29'))[1]
    assert groceries(dict(BASE,expiryDate='2027-02-29'))[1]
    assert groceries(dict(BASE,expiryDate='2027-2-01'))[1]
    assert groceries(dict(BASE,serialNumber='not groceries'))[1]


def test_preview_and_expected_versions(service):
    baseline(service)
    job = preview(service,[BASE,dict(BASE,sku='E200'),dict(BASE,sku='E300',quantity=-1)])
    assert job['previewCounts']=={'UNCHANGED':1,'CREATE':1,'INVALID':1,'UPDATE':0}
    assert len(service.inventory())==1
    assert service.plan(job['jobId'])[0]['expected']['version']==1
    assert service.plan(job['jobId'])[1]['expected'] is None
    updated=preview(service,[dict(BASE,quantity=100)])
    assert updated['previewCounts']['UPDATE']==1


def test_all_duplicates_are_invalid(service):
    job=preview(service,[BASE,dict(BASE,sku=' E100 ')])
    assert job['previewCounts']['INVALID']==2
    assert all('Duplicate' in p['errors'][0] for p in service.plan(job['jobId']))


def test_transactional_import_and_retry(service):
    job=preview(service,[BASE])
    job,_=execute(service,job,finish=False)
    original=service.inventory()[0]
    result=service.import_record(job['jobId'],job['attempt'],0)
    assert result['status']=='IMPORTED'
    assert service.inventory()[0]==original
    journal=service.store.query('journal',job['jobId'])
    assert len(journal)==1 and journal[0]['afterImage']==original
    assert isinstance(original['price'],Decimal)


def test_transaction_failure_does_not_write_inventory(service):
    with pytest.raises(Conflict):
        service.store.transact([('inventory',('demo-retail#electronics','E1'),BASE,None),('jobs',('bad','bad'),{'status':'X'},{'status':'does not exist'})])
    assert service.store.get('inventory',('demo-retail#electronics','E1')) is None


def test_concurrent_preview_conflict(service):
    baseline(service)
    job=preview(service,[dict(BASE,quantity=100)])
    old=service.inventory()[0]
    service.patch_inventory('electronics','E100',dict(version=old['version'],revision=old['revision'],quantity=9))
    job,results=execute(service,job)
    assert results[0]['status']=='CONFLICT' and job['status']=='PARTIAL'
    assert service.inventory()[0]['quantity']==9
    assert service.store.query('journal',job['jobId'])==[]


def test_two_jobs_create_same_sku(service):
    a,b=preview(service,[BASE]),preview(service,[BASE])
    _,ra=execute(service,a)
    _,rb=execute(service,b)
    assert ra[0]['status']=='IMPORTED' and rb[0]['status']=='CONFLICT'
    assert service.inventory()[0]['version']==1


def test_safe_rollback_and_legitimate_edit(service):
    baseline(service)
    job,_=execute(service,preview(service,[dict(BASE,quantity=100),dict(BASE,sku='E200'),dict(BASE,sku='E300',quantity=-1)]))
    old=next(p for p in service.inventory() if p['sku']=='E100')
    service.patch_inventory('electronics','E100',dict(version=old['version'],revision=old['revision'],quantity=98))
    immutable=service.store.query('journal',job['jobId'])
    job,results=rollback(service,job)
    assert job['rollbackCounts']['REVERSED']==1
    assert job['rollbackCounts']['CONFLICT']==1
    assert service.inventory()[0]['quantity']==98
    assert len(service.inventory())==1
    after=service.store.query('journal',job['jobId'])
    for before,new in zip(immutable,after):
        assert before['beforeImage']==new['beforeImage'] and before['afterImage']==new['afterImage']
    retried,results=rollback(service,job)
    assert retried['rollbackCounts']['ALREADY_REVERSED']==1
    assert retried['rollbackCounts']['CONFLICT']==1


def test_restore_increases_version_and_older_import_cannot_rollback(service):
    first=baseline(service)
    second,_=execute(service,preview(service,[dict(BASE,quantity=100)]))
    rollback(service,second)
    product=service.inventory()[0]
    assert product['quantity']==10 and product['version']==3
    first,results=rollback(service,first)
    assert results[0]['status']=='CONFLICT'
    assert service.inventory()[0]==product


def test_rollback_missing_resource_conflicts(service):
    job=baseline(service)
    item=service.inventory()[0]
    service.store.transact([('inventory',resource_key(service.tenant,'electronics','E100'),None,version_of(item))])
    job,results=rollback(service,job)
    assert results[0]['status']=='CONFLICT'


def test_delete_recreate_same_version_protected(service):
    first=baseline(service)
    item=service.inventory()[0]
    service.store.transact([('inventory',resource_key(service.tenant,'electronics','E100'),None,version_of(item))])
    baseline(service)
    _,results=rollback(service,first)
    assert results[0]['status']=='CONFLICT'


def test_partial_failure_remains_recoverable_and_stale_workers_fenced(service):
    job=preview(service,[BASE,dict(BASE,sku='E200')])
    job,_=service.claim(job['jobId'],'execute',job['planId'])
    service.import_record(job['jobId'],job['attempt'],0)
    service.fail(job['jobId'],job['attempt'],'execute')
    with pytest.raises(Conflict):service.import_record(job['jobId'],job['attempt'],1)
    job,_=rollback(service,job)
    assert job['rollbackCounts']['REVERSED']==1 and not service.inventory()


def test_partial_rollback_failure_retry(service):
    job,_=execute(service,preview(service,[BASE,dict(BASE,sku='E200')]))
    job,_=service.claim(job['jobId'],'rollback')
    service.rollback_record(job['jobId'],job['attempt'],'E100')
    service.fail(job['jobId'],job['attempt'],'rollback')
    job,_=rollback(service,job)
    assert job['rollbackCounts']['ALREADY_REVERSED']==1 and job['rollbackCounts']['REVERSED']==1
    assert not service.inventory()


def test_approval_and_duplicate_launch(service):
    job=preview(service,[BASE])
    with pytest.raises(Conflict):service.claim(job['jobId'],'execute','wrong checksum')
    first,claimed=service.claim(job['jobId'],'execute',job['planId'])
    second,claimed_again=service.claim(job['jobId'],'execute',job['planId'])
    assert claimed and not claimed_again and first['attempt']==second['attempt']


def test_patch_optimistic_concurrency(service):
    baseline(service)
    item=service.inventory()[0]
    body={'quantity':98,**version_of(item)}
    service.patch_inventory('electronics','E100',body)
    with pytest.raises(Conflict):service.patch_inventory('electronics','E100',body)


def test_preview_retry_and_plan_tamper(service):
    job=preview(service,[BASE])
    assert service.preview(job['jobId'],job['attempt'])['planId']==job['planId']
    service.store.put_object(job['planKey'],b'[]')
    with pytest.raises(Conflict):service.plan(job['jobId'])


def test_api_contract(service):
    status,data=route(service,'GET','/health')
    assert status==200
    with pytest.raises(Problem):route(service,'POST','/imports',{'inventoryType':'bad'})
    job=baseline(service)
    assert route(service,'GET',f'/imports/{job["jobId"]}/journal')[1]['items'][0]['importStatus']=='COMMITTED'
    assert route(service,'GET','/imports')[1]['items']
    assert route(service,'GET','/inventory',query={'inventoryType':'electronics'})[1]['items']
    with pytest.raises(Problem):route(service,'GET','/imports/missing')


def test_real_sample_scenario(service):
    seed(service,Path('samples/baseline-electronics.csv').read_bytes(),'baseline.csv','electronics')
    content=Path('samples/electronics.csv').read_bytes()
    job=service.create_job({'filename':'supplier.csv','size':len(content),'inventoryType':'electronics'})
    service.store.put_object(job['s3ObjectKey'],content)
    job,_=service.claim(job['jobId'],'preview')
    job=service.preview(job['jobId'],job['attempt'])
    assert job['previewCounts']=={'CREATE':1,'UPDATE':2,'UNCHANGED':1,'INVALID':3}
    job,_=execute(service,job)
    item=next(p for p in service.inventory() if p['sku']=='E100')
    service.patch_inventory('electronics','E100',dict(quantity=98,**version_of(item)))
    job,_=rollback(service,job)
    assert job['rollbackCounts']['REVERSED']==2 and job['rollbackCounts']['CONFLICT']==1
    assert {p['sku']:p['quantity'] for p in service.inventory()}=={'E100':98,'E200':24,'E300':15}


def test_parallel_retries_do_not_double_increment(tmp_path):
    # SQLite provides actual concurrent transactions; moto concurrency semantics
    # are not a substitute for deployed DynamoDB integration testing.
    from backend.ingestra.common.service import Service
    from backend.ingestra.common.store import LocalStore
    service=Service(LocalStore(tmp_path/'parallel.db'))
    job=preview(service,[BASE]);job,_=service.claim(job['jobId'],'execute',job['planId'])
    with ThreadPoolExecutor(max_workers=5) as pool:
        results=list(pool.map(lambda _:service.import_record(job['jobId'],job['attempt'],0),range(5)))
    assert all(r['status']=='IMPORTED' for r in results)
    assert service.inventory()[0]['version']==1 and len(service.store.query('journal',job['jobId']))==1


def test_worker_fence_is_checked_inside_transaction(service,monkeypatch):
    job=preview(service,[BASE])
    job,_=service.claim(job['jobId'],'execute',job['planId'])
    transact=service.store.transact
    interrupted=False
    def delayed(operations):
        nonlocal interrupted
        if operations[0][0]=='check:jobs' and not interrupted:
            interrupted=True
            service.fail(job['jobId'],job['attempt'],'execute')
        return transact(operations)
    monkeypatch.setattr(service.store,'transact',delayed)
    assert service.import_record(job['jobId'],job['attempt'],0)['status']=='CONFLICT'
    assert not service.inventory() and not service.store.query('journal',job['jobId'])
