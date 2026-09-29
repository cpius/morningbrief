#!/usr/bin/env python3
"""Check a Vercel token before putting it in the cloud environment's credential.

Reports whether the token is valid, what it is scoped to, when it expires, and whether it can
reach the morningbrief project. The token is read hidden from the terminal (or from stdin when
piped) and is never printed.

    python3 check_vercel_token.py
"""
import datetime, getpass, json, sys, urllib.error, urllib.request

TEAM = 'team_45ZNkd6F5HMXNUu6jGjrKr44'      # OxeanX
PROJECT = 'prj_oldsDQ1BxZ7gn3AqRnwUXLCpxC05'  # nothing-ever-happens-book

tok = (getpass.getpass('Paste the Vercel token (hidden), then press Enter: ') if sys.stdin.isatty()
       else sys.stdin.readline()).strip()
for prefix in ('VERCEL_TOKEN=', 'Bearer ', 'bearer '):
    if tok.startswith(prefix):
        print(f"⚠  The value starts with '{prefix}'. The credential must hold only the bare token; checking without it.")
        tok = tok[len(prefix):].strip()


def call(path, body=None):
    req = urllib.request.Request(f'https://api.vercel.com{path}', headers={'Authorization': f'Bearer {tok}', 'Content-Type': 'application/json'},
                                 data=json.dumps(body).encode() if body is not None else None)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.load(e)
        except Exception:
            return e.code, {}


def when(ms):
    return datetime.datetime.fromtimestamp(ms / 1000).strftime('%d %b %Y %H:%M') if ms else 'never'


status, j = call('/v5/user/tokens/current')
err = j.get('error', {}) if isinstance(j, dict) else {}
if status == 403 and (err.get('invalidToken') or err.get('missingToken')):
    print(f"✗ Token rejected: {err.get('message')} — invalid, revoked or expired")
    sys.exit(1)
if status == 200:
    t = j['token']
    print(f"✓ Valid token '{t.get('name')}' (created {when(t.get('createdAt'))}, last used {when(t.get('activeAt'))})")
    exp = t.get('expiresAt')
    if exp:
        days = (exp / 1000 - datetime.datetime.now().timestamp()) / 86400
        print(f"  Expires {when(exp)} ({days:.0f} days from now)" + ('  ⚠ soon' if days < 30 else ''))
    else:
        print('  Never expires')
    scopes = t.get('scopes', [])
    teams = [s.get('teamId') for s in scopes if s.get('type') == 'team']
    if TEAM in teams:
        print('  Scope: OxeanX team ✓')
    elif any(s.get('type') == 'user' for s in scopes):
        print('  Scope: your personal account (full access to your teams)')
    else:
        print(f"  Scope: {teams or scopes} — not OxeanX ✗")
else:
    print(f"• Token accepted, but Vercel won't show its details ({status}: {err.get('message', '')}).")
    print("  That usually means a team-owned token. Check its expiry in the token list on vercel.com.")

status, j = call(f'/v9/projects/{PROJECT}?teamId={TEAM}')
print(f"{'✓' if status == 200 else '✗'} Project nothing-ever-happens-book: "
      + ('readable' if status == 200 else f"{status} {j.get('error', {}).get('message', '')}"))
status2, j2 = call(f'/v6/deployments?teamId={TEAM}&projectId={PROJECT}&limit=1')
print(f"{'✓' if status2 == 200 else '✗'} Deployments: "
      + ('listable' if status2 == 200 else f"{status2} {j2.get('error', {}).get('message', '')}"))
# Harmless deploy-permission probe: an empty deployment is rejected as a bad request (400)
# when the token may deploy, and as forbidden (403) when it may not. Nothing is created.
status3, j3 = call(f'/v13/deployments?teamId={TEAM}', {})
can_deploy = status3 == 400
print(f"{'✓' if can_deploy else '✗'} Deploy permission: "
      + ('yes' if can_deploy else f"{status3} {j3.get('error', {}).get('message', '')}"))
ok = status == 200 and status2 == 200 and can_deploy
print('\nResult: ' + ('this token can deploy the morning brief.' if ok else 'this token will NOT work for the morning brief.'))
sys.exit(0 if ok else 1)
