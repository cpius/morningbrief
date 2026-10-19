"""On-demand refresh for the morning brief (Vercel Python function at /api/refresh).

GET  -> {"configured", "last_run", "next_allowed", "now"}: when the page may refresh again.
POST -> reruns refresh.py and redeploys the site, unless the last deployment is under 5 minutes old (429).

Calls the Vercel API with the project's VERCEL_TOKEN environment variable. Redeploys to the same
target it runs in (VERCEL_ENV), so a preview deployment only ever creates previews.
"""
import json, os, subprocess, sys, tempfile, time, urllib.error, urllib.request
from http.server import BaseHTTPRequestHandler

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import bundle  # noqa: E402

API = 'https://api.vercel.com'
TARGET = 'production' if os.environ.get('VERCEL_ENV') == 'production' else 'preview'


def vercel(method, path, data=None):
    headers = {'Content-Type': 'application/json'}
    if os.environ.get('VERCEL_TOKEN'):
        headers['Authorization'] = f"Bearer {os.environ['VERCEL_TOKEN']}"
    req = urllib.request.Request(f'{API}{path}', method=method, headers=headers,
                                 data=json.dumps(data).encode() if data is not None else None)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        try:
            err = json.load(e).get('error', {})
        except Exception:
            err = {}
        raise RuntimeError(f"Vercel {e.code}: {err.get('message', e.reason)}")


def last_run():
    """Creation time (epoch s) of the newest deployment for this target, in any state, so a refresh
    that is still building also counts."""
    q = f'teamId={bundle.TEAM}&projectId={bundle.PROJECT}&limit=1'
    if TARGET == 'production':
        q += '&target=production'
    deps = vercel('GET', f'/v6/deployments?{q}').get('deployments', [])
    return deps[0]['createdAt'] // 1000 if deps else 0


def status():
    if not os.environ.get('VERCEL_TOKEN'):
        return {'configured': False}
    last = last_run()
    return {'configured': True, 'last_run': last,
            'next_allowed': last + bundle.COOLDOWN, 'now': int(time.time())}


def refresh():
    s = status()
    if not s['configured']:
        return 503, {'error': 'VERCEL_TOKEN is not set on the project'}
    if s['now'] < s['next_allowed']:
        return 429, {**s, 'error': 'refreshed less than 5 minutes ago'}
    out = tempfile.mkdtemp()
    run = subprocess.run([sys.executable, os.path.join(ROOT, 'refresh.py')], cwd=ROOT, capture_output=True,
                         text=True, timeout=200, env={**os.environ, 'MORNINGBRIEF_OUT': out, 'MORNINGBRIEF_QUICK': '1'})
    if run.returncode:
        return 502, {'error': 'refresh.py failed', 'log': (run.stderr or run.stdout)[-2000:]}
    summary = next((l for l in run.stdout.splitlines() if l.startswith('snapshot ')), '')
    if last_run() != s['last_run']:  # another refresh started while this one ran
        return 429, {**status(), 'error': 'another refresh is already running'}
    with open(os.path.join(out, 'site', 'index.html')) as fh:
        d = vercel('POST', f'/v13/deployments?teamId={bundle.TEAM}', bundle.body(ROOT, fh.read(), TARGET))
    for _ in range(50):
        if d.get('readyState') in ('READY', 'ERROR', 'CANCELED'):
            break
        time.sleep(3)
        d = vercel('GET', f"/v13/deployments/{d['id']}?teamId={bundle.TEAM}")
    ok = d.get('readyState') == 'READY'
    return (200 if ok else 502), {'state': d.get('readyState'), 'url': f"https://{d.get('url')}", 'summary': summary}


class handler(BaseHTTPRequestHandler):
    def reply(self, code, payload):
        data = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        try:
            self.reply(200, status())
        except Exception as e:
            self.reply(502, {'error': str(e)})

    def do_POST(self):
        try:
            self.reply(*refresh())
        except Exception as e:
            self.reply(502, {'error': str(e)})
