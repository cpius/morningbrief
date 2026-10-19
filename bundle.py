"""Files and request body for a Vercel deployment of the morning brief.

Shared by deploy.py (the daily cloud routine) and api/refresh.py (the on-demand refresh button),
so both ship the same page, the refresh function and everything it needs to rerun refresh.py.
"""
import json, os

TEAM = 'team_45ZNkd6F5HMXNUu6jGjrKr44'      # OxeanX
PROJECT = 'prj_oldsDQ1BxZ7gn3AqRnwUXLCpxC05'  # nothing-ever-happens-book
COOLDOWN = 300  # seconds; the refresh button stays disabled this long after the last deployment

# Source files that go out with every deployment, relative to the repo root.
SOURCES = ['refresh.py', 'research.json', 'bundle.py', 'api/refresh.py']
VERCEL_JSON = {'functions': {'api/refresh.py': {'maxDuration': 300,
                                                'includeFiles': '{refresh.py,research.json,bundle.py}'}}}


def body(root, index_html, target='production'):
    files = [{'file': 'index.html', 'data': index_html},
             {'file': 'vercel.json', 'data': json.dumps(VERCEL_JSON, indent=2)}]
    for name in SOURCES:
        path = os.path.join(root, name)
        if os.path.exists(path):  # research.json may be missing; refresh.py copes
            with open(path) as fh:
                files.append({'file': name, 'data': fh.read()})
    body = {'name': 'nothing-ever-happens-book', 'project': PROJECT, 'files': files,
            'projectSettings': {'framework': None}}
    if target == 'production':
        body['target'] = 'production'
    return body
