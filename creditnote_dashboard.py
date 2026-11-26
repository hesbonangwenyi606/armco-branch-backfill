"""Local-only dashboard for the credit-note backfill journal and runner."""
import json
import os
import csv
from pathlib import Path
import secrets
import sqlite3
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from backfill_customer_branch import BackfillError, run_lock

ROOT = Path(__file__).resolve().parent
BATCH = ROOT / 'output/corrected_run/creditnote_field_only_batch'
TOKEN = secrets.token_urlsafe(32)
PROCESS = None
LOCK = threading.Lock()
STARTED = None
LOG = []
MODE = None


def status():
    counts = {}
    unresolved = []
    isolated = []
    recent = []
    completed_ids = set()
    dbpath = BATCH / 'progress.sqlite3'
    if dbpath.exists():
        with sqlite3.connect(dbpath.resolve().as_uri() + '?mode=ro', uri=True, timeout=5) as db:
            counts = dict(db.execute('SELECT status,COUNT(*) FROM attempts GROUP BY status'))
            completed_ids.update(r[0] for r in db.execute("SELECT id FROM attempts WHERE status IN ('verified','already_present')"))
            unresolved = [dict(number=n, status=s) for n, s in db.execute(
                "SELECT number,status FROM attempts WHERE status IN ('failed','attempting')")]
            isolated = [dict(number=n, status=s) for n, s in db.execute(
                "SELECT number,status FROM attempts WHERE status='isolated'")]
            recent = [dict(number=n, status=s) for n, s in db.execute(
                'SELECT number,status FROM attempts ORDER BY rowid DESC LIMIT 10')]
    legacy_path = ROOT / 'output/corrected_run/creditnote_batch/progress.sqlite3'
    if legacy_path.exists():
        with sqlite3.connect(legacy_path.resolve().as_uri() + '?mode=ro', uri=True, timeout=5) as db:
            completed_ids.update(r[0] for r in db.execute("SELECT id FROM attempts WHERE status IN ('verified','already_present')"))
    input_path = ROOT / 'output/corrected_run/creditnote_preliminary_candidates.csv'
    with input_path.open(encoding='utf-8-sig') as f:
        candidate_total = max(0, sum(1 for _ in f) - 1)
    with (ROOT / 'data/Armco_Branch_Backfill_API_Map_corrected.csv').open(encoding='utf-8-sig', newline='') as f:
        mapped_total = sum(1 for row in csv.DictReader(f) if row['type'] == 'creditnote')
    total = mapped_total
    scan_counts = {}
    scan_path = ROOT / 'output/corrected_run/creditnote_remaining_scan/scan.sqlite3'
    if scan_path.exists():
        with sqlite3.connect(scan_path.resolve().as_uri() + '?mode=ro', uri=True, timeout=5) as db:
            scan_counts = dict(db.execute('SELECT status,COUNT(*) FROM results GROUP BY status'))
    invoice_path = ROOT / 'output/corrected_run/invoice_field_id_run/progress.sqlite3'
    invoices = None
    if invoice_path.exists():
        with sqlite3.connect(invoice_path.resolve().as_uri() + '?mode=ro', uri=True, timeout=5) as db:
            invoice_counts = dict(db.execute("SELECT status,COUNT(*) FROM documents WHERE type='invoice' GROUP BY status"))
            invoices = {'counts': invoice_counts, 'total': sum(invoice_counts.values())}
    names = ['ZOHO_CLIENT_ID', 'ZOHO_CLIENT_SECRET', 'ZOHO_REFRESH_TOKEN', 'ZOHO_ORG_ID',
             'ZOHO_ACCOUNTS', 'ZOHO_API', 'CF_CREDITNOTE_ID', 'CF_INVOICE_ID']
    running = PROCESS is not None and PROCESS.poll() is None
    writer_active = False
    if BATCH.exists():
        try:
            with run_lock(BATCH):
                pass
        except BackfillError:
            writer_active = True
    return {'counts': counts, 'total': total, 'mapped_total': mapped_total, 'overall_completed': len(completed_ids), 'candidate_total': candidate_total, 'scan_counts': scan_counts,
            'unresolved': unresolved, 'isolated': isolated, 'recent': recent, 'invoices': invoices,
            'running': running, 'writer_active': writer_active, 'exit_code': PROCESS.poll() if PROCESS else None,
            'started': STARTED, 'mode': MODE, 'credentials_configured': all(os.environ.get(n, '').strip() for n in names),
            'log': LOG[-20:]}


def capture(process):
    for line in process.stdout:
        with LOCK:
            LOG.append(line.rstrip()[:500])
            if len(LOG) > 100:
                del LOG[:-100]


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def send(self, code, content, content_type):
        data = content.encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', content_type)
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Content-Security-Policy', "default-src 'self'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'")
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.headers.get('Host', '') not in ('127.0.0.1:8765', 'localhost:8765'):
            self.send(403, 'Request rejected', 'text/plain')
            return
        if self.path == '/':
            html = (ROOT / 'creditnote_dashboard.html').read_text(encoding='utf-8').replace('__TOKEN__', TOKEN)
            self.send(200, html, 'text/html; charset=utf-8')
        elif self.path == '/api/status':
            try:
                with LOCK:
                    data = status()
                self.send(200, json.dumps(data), 'application/json')
            except (OSError, sqlite3.Error, ValueError):
                self.send(503, json.dumps({'error': 'Progress is temporarily unavailable.'}), 'application/json')
        else:
            self.send(404, 'Not found', 'text/plain')

    def do_POST(self):
        global PROCESS, STARTED, MODE
        host = self.headers.get('Host', '')
        if (self.path not in ('/api/start', '/api/test', '/api/test-invoice', '/api/config') or self.headers.get('X-Dashboard-Token') != TOKEN
                or self.headers.get('Origin') != 'http://' + host
                or host not in ('127.0.0.1:8765', 'localhost:8765')):
            self.send(403, json.dumps({'error': 'Request rejected.'}), 'application/json')
            return
        try:
            size = int(self.headers.get('Content-Length', '0'))
            if size < 1 or size > 1024:
                raise ValueError()
            body = json.loads(self.rfile.read(size))
            if self.path == '/api/config':
                values = [body.get(k) for k in ('client_id', 'client_secret', 'refresh_token')]
                if any(not isinstance(v, str) or not v.strip() or len(v) > 512 for v in values):
                    raise ValueError()
                with LOCK:
                    if PROCESS is not None and PROCESS.poll() is None:
                        self.send(409, json.dumps({'error': 'Wait until the current operation finishes.'}), 'application/json')
                        return
                    for name, value in zip(('ZOHO_CLIENT_ID', 'ZOHO_CLIENT_SECRET', 'ZOHO_REFRESH_TOKEN'), values):
                        os.environ[name] = value.strip()
                    os.environ.update({'ZOHO_ORG_ID': '941141787', 'ZOHO_ACCOUNTS': 'https://accounts.zoho.com',
                        'ZOHO_API': 'https://www.zohoapis.com/books/v3', 'CF_INVOICE_ID': '1462401000020819910',
                        'CF_CREDITNOTE_ID': '1462401000020819915'})
                self.send(200, json.dumps({'configured': True}), 'application/json')
                return
            if self.path == '/api/start':
                limit = body.get('limit')
                if limit != 'all' and (type(limit) is not int or not 1 <= limit <= 100):
                    raise ValueError()
                command = [sys.executable, '-u', str(ROOT / 'run_creditnote_field_only.py'), '--limit', str(2452 if limit == 'all' else limit)]
                mode = 'batch'
            elif self.path == '/api/test':
                number = body.get('number')
                if not isinstance(number, str) or len(number) > 40 or not number:
                    raise ValueError()
                with (ROOT / 'output/corrected_run/creditnote_preliminary_candidates.csv').open(encoding='utf-8-sig', newline='') as f:
                    if not any(r['creditnote_number'] == number for r in csv.DictReader(f)):
                        self.send(400, json.dumps({'error': 'Credit note is not in the candidate list.'}), 'application/json')
                        return
                command = [sys.executable, '-u', str(ROOT / 'inspect_creditnote_candidate.py'), number]
                mode = 'test'
            else:
                number = body.get('number')
                if not isinstance(number, str) or not number or len(number) > 40:
                    raise ValueError()
                command = [sys.executable, '-u', str(ROOT / 'inspect_invoice_branch.py'), number]
                mode = 'invoice_test'
            with LOCK:
                current = status()
                if (current['running'] or current['writer_active'] or not current['credentials_configured']
                        or (mode == 'batch' and current['unresolved'])):
                    self.send(409, json.dumps({'error': 'Batch is running, blocked, or settings are missing.'}), 'application/json')
                    return
                LOG.clear()
                PROCESS = subprocess.Popen(command,
                    cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding='utf-8', errors='replace',
                    creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                STARTED = time.time()
                MODE = mode
                threading.Thread(target=capture, args=(PROCESS,), daemon=True).start()
            self.send(200, json.dumps({'started': True}), 'application/json')
        except (ValueError, OSError, sqlite3.Error, json.JSONDecodeError):
            self.send(400, json.dumps({'error': 'Could not start the batch.'}), 'application/json')


if __name__ == '__main__':
    server = ThreadingHTTPServer(('127.0.0.1', 8765), Handler)
    print('Credit-note dashboard: http://127.0.0.1:8765', flush=True)
    server.serve_forever()
