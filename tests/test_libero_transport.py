import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import subprocess
from threading import Thread
from types import SimpleNamespace

import pytest

from portable.libero_transport import public_recipe


@pytest.mark.parametrize('corrupt', (False, True))
def test_transient_http_failure_retries_and_preserves_native_checksum(tmp_path, corrupt):
    requests = []
    expected = b'pinned public asset fixture'

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            requests.append(self.path)
            body = b'wrong content' if corrupt else expected
            self.send_response(503 if len(requests) == 1 else 200)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    worker = Thread(target=server.serve_forever)
    worker.start()
    target = tmp_path/'asset'

    class Installation:
        root = tmp_path

        def run(self, *argv, **kwargs):
            return subprocess.run([str(item) for item in argv], check=True, capture_output=True, timeout=15)

    def native_assets(ctx):
        assert ctx.root == tmp_path
        ctx.run('curl', '--fail', '--silent', '--output', target,
                f'http://127.0.0.1:{server.server_port}/pinned-revision/asset')
        if hashlib.sha256(target.read_bytes()).digest() != hashlib.sha256(expected).digest():
            raise ValueError('native checksum mismatch')

    recipe = public_recipe(SimpleNamespace(SPEC={'data_commit': 'fixed-revision'}, assets=native_assets))
    try:
        if corrupt:
            with pytest.raises(ValueError, match='native checksum mismatch'):
                recipe.assets(Installation())
        else:
            recipe.assets(Installation())
            assert target.read_bytes() == expected
        assert requests == ['/pinned-revision/asset'] * 2
        assert recipe.SPEC == {'data_commit': 'fixed-revision'}
    finally:
        server.shutdown()
        server.server_close()
        worker.join()
