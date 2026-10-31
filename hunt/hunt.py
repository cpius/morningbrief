#!/usr/bin/env python3
"""Mispricing hunt: a book of Polymarket picks with my fair values, repriced every morning.

The book (picks, fair values, reasons, notes) is carried from run to run in hunt/work/state.json.
`build` publishes it next to the page as site/hunt/state.json, and the next run's `pull` reads it back
from the live site, so the cloud routine needs no git push. The repo copy hunt/seed.json is the fallback.

    python3 hunt/hunt.py pull                 # load the book (live site, else seed); new day: last ask becomes "previous"
    python3 hunt/hunt.py scan                 # reprice every pick, sweep for new events, print the worklist
    python3 hunt/hunt.py list                 # the book, with ids and returns
    python3 hunt/hunt.py show <id>            # one pick plus its market rules
    python3 hunt/hunt.py peek <event id|slug|url>  # a candidate event: its open markets, prices and rules
    python3 hunt/hunt.py set <id> [--fair F] [--why W] [--name N] [--tag T]
    python3 hunt/hunt.py drop <id> --reason R
    python3 hunt/hunt.py add <event id|slug|url> "<words from the question>" --side NO --fair F --why W [--name N] [--tag T]
    python3 hunt/hunt.py note "<one line on what changed>"
    python3 hunt/hunt.py smart                # smart-money share from top holders and leaderboard PnL
    python3 hunt/hunt.py build                # write site/hunt/{index.html,state.json,wallets.json,summary.json}

Return = fair / ask - 1, held to resolution, plus Polymarket's 3.25% APR holding reward where the market pays it.
Only the standard library is used.
"""
import argparse, datetime, html, json, os, re, sys, threading, time, urllib.error, urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
WORK = os.path.join(HERE, 'work')
STATE = os.path.join(WORK, 'state.json')
WALLETS = os.path.join(WORK, 'wallets.json')
CANDIDATES = os.path.join(WORK, 'candidates.txt')
SEED = os.path.join(HERE, 'seed.json')
SEED_WALLETS = os.path.join(HERE, 'seed-wallets.json')
SITE = os.path.join(ROOT, 'site', 'hunt')
LIVE = 'https://nothing-ever-happens-book.vercel.app/hunt/'
GAMMA, CLOB, DATA = 'https://gamma-api.polymarket.com', 'https://clob.polymarket.com', 'https://data-api.polymarket.com'
UA = {'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/120 Safari/537.36'}

NOW = datetime.datetime.now(ZoneInfo('Europe/Copenhagen'))
TODAY = NOW.date()
HOLD_APR = 0.0325       # Polymarket holding reward, variable at their discretion
MAIN_EDGE = 0.10        # main list
MIN_EDGE = 0.05         # appendix; below this a pick is dropped at build
REVIEW_MOVE = 0.05      # ask moved this much since the last run -> news check
SOON_DAYS = 14          # ends this soon -> news check
SMART_PNL = 50_000      # all-time PnL that counts as smart money
SMART_MIN_USD = 300     # hide the smart-money share below this much smart money
WALLET_TTL_DAYS = 30
MAX_WALLET_LOOKUPS = 1500
ADAPTER = '0xa5ef39c3d3e10d0b270233af41cac69796b12966'  # neg-risk adapter, holds NO everywhere
MAKERS = {'xtxpleasecallme', 'armageddonrewardsbilly'}  # liquidity-reward makers sitting on many books

# Politics/geo tag ids for the sweep (2026-10-29 set). Sports and crypto are skipped by tag slug.
TAGS = [2, 100265, 101970, 144, 1597, 154, 100410, 101206, 96, 303, 78, 180, 95, 100328, 126, 1628, 101794, 789, 514,
        102434, 100614, 245, 734, 1378, 100244, 518, 101522, 166, 867, 246, 192, 61, 849, 114, 956, 100185, 167, 159,
        101758, 100188, 872, 101270, 351, 520, 1382, 1370, 1383, 101272, 525, 191, 1373, 100861, 742, 101283, 101284,
        101748, 181, 102436, 282, 439, 1401]
NOISE = re.compile(r'tweet|post |posts|say |says|mention|# of|number of|views|followers|up or down|price of|above|below|'
                   r'temperature|highest|lowest|close at|market cap|ETF|stock|IPO|FDV|launch a token|airdrop|box office|'
                   r'rotten|billboard|spotify|album|song|movie|grammy|oscar|emmy|award|youtube|tiktok|streamer|twitch|'
                   r'kick|MrBeast|word|ranking|top AI model|best AI|arena|benchmark|search|google trends|approval rating|'
                   r'net approval|polling average|odds|seats|vote share|margin|turnout|% of|percent|by how much|how many|'
                   r'exact|between|range|\d+k|\$\d', re.I)


# ---------------------------------------------------------------- plumbing

def get(url, tries=5, timeout=20):
    for i in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code in (400, 404) or i == tries - 1:
                raise
            time.sleep(2 * (i + 1) if e.code == 429 else 1 + i)
        except Exception:
            if i == tries - 1:
                raise
            time.sleep(1 + i)


def read(path):
    with open(path) as fh:
        return json.load(fh)


def write(path, obj, indent=1):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path + '.tmp', 'w') as fh:
        json.dump(obj, fh, indent=indent, ensure_ascii=False)
    os.replace(path + '.tmp', path)


def load():
    if not os.path.exists(STATE):
        sys.exit('no hunt/work/state.json: run `python3 hunt/hunt.py pull` first')
    return read(STATE)


def find(st, pid):
    for p in st['picks']:
        if p['id'] == pid:
            return p
    sys.exit(f'no pick with id {pid} (see `hunt.py list`)')


def econ(p):
    """Return math for one pick at today's ask, or None without an ask."""
    if not p.get('ask'):
        return None
    yrs = max((datetime.date.fromisoformat(p['end']) - TODAY).days, 1) / 365
    ret = p['fair'] / p['ask'] - 1
    bonus = HOLD_APR * yrs * ((p['ask'] + p['fair']) / 2) / p['ask'] if p.get('hold') else 0.0
    tot = ret + bonus
    return dict(yrs=yrs, ret=ret, bonus=bonus, tot=tot, ann=tot / yrs)


def book(token):
    k = get(f'{CLOB}/book?token_id={token}', tries=3)
    asks = sorted((float(x['price']), float(x['size'])) for x in k.get('asks') or [])
    bids = sorted((float(x['price']) for x in k.get('bids') or []), reverse=True)
    a = asks[0][0] if asks else None
    return dict(ask=a, bid=bids[0] if bids else None,
                depth=round(sum(px * sz for px, sz in asks if px <= a + 0.02)) if a else 0)


def event_by_ref(ref):
    """Gamma event from an id, a slug or a polymarket.com URL."""
    ref = ref.strip().rstrip('/')
    if ref.isdigit():
        return get(f'{GAMMA}/events/{ref}')
    slug = ref.split('/event/')[-1].split('?')[0].split('/')[0]
    r = get(f'{GAMMA}/events?slug={slug}')
    if not r:
        sys.exit(f'no event for {ref}')
    return r[0]


# ---------------------------------------------------------------- pull

def cmd_pull(a):
    if os.path.exists(STATE) and not a.fresh and read(STATE).get('day') == str(TODAY):
        print('hunt/work/state.json is already from today; keeping it (pull --fresh to refetch)')
        return
    found = []
    try:
        found.append(('live site', get(f'{LIVE}state.json?t={int(time.time())}', tries=3)))
    except Exception as e:
        print(f'live state unavailable ({e})')
    found.append(('repo seed', read(SEED)))
    src, st = max(found, key=lambda f: f[1].get('built', ''))
    try:
        wallets = get(f'{LIVE}wallets.json?t={int(time.time())}', tries=3)
    except Exception:
        wallets = None
    if not wallets or len(wallets) < 100:
        wallets = read(SEED_WALLETS)
    if st.get('day') != str(TODAY):  # first run today: last run's prices become the "previous" column
        for p in st['picks']:
            p['prev_ask'], p['prev_fair'] = p.get('ask'), p['fair']
        st.update(prev_day=st.get('day'), day=str(TODAY), notes=[], dropped=[], resolved=[])
    write(STATE, st)
    write(WALLETS, wallets, indent=None)
    print(f"pulled the book from the {src}: {len(st['picks'])} picks, built {st.get('built', '?')}, "
          f"{len(wallets)} scored wallets")


# ---------------------------------------------------------------- scan

def reprice(p):
    try:
        m = get(f"{GAMMA}/markets/{p['id']}")
    except Exception as e:
        return p['id'], dict(err=f'gamma: {e}')
    r = dict(hold=bool(m.get('holdingRewardsEnabled')), fee=(m.get('feeSchedule') or {}).get('rate'))
    if m.get('closed'):
        prices = json.loads(m.get('outcomePrices') or '[]')
        i = 0 if p['side'] == 'YES' else 1
        r.update(closed=True, won=bool(prices) and float(prices[i]) > 0.5)
        return p['id'], r
    try:
        r.update(book(p['token']))
    except Exception as e:
        r.update(ask=None, bid=None, depth=0, err=f'book: {e}')
    return p['id'], r


def slim_event(e):
    return dict(id=e['id'], title=e.get('title', ''), slug=e.get('slug', ''),
                tags=[t.get('slug') for t in e.get('tags') or []],
                markets=[dict(id=m.get('id'), q=m.get('question', ''), g=m.get('groupItemTitle') or '',
                              bid=m.get('bestBid'), ask=m.get('bestAsk'), end=(m.get('endDate') or '')[:10],
                              liq=float(m.get('liquidityNum') or m.get('liquidity') or 0),
                              hold=bool(m.get('holdingRewardsEnabled')),
                              live=bool(m.get('active')) and not m.get('closed') and m.get('acceptingOrders', True))
                         for m in e.get('markets') or []])


def sweep_tag(tag):
    out = []
    for off in range(0, 2001, 100):  # Gamma rejects offsets above 2000
        try:
            r = get(f'{GAMMA}/events?tag_id={tag}&active=true&closed=false&limit=100&offset={off}', tries=3, timeout=30)
        except Exception:
            break
        out += [slim_event(e) for e in r]
        if len(r) < 100:
            break
    return out


def candidate_markets(e):
    """Markets in an event worth a look: live, two-sided, not noise, ends in 4 days to ~2.5 years."""
    if 'sports' in e['tags'] or 'crypto' in e['tags']:
        return []
    out = []
    for m in e['markets']:
        if not m['live'] or m['bid'] is None or m['ask'] is None or not m['end']:
            continue
        bid, ask = float(m['bid']), float(m['ask'])
        mid = (bid + ask) / 2
        days = (datetime.date.fromisoformat(m['end']) - TODAY).days
        if ask - bid > 0.15 or not 0.05 <= mid <= 0.95 or not 4 <= days <= 900 or NOISE.search(m['q'] + ' ' + e['title']):
            continue
        out.append(dict(m, mid=mid, days=days))
    return out


def cmd_scan(a):
    st = load()
    picks = st['picks']
    with ThreadPoolExecutor(6) as ex:
        fresh = dict(ex.map(reprice, picks))
    for p in picks:
        r = fresh[p['id']]
        if 'err' in r and len(r) == 1:
            p['err'] = r['err']
            continue
        p.pop('err', None)
        p.update(r)
        p['priced'] = NOW.strftime('%Y-%m-%d %H:%M')
    write(STATE, st)

    print(f'sweeping {len(TAGS)} tags ...', flush=True)
    events = {}
    with ThreadPoolExecutor(4) as ex:
        for evs in ex.map(sweep_tag, TAGS):
            for e in evs:
                events[e['id']] = e
    seen = set(st.get('seen', []))
    hold_events = [e for e in events.values() if any(m['hold'] for m in e['markets'])]
    def mkts(ms):
        return '; '.join(f"{(m['g'] or 'Yes')[:24]} {m['mid']:.2f}" for m in ms[:6])
    lines = []
    new = [e for e in events.values() if e['id'] not in seen and candidate_markets(e)]
    for e in sorted(new, key=lambda e: min(m['days'] for m in candidate_markets(e))):
        ms = sorted(candidate_markets(e), key=lambda m: -m['mid'])
        apr = ' APR' if any(m['hold'] for m in e['markets']) else ''
        lines.append(f"{e['id']} [{min(m['days'] for m in ms)}d L${sum(m['liq'] for m in ms) / 1000:.0f}k{apr}] {e['title'][:80]}"
                     f" :: {mkts(ms)}")
    os.makedirs(WORK, exist_ok=True)
    with open(CANDIDATES, 'w') as fh:
        fh.write(f'# New events since the last sweep ({len(new)}), nearest end first. Format: '
                 'event id [days to first end, liquidity, APR = pays the holding reward] title :: market YES-mid; ...\n')
        fh.write('\n'.join(lines) + '\n')
    st['seen'] = sorted(seen | set(events))
    st['hold_events'] = len(hold_events)
    st['swept'] = dict(events=len(events), markets=sum(len(e['markets']) for e in events.values()))
    write(STATE, st)

    print(f"hunt scan {NOW:%Y-%m-%d %H:%M}: {len(picks)} picks repriced; swept {len(events)} events; "
          f"{len(hold_events)} events pay the APR reward")
    for p in picks:
        if p.get('closed'):
            print(f"RESOLVED {p['id']} {p['name']}: our {p['side']} {'won' if p['won'] else 'lost'} (moves to Resolved at build)")
    for p in picks:
        if p.get('closed'):
            continue
        why = []
        if p.get('err'):
            why.append(p['err'][:60])
        elif not p.get('ask'):
            why.append('no ask on the book')
        if p.get('ask') and p.get('prev_ask') and abs(p['ask'] - p['prev_ask']) >= REVIEW_MOVE:
            why.append(f"ask moved {(p['ask'] - p['prev_ask']) * 100:+.0f}¢")
        days = (datetime.date.fromisoformat(p['end']) - TODAY).days
        if days <= SOON_DAYS:
            why.append(f'ends in {days}d')
        e = econ(p)
        if e and e['tot'] < MIN_EDGE:
            why.append(f"edge gone ({e['tot'] * 100:+.0f}%), dropped at build unless the fair value changes")
        if why:
            prev = f"{p['prev_ask']:.3g}→" if p.get('prev_ask') else ''
            ask = f"{p['ask']:.3g}" if p.get('ask') else '–'
            ret = f"{e['tot'] * 100:+.0f}%" if e else '–'
            print(f"REVIEW {p['id']} [{'; '.join(why)}] {p['name']} | {p['side']} ask {prev}{ask} fair {p['fair']:.2f} return {ret}")
    print(f'NEW {len(new)} new candidate events -> {os.path.relpath(CANDIDATES, ROOT)}')


# ---------------------------------------------------------------- edit the book

def cmd_list(a):
    st = load()
    rows = sorted(st['picks'], key=lambda p: -(econ(p) or {'tot': -9})['tot'])
    for p in rows:
        e = econ(p)
        print(f"{p['id']:>8} {p['side']:3} ask {p.get('ask') or '–':<6} fair {p['fair']:.3f} "
              f"{(e['tot'] * 100 if e else 0):+5.0f}% {'APR ' if p.get('hold') else '    '}{p.get('tag') or '':14.14} {p['name']}")


def cmd_show(a):
    st = load()
    p = find(st, a.id)
    print(json.dumps({k: v for k, v in p.items() if k != 'smart'}, indent=1, ensure_ascii=False))
    print('return:', econ(p))
    m = get(f"{GAMMA}/markets/{p['id']}")
    print('\nquestion:', m.get('question'))
    print('rules:', (m.get('description') or '').strip()[:3000])


def cmd_peek(a):
    e = event_by_ref(a.event)
    print(f"{e['id']} {e['title']}  https://polymarket.com/event/{e['slug']}")
    ms = [m for m in e.get('markets') or [] if not m.get('closed')]
    for m in ms:
        bid, ask = m.get('bestBid'), m.get('bestAsk')
        no = f'NO ask ~{1 - float(bid):.3g}' if bid is not None else 'no YES bid'
        print(f"  {m['question']} | YES bid {bid} ask {ask} | {no} | ends {(m.get('endDate') or '')[:10]}"
              f"{' | APR' if m.get('holdingRewardsEnabled') else ''}")
    if ms:
        print('\nrules:', (ms[0].get('description') or '').strip()[:2500])


def cmd_set(a):
    st = load()
    p = find(st, a.id)
    if a.fair is not None:
        p['fair'] = a.fair
    for k in ('why', 'name', 'tag'):
        if getattr(a, k) is not None:
            p[k] = getattr(a, k)
    p['reviewed'] = str(TODAY)
    write(STATE, st)
    e = econ(p)
    print(f"set {p['id']} {p['name']}: fair {p['fair']:.3f}, return {e['tot'] * 100:+.0f}%" if e else f"set {p['id']}")


def cmd_drop(a):
    st = load()
    p = find(st, a.id)
    st['picks'].remove(p)
    st['dropped'].append(dict(p, reason=a.reason))
    write(STATE, st)
    print(f"dropped {p['id']} {p['name']}")


def cmd_add(a):
    st = load()
    e = event_by_ref(a.event)
    words = a.question.lower().split()
    ms = [m for m in e.get('markets') or [] if not m.get('closed') and all(w in m['question'].lower() for w in words)]
    if len(ms) != 1:
        print(f"{len(ms)} open markets in event {e['id']} ({e.get('title')}) match {a.question!r}:")
        for m in e.get('markets') or []:
            if not m.get('closed'):
                print(f"  {m['question']}")
        sys.exit(1)
    m = ms[0]
    if any(p['id'] == m['id'] for p in st['picks']):
        sys.exit(f"already in the book as {m['id']}; use `set`")
    token = json.loads(m['clobTokenIds'])[0 if a.side == 'YES' else 1]
    p = dict(id=m['id'], cid=m['conditionId'], token=token, side=a.side, event=e['id'], q=m['question'],
             name=a.name or m['question'].rstrip('?'), url=f"https://polymarket.com/event/{e['slug']}",
             end=a.end or (m.get('endDate') or '')[:10], fair=a.fair, why=a.why, tag=a.tag or '',
             added=str(TODAY), reviewed=str(TODAY), prev_ask=None, prev_fair=None,
             hold=bool(m.get('holdingRewardsEnabled')), fee=(m.get('feeSchedule') or {}).get('rate'))
    p.update(book(token))
    p['priced'] = NOW.strftime('%Y-%m-%d %H:%M')
    st['picks'].append(p)
    write(STATE, st)
    x = econ(p)
    print(f"added {p['id']} {p['name']}: {a.side} ask {p['ask']} (${p['depth']} within 2¢), fair {a.fair}, "
          f"return {x['tot'] * 100:+.0f}%{' incl. APR reward' if p['hold'] else ''}, ends {p['end']}" if x else
          f"added {p['id']} {p['name']} (no ask on the book)")
    if x and x['tot'] < MIN_EDGE:
        print(f"warning: below the +{MIN_EDGE * 100:.0f}% floor, so build will drop it again")


def cmd_note(a):
    st = load()
    st['notes'].append(a.text)
    write(STATE, st)
    print(f"{len(st['notes'])} notes")


# ---------------------------------------------------------------- smart money

_lock, _last = threading.Lock(), [0.0]


def throttle(rate=3.5):
    with _lock:
        now = time.time()
        wait = _last[0] + 1 / rate - now
        if wait > 0:
            time.sleep(wait)
        _last[0] = max(now, _last[0] + 1 / rate)


def leaderboard(addr):
    for i in range(4):
        throttle()
        try:
            d = get(f'{DATA}/v2/leaderboard?user={addr}&timePeriod=all', tries=1, timeout=10).get('data') or {}
            return addr, d.get('pnl')
        except Exception:
            time.sleep(1.5 * (i + 1))
    return addr, 'fail'


def cmd_smart(a):
    st = load()
    wallets = read(WALLETS)
    picks = [p for p in st['picks'] if not p.get('closed')]

    def holders(p):
        try:
            return p['id'], get(f"{DATA}/holders?market={p['cid']}&limit=20", tries=3)
        except Exception:
            return p['id'], None
    with ThreadPoolExecutor(4) as ex:
        H = dict(ex.map(holders, picks))
    seen = {h['proxyWallet'] for v in H.values() if v for s in v for h in s['holders']} - {ADAPTER}
    stale = str(TODAY - datetime.timedelta(days=WALLET_TTL_DAYS))
    todo = sorted(w for w in seen if w not in wallets) + sorted(w for w in seen if w in wallets and wallets[w][1] < stale)
    todo = todo[:MAX_WALLET_LOOKUPS]
    print(f'{len(picks)} picks, {len(seen)} holder wallets, scoring {len(todo)} new or stale ones ...', flush=True)
    with ThreadPoolExecutor(4) as ex:
        for addr, pnl in ex.map(leaderboard, todo):
            if pnl != 'fail':
                wallets[addr] = [round(pnl) if pnl is not None else None, str(TODAY)]
    write(WALLETS, wallets, indent=None)
    for p in picks:
        v = H.get(p['id'])
        if not v:
            continue
        smart = {'YES': 0.0, 'NO': 0.0}
        names = {'YES': [], 'NO': []}
        for s in v:
            for h in s['holders']:
                name = h.get('name') or h.get('pseudonym') or h['proxyWallet'][:8]
                if h['proxyWallet'] == ADAPTER or name.lower() in MAKERS:
                    continue
                pnl = (wallets.get(h['proxyWallet']) or [None])[0]
                if pnl is not None and pnl >= SMART_PNL:
                    sd = 'YES' if h.get('outcomeIndex') == 0 else 'NO'
                    smart[sd] += float(h.get('amount') or 0)
                    names[sd].append(f'{name} (+${pnl / 1000:,.0f}k)')
        ours, theirs = p['side'], 'YES' if p['side'] == 'NO' else 'NO'
        px = p.get('ask') or 0.5
        vo, vt = smart[ours] * px, smart[theirs] * (1 - px)
        p['smart'] = dict(share=vo / (vo + vt) if vo + vt >= SMART_MIN_USD else None, ours=round(vo), theirs=round(vt),
                          top_ours=names[ours][:3], top_theirs=names[theirs][:3], day=str(TODAY))
    write(STATE, st)
    for p in picks:
        s = p.get('smart')
        if s and s['share'] is not None and s['share'] <= 0.4:
            print(f"SMART AGAINST {p['id']} {p['name']}: {s['share'] * 100:.0f}% on our side (${s['ours']:,} v ${s['theirs']:,}); "
                  f"their side: {', '.join(s['top_theirs'])}")
    print('smart done')


# ---------------------------------------------------------------- build

def cmd_build(a):
    st = load()
    keep = []
    for p in st['picks']:
        if p.get('closed'):
            st['resolved'].append(p)
            continue
        e = econ(p)
        if e and e['tot'] < MIN_EDGE:
            prev = f"{p['prev_ask']:.3g}→" if p.get('prev_ask') else ''
            st['dropped'].append(dict(p, reason=f"Edge gone: ask {prev}{p['ask']:.3g}, fair {p['fair']:.2f}"))
            continue
        keep.append(p)
    st['picks'] = keep
    rows = [(p, econ(p)) for p in keep if econ(p)]
    rows.sort(key=lambda r: -r[1]['tot'])
    main = [r for r in rows if r[1]['tot'] >= MAIN_EDGE]
    extra = [r for r in rows if r[1]['tot'] < MAIN_EDGE]
    noask = [p for p in keep if not econ(p)]
    st['built'] = NOW.isoformat(timespec='seconds')

    os.makedirs(SITE, exist_ok=True)
    with open(os.path.join(SITE, 'index.html'), 'w') as fh:
        fh.write(render(st, main, extra, noask))
    write(STATE, st)
    write(os.path.join(SITE, 'state.json'), st)
    with open(WALLETS) as src, open(os.path.join(SITE, 'wallets.json'), 'w') as dst:
        dst.write(src.read())
    short = lambda p, e: dict(name=p['name'], side=p['side'], ask=p['ask'], fair=p['fair'], tot=round(e['tot'], 4),
                              hold=bool(p.get('hold')), url=p['url'])
    summary = dict(day=str(TODAY), built=st['built'], main=len(main), extra=len(extra),
                   hold=sum(1 for p, _ in rows if p.get('hold')), dropped=len(st['dropped']),
                   new=sum(1 for p, _ in rows if p.get('added') == str(TODAY)),
                   top=[short(p, e) for p, e in main[:5]],
                   resolved=[dict(name=p['name'], side=p['side'], won=p['won']) for p in st['resolved']],
                   notes=st['notes'])
    write(os.path.join(SITE, 'summary.json'), summary)
    print(f"built site/hunt/index.html: {len(main)} main, {len(extra)} appendix, {len(noask)} without an ask, "
          f"{summary['hold']} earn the APR reward, {summary['new']} new today, {len(st['dropped'])} dropped, "
          f"{len(st['resolved'])} resolved")
    for p, e in main[:10]:
        print(f"  {e['tot'] * 100:+4.0f}% {'APR ' if p.get('hold') else '    '}{p['side']} {p['ask']:.3g} fair {p['fair']:.2f}  {p['name']}")
    for p in st['dropped']:
        print(f"  dropped: {p['name']}: {p['reason']}")
    for p in st['resolved']:
        print(f"  resolved: {p['name']}: our {p['side']} {'won' if p['won'] else 'lost'}")


# ---------------------------------------------------------------- page

def esc(s):
    return html.escape(str(s or ''))


def pct(x):
    return f'{x * 100:+.0f}%'


def px(x):
    return f'{x:.3g}' if x is not None else '–'


def row_html(i, p, e):
    side = f'<span class="side {p["side"].lower()}">{p["side"]}</span>'
    badges = ''
    if p.get('hold'):
        badges += '<span class="apr">3.25% APR</span>'
    if p.get('tag'):
        badges += f'<span class="tag">{esc(p["tag"])}</span>'
    if p.get('added') == str(TODAY):
        badges += '<span class="new">New</span>'
    prev = p.get('prev_ask')
    if p.get('added') == str(TODAY) or prev is None:
        move = ''
    elif abs(p['ask'] - prev) < 0.005:
        move = '<div class="sub">unchanged</div>'
    else:  # cheaper entry is good news for us
        move = f'<div class="sub {"up" if p["ask"] < prev else "down"}">was {px(prev)}</div>'
    fair_ch = ''
    if p.get('prev_fair') is not None and abs(p['fair'] - p['prev_fair']) >= 0.005:
        fair_ch = f'<div class="sub">was {p["prev_fair"]:.2f}</div>'
    tone = 'r3' if e['tot'] >= 0.4 else 'r2' if e['tot'] >= 0.2 else 'r1' if e['tot'] >= 0.1 else 'r0'
    bonus = f'<div class="sub gold">incl. +{e["bonus"] * 100:.1f}% reward</div>' if p.get('hold') else ''
    per_yr = f'{e["ann"] * 100:.0f}%' if e['yrs'] >= 0.5 else '<span class="faint">&lt;6 mo</span>'
    s = p.get('smart') or {}
    if s.get('share') is None:
        smart = '<span class="faint">–</span>'
    else:
        v = s['share'] * 100
        t = 'up' if v >= 60 else 'down' if v <= 40 else ''
        tip = esc('Ours: ' + (', '.join(s['top_ours']) or 'none') + ' · Theirs: ' + (', '.join(s['top_theirs']) or 'none'))
        smart = f'<b class="{t}" title="{tip}">{v:.0f}%</b><div class="sub">${s["ours"]:,} v ${s["theirs"]:,}</div>'
    end = datetime.date.fromisoformat(p['end'])
    return f'''<tr>
  <td class="n">{i}</td>
  <td class="mkt"><a href="{esc(p['url'])}">{esc(p['name'])}</a><div class="meta">{side}{badges}</div><p class="why">{esc(p['why'])}</p></td>
  <td class="num" data-k="Ask"><b>{px(p['ask'])}</b>{move}</td>
  <td class="num" data-k="Fair"><b>{p['fair']:.2f}</b>{fair_ch}</td>
  <td class="num ret {tone}" data-k="Return"><b>{pct(e['tot'])}</b>{bonus}</td>
  <td class="num" data-k="Per yr">{per_yr}</td>
  <td class="num" data-k="Smart $">{smart}</td>
  <td class="num" data-k="Depth">${p.get('depth') or 0:,}</td>
  <td class="num" data-k="Ends">{end:%-d %b %Y}</td>
</tr>'''


def table_html(rows):
    head = ('<thead><tr><th class="n">#</th><th class="mkt">Market</th><th>Ask</th><th>Fair</th><th>Return</th>'
            '<th>Per yr</th><th title="Share of smart-money dollars on our side">Smart $</th><th>Depth</th><th>Ends</th></tr></thead>')
    body = '\n'.join(row_html(i, p, e) for i, (p, e) in enumerate(rows, 1))
    return f'<div class="table-box"><table class="hunt">{head}<tbody>{body}</tbody></table></div>'


def render(st, main, extra, noask):
    rows = main + extra
    n_hold = sum(1 for p, _ in rows if p.get('hold'))
    clusters = Counter(p.get('tag') for p, _ in rows if p.get('tag'))
    clusters = [f'{esc(t)} ({n})' for t, n in clusters.most_common() if n >= 2]
    boxes = []
    if st['notes']:
        boxes.append('<div class="box warn"><h3>What changed</h3><ul>' +
                     ''.join(f'<li>{esc(n)}</li>' for n in st['notes']) + '</ul></div>')
    if st['resolved']:
        boxes.append('<div class="box"><h3>Resolved since the last run</h3><ul>' + ''.join(
            f'<li><a href="{esc(p["url"])}">{esc(p["name"])}</a>: our {p["side"]} '
            f'<b class="{"up" if p["won"] else "down"}">{"won" if p["won"] else "lost"}</b></li>' for p in st['resolved']) +
            '</ul></div>')
    boxes.append(f'<div class="box gold"><h3>APR rewards</h3><p>Polymarket pays a 3.25% annual holding reward on the '
                 f'mid-price value of YES and NO positions in {st.get("hold_events", "the")} eligible events, sampled hourly '
                 f'and paid daily. {n_hold} picks below earn it (gold badge); their return includes the reward to resolution, '
                 'assuming the rate holds. It matters most on long-dated NO carry: a 2028 NO earns about 7% from the reward alone.</p></div>')
    if clusters:
        boxes.append('<div class="box"><h3>Correlation clusters</h3><p>Size each as one bet: ' + ' · '.join(clusters) +
                     '. Books under $200 are token size; use resting bids.</p></div>')
    dropped = ''
    if st['dropped']:
        dropped = ('<section><h2>Dropped today</h2><ul class="plain">' + ''.join(
            f'<li><a href="{esc(p["url"])}">{esc(p["name"])}</a> <span class="faint">{p["side"]} '
            f'{px(p.get("prev_ask"))} → {px(p.get("ask"))}</span><div class="li-body">{esc(p["reason"])}</div></li>'
            for p in st['dropped']) + '</ul></section>')
    noask_html = ''
    if noask:
        noask_html = ('<section><h2>No sellers today</h2><p class="note">Still in the book, but nobody is offering our side, '
                      'so there is no return to show.</p><ul class="plain">' + ''.join(
                          f'<li><a href="{esc(p["url"])}">{esc(p["name"])}</a> <span class="faint">{p["side"]}, fair '
                          f'{p["fair"]:.2f}</span></li>' for p in noask) + '</ul></section>')
    main_html = table_html(main) if main else '<p class="note">Nothing clears +10% today.</p>'
    extra_html = f'<section><h2>Smaller edges (+5–10%)</h2>{table_html(extra)}</section>' if extra else ''
    swept = st.get('swept') or {}
    return f'''<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex">
<title>Mispricing hunt · {TODAY:%-d %b %Y}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Bricolage+Grotesque:opsz,wght@12..96,600;12..96,700&family=IBM+Plex+Mono:wght@400;500;600&family=Schibsted+Grotesk:wght@400;500;600&display=swap">
<style>
:root {{
  --ground: #F3F5F9; --surface: #FFFFFF; --surface-2: #EEF1F7; --ink: #141A26; --muted: #5D6679; --faint: #8A93A6;
  --rule: #DCE1EA; --accent: #2F4BCF; --up: #177A47; --down: #BD3434; --warn-bg: #FFF1D6; --warn-ink: #8A5300;
  --gold: #8F6200; --gold-bg: #FFF4D1; --gold-line: #E9C46A;
  --display: "Bricolage Grotesque", "Schibsted Grotesk", system-ui, sans-serif;
  --body: "Schibsted Grotesk", system-ui, -apple-system, "Segoe UI", sans-serif;
  --mono: "IBM Plex Mono", ui-monospace, "SF Mono", Menlo, monospace;
}}
@media (prefers-color-scheme: dark) {{
  :root:not([data-theme="light"]) {{
    --ground: #0D111A; --surface: #141A25; --surface-2: #1A2130; --ink: #E7EAF1; --muted: #9AA3B5; --faint: #6E7789;
    --rule: #262E3D; --accent: #8CA0FF; --up: #4FC98C; --down: #F27A7A; --warn-bg: #3A2A0B; --warn-ink: #F5C165;
    --gold: #F2C14E; --gold-bg: #33290C; --gold-line: #7A5D16;
  }}
}}
:root[data-theme="dark"] {{
  --ground: #0D111A; --surface: #141A25; --surface-2: #1A2130; --ink: #E7EAF1; --muted: #9AA3B5; --faint: #6E7789;
  --rule: #262E3D; --accent: #8CA0FF; --up: #4FC98C; --down: #F27A7A; --warn-bg: #3A2A0B; --warn-ink: #F5C165;
  --gold: #F2C14E; --gold-bg: #33290C; --gold-line: #7A5D16;
}}
* {{ box-sizing: border-box; }}
body {{ margin: 0; background: var(--ground); color: var(--ink); font-family: var(--body); font-size: 15px; line-height: 1.5; }}
.wrap {{ max-width: 1240px; margin: 0 auto; padding: 40px 28px 64px; display: grid; gap: 28px; }}
a {{ color: inherit; text-decoration: none; }}
a:hover {{ color: var(--accent); }}
a:focus-visible {{ outline: 2px solid var(--accent); outline-offset: 2px; border-radius: 2px; }}
.up {{ color: var(--up); }} .down {{ color: var(--down); }} .faint {{ color: var(--faint); }} .gold {{ color: var(--gold); }}
header {{ display: flex; flex-wrap: wrap; justify-content: space-between; align-items: end; gap: 12px 32px; }}
.eyebrow {{ font-size: 12px; letter-spacing: .08em; text-transform: uppercase; color: var(--muted); font-weight: 600; }}
.eyebrow a {{ color: var(--accent); }}
h1 {{ font-family: var(--display); font-weight: 700; font-size: clamp(30px, 4vw, 42px); line-height: 1.05; letter-spacing: -.02em; margin: 6px 0 0; }}
.lede {{ margin: 8px 0 0; color: var(--muted); max-width: 72ch; font-size: 14px; }}
.stamp {{ font-family: var(--mono); font-size: 12.5px; color: var(--muted); text-align: right; line-height: 1.6; }}
.summary {{ display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); border-top: 2px solid var(--ink); border-bottom: 1px solid var(--rule); }}
.stat {{ padding: 14px 20px 16px 0; display: grid; gap: 2px; align-content: start; }}
.stat + .stat {{ padding-left: 20px; border-left: 1px solid var(--rule); }}
.stat .k {{ font-size: 12px; letter-spacing: .06em; text-transform: uppercase; color: var(--muted); font-weight: 600; }}
.stat .v {{ font-family: var(--mono); font-size: 24px; font-weight: 500; }}
.stat .s {{ font-size: 12.5px; color: var(--muted); }}
.stat.goldk {{ background: var(--gold-bg); padding-left: 20px; }}
.stat.goldk .v {{ color: var(--gold); }}
.boxes {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(340px, 1fr)); gap: 14px; }}
.box {{ border-left: 3px solid var(--accent); background: var(--surface); border-radius: 0 8px 8px 0; padding: 12px 16px; font-size: 14px; }}
.box.warn {{ border-color: var(--warn-ink); }}
.box.gold {{ border-color: var(--gold-line); background: var(--gold-bg); }}
.box h3 {{ margin: 0 0 4px; font-size: 12px; letter-spacing: .06em; text-transform: uppercase; color: var(--muted); }}
.box p {{ margin: 0; }}
.box ul {{ margin: 0; padding-left: 18px; display: grid; gap: 4px; }}
h2 {{ font-family: var(--display); font-size: 19px; font-weight: 600; letter-spacing: -.01em; margin: 0 0 10px; }}
.table-box {{ background: var(--surface); border: 1px solid var(--rule); border-radius: 10px; overflow-x: auto; }}
table.hunt {{ width: 100%; border-collapse: collapse; }}
th {{ text-align: right; font-size: 11.5px; letter-spacing: .06em; text-transform: uppercase; color: var(--muted); font-weight: 600; padding: 12px 12px 8px; border-bottom: 1px solid var(--rule); white-space: nowrap; }}
th.mkt {{ text-align: left; }}
td {{ padding: 12px; border-bottom: 1px solid var(--rule); vertical-align: top; }}
tr:last-child td {{ border-bottom: 0; }}
td.n, th.n {{ width: 36px; color: var(--faint); text-align: right; font-family: var(--mono); font-size: 12.5px; padding-right: 0; }}
td.mkt {{ min-width: 300px; width: 42%; }}
td.mkt a {{ font-weight: 600; line-height: 1.35; }}
.meta {{ display: flex; flex-wrap: wrap; gap: 4px 6px; margin-top: 5px; }}
.side, .apr, .tag, .new {{ font-size: 11px; font-weight: 600; padding: 1px 6px; border-radius: 4px; line-height: 1.5; }}
.side {{ font-family: var(--mono); border: 1px solid var(--rule); background: var(--surface-2); }}
.side.no {{ color: var(--down); }} .side.yes {{ color: var(--up); }}
.apr {{ color: var(--gold); background: var(--gold-bg); border: 1px solid var(--gold-line); }}
.tag {{ color: var(--muted); border: 1px solid var(--rule); }}
.new {{ color: var(--accent); border: 1px solid color-mix(in srgb, var(--accent) 40%, transparent); }}
.why {{ margin: 6px 0 0; font-size: 13px; color: var(--muted); max-width: 80ch; }}
td.num {{ text-align: right; font-family: var(--mono); font-size: 14px; white-space: nowrap; font-variant-numeric: tabular-nums; }}
td.num b {{ font-weight: 500; }}
td.ret b {{ font-weight: 600; }}
td.r3 b {{ color: var(--up); }} td.r2 b {{ color: color-mix(in srgb, var(--up) 75%, var(--ink)); }} td.r0 b {{ color: var(--muted); }}
.sub {{ font-size: 12px; color: var(--muted); margin-top: 2px; }}
.sub.up {{ color: var(--up); }} .sub.down {{ color: var(--down); }} .sub.gold {{ color: var(--gold); }}
.note {{ color: var(--muted); font-size: 13.5px; margin: 0 0 8px; max-width: 90ch; }}
ul.plain {{ list-style: none; margin: 0; padding: 0; display: grid; gap: 10px; }}
ul.plain li {{ padding-top: 10px; border-top: 1px solid var(--rule); }}
ul.plain a {{ font-weight: 600; }}
.li-body {{ color: var(--muted); font-size: 13.5px; }}
footer {{ color: var(--muted); font-size: 12.5px; max-width: 110ch; display: grid; gap: 6px; }}
footer p {{ margin: 0; }}
@media (max-width: 720px) {{
  .wrap {{ padding: 24px 16px 48px; }}
  .stamp {{ text-align: left; }}
  .summary {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
  .stat, .stat + .stat {{ padding: 12px 12px 12px 0; border-left: 0; border-bottom: 1px solid var(--rule); }}
  .stat.goldk {{ padding-left: 12px; }}
  .boxes {{ grid-template-columns: 1fr; }}
}}
@media (max-width: 1040px) {{
  table.hunt thead {{ display: none; }}
  table.hunt, table.hunt tbody {{ display: block; }}
  table.hunt tr {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(88px, 1fr)); gap: 8px 12px; padding: 14px 14px 16px; border-bottom: 1px solid var(--rule); }}
  table.hunt tr:last-child {{ border-bottom: 0; }}
  table.hunt td {{ padding: 0; border: 0; }}
  td.n {{ display: none; }}
  td.mkt {{ grid-column: 1 / -1; min-width: 0; }}
  td.num {{ text-align: left; white-space: normal; }}
  td.num::before {{ content: attr(data-k); display: block; font-family: var(--body); font-size: 11px; letter-spacing: .06em; text-transform: uppercase; color: var(--faint); font-weight: 600; }}
}}
@media print {{
  @page {{ size: A4 landscape; margin: 10mm; }}
  :root {{ --ground: #fff; --surface: #fff; --surface-2: #f4f4f4; --ink: #111; --muted: #444; --faint: #777; --rule: #ddd;
           --gold: #8F6200; --gold-bg: #FFF4D1; --gold-line: #E9C46A; --up: #177A47; --down: #BD3434; --accent: #2F4BCF; }}
  body {{ font-size: 9pt; }}
  .wrap {{ max-width: none; padding: 0; gap: 12px; }}
  .eyebrow a {{ display: none; }}
  td {{ padding: 5px 6px; }} th {{ padding: 6px; }}
  td.mkt {{ min-width: 0; width: 42%; }}
  tr {{ break-inside: avoid; }}
  .table-box {{ border: 0; }}
  * {{ -webkit-print-color-adjust: exact; print-color-adjust: exact; }}
}}
</style></head>
<body><div class="wrap">
  <header>
    <div>
      <div class="eyebrow"><a href="/">← Morning brief</a> · Polymarket</div>
      <h1>Mispricing hunt</h1>
      <p class="lede">Markets where my fair value beats the best ask, mostly "nothing ever happens" NO bets on politics and
      geopolitics. Return is fair ÷ ask − 1 held to resolution, plus the holding reward where it applies. Not financial advice.</p>
    </div>
    <div class="stamp">Asks at {NOW:%-d %b %Y, %H:%M}<br>{swept.get('events', 0):,} events swept<br>{len(st['picks'])} picks in the book</div>
  </header>
  <section class="summary" aria-label="Summary">
    <div class="stat"><span class="k">≥ +10% upside</span><span class="v">{len(main)}</span><span class="s">main list</span></div>
    <div class="stat"><span class="k">Smaller edges</span><span class="v">{len(extra)}</span><span class="s">+5–10%</span></div>
    <div class="stat goldk"><span class="k">Earn 3.25% APR</span><span class="v">{n_hold}</span><span class="s">gold badge</span></div>
    <div class="stat"><span class="k">Changes today</span><span class="v">{sum(1 for p, _ in rows if p.get('added') == str(TODAY))} / {len(st['dropped'])}</span><span class="s">new / dropped</span></div>
  </section>
  <div class="boxes">{''.join(boxes)}</div>
  <section><h2>Main list: {len(main)} picks with at least +10% upside</h2>{main_html}</section>
  {extra_html}
  {noask_html}
  {dropped}
  <footer>
    <p><b>Ask</b> is the best offer for our side now; "was" shows the last run's ask (green = cheaper today). <b>Fair</b> is my
    probability that our side pays out. <b>Per yr</b> divides the return by years to resolution (simple, not compounded).
    <b>Depth</b> is the dollars on offer at or within 2¢ of the ask.</p>
    <p><b>Smart $</b> is the share of dollars held by wallets with at least $50k all-time profit (Polymarket leaderboard) among
    the top 20 holders of each side that sits on our side; green ≥ 60%, red ≤ 40%, "–" under $300 of such money. Two
    liquidity-reward makers and the neg-risk adapter are excluded; other makers and basket hedgers are not, so read it as a hint.</p>
    <p>Holding-reward eligibility is Gamma's <i>holdingRewardsEnabled</i> flag, read today. Prices from Polymarket's CLOB.
    Rebuilt every morning by the morning brief routine.</p>
  </footer>
</div></body></html>
'''


# ---------------------------------------------------------------- cli

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest='cmd', required=True)
    s = sub.add_parser('pull'); s.add_argument('--fresh', action='store_true'); s.set_defaults(f=cmd_pull)
    sub.add_parser('scan').set_defaults(f=cmd_scan)
    sub.add_parser('list').set_defaults(f=cmd_list)
    s = sub.add_parser('show'); s.add_argument('id'); s.set_defaults(f=cmd_show)
    s = sub.add_parser('peek'); s.add_argument('event'); s.set_defaults(f=cmd_peek)
    s = sub.add_parser('set'); s.add_argument('id'); s.add_argument('--fair', type=float); s.add_argument('--why')
    s.add_argument('--name'); s.add_argument('--tag'); s.set_defaults(f=cmd_set)
    s = sub.add_parser('drop'); s.add_argument('id'); s.add_argument('--reason', required=True); s.set_defaults(f=cmd_drop)
    s = sub.add_parser('add'); s.add_argument('event'); s.add_argument('question')
    s.add_argument('--side', choices=['YES', 'NO'], default='NO'); s.add_argument('--fair', type=float, required=True)
    s.add_argument('--why', required=True); s.add_argument('--name'); s.add_argument('--tag'); s.add_argument('--end')
    s.set_defaults(f=cmd_add)
    s = sub.add_parser('note'); s.add_argument('text'); s.set_defaults(f=cmd_note)
    sub.add_parser('smart').set_defaults(f=cmd_smart)
    sub.add_parser('build').set_defaults(f=cmd_build)
    a = ap.parse_args()
    for k in ('fair',):
        v = getattr(a, k, None)
        if v is not None and not 0 < v < 1:
            sys.exit('--fair must be a probability between 0 and 1')
    a.f(a)


if __name__ == '__main__':
    main()
