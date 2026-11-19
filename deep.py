#!/usr/bin/env python3
"""Hourly deep dives: pick the events in the wallet that deserve one and publish each as a sub-page of the brief.

    python3 deep.py candidates           # rank the wallet's events; the ELIGIBLE ones may get a deep dive now
    python3 deep.py show <event slug>    # one event: rules, books, the wallet's markets, the last deep dive
    python3 deep.py add <event slug> --title .. --verdict .. --body <file> [--fair <asset>=<p> ...]
    python3 deep.py publish [--preview]  # the live site as it is, plus this run's deep dives, deployed
    python3 deep.py sync                 # live deep dives -> site/deep/, links on site/index.html (deploy.py runs it)

An event is eligible when its price moved significantly in the last 24 hours, that move had a significant effect
on the wallet's 24h P/L, and no recent deep dive was written with the price where it is now. See README, "Deep dives".

Like the hunt's book and the run log, the deep dives live in the deployment: site/deep/index.json (one entry per
event, the latest dive) and site/deep/<event>/index.html. A new dive of an event replaces the old one; the others
stay until they are replaced too. Standard library only; must stay valid Python 3.11 (the cloud environment's).
"""
import argparse, datetime, gzip, hashlib, html, json, os, re, shutil, subprocess, sys, time
import urllib.error, urllib.parse, urllib.request
from concurrent.futures import ThreadPoolExecutor

os.environ['TZ'] = 'Europe/Copenhagen'  # cloud runners are on UTC; every time on the pages is Copenhagen
time.tzset()

WALLET = '0x9d4a9ff98bdce01ff8081362f676749db0c85887'
HERE = os.path.dirname(os.path.abspath(__file__))
SITE = os.path.join(HERE, 'site')
DEEP = os.path.join(SITE, 'deep')
INDEX = os.path.join(DEEP, 'index.json')
WORK = os.path.join(HERE, '.deep')  # working files (ignored)
CANDS = os.path.join(WORK, 'candidates.json')
LIVE = 'https://nothing-ever-happens-book.vercel.app/'
PUBLIC = 'https://morningbrief.dorup.dk/'
TEAM = 'team_45ZNkd6F5HMXNUu6jGjrKr44'      # OxeanX
PROJECT = 'prj_oldsDQ1BxZ7gn3AqRnwUXLCpxC05'  # nothing-ever-happens-book
GAMMA = 'https://gamma-api.polymarket.com'
CLOB = 'https://clob.polymarket.com'
DATA = 'https://data-api.polymarket.com'
UA = {'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/120 Safari/537.36'}

# What makes an event worth a deep dive now.
MOVE_CENTS = 2.0    # a price moved significantly: this many cents in 24h ...
MOVE_REL = 0.15     # ... or this share of its distance to the nearer end (97¢ -> 95.5¢ is half again the risk) ...
MOVE_FLOOR = 0.5    # ... but never less than this many cents (a tick or two is noise)
USD_MIN = 20.0      # a significant effect on the 24h P/L: at least this many dollars ...
SHARE_MIN = 0.05    # ... and this share of the day's gross swing (the sum of every event's |24h change|)
FRESH_DAYS = 3      # a deep dive this recent, written with the price where it is now, still stands
COOLDOWN_H = 6      # within this many hours of a dive, only a big move since it earns another ...
BIG_CENTS = 5.0     # ... of this many cents
MAX_DIVES = 3       # per run
WIDE_BOOK = 0.06    # a bid-ask spread this wide: the midpoint can move without a trade


class Unreachable(Exception):
    """The live deep dives could not be read. Deploying now would wipe them, so deploy.py stops instead."""


def say(*a):
    print(*a, file=sys.stderr, flush=True)  # stdout stays clean for `deploy.py --body-only`


# ---------------------------------------------------------------- plumbing

def fetch(url, tries=5, timeout=30):
    """Bytes of a URL. A 404 raises at once; anything else is retried."""
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={**UA, 'Accept-Encoding': 'gzip'})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                body = r.read()
                return gzip.decompress(body) if r.headers.get('Content-Encoding') == 'gzip' else body
        except urllib.error.HTTPError as e:
            if e.code in (400, 404) or i == tries - 1:
                raise
            time.sleep(2 * (i + 1) if e.code == 429 else 1 + i)
        except Exception:
            if i == tries - 1:
                raise
            time.sleep(1 + i)


def get(url, **kw):
    return json.loads(fetch(url, **kw))


def pages(url):
    """Every row of a Data API v2 list, following its next_cursor."""
    rows, cursor = [], None
    while True:
        d = get(url + (f'&cursor={urllib.parse.quote(cursor)}' if cursor else ''))
        rows += d['data']
        cursor = d['pagination'].get('next_cursor')
        if not cursor:
            return rows


def read(path, default=None):
    try:
        with open(path, encoding='utf-8') as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def write(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + '.tmp'
    if isinstance(data, str):
        data = data.encode('utf-8')
    with open(tmp, 'wb') as fh:
        fh.write(data)
    os.replace(tmp, path)


def write_json(path, obj):
    write(path, json.dumps(obj, indent=1, ensure_ascii=False))


def cents(p):
    return f"{p * 100:.2f}".rstrip('0').rstrip('.') + '¢'


def usd(x, sign=False):
    s = f"${abs(x):,.2f}"
    if not sign:
        return s
    if round(x, 2) == 0:
        return '$0.00'
    return ('+' if x > 0 else '−') + s


def signed_cents(dc):
    return f"{'+' if dc > 0.05 else '−' if dc < -0.05 else '±'}{abs(dc):.1f}¢"


def tone(x):
    x = round(x, 2)
    return 'up' if x > 0 else 'down' if x < 0 else 'flat'


def when(ts):
    d = datetime.datetime.fromtimestamp(ts)
    return f"{d.day} {d:%b %H:%M}"


def slugify(s):
    return re.sub(r'[^a-z0-9]+', '-', s.lower()).strip('-')[:120]


def significant(then, now):
    """Did a price move significantly between two levels (0-1)?"""
    d = abs(now - then) * 100
    return d >= MOVE_CENTS or (d >= MOVE_FLOOR and d >= MOVE_REL * 100 * max(min(then, 1 - then), 0.005))


# ---------------------------------------------------------------- the wallet's last 24 hours

def wallet_rows(now):
    """One row per market the wallet holds or traded in the last 24h, with its trade-adjusted 24h change.
    The same sums as refresh.py (which runs as a script, so they are repeated here)."""
    t0 = now - 86400
    positions = pages(f'{DATA}/v2/positions?user={WALLET}&limit=500&filter_amount=0.1')
    activity = pages(f'{DATA}/v2/activity?user={WALLET}&start={t0}&limit=500')
    flows, meta = {}, {}
    for a in activity:
        if a['timestamp'] < t0 or not a.get('token_id'):
            continue
        if a['type'] not in ('TRADE', 'REDEEM'):
            continue
        f = flows.setdefault(a['token_id'], dict(buy_sh=0.0, buy_cash=0.0, sell_sh=0.0, sell_cash=0.0))
        if a['type'] == 'TRADE' and a['side'] == 'BUY':
            f['buy_sh'] += a['size']; f['buy_cash'] += a['usdc_size']
        else:  # a sale, or a redemption (a sale at the payout)
            f['sell_sh'] += a['size']; f['sell_cash'] += a['usdc_size']
        meta.setdefault(a['token_id'], a)

    def history(asset):
        h = get(f'{CLOB}/prices-history?market={asset}&startTs={t0 - 7200}&endTs={now}&fidelity=10').get('history', [])
        if not h:
            return None, None
        before = [x for x in h if x['t'] <= t0]
        return (before[-1] if before else h[0])['p'], h[-1]['p']

    rows, held = [], set()
    for p in positions:
        held.add(p['token_id'])
        rows.append(dict(asset=p['token_id'], title=p['title'], outcome=p['outcome'], event=p['event_slug'],
                         end=p['end_date'], size=p['current_size'], avg=p['avg_price'], now=p['current_price'],
                         value=p['current_value'], redeemable=p['redeemable'], closed=False))
    for asset, a in meta.items():
        if asset not in held:
            rows.append(dict(asset=asset, title=a['title'], outcome=a['outcome'], event=a.get('event_slug'),
                             end=None, size=0, avg=None, now=None, value=0, redeemable=False, closed=True))
    with ThreadPoolExecutor(8) as ex:
        hist = list(ex.map(lambda r: history(r['asset']), rows))
    for r, (p24, last) in zip(rows, hist):
        if r['now'] is None:
            r['now'] = last if last is not None else 0
        f = flows.get(r['asset'], dict(buy_sh=0, buy_cash=0, sell_sh=0, sell_cash=0))
        r['p24'] = p24 if p24 is not None else r['now']
        sh24 = r['size'] - f['buy_sh'] + f['sell_sh']
        # value now - value 24h ago - cash in + cash out (fees are inside usdc_size)
        r['d24'] = r['size'] * r['now'] - sh24 * r['p24'] - f['buy_cash'] + f['sell_cash']
        r['move'] = (r['now'] - r['p24']) * 100
        r['bought'], r['sold'] = f['buy_sh'], f['sell_sh']
    return rows


def gamma_event(slug):
    r = get(f'{GAMMA}/events?slug={urllib.parse.quote(slug)}')
    return r[0] if r else None


def quotes(ev, assets):
    """{asset: bid, ask, 24h volume, market id, question, rules, end} for the wallet's tokens in a Gamma event."""
    out = {}
    for m in (ev or {}).get('markets', []):
        try:
            tokens = json.loads(m.get('clobTokenIds') or '[]')
        except ValueError:
            continue
        for i, tok in enumerate(tokens):
            if tok not in assets:
                continue
            bid, ask = m.get('bestBid'), m.get('bestAsk')  # the YES side's
            if i == 1:  # the NO token: its bid is 1 - the YES ask
                bid, ask = (1 - ask if ask is not None else None), (1 - bid if bid is not None else None)
            out[tok] = dict(bid=bid, ask=ask, vol24=m.get('volume24hr') or 0, market=m.get('id'),
                            question=m.get('question'), rules=m.get('description') or '', end=(m.get('endDate') or '')[:10])
    return out


def rank(rows, index, now):
    """The wallet's events, biggest 24h effect first, each with whether it is eligible for a deep dive and why."""
    by_event = {}
    for r in rows:
        if r['event']:
            by_event.setdefault(r['event'], []).append(r)
    swing = sum(abs(sum(r['d24'] for r in rs)) for rs in by_event.values())
    out = []
    for slug, rs in by_event.items():
        live = [r for r in rs if not r['closed'] and not r['redeemable']]
        if not live:
            continue  # sold or resolved: nothing left to decide
        d24 = sum(r['d24'] for r in rs)
        top = max(live, key=lambda r: abs(r['move']))
        share = abs(d24) / swing if swing else 0
        e = dict(event=slug, d24=d24, share=share, move=top['move'], rows=sorted(live, key=lambda r: -abs(r['d24'])))
        moved = any(significant(r['p24'], r['now']) for r in live)
        last = (index.get('dives') or {}).get(slug)
        why = None
        if not moved:
            why = f"no price moved significantly (largest {signed_cents(top['move'])})"
        elif abs(d24) < USD_MIN or share < SHARE_MIN:
            why = f"24h effect {usd(d24, True)} is {share:.0%} of the day's swing (needs {usd(USD_MIN)} and {SHARE_MIN:.0%})"
        if last:
            then = {m['asset']: m['price'] for m in last.get('markets', [])}
            common = [r for r in live if r['asset'] in then]
            age_h = (now - last['ts']) / 3600
            since = max((abs(r['now'] - then[r['asset']]) * 100 for r in common), default=None)
            at_level = bool(common) and not any(significant(then[r['asset']], r['now']) for r in common)
            e['last'] = dict(ts=last['ts'], age_h=age_h, since=since, at_level=at_level, verdict=last.get('verdict'))
            for r in live:
                r['dive_price'] = then.get(r['asset'])
            if not why:
                if age_h < COOLDOWN_H and (since is None or since < BIG_CENTS):
                    why = f"deep dive {age_h:.1f}h ago; only a move of {BIG_CENTS:g}¢ since earns another within {COOLDOWN_H}h"
                elif age_h < FRESH_DAYS * 24 and at_level:
                    why = f"deep dive {when(last['ts'])} was written at this price level"
        e['eligible'], e['why_not'] = why is None, why
        out.append(e)
    out.sort(key=lambda e: (not e['eligible'], -abs(e['d24'])))
    return out, swing


def cmd_candidates(a):
    now = int(time.time())
    index = sync()
    rows = wallet_rows(now)
    events, swing = rank(rows, index, now)
    eligible = [e for e in events if e['eligible']]
    for e in eligible[:MAX_DIVES * 2]:  # books and volume for the ones that may be picked
        try:
            ev = gamma_event(e['event'])
            q = quotes(ev, {r['asset'] for r in e['rows']})
            e['title'] = (ev or {}).get('title')
            for r in e['rows']:
                r.update({k: v for k, v in q.get(r['asset'], {}).items() if k != 'rules'})
        except Exception as x:
            say(f"  no Gamma quotes for {e['event']}: {type(x).__name__}: {x}")
    total = sum(r['d24'] for r in rows)
    write_json(CANDS, dict(ts=now, total=total, swing=swing, events=events))
    stamp = datetime.datetime.fromtimestamp(now)
    print(f"deep-dive candidates, {stamp:%a %d %b %Y %H:%M} Copenhagen: 24h P/L {usd(total, True)}, "
          f"gross swing {usd(swing)}, {len(events)} events held, {len(index.get('dives') or {})} deep dives live")

    def lines(e):
        for r in e['rows']:
            book = ''
            if r.get('bid') is not None and r.get('ask') is not None:
                wide = ', WIDE BOOK' if r['ask'] - r['bid'] >= WIDE_BOOK else ''
                book = f"  book {r['bid']:.3f}/{r['ask']:.3f}{wide}, 24h volume ${r.get('vol24', 0):,.0f}"
            dive = f"  [dive at {cents(r['dive_price'])}]" if r.get('dive_price') is not None else ''
            print(f"      {r['title']} {r['outcome']}: {r['size']:,.0f} sh, {cents(r['p24'])} -> {cents(r['now'])} "
                  f"({signed_cents(r['move'])}), 24h {usd(r['d24'], True)}{book}{dive}")
        last = e.get('last')
        if last:
            since = f"{last['since']:.1f}¢ since" if last['since'] is not None else 'other markets held then'
            print(f"      last deep dive {when(last['ts'])} ({last['age_h']:.0f}h ago, {since}): {last.get('verdict') or ''}")

    print(f"ELIGIBLE: {len(eligible)} (pick up to {MAX_DIVES}, biggest effect first)")
    for i, e in enumerate(eligible, 1):
        print(f"  {i}. {e['event']}  24h {usd(e['d24'], True)} ({e['share']:.0%} of the swing), "
              f"largest move {signed_cents(e['move'])}")
        lines(e)
    rest = [e for e in events if not e['eligible']]
    print(f"NOT ELIGIBLE: {len(rest)} (the first 12)")
    for e in rest[:12]:
        print(f"  - {e['event']}  24h {usd(e['d24'], True)}, {signed_cents(e['move'])}: {e['why_not']}")


def cmd_show(a):
    ev = gamma_event(a.event)
    if not ev:
        sys.exit(f'no event {a.event}')
    c = read(CANDS, {})
    mine = next((e for e in c.get('events', []) if e['event'] == a.event), None)
    assets = {r['asset'] for r in mine['rows']} if mine else set()
    q = quotes(ev, assets)
    print(f"{ev.get('title')}  (https://polymarket.com/event/{a.event})")
    print(f"ends {(ev.get('endDate') or '')[:10]}, 24h volume ${ev.get('volume24hr') or 0:,.0f}, "
          f"liquidity ${ev.get('liquidity') or 0:,.0f}, {len(ev.get('markets', []))} markets")
    if mine:
        print('\nYOUR MARKETS')
        for r in mine['rows']:
            m = q.get(r['asset'], {})
            print(f"- asset {r['asset']}\n  {r['title']} {r['outcome']}: {r['size']:,.0f} shares at {cents(r['avg'])}, "
                  f"{cents(r['p24'])} -> {cents(r['now'])} in 24h ({usd(r['d24'], True)}), "
                  f"book {m.get('bid')}/{m.get('ask')}, ends {m.get('end')}")
            print('  RULES: ' + re.sub(r'\s+', ' ', m.get('rules', ''))[:2500])
    print('\nEVERY OPEN MARKET IN THE EVENT (YES bid/ask)')
    for m in ev.get('markets', []):
        if m.get('closed'):
            continue
        print(f"- {m.get('question')}: {m.get('bestBid')}/{m.get('bestAsk')}, 24h volume ${m.get('volume24hr') or 0:,.0f}")
    last = (read(INDEX, {}).get('dives') or {}).get(a.event)
    if last:
        print(f"\nLAST DEEP DIVE {when(last['ts'])}: {last.get('verdict')}\n  {LIVE}{last['page']}")
        for m in last.get('markets', []):
            fair = f", fair {cents(m['fair'])}" if m.get('fair') is not None else ''
            print(f"  {m['title']} {m['outcome']}: at {cents(m['price'])}{fair}")


# ---------------------------------------------------------------- a deep dive's page

PAGE_CSS = """
:root { --ground:#F3F5F9; --surface:#FFFFFF; --surface-2:#EEF1F7; --ink:#141A26; --muted:#5D6679; --faint:#8A93A6;
  --rule:#DCE1EA; --accent:#2F4BCF; --up:#177A47; --down:#BD3434; --warn-bg:#FFF1D6; --warn-ink:#8A5300;
  --display:"Bricolage Grotesque","Schibsted Grotesk",system-ui,sans-serif;
  --body:"Schibsted Grotesk",system-ui,-apple-system,"Segoe UI",sans-serif;
  --mono:"IBM Plex Mono",ui-monospace,"SF Mono",Menlo,monospace; }
@media (prefers-color-scheme: dark) { :root { --ground:#0D111A; --surface:#141A25; --surface-2:#1A2130; --ink:#E7EAF1;
  --muted:#9AA3B5; --faint:#6E7789; --rule:#262E3D; --accent:#8CA0FF; --up:#4FC98C; --down:#F27A7A;
  --warn-bg:#3A2A0B; --warn-ink:#F5C165; } }
* { box-sizing: border-box; }
body { margin: 0; background: var(--ground); color: var(--ink); font-family: var(--body); font-size: 16px; line-height: 1.55; }
.wrap { max-width: 880px; margin: 0 auto; padding: 32px 20px 72px; }
a { color: var(--accent); text-decoration: none; } a:hover { text-decoration: underline; }
.up { color: var(--up); } .down { color: var(--down); } .flat, .faint { color: var(--muted); }
.back { font-size: 14px; font-weight: 600; }
.kicker { margin: 28px 0 6px; font: 600 12px var(--body); letter-spacing: .08em; text-transform: uppercase; color: var(--accent); }
h1 { font-family: var(--display); font-weight: 700; font-size: clamp(28px, 5vw, 40px); line-height: 1.08; letter-spacing: -.02em; margin: 0 0 10px; text-wrap: balance; }
.stamp { font-family: var(--mono); font-size: 12.5px; color: var(--muted); margin: 0 0 22px; }
h2 { font-family: var(--display); font-size: 21px; font-weight: 600; letter-spacing: -.01em; margin: 34px 0 10px; padding-top: 14px; border-top: 2px solid var(--ink); }
h3 { font-size: 16px; font-weight: 600; margin: 20px 0 6px; }
p { margin: 0 0 12px; max-width: 72ch; } ul, ol { margin: 0 0 12px; padding-left: 22px; max-width: 72ch; } li { margin: 0 0 6px; }
.verdict { background: var(--surface); border: 1px solid var(--rule); border-left: 4px solid var(--accent); border-radius: 0 10px 10px 0; padding: 14px 18px; margin: 0 0 22px; font-size: 17px; }
.verdict p:last-child { margin-bottom: 0; }
.box { background: var(--surface); border: 1px solid var(--rule); border-radius: 10px; overflow-x: auto; margin: 0 0 14px; }
table { width: 100%; border-collapse: collapse; font-size: 14px; }
th { text-align: right; font-size: 11.5px; letter-spacing: .06em; text-transform: uppercase; color: var(--muted); font-weight: 600; padding: 11px 12px 8px; border-bottom: 1px solid var(--rule); white-space: nowrap; }
td { text-align: right; padding: 10px 12px; border-bottom: 1px solid var(--rule); vertical-align: top; font-family: var(--mono); font-size: 13.5px; font-variant-numeric: tabular-nums; }
th:first-child, td:first-child { text-align: left; padding-left: 16px; font-family: var(--body); font-size: 14px; }
td { white-space: nowrap; } td:first-child, td.l { white-space: normal; min-width: 180px; }
tr:last-child td { border-bottom: 0; }
td.l, th.l { text-align: left; font-family: var(--body); }
.side { font-family: var(--mono); font-size: 11.5px; font-weight: 600; padding: 1px 7px; border-radius: 4px; border: 1px solid var(--rule); background: var(--surface-2); margin-left: 6px; }
.sub { display: block; font-size: 12px; color: var(--muted); }
.note { color: var(--muted); font-size: 13.5px; }
.kpis { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 10px; margin: 0 0 14px; }
.kpi { background: var(--surface); border: 1px solid var(--rule); border-radius: 10px; padding: 10px 14px; }
.kpi b { display: block; font-family: var(--mono); font-size: 20px; font-weight: 500; }
.kpi span { color: var(--muted); font-size: 12.5px; }
.src { font-size: 13px; color: var(--muted); } .src a { word-break: break-word; }
.prev { background: var(--surface-2); border-radius: 8px; padding: 10px 14px; font-size: 14px; margin: 0 0 14px; }
footer { margin-top: 40px; padding-top: 14px; border-top: 1px solid var(--rule); font-size: 13px; color: var(--muted); }
@media (max-width: 640px) { .wrap { padding: 20px 16px 56px; } body { font-size: 15.5px; } }
"""

MODEL_NAMES = {'claude-fable-5-2': 'Fable 5.2', 'claude-opus-5-5': 'Opus 5.5', 'claude-sonnet-5-5': 'Sonnet 5.5'}
EFFORT_NAMES = {'xhigh': 'extra', 'high': 'high', 'medium': 'medium', 'low': 'low', 'max': 'max'}


def clean_body(s):
    """The agent's HTML fragment, without anything that runs or loads from elsewhere."""
    s = re.sub(r'(?is)<(script|iframe|object|embed|link|meta|base|form)\b.*?(</\1\s*>|$)', '', s)
    s = re.sub(r'(?is)<(script|iframe|object|embed|link|meta|base|form)\b[^>]*>', '', s)
    s = re.sub(r'(?i)\son[a-z]+\s*=\s*("[^"]*"|\'[^\']*\'|[^\s>]+)', '', s)
    s = re.sub(r'(?i)(href|src)\s*=\s*(["\']?)\s*javascript:[^"\'>\s]*\2', r'\1="#"', s)
    return re.sub(r'(?is)<img\b[^>]*>', '', s)  # self-contained pages: no images to host or hot-link


def page_html(d, body):
    esc = html.escape
    rows = []
    for m in d['markets']:
        bid_ask = '–'
        if m.get('bid') is not None and m.get('ask') is not None:
            bid_ask = f"{m['bid']:.3f} / {m['ask']:.3f}".replace('0.', '.')
        fair = hold = '–'
        if m.get('fair') is not None:
            fair = cents(m['fair'])
            if m.get('bid'):
                h = m['fair'] / m['bid'] - 1
                hold = f'<span class="{tone(h)}">{h * 100:+.1f}%</span>'
        rows.append(
            f'<tr><td>{esc(m["title"])}<span class="side">{esc(m["outcome"])}</span></td>'
            f'<td>{m["size"]:,.0f}<span class="sub">at {cents(m["avg"]) if m.get("avg") is not None else "–"}</span></td>'
            f'<td>{cents(m["p24"])} → <b>{cents(m["price"])}</b><span class="sub {tone(m["move"])}">{signed_cents(m["move"])}</span></td>'
            f'<td class="{tone(m["d24"])}">{usd(m["d24"], True)}</td><td>{bid_ask}</td><td>{fair}</td><td>{hold}</td></tr>')
    prev = ''
    if d.get('prev'):
        p = d['prev']
        then = ', '.join(f"{esc(m['outcome'])} {cents(m['price'])}" for m in p.get('markets', [])[:4])
        prev = (f'<div class="prev"><b>Replaces the deep dive of {when(p["ts"])}</b> (written at {then}). '
                f'Then: {esc(p.get("verdict") or "")}</div>')
    stamp = datetime.datetime.fromtimestamp(d['ts'])
    by = MODEL_NAMES.get(d.get('model'), d.get('model') or '')
    if d.get('effort'):
        by += f" ({EFFORT_NAMES.get(d['effort'], d['effort'])})"
    return f'''<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex">
<title>{esc(d["title"])} · deep dive</title>
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Bricolage+Grotesque:opsz,wght@12..96,600;12..96,700&family=IBM+Plex+Mono:wght@400;500;600&family=Schibsted+Grotesk:wght@400;500;600&display=swap">
<style>{PAGE_CSS}</style></head>
<body><div class="wrap">
<a class="back" href="../../">← Morning brief</a>
<div class="kicker">Deep dive</div>
<h1>{esc(d["title"])}</h1>
<p class="stamp">{stamp:%a} {stamp.day} {stamp:%b %Y, %H:%M} Copenhagen · {esc(by)} · <a href="https://polymarket.com/event/{esc(d["event"])}">the event on Polymarket</a></p>
<div class="verdict"><p>{esc(d["verdict"])}</p></div>
{prev}
<div class="box"><table>
<thead><tr><th>Your position</th><th>Shares</th><th>24h ago → now</th><th>24h P/L</th><th>Bid / ask</th><th>Fair</th><th>Hold vs sell</th></tr></thead>
<tbody>{"".join(rows)}</tbody></table></div>
<p class="note">Why this event, this hour: {esc(d.get("picked") or "")} Prices are midpoints when the dive was written; hold vs sell is fair ÷ bid − 1.</p>
{body}
<footer>Written by an automated hourly run. Fair values are the model's own judgment from the sources above, and nothing here is financial advice. <a href="../../#deep-dives">All deep dives</a> · <a href="../../">Morning brief</a></footer>
</div></body></html>
'''


def cmd_add(a):
    c = read(CANDS)
    if not c:
        sys.exit('run `python3 deep.py candidates` first')
    e = next((x for x in c['events'] if x['event'] == a.event), None)
    if not e:
        sys.exit(f'{a.event} is not an event the wallet holds (see candidates)')
    if not e['eligible'] and not a.force:
        sys.exit(f"{a.event} is not eligible: {e['why_not']}")
    index = read(INDEX, {}) or {}
    dives = index.setdefault('dives', {})
    added = [s for s, d in dives.items() if d['ts'] >= c['ts'] and s != a.event]
    if len(added) >= MAX_DIVES and not a.force:
        sys.exit(f'{MAX_DIVES} deep dives already added this run')
    fairs = {}
    for f in a.fair or []:
        k, _, v = f.partition('=')
        if k not in {r['asset'] for r in e['rows']} or not 0 < float(v) < 1:
            sys.exit(f'--fair {f}: give <asset id of one of your markets>=<probability your side pays, 0-1>')
        fairs[k] = float(v)
    with open(a.body, encoding='utf-8') as fh:
        body = clean_body(fh.read())
    if len(re.sub(r'<[^>]+>', '', body).split()) < 150:
        sys.exit('the body is under 150 words: that is not a deep dive')
    now = int(time.time())
    old = dives.get(a.event)
    d = dict(event=a.event, title=a.title, page=f'deep/{slugify(a.event)}/', ts=now,
             at=datetime.datetime.fromtimestamp(now).astimezone().isoformat(timespec='minutes'),
             model=a.model, effort=a.effort, verdict=a.verdict, picked=a.picked,
             d24=round(e['d24'], 2), move=round(e['move'], 2), share=round(e['share'], 3),
             markets=[dict(asset=r['asset'], market=r.get('market'), title=r['title'], outcome=r['outcome'],
                           size=r['size'], avg=r['avg'],
                           price=r['now'], p24=r['p24'], move=round(r['move'], 2), d24=round(r['d24'], 2),
                           bid=r.get('bid'), ask=r.get('ask'), fair=fairs.get(r['asset'])) for r in e['rows']])
    if old:
        d['prev'] = dict(ts=old['ts'], verdict=old.get('verdict'),
                         markets=[dict(outcome=m['outcome'], price=m['price']) for m in old.get('markets', [])])
    write(os.path.join(SITE, d['page'], 'index.html'), page_html(d, body))
    dives[a.event] = d
    index['updated'] = now
    write_json(INDEX, index)
    print(f"added {d['page']} ({len(body):,} characters){', replacing the dive of ' + when(old['ts']) if old else ''}")


# ---------------------------------------------------------------- the live site

def sync():
    """Bring the live deep dives into site/deep/ (the newer dive of an event wins, so this run's own stay), then put
    their links on site/index.html. deploy.py runs this before every deploy, so a deploy never drops a deep dive.
    Raises Unreachable when the live copy can't be read and there is no local one to fall back on."""
    local = read(INDEX, {}) or {}
    mine = local.setdefault('dives', {})
    try:
        live = get(f'{LIVE}deep/index.json?t={int(time.time())}')
    except urllib.error.HTTPError as e:
        if e.code != 404:
            raise Unreachable(f'live deep/index.json: HTTP {e.code}')
        live = {}  # no deep dives published yet
    except Exception as e:
        if not os.path.exists(INDEX):
            raise Unreachable(f'live deep/index.json: {type(e).__name__}: {e}')
        say(f'deep: live index unreadable ({type(e).__name__}), keeping the local copy')
        live = {}
    pulled = 0
    for slug, d in (live.get('dives') or {}).items():
        have = mine.get(slug)
        path = os.path.join(SITE, d['page'], 'index.html')
        if have and (have['ts'] > d['ts'] or (have['ts'] == d['ts'] and os.path.exists(path))):
            continue
        try:
            write(path, fetch(f"{LIVE}{d['page']}index.html?t={d['ts']}"))
        except urllib.error.HTTPError as e:
            if e.code == 404:
                say(f"deep: {d['page']} is listed but missing, dropped")
                continue
            raise Unreachable(f"live {d['page']}: HTTP {e.code}")
        except Exception as e:
            raise Unreachable(f"live {d['page']}: {type(e).__name__}: {e}")
        mine[slug] = d
        pulled += 1
    if mine:
        local['updated'] = max(d['ts'] for d in mine.values())
        write_json(INDEX, local)
    brief = os.path.join(SITE, 'index.html')
    if os.path.exists(brief):
        with open(brief, encoding='utf-8') as fh:
            page = fh.read()
        linked, n = link_brief(page, local)
        if linked != page:
            write(brief, linked)
        say(f'deep: {len(mine)} deep dives ({pulled} pulled), linked from {n} position rows')
    else:
        say(f'deep: {len(mine)} deep dives ({pulled} pulled)')
    return local


LIST_CSS = """<style id="deep-css">
td.mkt a.deep { display: inline-block; font-family: var(--mono); font-size: 11.5px; font-weight: 500; line-height: 1.5; padding: 1px 7px; border-radius: 4px; color: var(--accent); background: color-mix(in srgb, var(--accent) 8%, transparent); border: 1px solid color-mix(in srgb, var(--accent) 40%, transparent); }
td.mkt a.deep:hover { background: color-mix(in srgb, var(--accent) 16%, transparent); }
#deep-dives { scroll-margin-top: 64px; }
#deep-dives .li-title a { color: var(--accent); }
#deep-dives .faint { color: var(--muted); font-family: var(--mono); font-size: 12.5px; margin-left: 8px; }
#deep-dives .li-body { max-width: 90ch; }
</style>"""
ROW = re.compile(r'(<td class="mkt"><a href="https://polymarket\.com/event/([^"]+)">(.*?)</a>\s*'
                 r'<div class="meta"><span class="side">(.*?)</span>.*?)(</div></td>)', re.S)
FOOT = '<footer class="page-foot">'
TABS = re.compile(r'(<nav class="topnav"[^>]*><div class="tabs">.*?)(</div>)', re.S)  # the top menu's tabs


def link_brief(page, index):
    """The brief's HTML with a chip on every position row whose event has a deep dive, the list of all deep dives
    above the page's footer and a tab for it at the end of the top menu. Takes out what an earlier call put in, so it can run on its own output."""
    page = re.sub(r'<a class="deep"[^>]*>.*?</a>', '', page, flags=re.S)
    page = re.sub(r'<section id="deep-dives">.*?</section>\s*', '', page, flags=re.S)
    page = re.sub(r'<style id="deep-css">.*?</style>\s*', '', page, flags=re.S)
    page = re.sub(r'<span class="sep" data-deep[^>]*></span><a data-deep[^>]*>.*?</a>', '', page, flags=re.S)
    dives = index.get('dives') or {}
    if not dives:
        return page, 0
    hits = 0

    def chip(m):
        nonlocal hits
        d = dives.get(html.unescape(m.group(2)))
        if not d:
            return m.group(0)
        title, outcome = html.unescape(m.group(3)), html.unescape(m.group(4))
        mk = next((x for x in d.get('markets', []) if x['title'] == title and x['outcome'] == outcome), {})
        hits += 1
        text = f'Deep dive {when(d["ts"])}'
        if mk.get('price') is not None:
            text += f' · at {cents(mk["price"])}'
        if mk.get('fair') is not None:  # the dive's fair value; the hunt book takes it at its next pull (hunt.py apply_dives)
            text += f' · fair {cents(mk["fair"])}'
        return (f'{m.group(1)}<a class="deep" href="{html.escape(d["page"])}" title="{html.escape(d.get("verdict") or "")}">'
                f'{text}</a>{m.group(5)}')

    page = ROW.sub(chip, page)
    items = ''.join(
        f'<li><span class="li-title"><a href="{html.escape(d["page"])}">{html.escape(d["title"])}</a>'
        f'<span class="faint">{when(d["ts"])}</span></span>'
        f'<span class="li-body">{html.escape(d.get("verdict") or "")}</span></li>'
        for d in sorted(dives.values(), key=lambda d: -d['ts']))
    block = (f'{LIST_CSS}<section id="deep-dives"><h2>Deep dives</h2>'
             f'<p class="wx-note">Every hour a run looks for up to {MAX_DIVES} events whose price moved, that moved the '
             f'24h P/L, and that have no recent deep dive at the current price. A newer deep dive of an event replaces '
             f'the older one.</p><ul>{items}</ul></section>\n  ')
    tab = (f'<span class="sep" data-deep aria-hidden="true"></span>'
           f'<a data-deep href="#deep-dives">Deep dives<span>{len(dives)}</span></a>')
    page = TABS.sub(lambda m: m.group(1) + tab + m.group(2), page, count=1)  # last: the list is at the foot of the page
    if FOOT in page:
        page = page.replace(FOOT, block + FOOT, 1)
    else:
        page = page.replace('</html>', block + '</html>', 1) if '</html>' in page else page + block
    return page, hits


def api(method, path, data=None):
    """The Vercel API. In the cloud routine curl goes through the proxy that adds the token (as in deploy.py);
    locally, DEEP_VERCEL_CLI=1 sends the call through the signed-in `vercel` CLI instead."""
    if os.environ.get('DEEP_VERCEL_CLI'):
        cmd = ['vercel', 'api', path, '-X', method]
        if data is not None:
            tmp = os.path.join(WORK, 'body.json')
            write_json(tmp, data)
            cmd += ['--input', tmp]
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    else:
        cmd = ['curl', '-sS', '-X', method, f'https://api.vercel.com{path}', '-H', 'Content-Type: application/json']
        if data is not None:
            cmd += ['--data-binary', '@-']
        out = subprocess.run(cmd, input=json.dumps(data) if data is not None else None,
                             capture_output=True, text=True, timeout=180)
    if out.returncode:
        sys.exit(f'Vercel API call failed: {out.stderr.strip()[:300]}')
    j = json.loads(out.stdout)
    if isinstance(j, dict) and 'error' in j:
        sys.exit(f"Vercel error: {j['error'].get('code')}: {j['error'].get('message')}")
    return j


def production():
    return api('GET', f'/v9/projects/{PROJECT}?teamId={TEAM}')['targets']['production']['id']


def pull_site():
    """Make site/ the live production deployment, file for file, leaving site/deep/ to sync(). Returns its id.
    Each file is checked against the deployment's own sha1, so a deploy landing meanwhile can't mix two versions."""
    dpl = production()
    files = {}

    def walk(nodes, pre):
        for n in nodes:
            if n.get('type') == 'directory':
                walk(n.get('children') or [], pre + n['name'] + '/')
            elif n.get('type') == 'file':
                files[pre + n['name']] = n['uid']

    walk(api('GET', f'/v6/deployments/{dpl}/files?teamId={TEAM}'), '')
    files = {(p[4:] if p.startswith('src/') else p): u for p, u in files.items()}
    if 'index.html' not in files:
        sys.exit(f'production deployment {dpl} lists no index.html: {sorted(files)[:5]}')
    keep = {p for p in files if not p.startswith('deep/')}
    for p in sorted(keep):
        path = os.path.join(SITE, p)
        if os.path.exists(path):
            with open(path, 'rb') as fh:
                if hashlib.sha1(fh.read()).hexdigest() == files[p]:
                    continue
        data = fetch(f'{LIVE}{urllib.parse.quote(p)}?t={int(time.time())}')
        if hashlib.sha1(data).hexdigest() != files[p]:
            raise Unreachable(f'{p} on the live site is not the file in deployment {dpl}')
        write(path, data)
    for d, _, names in os.walk(SITE):  # nothing but the live files and the deep dives
        for n in names:
            rel = os.path.relpath(os.path.join(d, n), SITE)
            if rel not in keep and not rel.startswith('deep' + os.sep):
                os.remove(os.path.join(d, n))
    say(f'pulled {len(keep)} files of production deployment {dpl}')
    return dpl


def cmd_publish(a):
    import deploy
    c = read(CANDS) or {}
    new = [d for d in (read(INDEX, {}) or {}).get('dives', {}).values() if d['ts'] >= c.get('ts', 0)]
    if not new and not a.force:
        sys.exit('no deep dive was added in this run: nothing to publish')
    for attempt in range(3):
        try:
            dpl = pull_site()
            sync()
        except Unreachable as e:
            say(f'publish: {e}; trying again')
            time.sleep(10)
            continue
        if production() == dpl:  # nobody deployed while we were pulling
            break
        say('publish: a new production deployment landed meanwhile; pulling again')
    else:
        sys.exit('could not get a steady copy of the live site in three tries; nothing deployed')
    code = deploy.post(deploy.body(preview=a.preview))
    for d in new:
        print(f"deep dive{' (preview only)' if a.preview else ''}: {PUBLIC}{d['page']}  {d['title']}")
    sys.exit(code)


def cmd_sync(a):
    sync()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest='cmd', required=True)
    sub.add_parser('candidates').set_defaults(fn=cmd_candidates)
    p = sub.add_parser('show'); p.add_argument('event'); p.set_defaults(fn=cmd_show)
    p = sub.add_parser('add')
    p.add_argument('event')
    p.add_argument('--title', required=True, help='the page headline: what the dive found, in plain words')
    p.add_argument('--verdict', required=True, help='one or two sentences: where it stands and the fair value')
    p.add_argument('--body', required=True, help='file with the HTML fragment (h2, h3, p, ul, table, .kpis, .src)')
    p.add_argument('--picked', default='', help='one sentence: why this event was picked this hour')
    p.add_argument('--fair', action='append', help='<asset id>=<probability your side pays out>; repeatable')
    p.add_argument('--model', default='claude-fable-5-2')
    p.add_argument('--effort', default=os.environ.get('CLAUDE_EFFORT') or 'xhigh')
    p.add_argument('--force', action='store_true', help='add it although it is not eligible')
    p.set_defaults(fn=cmd_add)
    p = sub.add_parser('publish')
    p.add_argument('--preview', action='store_true'); p.add_argument('--force', action='store_true')
    p.set_defaults(fn=cmd_publish)
    sub.add_parser('sync').set_defaults(fn=cmd_sync)
    a = ap.parse_args()
    a.fn(a)


if __name__ == '__main__':
    main()
