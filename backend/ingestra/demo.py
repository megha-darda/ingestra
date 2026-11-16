"""Seed by the same preview, approval, journal and conditional import path."""
from .common.model import dumps


def seed(service, content, filename, kind):
    job = service.create_job({'inventoryType': kind, 'filename': filename, 'size': len(content)})
    jid = job['jobId']
    service.store.put_object(job['s3ObjectKey'], content)
    job, _ = service.claim(jid, 'preview')
    job = service.preview(jid, job['attempt'])
    job, _ = service.claim(jid, 'execute', job['planId'])
    refs = service.prepare(jid, job['attempt'], 'execute')['refs']
    for ref in refs:
        service.import_record(jid, job['attempt'], ref)
    service.finish(jid, job['attempt'], 'execute', refs)
    return service.job(jid)
