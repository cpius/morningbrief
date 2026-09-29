#!/usr/bin/env python3
"""Deploy site/index.html to the Vercel project as a production deployment.

Sends no token itself: in the cloud routine, the environment's "Vercel API Token" credential
adds the Authorization header to requests for api.vercel.com. Uses curl so the request goes
through the sandbox proxy that injects it.

    python3 deploy.py              # deploy (cloud)
    python3 deploy.py --body-only  # print the request body, e.g. for `vercel api ... --input -` locally
"""
import json, os, subprocess, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
TEAM = 'team_45ZNkd6F5HMXNUu6jGjrKr44'      # OxeanX
PROJECT = 'prj_oldsDQ1BxZ7gn3AqRnwUXLCpxC05'  # nothing-ever-happens-book
API = 'https://api.vercel.com'

with open(os.path.join(HERE, 'site', 'index.html')) as fh:
    body = {'name': 'nothing-ever-happens-book', 'project': PROJECT, 'target': 'production',
            'files': [{'file': 'index.html', 'data': fh.read()}], 'projectSettings': {'framework': None}}

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
for _ in range(40):
    if d.get('readyState') in ('READY', 'ERROR', 'CANCELED'):
        break
    time.sleep(3)
    d = call('GET', f"/v13/deployments/{d['id']}?teamId={TEAM}")
print(f"state {d.get('readyState')}; production aliases: {', '.join(d.get('alias', [])) or 'none yet'}")
sys.exit(0 if d.get('readyState') == 'READY' else 1)
