import hashlib
import os
from collections import Counter
from decimal import Decimal
from ..common.model import Conflict, Problem, MAX_BYTES, TYPES, dumps, loads, now, resource_key, token
from ..validators.common import duplicate_skus, parse_file
from ..validators import electronics, groceries

BUSINESS_FIELDS = {'sku', 'name', 'quantity', 'price', 'category', 'serialNumber', 'warrantyMonths', 'expiryDate'}


def version_of(item):
    return {'version': item['version'], 'revision': item['revision']}


class Service:
    def __init__(self, store, tenant=None):
        self.store = store
        self.tenant = tenant or os.environ.get('DEMO_TENANT', 'demo-retail')

    def job(self, job_id):
        job = self.store.get('jobs', (self.tenant, job_id))
        if not job:
            raise Problem('Import not found.', 404)
        return job

    def save_job(self, job, expected):
        job = dict(job, updatedAt=now())
        self.store.transact([('jobs', (self.tenant, job['jobId']), job, expected)])
        return job

    def create_job(self, request):
        kind, filename, size = request.get('inventoryType'), request.get('filename'), request.get('size')
        if kind not in TYPES:
            raise Problem('Select electronics or groceries.')
        if not isinstance(filename, str) or len(filename) > 200 or not filename.lower().endswith(('.csv', '.json')) or '/' in filename or '\\' in filename:
            raise Problem('Provide a CSV or JSON filename without directories.')
        if isinstance(size, bool) or not isinstance(size, int) or size < 1:
            raise Problem('Provide a file size of at least 1 byte.')
        if size > MAX_BYTES:
            raise Problem('File exceeds the 2 MiB upload limit.', 413)
        jid = token()
        job = dict(jobId=jid, tenantId=self.tenant, inventoryType=kind, filename=filename, size=size,
                   s3ObjectKey=f'uploads/{self.tenant}/{jid}/{filename}', status='UPLOADING', createdAt=now())
        return self.save_job(job, None)

    def list_jobs(self):
        return sorted(self.store.query('jobs', self.tenant), key=lambda j: j['createdAt'], reverse=True)

    def inventory(self, kind=None):
        if kind and kind not in TYPES:
            raise Problem('Unknown inventory type.')
        return [item for k in ([kind] if kind else TYPES) for item in self.store.query('inventory', f'{self.tenant}#{k}')]

    def claim(self, job_id, action, plan_id=None):
        job = self.job(job_id)
        allowed = {'preview': ('UPLOADING', 'PREVIEW_FAILED'), 'execute': ('PREVIEW_READY',),
                   'rollback': ('COMPLETED', 'PARTIAL', 'FAILED', 'ROLLED_BACK', 'ROLLBACK_PARTIAL', 'ROLLBACK_FAILED')}
        running = {'preview': 'PREVIEWING', 'execute': 'IMPORTING', 'rollback': 'ROLLING_BACK'}[action]
        if action == 'execute' and (not plan_id or plan_id != job.get('planId')):
            raise Conflict('Approval must include the exact preview planId.')
        if job['status'] == running:
            return job, False
        if job['status'] not in allowed[action]:
            raise Conflict(f'Cannot {action} an import in {job["status"]}.')
        if action == 'rollback' and not job.get('approvedPlanId'):
            raise Conflict('Only an executed import can be rolled back.')
        old_status = job['status']
        attempt = token()
        job.update(status=running, attempt=attempt, action=action, errorMessage=None)
        if action == 'execute':
            job['approvedPlanId'] = plan_id
        return self.save_job(job, {'status': old_status}), True

    def preview(self, job_id, attempt):
        job = self.job(job_id)
        if job.get('attempt') == attempt and job['status'] == 'PREVIEW_READY':
            return job
        self.require_attempt(job, attempt, 'PREVIEWING')
        rows = parse_file(self.store.get_object(job['s3ObjectKey']), job['filename'])
        duplicates = duplicate_skus(rows)
        validator = electronics.validate if job['inventoryType'] == 'electronics' else groceries.validate
        plan = []
        for index, row in enumerate(rows):
            data, errors = validator(row)
            if data.get('sku') in duplicates:
                errors.append('Duplicate SKU in this file; all occurrences are invalid.')
            before = self.store.get('inventory', resource_key(self.tenant, job['inventoryType'], data['sku'])) if not errors else None
            operation = 'INVALID' if errors else 'CREATE' if before is None else 'UNCHANGED' if {k: v for k, v in before.items() if k in BUSINESS_FIELDS} == data else 'UPDATE'
            plan.append(dict(index=index, row=index + 1, operation=operation, data=data, errors=errors,
                             beforeImage=before, expected=version_of(before) if before else None))
        content = dumps(plan).encode()
        digest = hashlib.sha256(content).hexdigest()
        key = f'plans/{self.tenant}/{job_id}/{digest}.json'
        # A unique plan key is never overwritten, including after upload URL reuse.
        self.store.put_object(key, content, immutable=True)
        counts = dict.fromkeys(('CREATE', 'UPDATE', 'UNCHANGED', 'INVALID'), 0)
        counts.update(Counter(p['operation'] for p in plan))
        job.update(planKey=key, planId=digest, totalRecords=len(plan), previewCounts=counts, status='PREVIEW_READY')
        return self.save_job(job, {'attempt': attempt, 'status': 'PREVIEWING'})

    def plan(self, job_id, approved=False):
        job = self.job(job_id)
        if not job.get('planKey'):
            raise Conflict('Preview is not ready.')
        content = self.store.get_object(job['planKey'])
        digest = hashlib.sha256(content).hexdigest()
        if digest != job['planId'] or (approved and digest != job.get('approvedPlanId')):
            raise Conflict('Approved plan checksum mismatch.')
        return loads(content)

    @staticmethod
    def require_attempt(job, attempt, status):
        if job.get('attempt') != attempt or job['status'] != status:
            raise Conflict('Stale workflow attempt.')

    def prepare(self, job_id, attempt, action):
        job = self.job(job_id)
        self.require_attempt(job, attempt, 'IMPORTING' if action == 'execute' else 'ROLLING_BACK')
        refs = [p['index'] for p in self.plan(job_id, True)] if action == 'execute' else [j['resourceKey'] for j in self.store.query('journal', job_id)]
        return dict(jobId=job_id, attempt=attempt, action=action, refs=refs)

    def outcome(self, job_id, attempt, ref, result):
        key = hashlib.sha256(str(ref).encode()).hexdigest()
        self.store.put_object(f'outcomes/{job_id}/{attempt}/{key}.json', dumps(result).encode())
        return result

    def import_record(self, job_id, attempt, index):
        job = self.job(job_id)
        self.require_attempt(job, attempt, 'IMPORTING')
        entry = self.plan(job_id, True)[int(index)]
        result = dict(row=entry['row'], sku=entry['data'].get('sku', ''), status=entry['operation'])
        if entry['operation'] not in ('CREATE', 'UPDATE'):
            result['message'] = '; '.join(entry['errors'])
            return self.outcome(job_id, attempt, index, result)
        sku = entry['data']['sku']
        journal_key = (job_id, sku)
        existing = self.store.get('journal', journal_key)
        if existing:
            return self.outcome(job_id, attempt, index, dict(result, status='IMPORTED', message='Previously committed; retry did not write again.'))
        before = entry['beforeImage']
        after = dict(entry['data'], tenantId=self.tenant, inventoryType=job['inventoryType'],
                     version=int(before['version']) + 1 if before else 1, revision=token(),
                     createdAt=before['createdAt'] if before else now(), updatedAt=now())
        journal = dict(jobId=job_id, resourceKey=sku, operation=entry['operation'],
                       operationId=hashlib.sha256(f'{job_id}:{sku}'.encode()).hexdigest(),
                       tenantId=self.tenant, inventoryType=job['inventoryType'], beforeImage=before, afterImage=after,
                       beforeVersion=before['version'] if before else None, afterVersion=after['version'],
                       importStatus='COMMITTED', rollbackStatus='PENDING', createdAt=now())
        try:
            self.store.transact([('check:jobs', (self.tenant, job_id), None, {'status': 'IMPORTING', 'attempt': attempt}),
                                 ('inventory', resource_key(self.tenant, job['inventoryType'], sku), after, entry['expected']),
                                 ('journal', journal_key, journal, None)])
            result['status'] = 'IMPORTED'
        except Conflict:
            committed = self.store.get('journal', journal_key)
            result.update(status='IMPORTED' if committed else 'CONFLICT', message='Resource changed after preview.' if not committed else 'Already committed.')
        return self.outcome(job_id, attempt, index, result)

    def rollback_record(self, job_id, attempt, sku):
        job = self.job(job_id)
        self.require_attempt(job, attempt, 'ROLLING_BACK')
        journal = self.store.get('journal', (job_id, sku))
        if not journal or journal.get('importStatus') != 'COMMITTED':
            raise Conflict('No committed journal entry.')
        if journal['rollbackStatus'] == 'REVERSED':
            return self.outcome(job_id, attempt, sku, dict(sku=sku, status='ALREADY_REVERSED'))
        before, after = journal['beforeImage'], journal['afterImage']
        restored = dict(before, version=int(after['version']) + 1, revision=token(), updatedAt=now()) if before else None
        reversed_journal = dict(journal, rollbackStatus='REVERSED', rolledBackAt=now(), rollbackAttempt=attempt)
        result = dict(sku=sku, status='REVERSED')
        try:
            self.store.transact([('check:jobs', (self.tenant, job_id), None, {'status': 'ROLLING_BACK', 'attempt': attempt}),
                                 ('inventory', resource_key(self.tenant, job['inventoryType'], sku), restored, version_of(after)),
                                 ('journal', (job_id, sku), reversed_journal, {'rollbackStatus': journal['rollbackStatus']})])
        except Conflict:
            latest = self.store.get('journal', (job_id, sku))
            if latest['rollbackStatus'] == 'REVERSED':
                result['status'] = 'ALREADY_REVERSED'
            else:
                result.update(status='CONFLICT', message='Resource changed after import or is missing. Manual reconciliation required.')
                try:
                    self.store.transact([('journal', (job_id, sku), dict(latest, rollbackStatus='CONFLICT', rollbackMessage=result['message']), {'rollbackStatus': latest['rollbackStatus']})])
                except Conflict:
                    pass
        return self.outcome(job_id, attempt, sku, result)

    def process(self, job_id, attempt, action, ref):
        if action == 'execute':
            return self.import_record(job_id, attempt, ref)
        return self.rollback_record(job_id, attempt, ref)

    def record_failure(self, job_id, attempt, action, ref):
        """Reconcile a lost task response with the authoritative commit journal."""
        result = {'status': 'FAILED', 'resource': ref, 'message': 'Record failed after retries; inspect workflow logs.'}
        if action == 'execute':
            row = self.plan(job_id, True)[int(ref)]
            sku = row['data'].get('sku')
            journal = self.store.get('journal', (job_id, sku)) if sku else None
            result.update(sku=sku, row=row['row'])
            if journal and journal['importStatus'] == 'COMMITTED':
                result.update(status='IMPORTED', message='Commit confirmed from the atomic journal after a lost task response.')
        else:
            journal = self.store.get('journal', (job_id, ref))
            result['sku'] = ref
            if journal and journal['rollbackStatus'] == 'REVERSED':
                result.update(status='REVERSED' if journal.get('rollbackAttempt') == attempt else 'ALREADY_REVERSED', message='Recovery commit confirmed from the journal.')
            elif journal and journal['rollbackStatus'] == 'CONFLICT':
                result.update(status='CONFLICT', message=journal.get('rollbackMessage'))
        return self.outcome(job_id, attempt, ref, result)

    def finish(self, job_id, attempt, action, refs):
        job = self.job(job_id)
        status = 'IMPORTING' if action == 'execute' else 'ROLLING_BACK'
        self.require_attempt(job, attempt, status)
        results = []
        for ref in refs:
            key = hashlib.sha256(str(ref).encode()).hexdigest()
            results.append(loads(self.store.get_object(f'outcomes/{job_id}/{attempt}/{key}.json')))
        counts = dict(Counter(r['status'] for r in results))
        report_key = f'reports/{job_id}/{attempt}.json'
        self.store.put_object(report_key, dumps(results).encode())
        if action == 'execute':
            job.update(executionCounts=counts, executionReportKey=report_key,
                       status='PARTIAL' if any(counts.get(k) for k in ('CONFLICT', 'FAILED', 'INVALID')) else 'COMPLETED')
        else:
            job.update(rollbackCounts={k: counts.get(k, 0) for k in ('REVERSED', 'CONFLICT', 'ALREADY_REVERSED', 'FAILED')}, rollbackReportKey=report_key,
                       status='ROLLBACK_PARTIAL' if counts.get('CONFLICT') or counts.get('FAILED') else 'ROLLED_BACK')
        return self.save_job(job, {'attempt': attempt, 'status': status})

    def fail(self, job_id, attempt, action):
        job = self.job(job_id)
        if job.get('attempt') != attempt:
            return job
        running = {'preview': 'PREVIEWING', 'execute': 'IMPORTING', 'rollback': 'ROLLING_BACK'}[action]
        if job['status'] != running:
            return job
        job.update(status={'preview': 'PREVIEW_FAILED', 'execute': 'FAILED', 'rollback': 'ROLLBACK_FAILED'}[action],
                   errorMessage='Workflow failed. Review execution logs. Committed records remain eligible for recovery.')
        return self.save_job(job, {'attempt': attempt, 'status': running})

    def patch_inventory(self, kind, sku, request):
        if kind not in TYPES:
            raise Problem('Unknown inventory type.')
        version = request.get('version')
        if isinstance(version, bool) or not isinstance(version, (int, Decimal)) or version < 1 or version != int(version) or not isinstance(request.get('revision'), str):
            raise Problem('Provide the current integer version and revision token.')
        old = self.store.get('inventory', resource_key(self.tenant, kind, sku))
        if not old:
            raise Problem('Product not found.', 404)
        if request.get('version') != old['version'] or request.get('revision') != old['revision']:
            raise Conflict()
        if set(request) - BUSINESS_FIELDS - {'version', 'revision'} or ('sku' in request and request['sku'] != sku):
            raise Problem('Only product fields may be edited; SKU cannot change.')
        merged = {k: v for k, v in old.items() if k in BUSINESS_FIELDS}
        merged.update({k: v for k, v in request.items() if k in BUSINESS_FIELDS})
        data, errors = (electronics.validate if kind == 'electronics' else groceries.validate)(merged)
        if errors:
            raise Problem('; '.join(errors))
        new = dict(old, **data)
        new.update(version=int(old['version']) + 1, revision=token(), updatedAt=now())
        self.store.transact([('inventory', resource_key(self.tenant, kind, sku), new, version_of(old))])
        return new
