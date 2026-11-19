#!/usr/bin/env python3
"""Deploy everything under site/ (the brief, the mispricing hunt in site/hunt/, the run log, the deep dives) to the
Vercel project.

Sends no token itself: in the cloud routine, the environment's "Vercel API Token" credential
adds the Authorization header to requests for api.vercel.com. Uses curl so the request goes
through the sandbox proxy that injects it.

    python3 deploy.py              # production deploy (cloud)
    python3 deploy.py --preview    # preview deploy: gets its own URL, leaves the production alias alone
    python3 deploy.py --body-only  # print the request body, e.g. for `vercel api ... --input -` locally

The hourly deep dives (deep.py) are published by other runs and live only in the deployment, so every deploy first
brings the live ones into site/deep/ and puts their links on the brief (`deep.sync()`). If the live copy can't be
read, the deploy stops rather than wipe them.
"""
import json, os, subprocess, sys, time

import deep

HERE = os.path.dirname(os.path.abspath(__file__))
TEAM = 'team_45ZNkd6F5HMXNUu6jGjrKr44'      # OxeanX
PROJECT = 'prj_oldsDQ1BxZ7gn3AqRnwUXLCpxC05'  # nothing-ever-happens-book
API = 'https://api.vercel.com'
SITE = os.path.join(HERE, 'site')


def body(preview=False):
    files = []
    for d, _, names in os.walk(SITE):
        for n in sorted(names):
            if n.endswith('.tmp'):
                continue
            with open(os.path.join(d, n)) as fh:
                files.append({'file': os.path.relpath(os.path.join(d, n), SITE), 'data': fh.read()})
    b = {'name': 'nothing-ever-happens-book', 'project': PROJECT, 'files': files, 'projectSettings': {'framework': None}}
    if not preview:
        b['target'] = 'production'
    return b


def call(method, path, data=None):
    if os.environ.get('DEEP_VERCEL_CLI'):  # local runs: through the signed-in CLI (see deep.api)
        return deep.api(method, path, data)
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


def post(b):
    """Create the deployment and wait until it is ready. Returns the exit code."""
    d = call('POST', f'/v13/deployments?teamId={TEAM}', b)
    print(f"deployment {d['id']} https://{d['url']} {d.get('readyState')}: {', '.join(f['file'] for f in b['files'])}")
    for _ in range(40):
        if d.get('readyState') in ('READY', 'ERROR', 'CANCELED'):
            break
        time.sleep(3)
        d = call('GET', f"/v13/deployments/{d['id']}?teamId={TEAM}")
    print(f"state {d.get('readyState')}; production aliases: {', '.join(d.get('alias', [])) or 'none yet'}")
    return 0 if d.get('readyState') == 'READY' else 1


def main():
    try:
        deep.sync()
    except deep.Unreachable as e:
        sys.exit(f'not deployed: the live deep dives could not be read ({e}), and deploying without them would '
                 f'delete them. Run it again.')
    except Exception as e:  # a bug in the deep dives' own code must not hold up the brief
        print(f'deep: not synced ({type(e).__name__}: {e}); deploying what is in site/', file=sys.stderr)
    b = body(preview='--preview' in sys.argv)
    if '--body-only' in sys.argv:
        print(json.dumps(b))
        return 0
    return post(b)


if __name__ == '__main__':
    sys.exit(main())
