#!/usr/bin/env python3
"""Run log for the morning brief routine, published at /log/ next to the brief.

Records each stage of a run (who ran it, model, effort, start, time spent, exit code, notes), every sub-job
(research subagents: model, effort, time, tokens, tool uses, searches, result) and the Claude plan usage
(weekly and 5-hour) at the start, at every stage boundary and at the end.

    python3 runlog.py start --model claude-opus-5-5 --effort xhigh
    python3 runlog.py run <key> "<title>" -- <command ...>   # time a script; its output passes through unchanged
    python3 runlog.py begin <key> "<title>"                 # a stage the agent does itself
    python3 runlog.py end <key> [--status ok|warn|failed|skipped] [--note "..."] [--metric k=v ...]
    python3 runlog.py jobs <key> --model claude-sonnet-5-5 --effort medium < lines   # one line per subagent
    python3 runlog.py note "<text>" [--level info|warn|error]
    python3 runlog.py metric k=v ...                        # run-level facts (value, positions, picks ...)
    python3 runlog.py finish --status ok|partial|failed --summary "<one line>"
    python3 runlog.py usage                                 # print the plan usage now

`jobs` reads one line per subagent: the batch name, then the Agent result's usage line and the subagent's reply,
e.g. `b001 subagent_tokens: 50951 tool_uses: 3 duration_ms: 12226 | 22 of 22 markets written; 4 searches`
(a JSON object per line also works).

The log lives in site/log/ (index.json, runs/<id>.json, index.html) and ships with deploy.py. Like the hunt's
book, history is carried in the deployment: `start` pulls the live index and the last 30 runs back first.
Logging never stops a run: every command catches its own errors, and `run` always runs the command.
Plan usage comes from the Claude OAuth usage endpoint when the environment has a Claude OAuth token; otherwise
each checkpoint records why it is unavailable (set RUNLOG_USAGE_FILE to a JSON file to test with fixed data).
"""
import argparse, datetime, json, os, re, subprocess, sys, time, urllib.error, urllib.request
from collections import deque

os.environ['TZ'] = 'Europe/Copenhagen'  # ids and the page read in Copenhagen time, like the brief
time.tzset()

HERE = os.path.dirname(os.path.abspath(__file__))
LOG = os.path.join(HERE, 'site', 'log')
RUNS = os.path.join(LOG, 'runs')
INDEX = os.path.join(LOG, 'index.json')
PAGE_SRC = os.path.join(HERE, 'runlog.html')
WORK = os.path.join(HERE, '.runlog')  # this VM only: the current run id and the last usage reading
CURRENT = os.path.join(WORK, 'current')
CACHE = os.path.join(WORK, 'usage.json')
LIVE = 'https://nothing-ever-happens-book.vercel.app/log/'
USAGE_URL = 'https://api.anthropic.com/api/oauth/usage'
UA = {'User-Agent': 'Mozilla/5.0 (morningbrief runlog)'}
KEEP_DETAIL = 30   # runs whose full log is kept
KEEP_INDEX = 180   # runs listed in the index
USAGE_GAP = 30     # seconds; a checkpoint this soon after the last reading reuses it
RUN_ID = re.compile(r'\d{4}-\d{2}-\d{2}-\d{4,6}')


def now():
    return datetime.datetime.now().astimezone()


def iso(t=None):
    return (t or now()).isoformat(timespec='seconds')


def secs(a, b):
    return round((datetime.datetime.fromisoformat(b) - datetime.datetime.fromisoformat(a)).total_seconds(), 1)


def say(msg):
    print(f'runlog: {msg}', file=sys.stderr, flush=True)


def read_json(path, default=None):
    try:
        with open(path) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def write_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + '.tmp'  # deploy.py skips .tmp files
    with open(tmp, 'w') as fh:
        json.dump(obj, fh, ensure_ascii=False, separators=(',', ':'))
    os.replace(tmp, path)


def get_json(url, timeout=20):
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout) as r:
        return json.load(r)


def num(s):
    try:
        f = float(s)
        return int(f) if f.is_integer() and '.' not in str(s) else f
    except (TypeError, ValueError):
        return s


# ---- plan usage -------------------------------------------------------------------------------------------

PCT_KEYS = ('utilization', 'used_percentage', 'percent_used', 'percentUsed', 'percent')
RESET_KEYS = ('resets_at', 'resetsAt', 'reset_at')


def slug(s):
    return re.sub(r'[^a-z0-9]+', '_', str(s).lower()).strip('_')


def window(d):
    pct = next((d[k] for k in PCT_KEYS if isinstance(d.get(k), (int, float))), None)
    if pct is None:
        return None
    r = next((d[k] for k in RESET_KEYS if d.get(k)), None)
    if isinstance(r, (int, float)):  # the status line gives epoch seconds
        r = iso(datetime.datetime.fromtimestamp(r).astimezone())
    return {'used': round(float(pct), 1), 'resets': r}


def normalize(raw):
    """Plan windows keyed weekly, five_hour and weekly_<model>, each {used: percent, resets: iso}.

    Reads the usage endpoint's shape ({"five_hour": {...}, "seven_day": {...}, "seven_day_opus": ...}), a status
    line's `rate_limits` object, and the newer list shape ({"limits": [{"kind": "weekly_all", ...}]})."""
    src = raw.get('rate_limits', raw) if isinstance(raw, dict) else {}
    pairs = []
    if isinstance(src.get('limits'), list):
        for it in src['limits']:
            if not isinstance(it, dict):
                continue
            kind = str(it.get('kind', ''))
            if kind == 'session':
                key = 'five_hour'
            elif kind == 'weekly_all':
                key = 'weekly'
            elif kind.startswith('weekly'):
                name = re.sub(r'^weekly_?', '', slug(it.get('model') or it.get('scope') or it.get('label') or ''))
                key = f'weekly_{name or "scoped"}'
            else:
                key = slug(kind or it.get('label') or 'other')
            pairs.append((key, it))
    else:
        for k, v in src.items():
            if isinstance(v, dict):
                key = 'weekly' if k == 'seven_day' else 'weekly_' + k[10:] if k.startswith('seven_day_') else k
                pairs.append((key, v))
    out = {}
    for key, d in pairs:
        w = window(d)
        if w:
            out[key] = w
    return out


def oauth_token():
    """The Claude OAuth token this environment signs in with, and where it came from (never logged)."""
    if os.environ.get('CLAUDE_CODE_OAUTH_TOKEN'):
        return os.environ['CLAUDE_CODE_OAUTH_TOKEN'], 'env CLAUDE_CODE_OAUTH_TOKEN'
    for d in filter(None, [os.environ.get('CLAUDE_CONFIG_DIR'), os.path.expanduser('~/.claude')]):
        p = os.path.join(d, '.credentials.json')
        t = ((read_json(p, {}) or {}).get('claudeAiOauth') or {}).get('accessToken')
        if t:
            return t, p.replace(os.path.expanduser('~'), '~')
    return None, None


def env_names():
    return ', '.join(sorted(k for k in os.environ if re.match(r'(CLAUDE|ANTHROPIC)', k))) or 'none'


def fetch_usage():
    """(windows, source, error); never raises."""
    if os.environ.get('RUNLOG_USAGE_FILE'):
        f = os.environ['RUNLOG_USAGE_FILE']
        raw = read_json(f)
        return (normalize(raw), f'file {f}', None) if raw else ({}, None, f'RUNLOG_USAGE_FILE {f} is not readable JSON')
    cache = read_json(CACHE, {}) or {}
    if cache.get('windows') and time.time() - cache.get('t', 0) < USAGE_GAP:
        return cache['windows'], cache['source'], None
    tok, where = oauth_token()
    if not tok:
        return {}, None, ('no Claude OAuth token in this environment (CLAUDE_CODE_OAUTH_TOKEN is unset and there is '
                          f'no ~/.claude/.credentials.json); CLAUDE_/ANTHROPIC_ variables present: {env_names()}')
    req = urllib.request.Request(USAGE_URL, headers={
        'Authorization': f'Bearer {tok}', 'anthropic-beta': 'oauth-2025-04-20', 'Accept': 'application/json', **UA})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            raw = json.load(r)
    except urllib.error.HTTPError as e:
        body = ' '.join(e.read(400).decode('utf-8', 'replace').split())[:200]
        return {}, None, f'usage endpoint answered HTTP {e.code} for the token from {where}: {body}'
    except (urllib.error.URLError, OSError, ValueError) as e:
        return {}, None, f'usage endpoint unreachable: {e}'
    w = normalize(raw)
    if not w:
        return {}, None, f'usage endpoint answered without usage fields: {json.dumps(raw)[:200]}'
    src = f'Claude usage endpoint (token from {where})'
    write_json(CACHE, {'t': time.time(), 'windows': w, 'source': src})
    return w, src, None


def checkpoint(run, label, stage=None):
    """Read the plan usage into the run; returns the checkpoint's index. Reuses the last one if it is fresh."""
    cps = run['checkpoints']
    if cps and not os.environ.get('RUNLOG_USAGE_FILE') and secs(cps[-1]['at'], iso()) < USAGE_GAP:
        return len(cps) - 1
    w, src, err = fetch_usage()
    cp = {'at': iso(), 'label': label, 'windows': w}
    if stage:
        cp['stage'] = stage
    if err:
        cp['error'] = err
        run['usage_error'] = err
    if src:
        run['usage_source'] = src
    cps.append(cp)
    return len(cps) - 1


# ---- the run record ---------------------------------------------------------------------------------------

def git(*args):
    try:
        return subprocess.run(['git', *args], cwd=HERE, capture_output=True, text=True, timeout=10).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ''


def session_link():
    for k, v in os.environ.items():
        if k.endswith('SESSION_ID') and re.fullmatch(r'(session|cse)_[A-Za-z0-9]+', v or ''):
            return 'https://claude.ai/code/session_' + v.split('_', 1)[1]
    return None


def run_path(rid):
    return os.path.join(RUNS, f'{rid}.json')


def new_run(model=None, effort=None):
    t = now()
    rid = t.strftime('%Y-%m-%d-%H%M')
    if os.path.exists(run_path(rid)):
        rid = t.strftime('%Y-%m-%d-%H%M%S')
    env_model = os.environ.get('ANTHROPIC_MODEL')
    effort_var = next((k for k in ('CLAUDE_EFFORT', 'CLAUDE_CODE_EFFORT_LEVEL') if os.environ.get(k)), None)
    env_effort = os.environ.get(effort_var) if effort_var else None
    run = {
        'id': rid, 'v': 1, 'started': iso(t), 'ended': None, 'seconds': 0, 'status': 'running', 'summary': '',
        'agent': {'model': env_model or model or 'unknown', 'effort': env_effort or effort or 'unknown',
                  'source': '; '.join([f'model from {"ANTHROPIC_MODEL" if env_model else "the routine settings (runlog start --model)"}',
                                       f'effort from {effort_var or "the routine settings (runlog start --effort)"}'])},
        'env': {'runner': 'cloud' if HERE.startswith('/home/user') else 'local', 'commit': git('rev-parse', '--short', 'HEAD'),
                'branch': git('rev-parse', '--abbrev-ref', 'HEAD'), 'session': session_link()},
        'usage_source': None, 'usage_error': None,
        'checkpoints': [], 'stages': [], 'jobs': [], 'notes': [], 'metrics': {},
    }
    os.makedirs(WORK, exist_ok=True)
    with open(CURRENT, 'w') as fh:
        fh.write(rid)
    checkpoint(run, 'Run start')
    return run


def load_run():
    try:
        with open(CURRENT) as fh:
            run = read_json(run_path(fh.read().strip()))
    except OSError:
        run = None
    if not run:
        run = new_run()
        run['notes'].append({'at': iso(), 'level': 'warn', 'text': 'runlog start was not called; the run was started automatically'})
    return run


def used(cp, key='weekly'):
    return ((cp or {}).get('windows') or {}).get(key, {}).get('used')


def summary(run):
    cps = [c for c in run['checkpoints'] if c.get('windows')]
    first, last = (cps[0], cps[-1]) if cps else (None, None)
    warnings = sum(n['level'] != 'info' for n in run['notes']) + sum(s['status'] in ('failed', 'warn', 'unfinished') for s in run['stages'])
    return {
        'id': run['id'], 'started': run['started'], 'ended': run['ended'], 'seconds': run['seconds'],
        'status': run['status'], 'summary': run['summary'], 'model': run['agent']['model'], 'effort': run['agent']['effort'],
        'weekly': [used(first), used(last)], 'five_hour': [used(first, 'five_hour'), used(last, 'five_hour')],
        'stages': len(run['stages']), 'jobs': len(run['jobs']),
        'job_seconds': round(sum(j.get('seconds') or 0 for j in run['jobs'])), 'warnings': warnings, 'detail': True,
    }


def save(run):
    run['seconds'] = secs(run['started'], run['ended'] or iso())
    write_json(run_path(run['id']), run)
    idx = read_json(INDEX, {}) or {}
    runs = [r for r in idx.get('runs', []) if r.get('id') != run['id']]
    runs = sorted([summary(run)] + runs, key=lambda r: r.get('started') or '', reverse=True)[:KEEP_INDEX]
    for i, r in enumerate(runs):
        if i >= KEEP_DETAIL and r.get('detail'):
            try:
                os.remove(run_path(r['id']))
            except OSError:
                pass
            r['detail'] = False
    write_json(INDEX, {'updated': iso(), 'runs': runs})
    if os.path.exists(PAGE_SRC):
        from favicon import FAVICON
        with open(PAGE_SRC) as fh:
            page = fh.read().replace('__FAVICON__', FAVICON)
        with open(os.path.join(LOG, 'index.html'), 'w') as fh:
            fh.write(page)


def pull():
    """Bring the live log's index and recent runs back into site/log/ so this deploy keeps them."""
    try:
        idx = get_json(f'{LIVE}index.json?t={int(time.time())}')
    except (urllib.error.URLError, OSError, ValueError) as e:
        return f'no log on the live site yet ({e}); starting a new one'
    runs = [r for r in idx.get('runs', []) if isinstance(r, dict) and RUN_ID.fullmatch(str(r.get('id', '')))]
    got = 0
    for r in runs[:KEEP_DETAIL]:
        if not r.get('detail'):
            continue
        try:
            write_json(run_path(r['id']), get_json(f"{LIVE}runs/{r['id']}.json"))
            got += 1
        except (urllib.error.URLError, OSError, ValueError):
            r['detail'] = False
    for r in runs[KEEP_DETAIL:]:
        r['detail'] = False
    write_json(INDEX, {'updated': idx.get('updated'), 'runs': runs})
    return f'pulled {len(runs)} runs from the live log ({got} in full)'


def find_open(run, key):
    return next((s for s in reversed(run['stages']) if s['key'] == key and not s['ended']), None)


def begin(run, key, title, by='agent', model=None, effort=None, command=None):
    if by == 'agent':
        model, effort = model or run['agent']['model'], effort or run['agent']['effort']
    st = {'key': key, 'title': title or key, 'by': by, 'model': model, 'effort': effort, 'started': iso(),
          'ended': None, 'seconds': None, 'status': 'running', 'notes': [], 'metrics': {}}
    if command:
        st['command'] = command[:300]
    run['stages'].append(st)
    st['cp_start'] = checkpoint(run, f'{st["title"]}: start', key)
    return st


def end(run, key, status='ok', notes=(), metrics=None, exit_code=None, output=None):
    st = find_open(run, key)
    if not st:
        st = begin(run, key, key)
        st['notes'].append('no matching begin; logged as an instant stage')
    st['ended'] = iso()
    st['seconds'] = secs(st['started'], st['ended'])
    st['status'] = status
    st['notes'] += [n for n in notes if n]
    st['metrics'].update(metrics or {})
    if exit_code is not None:
        st['exit'] = exit_code
    if output:
        st['output'] = output
    st['cp_end'] = checkpoint(run, f'{st["title"]}: end', key)
    return st


def parse_job(line):
    line = line.strip()
    if not line:
        return None
    if line.startswith('{'):
        d = json.loads(line)
        if 'duration_ms' in d and 'seconds' not in d:
            d['seconds'] = round(d.pop('duration_ms') / 1000, 1)
        return d
    name, _, rest = line.partition(' ')

    def grab(pat):
        m = re.search(pat, rest, re.I)
        return int(m.group(1)) if m else None

    d = {'name': name.strip(' |:,'), 'tokens': grab(r'(?:subagent_tokens|total_tokens|tokens)[:=]\s*(\d+)'),
         'tool_uses': grab(r'tool_uses[:=]\s*(\d+)'), 'searches': grab(r'(\d+)\s+(?:web\s+)?searches')}
    ms = grab(r'duration_ms[:=]\s*(\d+)')
    d['seconds'] = round(ms / 1000, 1) if ms is not None else None
    m = re.search(r'(\d+)\s+of\s+(\d+)\s+markets', rest)
    if m:
        d['written'], d['total'] = int(m.group(1)), int(m.group(2))
    if all(d.get(k) is None for k in ('seconds', 'tokens', 'tool_uses', 'written')):
        raise ValueError(f'no usage figures or "n of m markets" in: {line[:80]}')
    res = re.sub(r'</?usage>|(?:subagent_tokens|total_tokens|tool_uses|duration_ms)[:=]\s*\d+', ' ', rest)
    d['result'] = ' '.join(res.split()).strip(' |;,')[:300]
    return d


# ---- commands ---------------------------------------------------------------------------------------------

def cmd_start(a):
    os.makedirs(RUNS, exist_ok=True)
    say(pull())
    run = new_run(a.model, a.effort)
    save(run)
    w = run['checkpoints'][-1]['windows']
    say(f"run {run['id']} started; weekly plan used {w['weekly']['used']}%" if 'weekly' in w
        else f"run {run['id']} started; plan usage unavailable: {run['usage_error']}")


def cmd_begin(a):
    run = load_run()
    begin(run, a.key, a.title, a.by, a.model, a.effort)
    save(run)


def cmd_end(a):
    run = load_run()
    metrics = dict((k, num(v)) for k, _, v in (m.partition('=') for m in a.metric))
    st = end(run, a.key, a.status, a.note, metrics)
    save(run)
    say(f"{st['title']}: {st['status']} in {st['seconds']}s")


def cmd_run(a):
    cmd = a.cmd[1:] if a.cmd[:1] == ['--'] else a.cmd
    if not cmd:
        sys.exit('runlog run: give the command after --')
    run = None
    try:
        run = load_run()
        begin(run, a.key, a.title, 'script', command=' '.join(cmd))
        save(run)
    except Exception as e:  # logging must never stop the command
        say(f'could not log the start of {a.key}: {e}')
    head, tail, n = [], deque(maxlen=12), 0
    try:
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors='replace')
    except OSError as e:
        say(f'cannot run {cmd[0]}: {e}')
        rc = 127
        tail.append(str(e))
    else:
        for line in p.stdout:
            sys.stdout.write(line)
            sys.stdout.flush()
            n += 1
            (head if len(head) < 3 else tail).append(line.rstrip('\n')[:240])
        rc = p.wait()
    if run is not None:
        try:
            out = head + ([f'… {n - len(head) - len(tail)} more lines …'] if n > len(head) + len(tail) else []) + list(tail)
            st = end(run, a.key, 'ok' if rc == 0 else 'failed', exit_code=rc, output=out)
            save(run)
            say(f"{st['title']}: exit {rc} in {st['seconds']}s")
        except Exception as e:
            say(f'could not log the end of {a.key}: {e}')
    sys.exit(rc)


def cmd_jobs(a):
    run = load_run()
    added = 0
    for line in sys.stdin:
        try:
            d = parse_job(line)
        except ValueError as e:
            say(f'skipped a line: {e}')
            continue
        if not d:
            continue
        status = d.get('status') or ('partial' if d.get('written') is not None and d['written'] < d.get('total', 0)
                                     else 'failed' if re.search(r'\b(fail|error)', d.get('result', ''), re.I) else 'ok')
        run['jobs'].append({'stage': a.key, 'name': str(d.get('name', '?'))[:60], 'model': d.get('model') or a.model,
                            'effort': d.get('effort') or a.effort, 'seconds': d.get('seconds'), 'tokens': d.get('tokens'),
                            'tool_uses': d.get('tool_uses'), 'searches': d.get('searches'), 'written': d.get('written'),
                            'total': d.get('total'), 'result': str(d.get('result', ''))[:300], 'status': status, 'logged': iso()})
        added += 1
    checkpoint(run, f'{added} sub-jobs logged', a.key)
    save(run)
    say(f'{added} sub-jobs logged under {a.key} ({len(run["jobs"])} in this run)')


def cmd_note(a):
    run = load_run()
    run['notes'].append({'at': iso(), 'level': a.level, 'text': a.text[:600]})
    save(run)


def cmd_metric(a):
    run = load_run()
    run['metrics'].update((k, num(v)) for k, _, v in (m.partition('=') for m in a.pairs))
    save(run)


def cmd_finish(a):
    run = load_run()
    for st in run['stages']:
        if not st['ended']:
            end(run, st['key'], 'unfinished', ['still open when the run finished'])
    run['ended'] = iso()
    run['status'] = a.status
    run['summary'] = (a.summary or '')[:400]
    checkpoint(run, 'Run end')
    save(run)
    s = summary(run)
    w0, w1 = s['weekly']
    say(f"run {run['id']} {a.status} after {run['seconds']:.0f}s; "
        + (f'weekly plan used {w0}% → {w1}%' if w1 is not None else 'plan usage unavailable'))


def cmd_usage(a):
    w, src, err = fetch_usage()
    print(json.dumps({'windows': w, 'source': src, 'error': err}, indent=1))


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    sub = ap.add_subparsers(dest='cmd', required=True)
    p = sub.add_parser('start')
    p.add_argument('--model')
    p.add_argument('--effort')
    p.set_defaults(fn=cmd_start)
    p = sub.add_parser('begin')
    p.add_argument('key')
    p.add_argument('title', nargs='?')
    p.add_argument('--by', default='agent', choices=['agent', 'script', 'subagents'])
    p.add_argument('--model')
    p.add_argument('--effort')
    p.set_defaults(fn=cmd_begin)
    p = sub.add_parser('end')
    p.add_argument('key')
    p.add_argument('--status', default='ok', choices=['ok', 'warn', 'failed', 'skipped'])
    p.add_argument('--note', action='append', default=[])
    p.add_argument('--metric', action='append', default=[])
    p.set_defaults(fn=cmd_end)
    p = sub.add_parser('run')
    p.add_argument('key')
    p.add_argument('title')
    p.add_argument('cmd', nargs=argparse.REMAINDER)
    p.set_defaults(fn=cmd_run)
    p = sub.add_parser('jobs')
    p.add_argument('key')
    p.add_argument('--model')
    p.add_argument('--effort')
    p.set_defaults(fn=cmd_jobs)
    p = sub.add_parser('note')
    p.add_argument('text')
    p.add_argument('--level', default='info', choices=['info', 'warn', 'error'])
    p.set_defaults(fn=cmd_note)
    p = sub.add_parser('metric')
    p.add_argument('pairs', nargs='+')
    p.set_defaults(fn=cmd_metric)
    p = sub.add_parser('finish')
    p.add_argument('--status', default='ok', choices=['ok', 'partial', 'failed'])
    p.add_argument('--summary')
    p.set_defaults(fn=cmd_finish)
    p = sub.add_parser('usage')
    p.set_defaults(fn=cmd_usage)
    a = ap.parse_args()
    if a.fn is cmd_run:
        return cmd_run(a)
    try:
        a.fn(a)
    except Exception as e:  # never stop the routine over its log
        say(f'{a.cmd} failed: {type(e).__name__}: {e}')


if __name__ == '__main__':
    main()
