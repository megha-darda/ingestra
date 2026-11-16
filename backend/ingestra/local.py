"""Real local API + asynchronous workflow runner; no AWS credentials required."""
import argparse
import logging
import os
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse
from .common.model import MAX_BYTES, Problem, dumps, loads
from .handler import get_service, route

workflows = ThreadPoolExecutor(max_workers=4, thread_name_prefix='workflow')


def run_workflow(payload):
    service = get_service()
    jid, attempt, action = payload['jobId'], payload['attempt'], payload['action']
    try:
        if action == 'preview':
            service.preview(jid, attempt)
        else:
            refs = service.prepare(jid, attempt, action)['refs']
            def process(ref):
                try:
                    service.process(jid, attempt, action, ref)
                except Exception:
                    logging.exception('Record failed')
                    service.record_failure(jid, attempt, action, ref)
            with ThreadPoolExecutor(max_workers=5) as pool:
                list(pool.map(process, refs))
            service.finish(jid, attempt, action, refs)
    except Exception as exc:
        logging.exception('Workflow failed')
        service.fail(jid, attempt, action)
        if isinstance(exc, Problem):
            job = service.job(jid)
            job['errorMessage'] = str(exc)
            service.save_job(job, {'attempt': attempt, 'status': job['status']})


class Handler(BaseHTTPRequestHandler):
    def respond(self, status, data):
        content = dumps(data).encode()
        self.send_response(status)
        origin = os.environ.get('FRONTEND_ORIGIN', 'http://localhost:5173')
        if self.headers.get('Origin') == origin:
            self.send_header('Access-Control-Allow-Origin', origin)
        self.send_header('Vary', 'Origin')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type,x-amz-server-side-encryption')
        self.send_header('Access-Control-Allow-Methods', 'GET,POST,PATCH,PUT,OPTIONS')
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def do_OPTIONS(self):
        self.respond(200, {})

    def handle_api(self):
        try:
            parsed = urlparse(self.path)
            size = int(self.headers.get('Content-Length', '0'))
            if size < 0 or size > MAX_BYTES:
                raise Problem('File exceeds 2 MB.', 413)
            raw = self.rfile.read(size)
            service = get_service()
            if self.command == 'PUT' and parsed.path.startswith('/local/uploads/'):
                job = service.job(parsed.path.rsplit('/', 1)[1])
                if job['status'] != 'UPLOADING' or len(raw) != job['size']:
                    raise Problem('Upload size or state does not match the request.')
                service.store.put_object(job['s3ObjectKey'], raw)
                self.respond(200, {'uploaded': True})
                return
            body = loads(raw) if raw else {}
            status, data = route(service, self.command, parsed.path, body,
                                 {k: v[0] for k, v in parse_qs(parsed.query).items()},
                                 local_start=lambda p: workflows.submit(run_workflow, p), base_url=f'http://127.0.0.1:{self.server.server_port}')
            self.respond(status, data)
        except (ValueError, UnicodeError):
            self.respond(400, {'error': {'message': 'Invalid request body.'}})
        except Problem as exc:
            self.respond(exc.status, {'error': {'message': str(exc)}})
        except Exception:
            logging.exception('Request failed')
            self.respond(500, {'error': {'message': 'Request failed. See local server log.'}})

    do_GET = do_POST = do_PATCH = do_PUT = handle_api


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--seed', action='store_true', help='Seed demo products only when inventory is empty.')
    parser.add_argument('--port', type=int, default=3001)
    args = parser.parse_args()
    if args.seed:
        service = get_service()
        if not service.inventory():
            from pathlib import Path
            from .demo import seed
            seed(service, Path('samples/baseline-electronics.csv').read_bytes(), 'baseline-electronics.csv', 'electronics')
            seed(service, Path('samples/baseline-groceries.csv').read_bytes(), 'baseline-groceries.csv', 'groceries')
    print(f'Ingestra local SQLite API: http://127.0.0.1:{args.port} (not AWS Step Functions)', flush=True)
    server = ThreadingHTTPServer(('127.0.0.1', args.port), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        workflows.shutdown(wait=True)


if __name__ == '__main__':
    main()
