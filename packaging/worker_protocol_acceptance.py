"""Exercise the installed windowed executable's worker pipes using loopback only."""
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import threading


def verify_workers(command):
    from shaq_daily_oracle.model_execution import run_model_process
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_POST(self):
            self.rfile.read(int(self.headers['Content-Length']))
            body = b'{"acceptance":"worker-pipe"}'
            self.send_response(200)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        cases = [('--model-http-worker', {'operation': 'http-json', 'payload': {
            'url': f'http://127.0.0.1:{server.server_port}/', 'headers': {}, 'payload': {}, 'timeout': 5}},
            {'result': {'acceptance': 'worker-pipe'}}),
            ('--collection-worker', {'operation': 'ping', 'payload': {}},
             {'result': {'worker': 'yahoo-collection', 'protocol_version': 1}})]
        for flag, request, expected in cases:
            completed = run_model_process([*command, flag], input=json.dumps(request), capture_output=True,
                text=True, encoding='utf-8', timeout=20,
                env={**os.environ, 'NO_PROXY': '127.0.0.1', 'no_proxy': '127.0.0.1'})
            if completed.returncode != 0 or json.loads(completed.stdout) != expected:
                raise RuntimeError(flag + ' did not complete its exact pipe protocol')
        return {'model_http_worker': True, 'collection_worker': True}
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('executable', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    try:
        result = {'status': 'passed', 'checks': verify_workers([str(args.executable)])}
    except Exception as exc:
        result = {'status': 'failed', 'error': str(exc)}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2), encoding='utf-8')
    return 0 if result['status'] == 'passed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
