"""MCP_ABAP_Explorer: Python diretto o Ollama locale con tool MCP.

Installazione: python -m pip install -r requirements.txt
Oppure: python -m pip install "mcp>=1.26,<2" "anyio>=4.6,<5" "httpx>=0.28,<1" "magika>=0.6,<2"
Avvio: python app.py --port 8036
"""

from __future__ import annotations

import argparse
import json
import logging
import secrets
import socket
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import anyio

from mcp_engine import ENDPOINT, ExplorerError, detector, export_response, invoke_tool
from llm_engine import list_local_models, run_local_llm

BASE_DIR = Path(__file__).resolve().parent
TOKEN = secrets.token_urlsafe(32)
RESULTS: dict[str, tuple[float, dict]] = {}
RESULT_LOCK = threading.Lock()
SEARCH_LOCK = threading.Lock()
DOWNLOADS: dict[str, str] = {}
MIME_TYPES = {'json': 'application/json', 'txt': 'text/plain', 'md': 'text/markdown'}


def store_result(snapshot: dict) -> str:
    with RESULT_LOCK:
        now = time.monotonic()
        for key in list(RESULTS):
            if RESULTS[key][0] < now:
                del RESULTS[key]
        while len(RESULTS) >= 16:
            del RESULTS[next(iter(RESULTS))]
        result_id = secrets.token_urlsafe(24)
        RESULTS[result_id] = (now + 3600, snapshot)
        return result_id


class LocalServer(ThreadingHTTPServer):
    allow_reuse_address = False
    allow_reuse_port = False
    daemon_threads = True

    def server_bind(self):
        if hasattr(socket, 'SO_EXCLUSIVEADDRUSE'):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


class Handler(BaseHTTPRequestHandler):
    def setup(self):
        super().setup()
        self.connection.settimeout(360)

    def reply(self, status, data, content_type='application/json; charset=utf-8', filename=None):
        if isinstance(data, dict):
            data = json.dumps(data, ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        if filename:
            self.send_header('Content-Disposition', f'attachment; filename="{filename}"')
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def trusted_host(self):
        return self.headers.get('Host') in {
            f'127.0.0.1:{self.server.server_port}', f'localhost:{self.server.server_port}',
        }

    def do_GET(self):
        if not self.trusted_host():
            return self.reply(403, {'error': 'Host non consentito.'})
        if self.path == '/':
            page = (BASE_DIR / 'index.html').read_text(encoding='utf-8').replace('__TOKEN__', TOKEN)
            return self.reply(200, page.encode('utf-8'), 'text/html; charset=utf-8')
        if self.path == '/api/health':
            return self.reply(200, {'ready': True, 'endpoint': ENDPOINT, 'port': self.server.server_port})
        if self.path == '/api/models':
            try:
                return self.reply(200, {'provider': 'Ollama locale', 'models': anyio.run(list_local_models)})
            except ExplorerError as error:
                return self.reply(503, {'error': str(error)})
        assets = {'/app.js': 'text/javascript', '/style.css': 'text/css', '/lucide.min.js': 'text/javascript'}
        if self.path in assets:
            return self.reply(200, (BASE_DIR / self.path[1:]).read_bytes(), assets[self.path])
        if self.path.startswith('/api/download/'):
            filename = self.path.removeprefix('/api/download/')
            with RESULT_LOCK:
                mime_type = DOWNLOADS.get(filename)
            if mime_type is None or not (BASE_DIR / filename).is_file():
                return self.reply(404, {'error': 'File non disponibile in questa sessione.'})
            return self.reply(200, (BASE_DIR / filename).read_bytes(), mime_type + '; charset=utf-8', filename)
        self.reply(404, {'error': 'Risorsa non trovata.'})

    def read_json(self):
        if self.headers.get('Transfer-Encoding'):
            raise ExplorerError('Trasferimento chunked non supportato.')
        try:
            length = int(self.headers.get('Content-Length', '0'))
        except ValueError as error:
            raise ExplorerError('Dimensione richiesta non valida.') from error
        if not 0 < length <= 320 * 1024:
            raise ExplorerError('La richiesta deve contenere al massimo 320 KB.')
        if self.headers.get('Content-Type', '').split(';')[0].strip() != 'application/json':
            raise ExplorerError('Content-Type richiesto: application/json.')
        try:
            payload = self.rfile.read(length)
            if len(payload) != length:
                raise ExplorerError('Richiesta incompleta.')
            data = json.loads(payload.decode('utf-8'))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ExplorerError('Corpo JSON UTF-8 non valido.') from error
        if not isinstance(data, dict):
            raise ExplorerError('La richiesta JSON deve essere un oggetto.')
        return data

    def do_POST(self):
        supplied_token = self.headers.get('X-App-Token', '').encode('utf-8')
        if not self.trusted_host() or not secrets.compare_digest(supplied_token, TOKEN.encode('ascii')):
            return self.reply(403, {'error': 'Richiesta non autorizzata. Ricarica la pagina.'})
        if self.path not in {'/api/invoke', '/api/search', '/api/export'}:
            return self.reply(404, {'error': 'Risorsa non trovata.'})
        acquired = False
        try:
            data = self.read_json()
            if self.path in {'/api/invoke', '/api/search'}:
                acquired = SEARCH_LOCK.acquire(blocking=False)
                if not acquired:
                    return self.reply(429, {'error': 'Una richiesta MCP e gia in corso. Attendi il completamento.'})
                mode = data.get('mode', 'python')
                if mode == 'llm':
                    snapshot = anyio.run(run_local_llm, data.get('model'), data.get('text'))
                elif mode == 'python':
                    snapshot = anyio.run(
                        invoke_tool, data.get('tool', 'search'),
                        data.get('text', data.get('query')), data.get('count', 3),
                    )
                else:
                    raise ExplorerError('Modalita non supportata. Scegli Python oppure Ollama locale.')
                return self.reply(200, {
                    'result_id': store_result(snapshot), 'raw_text': snapshot['raw_text'],
                    'created_at': snapshot['created_at'], 'response': snapshot['response'],
                    'tool': snapshot.get('tool', 'search'), 'arguments': snapshot.get('arguments', {}),
                    'query': snapshot['query'], 'optimized_query': snapshot.get('optimized_query', snapshot['query']),
                    'attempts': snapshot.get('attempts', []), 'search_log': snapshot.get('search_log', []),
                    'mode': snapshot.get('mode', 'python'), 'model': snapshot.get('model'),
                    'mcp_raw_text': snapshot.get('mcp_raw_text', ''),
                })
            result_id = data.get('result_id')
            if not isinstance(result_id, str):
                raise ExplorerError('Interroga un tool prima di esportare.')
            with RESULT_LOCK:
                stored = RESULTS.get(result_id)
            if stored is None or stored[0] < time.monotonic():
                raise ExplorerError('Risposta scaduta o non disponibile. Ripeti la richiesta.')
            format_name = data.get('format')
            if not isinstance(format_name, str) or format_name not in MIME_TYPES:
                raise ExplorerError('Formato non supportato.')
            result = export_response(stored[1], format_name, BASE_DIR)
            with RESULT_LOCK:
                DOWNLOADS[result['filename']] = MIME_TYPES[format_name]
                while len(DOWNLOADS) > 256:
                    del DOWNLOADS[next(iter(DOWNLOADS))]
            result['download_url'] = '/api/download/' + result['filename']
            self.reply(200, result)
        except ExplorerError as error:
            self.reply(400, {'error': str(error)})
        except Exception:
            logging.exception('Errore durante la richiesta %s', self.path)
            self.reply(500, {'error': 'Operazione non riuscita. Consulta il terminale del server.'})
        finally:
            if acquired:
                SEARCH_LOCK.release()


def main():
    parser = argparse.ArgumentParser(description='MCP_ABAP_Explorer: Python diretto o Ollama locale')
    parser.add_argument('--port', type=int, default=8036)
    parser.add_argument('--no-browser', action='store_true')
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error('La porta deve essere compresa tra 1 e 65535.')
    try:
        detector()
        server = LocalServer(('127.0.0.1', args.port), Handler)
    except OSError as error:
        raise SystemExit(f'Impossibile avviare sulla porta {args.port}: {error}. Scegli --port con una porta libera.') from error
    url = f'http://127.0.0.1:{server.server_port}'
    print(f'MCP_ABAP_Explorer: {url}\nEndpoint MCP: {ENDPOINT}\nEsportazioni: {BASE_DIR}', flush=True)
    if not args.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    main()