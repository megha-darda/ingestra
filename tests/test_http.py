"""Actual HTTP upload, asynchronous preview/import, PATCH and rollback.

Uses a temporary SQLite database and ephemeral loopback server; never AWS.
"""
import json
import threading
import time
from pathlib import Path
from urllib.request import Request, urlopen
from http.server import ThreadingHTTPServer
from backend.ingestra.local import Handler


def test_end_to_end_http(tmp_path, monkeypatch):
    monkeypatch.delenv('BUCKET', raising=False)
    monkeypatch.setenv('LOCAL_DB', str(tmp_path/'http.db'))
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True)
    thread.start()
    base=f'http://127.0.0.1:{server.server_port}'
    def request(path,method='GET',body=None):
        raw=json.dumps(body).encode() if body is not None else None
        with urlopen(Request(base+path,data=raw,method=method,headers={'Content-Type':'application/json','Origin':'http://localhost:5173'}),timeout=10) as response:
            assert response.headers['Access-Control-Allow-Origin']=='http://localhost:5173'
            return json.load(response)
    def wait(jid):
        deadline=time.monotonic()+10
        while time.monotonic()<deadline:
            job=request('/imports/'+jid)
            if job['status'] not in ('PREVIEWING','IMPORTING','ROLLING_BACK'):return job
            time.sleep(.03)
        raise AssertionError('Workflow timed out')
    def import_file(filename):
        content=Path('samples',filename).read_bytes()
        result=request('/imports','POST',{'filename':filename,'size':len(content),'inventoryType':'electronics'})
        with urlopen(Request(result['uploadUrl'],data=content,method='PUT',headers=result['uploadHeaders'])) as response:
            assert response.status==200
        jid=result['job']['jobId']
        request(f'/imports/{jid}/preview','POST',{})
        job=wait(jid)
        assert job['status']=='PREVIEW_READY'
        request(f'/imports/{jid}/execute','POST',{'planId':job['planId']})
        return wait(jid)
    try:
        assert request('/health')['mode']=='local-sqlite'
        import_file('baseline-electronics.csv')
        job=import_file('electronics.csv')
        assert job['previewCounts']==dict(CREATE=1,UPDATE=2,UNCHANGED=1,INVALID=3)
        assert job['executionCounts']['IMPORTED']==3
        item=next(i for i in request('/inventory')['items'] if i['sku']=='E100')
        request('/inventory/electronics/E100','PATCH',{'quantity':98,'version':item['version'],'revision':item['revision']})
        jid=job['jobId']
        request(f'/imports/{jid}/rollback','POST',{})
        result=wait(jid)
        assert result['rollbackCounts']==dict(REVERSED=2,CONFLICT=1,ALREADY_REVERSED=0,FAILED=0)
        assert {i['sku']:i['quantity'] for i in request('/inventory')['items']}==dict(E100=98,E200=24,E300=15)
        report=request(f'/imports/{jid}/rollback')
        assert len(report['journal'])==3 and len(report['items'])==3
        request(f'/imports/{jid}/rollback','POST',{})
        assert wait(jid)['rollbackCounts']['ALREADY_REVERSED']==2
    finally:
        server.shutdown();server.server_close();thread.join(timeout=5)
