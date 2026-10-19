#!/usr/bin/env python3
"""Deploy site/index.html to the Vercel project as a production deployment, together with the
/api/refresh function behind the page's refresh button (see bundle.py).

Sends no token itself: in the cloud routine, the environment's "Vercel API Token" credential
adds the Authorization header to requests for api.vercel.com. Uses curl so the request goes
through the sandbox proxy that injects it.

    python3 deploy.py              # deploy (cloud)
    python3 deploy.py --preview    # preview deployment, leaves production alone
    python3 deploy.py --body-only  # print the request body, e.g. for `vercel api ... --input -` locally
"""
import json, os, subprocess, sys, time

from bundle import TEAM, body as deployment_body

HERE = os.path.dirname(os.path.abspath(__file__))
API = 'https://api.vercel.com'

with open(os.path.join(HERE, 'site', 'index.html')) as fh:
    body = deployment_body(HERE, fh.read(), 'preview' if '--preview' in sys.argv else 'production')

if '--body-only' in sys.argv:
    print(json.dumps(body))
    sys.exit()


def call(method, path, data=None):
    cmd = ['curl', '-sS', '-X', method, f'{API}{path}', '-H', 'Content-Type: application/json']
    if data is not None:
        cmd += ['--data-binary', '@-']
    out = subprocess.run(cmd, input=json.dumps(data) if data is not None else None,
                         capture_output=True, text=True, timeout=120)
    if out.returncode:
        sys.exit(f'curl failed: {out.stderr.strip()}')
    j = json.loads(out.stdout)
    if 'error' in j:
        sys.exit(f"Vercel error: {j['error'].get('code')}: {j['error'].get('message')}")
    return j


d = call('POST', f'/v13/deployments?teamId={TEAM}', body)
print(f"deployment {d['id']} https://{d['url']} {d.get('readyState')}")
for _ in range(60):  # the refresh function makes each deployment a short build
    if d.get('readyState') in ('READY', 'ERROR', 'CANCELED'):
        break
    time.sleep(3)
    d = call('GET', f"/v13/deployments/{d['id']}?teamId={TEAM}")
print(f"state {d.get('readyState')}; production aliases: {', '.join(d.get('alias', [])) or 'none yet'}")
sys.exit(0 if d.get('readyState') == 'READY' else 1)
