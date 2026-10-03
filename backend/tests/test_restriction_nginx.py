"""Real nginx rewrites plus real HTTP password proxy, isolated from production."""
import json
import shutil
import socket
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest
from test_restrictions import client  # noqa: F401 - shared isolated API fixture

from app.main import app


@pytest.mark.parametrize('login_path', ['/emby/Users/AuthenticateByName', '/users/authenticatebyname', '/emby/Users/u1/Authenticate'])
def test_real_nginx_login_chain(client, tmp_path, monkeypatch, login_path):  # noqa: F811
    nginx = shutil.which('nginx')
    if not nginx:
        pytest.skip('nginx required')
    requests = []

    class Backend(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            self.send_response(200)
            self.end_headers()

        def do_POST(self):
            raw = self.rfile.read(int(self.headers.get('Content-Length', 0)))
            if self.path.startswith('/api/access/emby-login'):
                response = client.post(self.path, content=raw, headers=dict(self.headers))
                self.send_response(response.status_code)
                self.end_headers()
                self.wfile.write(response.content)
                return
            assert self.path.startswith('/emby/Users/')
            payload = json.loads(raw)
            auth = self.headers.get('X-Emby-Authorization', '')
            requests.append((self.path, dict(self.headers), payload))
            if payload['Pw'] != 'correct':
                self.send_response(401)
                self.end_headers()
                return
            data = {'User': {'Id': 'u1'}, 'AccessToken': 'test-secret-token', 'SessionInfo': {
                'Id': 's', 'UserId': 'u1', 'Client': 'Emby Web' if 'Emby Web' in auth else 'Hills'}}
            self.send_response(200)
            self.end_headers()
            self.wfile.write(json.dumps(data).encode())

    backend = ThreadingHTTPServer(('127.0.0.1', 0), Backend)
    thread = threading.Thread(target=backend.serve_forever, daemon=True)
    thread.start()
    address = f'127.0.0.1:{backend.server_port}'
    monkeypatch.setattr(app.state.emby, '_conn', lambda: ('http://' + address, {'X-Emby-Token': 'admin-secret'}, 10, True), raising=False)
    monkeypatch.setattr(app.state.emby, '_client', lambda *a: httpx.AsyncClient(timeout=10, trust_env=False), raising=False)
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    snippet = (Path(__file__).resolve().parents[2] / 'deploy/nginx/playback-admission.conf').read_text()
    config = tmp_path / 'nginx.conf'
    config.write_text(f'''daemon off; master_process off;
pid {tmp_path}/nginx.pid;
error_log {tmp_path}/error.log;
events {{ worker_connections 64; }}
http {{ access_log off;
upstream deck_admission_backend {{ server {address}; }}
upstream emby_playback_origin {{ server {address}; }}
server {{ listen 127.0.0.1:{port}; {snippet}
location / {{ proxy_pass http://emby_playback_origin; }} }} }}''')
    subprocess.run([nginx, '-t', '-p', str(tmp_path), '-c', str(config)], check=True, capture_output=True)
    proc = subprocess.Popen([nginx, '-p', str(tmp_path), '-c', str(config)])
    try:
        with httpx.Client(base_url=f'http://127.0.0.1:{port}', trust_env=False) as edge:
            for _ in range(100):
                try:
                    edge.get('/health')
                    break
                except httpx.ConnectError:
                    time.sleep(.02)
            web = {'X-Emby-Authorization': 'MediaBrowser Client="Emby Web", DeviceId="d", Token="forged"'}
            assert edge.post(login_path, headers=web, json={'Username': 'demo', 'Pw': 'wrong'}).status_code == 401
            assert not app.state.restrictions.events()
            native = {'X-Emby-Authorization': 'MediaBrowser Client="Hills", DeviceId="d"'}
            response = edge.post(login_path, headers=native, json={'Username': 'demo', 'Pw': 'correct'})
            assert response.status_code == 200 and response.json()['AccessToken'] == 'test-secret-token'
            response = edge.post(login_path, headers=web, json={'Username': 'demo', 'Pw': 'correct'})
            assert response.status_code == 403 and 'test-secret-token' not in response.text
            assert len(app.state.restrictions.events()) == 1
            app.state.emby.list_users.return_value[0]['Policy']['IsAdministrator'] = True
            response = edge.post(login_path, headers=web, json={'Username': 'demo', 'Pw': 'correct'})
            assert response.status_code == 200
            assert len(requests) == 4
            assert all('forged' not in str(r[1]) and 'admin-secret' not in str(r[1]) for r in requests)
    finally:
        proc.terminate()
        proc.wait(timeout=10)
        backend.shutdown()
        backend.server_close()
        thread.join(timeout=10)
