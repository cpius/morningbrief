#!/usr/bin/env python3
"""Mispricing hunt: a book of Polymarket picks with my fair values, repriced every morning.

The book (picks, fair values, reasons, notes) is carried from run to run in hunt/work/state.json.
`build` publishes it next to the page as site/hunt/state.json, and the next run's `pull` reads it back
from the live site, so the cloud routine needs no git push. The repo copy hunt/seed.json is the fallback.

    python3 hunt/hunt.py pull                 # load the book (live site, else seed), apply hunt/updates.json; new day: last ask becomes "previous"
    python3 hunt/hunt.py scan                 # reprice every pick, sweep, print the worklist, write the review batches
    python3 hunt/hunt.py list                 # the book, with ids and returns
    python3 hunt/hunt.py show <id>            # one pick plus its market rules
    python3 hunt/hunt.py peek <event id|slug|url>  # a candidate event: its open markets, prices and rules
    python3 hunt/hunt.py set <id> [--fair F] [--why W] [--name N] [--tag T]
    python3 hunt/hunt.py drop <id> --reason R
    python3 hunt/hunt.py add <event id|slug|url> "<words from the question>|<market id>" --side NO --fair F --why W [--name N] [--tag T]
    python3 hunt/hunt.py note "<one line on what changed>"
    python3 hunt/hunt.py ingest               # full review: read the subagents' fair values, print MISSING/FLAG/BOOK DIFF
    python3 hunt/hunt.py smart                # smart-money share from top holders and leaderboard PnL
    python3 hunt/hunt.py build                # write site/hunt/{index.html,state.json,wallets.json,summary.json}

Return = fair / ask - 1, held to resolution, plus Polymarket's 3.25% APR holding reward where the market pays it.
The book is ranked by that return as a yearly rate, return * 365 / days (days at least 14), so a small edge that
resolves in weeks beats a big one that takes years.

The wallet's open positions are part of the book too. `scan` marks picks the wallet holds and adds any held market
that is missing, with no fair value yet (it prints FAIR NEEDED; set one with `set`). For a held position the page
shows "hold vs sell": fair / best bid - 1 plus the reward, i.e. what holding to resolution should earn over selling
into the bid now. Held positions are never auto-dropped; they leave the book when sold, unless they are also hunt picks.

Full review: every candidate market (about 2,000 in 1,000 events) is researched every day, not only new ones. `scan`
splits them into hunt/work/batches/bNNN.json, nearest end first, with the series history of recurring events; the
routine gives each batch to a research subagent, which writes its probability of YES per market (and, where the market
is decided well before its end date, the expected day) to hunt/work/reviews/bNNN.json; `ingest` reads those into the
book's `reviews` (kept for the next day, so the next review can look only at news since) and lists the markets where
either side clears the main list's bars at today's prices. The routine verifies those itself before it `add`s them.
Only the standard library is used.
"""
import argparse, datetime, glob, html, json, os, re, shutil, sys, threading, time, urllib.error, urllib.parse, urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
from favicon import FAVICON  # noqa: E402  (shared with the brief)
WORK = os.path.join(HERE, 'work')
STATE = os.path.join(WORK, 'state.json')
WALLETS = os.path.join(WORK, 'wallets.json')
CANDIDATES = os.path.join(WORK, 'candidates.txt')
BATCHES = os.path.join(WORK, 'batches')  # full review: scan writes one file per research subagent here ...
REVIEWS = os.path.join(WORK, 'reviews')  # ... each subagent writes its fair values here, and ingest reads them
SEED = os.path.join(HERE, 'seed.json')
SEED_WALLETS = os.path.join(HERE, 'seed-wallets.json')
UPDATES = os.path.join(HERE, 'updates.json')  # fair values revised outside the routine (deep dives), applied once by pull
SITE = os.path.join(ROOT, 'site', 'hunt')
LIVE = 'https://nothing-ever-happens-book.vercel.app/hunt/'
GAMMA, CLOB, DATA = 'https://gamma-api.polymarket.com', 'https://clob.polymarket.com', 'https://data-api.polymarket.com'
UA = {'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/120 Safari/537.36'}

NOW = datetime.datetime.now(ZoneInfo('Europe/Copenhagen'))
TODAY = NOW.date()
WALLET = '0x9d4a9ff98bdce01ff8081362f676749db0c85887'  # mdorup
HOLD_APR = 0.0325       # Polymarket holding reward, variable at their discretion
MAIN_ANN = 0.30         # main list: at least this much a year
MIN_ANN = 0.10          # appendix; below this a year a pick is dropped at build
MIN_EDGE = 0.03         # also dropped below this to resolution: inside a fair value's own error
ANN_MIN_DAYS = 14       # annualize over at least this many days: the money doesn't go into the next bet the next day
BATCH_EVENTS = 15       # full review: events per research subagent ...
BATCH_MARKETS = 30      # ... and at most this many markets (a long ladder gets a batch of its own)
REVIEW_KEEP_DAYS = 30   # forget a market's review once it is this old (the market stopped being a candidate)
REVIEW_GAP = 0.05       # a review must differ from the mid (FLAG) or from a pick's fair value (BOOK DIFF) by this much
REVIEW_MOVE = 0.05      # ask moved this much since the last run -> news check
SOON_DAYS = 14          # ends this soon -> news check
SMART_PNL = 50_000      # all-time PnL that counts as smart money
SMART_MIN_USD = 300     # hide the smart-money share below this much smart money
WALLET_TTL_DAYS = 30
MAX_WALLET_LOOKUPS = 1500
ADAPTER = '0xa5ef39c3d3e10d0b270233af41cac69796b12966'  # neg-risk adapter, holds NO everywhere
MAKERS = {'xtxpleasecallme', 'armageddonrewardsbilly'}  # liquidity-reward makers sitting on many books

# Tag ids for the sweep: the politics/geo set of 2026-10-29, plus Finance, Culture and Weather (2026-11-07) for
# nothing-ever-happens events outside politics (company exits, celebrity news, disasters). Sports and crypto are skipped
# by tag slug; the NOISE regexes drop the price, earnings, FX, rainfall and review-score markets these categories carry.
TAGS = [2, 100265, 101970, 144, 1597, 154, 100410, 101206, 96, 303, 78, 180, 95, 100328, 126, 1628, 101794, 789, 514,
        102434, 100614, 245, 734, 1378, 100244, 518, 101522, 166, 867, 246, 192, 61, 849, 114, 956, 100185, 167, 159,
        101758, 100188, 872, 101270, 351, 520, 1382, 1370, 1383, 101272, 525, 191, 1373, 100861, 742, 101283, 101284,
        101748, 181, 102436, 282, 439, 1401,
        120, 596, 84]  # finance, pop-culture (Culture), weather
NOISE_WORDS = (r'tweet|post |posts|say |says|mention|views|followers|up or down|price of|above|below|'
               r'temperature|highest|lowest|close at|market cap|ETF|stock|IPO|FDV|launch a token|airdrop|box office|'
               r'rotten|billboard|spotify|album|song|movie|grammy|oscar|emmy|award|youtube|tiktok|streamer|twitch|'
               r'kick|MrBeast|word|ranking|top AI model|best AI|arena|benchmark|search|google trends|approval rating|'
               r'net approval|polling average|odds|seats|vote share|margin|turnout|% of|percent|by how much|'
               r'exact|between|range|\d+k|\$\d|'
               r'earnings|sales.{0,12}growth|level will|hit __|close price|precipitation|metacritic|auction')
# Count words are noise in a market's question but not in its event's title: "How many Starship launches reach space
# in 2026?" or "How many Trump vetoes ...?" is a ladder whose low rung ("less than 5") is a nothing-ever-happens bet.
NOISE = re.compile(r'# of|number of|how many|' + NOISE_WORDS, re.I)  # the market's question
TITLE_NOISE = re.compile(NOISE_WORDS, re.I)                           # the event's title


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


def pages(url):
    """Every row of a Data API v2 list, following its next_cursor (v2 has no offset)."""
    rows, cursor = [], None
    while True:
        d = get(url + (f'&cursor={urllib.parse.quote(cursor)}' if cursor else ''))
        rows += d['data']
        cursor = d['pagination'].get('next_cursor')
        if not cursor:
            return rows


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


def per_year(tot, days):
    """A return to resolution as a simple yearly rate, as if bets like it kept paying the same for 12 months."""
    return tot * 365 / max(days, ANN_MIN_DAYS)


def econ(p):
    """Return math for one pick at today's ask, or None without an ask or a fair value."""
    if not p.get('ask') or p.get('fair') is None:
        return None
    days = max((datetime.date.fromisoformat(p['end']) - TODAY).days, 1)
    yrs = days / 365
    ret = p['fair'] / p['ask'] - 1
    bonus = HOLD_APR * yrs * ((p['ask'] + p['fair']) / 2) / p['ask'] if p.get('hold') else 0.0
    tot = ret + bonus
    return dict(yrs=yrs, ret=ret, bonus=bonus, tot=tot, ann=per_year(tot, days))


def weak(e):
    """Why a pick no longer clears the bar to stay in the book, or None."""
    if e['ann'] < MIN_ANN:
        return f"{e['ann'] * 100:+.0f}% a year"
    if e['tot'] < MIN_EDGE:
        return f"{e['tot'] * 100:+.1f}% to resolution"
    return None


def hold_econ(p):
    """For a held position: holding to resolution vs selling into today's best bid (per share and in $)."""
    h = p.get('held')
    if not h or p.get('fair') is None or not p.get('bid'):
        return None
    days = max((datetime.date.fromisoformat(p['end']) - TODAY).days, 1)
    yrs = days / 365
    bid, fair = p['bid'], p['fair']
    reward = HOLD_APR * yrs * (bid + fair) / 2 if p.get('hold') else 0.0  # per share, on the mid value
    tot = (fair + reward) / bid - 1
    return dict(yrs=yrs, tot=tot, bonus=reward / bid, ann=per_year(tot, days), edge=h['size'] * (fair + reward - bid),
                value=h['size'] * bid)


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
    if os.path.exists(UPDATES):  # each applies once: when it is newer than the pick's last review
        for u in read(UPDATES):
            for p in st['picks']:
                if p['id'] == u['id'] and (p.get('reviewed') or '') < u['at']:
                    p.update(fair=u['fair'], why=u['why'], reviewed=u['at'])
                    print(f"applied update from {u['at']}: {p['name']} fair {u['fair']}")
    if st.get('day') != str(TODAY):  # first run today: last run's prices become the "previous" column
        for p in st['picks']:
            p['prev_ask'], p['prev_bid'], p['prev_fair'] = p.get('ask'), p.get('bid'), p.get('fair')
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
    rules = next((m.get('description') for m in e.get('markets') or [] if not m.get('closed') and m.get('description')),
                 e.get('description')) or ''
    return dict(id=e['id'], title=e.get('title', ''), slug=e.get('slug', ''), rules=rules.strip()[:2500],
                tags=[t.get('slug') for t in e.get('tags') or []],
                markets=[dict(id=m.get('id'), q=m.get('question', ''), g=m.get('groupItemTitle') or '',
                              bid=m.get('bestBid'), ask=m.get('bestAsk'), end=(m.get('endDate') or '')[:10],
                              liq=float(m.get('liquidityNum') or m.get('liquidity') or 0),
                              hold=bool(m.get('holdingRewardsEnabled')),
                              live=bool(m.get('active')) and not m.get('closed') and m.get('acceptingOrders', True))
                         for m in e.get('markets') or []])


def sweep_tag(tag):
    out, cursor = [], ''
    while True:  # keyset paging: /events rejects offsets above 2000, this has no cap
        try:
            r = get(f'{GAMMA}/events/keyset?tag_id={tag}&active=true&closed=false&limit=100'
                    + (f'&after_cursor={urllib.parse.quote(cursor)}' if cursor else ''), tries=3, timeout=30)
        except Exception:
            break
        out += [slim_event(e) for e in r.get('events') or []]
        cursor = r.get('next_cursor')
        if not cursor or not r.get('events'):
            break
    return out


def candidate_markets(e):
    """Markets in an event worth a look: live, two-sided, not noise, ends in 4 days to ~2.5 years."""
    if 'sports' in e['tags'] or 'crypto' in e['tags'] or TITLE_NOISE.search(e['title']):
        return []
    out = []
    for m in e['markets']:
        if not m['live'] or m['bid'] is None or m['ask'] is None or not m['end']:
            continue
        bid, ask = float(m['bid']), float(m['ask'])
        mid = (bid + ask) / 2
        days = (datetime.date.fromisoformat(m['end']) - TODAY).days
        if ask - bid > 0.15 or not 0.05 <= mid <= 0.95 or not 4 <= days <= 900 or NOISE.search(m['q']):
            continue
        out.append(dict(m, mid=mid, days=days))
    return out


HISTORY_EVENTS = 4    # series history: the last this many resolved events ...
HISTORY_MARKETS = 25  # ... with at most this many outcomes each (a date ladder takes a dozen)
_MONTHS = 'january|february|march|april|may|june|july|august|september|october|november|december'


def _same_q(q):
    """A question with its numbers, months and punctuation taken out, to match it across the events of a series."""
    return ' '.join(re.sub(rf'\d+|{_MONTHS}|[^\w ]', ' ', q.lower()).split())


def series_history(e, cache):
    """How the last few resolved events in this event's series went, for the reviewer.

    A recurring event (a flight test, a monthly central-bank decision, a "before 2027" question renewed every year) is
    best judged by how its predecessors resolved: the booster catch on Starship flights 11-14 resolved NO each time
    while the market priced flight 15's at 75%. Each outcome is the past market's short title (or question) and
    YES, NO or its final price; outcomes whose question matches one of this event's come first, then the YES ones.
    """
    full = get(f"{GAMMA}/events/{e['id']}", tries=3)
    here = {_same_q(m['q']) for m in e['markets']}
    out = []
    for s in full.get('series') or []:
        if s['id'] not in cache:
            past = get(f"{GAMMA}/events?series_id={s['id']}&closed=true&order=id&ascending=false"
                       f"&limit={HISTORY_EVENTS * 2}", tries=3)
            past.sort(key=lambda v: v.get('closedTime') or v.get('endDate') or '', reverse=True)
            cache[s['id']] = past[:HISTORY_EVENTS]
        for v in cache[s['id']]:
            last = {}  # label -> market; a market re-listed under the same label (Flight 12's were) keeps the last one
            for m in sorted(v.get('markets') or [], key=lambda m: m.get('closedTime') or ''):
                last[m.get('groupItemTitle') or m.get('question') or ''] = m
            rows = []
            for label, m in last.items():
                try:
                    yes = float(json.loads(m.get('outcomePrices') or '[]')[0])
                except (ValueError, IndexError, TypeError):
                    continue
                res = 'YES' if yes >= 0.99 else 'NO' if yes <= 0.01 else f'{yes:.2f}'
                rows.append((_same_q(m.get('question') or '') not in here, res != 'YES', f'{label}: {res}'))
            if rows:
                out.append(dict(event=v.get('title', ''), closed=(v.get('closedTime') or v.get('endDate') or '')[:10],
                                outcomes=[r[2] for r in sorted(rows)[:HISTORY_MARKETS]]))
    return out


def sync_positions(st):
    """Mark the picks the wallet holds; add held markets missing from the book (fair value still to be set)."""
    try:
        pos = pages(f'{DATA}/v2/positions?user={WALLET}&limit=500&filter_amount=0.1')
    except Exception as e:
        print(f'positions unavailable ({e}); held marks left as they were')
        return
    pos = [x for x in pos if not x.get('redeemable') and 0 < float(x.get('current_price') or 0) < 1]
    by_token = {p['token']: p for p in st['picks']}
    seeded = {p['token']: p for p in read(SEED)['picks'] if p.get('fair') is not None}  # values researched when seeding
    for x in pos:
        held = dict(size=round(x['current_size'], 2), avg=x['avg_price'], cost=round(x['entry_cost_usdc'], 2))
        if x['token_id'] in by_token:
            by_token[x['token_id']]['held'] = held
            continue
        try:
            m = get(f"{GAMMA}/markets?condition_ids={x['condition_id']}")[0]
        except Exception as e:
            print(f"positions: no Gamma market for {x['title']} ({e})")
            continue
        p = dict(id=m['id'], cid=x['condition_id'], token=x['token_id'], side='YES' if x['outcome_index'] == 0 else 'NO',
                 outcome=x['outcome'], event=str(x.get('event_id') or ''), q=m['question'], name=x['title'].rstrip('?'),
                 url=f"https://polymarket.com/event/{x['event_slug']}", end=(m.get('endDate') or x.get('end_date') or '')[:10],
                 fair=None, why='', tag='', added=str(TODAY), origin='position', prev_ask=None, prev_fair=None, held=held)
        if x['token_id'] in seeded:
            p.update({k: seeded[x['token_id']][k] for k in ('fair', 'why', 'name', 'tag', 'reviewed')})
        p.update(reprice(p)[1])
        p['priced'] = NOW.strftime('%Y-%m-%d %H:%M')
        st['picks'].append(p)
        print(f"HELD NEW {p['id']} {p['name']}: {p['outcome']} {held['size']:,.0f} sh @ {held['avg']:.3f}, added to the book"
              f"{' with the seed fair value ' + str(p['fair']) if p['fair'] is not None else ''}")
    live = {x['token_id'] for x in pos}
    for p in list(st['picks']):
        if p.get('held') and p['token'] not in live and not p.get('closed'):
            p['held'] = None
            if p.get('origin') == 'position':
                st['picks'].remove(p)
            print(f"SOLD {p['id']} {p['name']}: no longer held"
                  f"{', removed from the book' if p.get('origin') == 'position' else ', stays in the hunt'}")


def write_batches(st, events, cands, order, seen):
    """Split today's candidates into files for the full review's research subagents, nearest end first.

    Each market carries its last review and, if it is in the book, the book's side and fair value, so the subagent can
    look only for news since then. A rerun the same day rewrites the batches but keeps today's results (ingest
    matches them by market id); results from an earlier day are cleared.
    """
    idx_path = os.path.join(BATCHES, 'index.json')
    if os.path.exists(idx_path) and read(idx_path).get('day') != str(TODAY):
        shutil.rmtree(REVIEWS, ignore_errors=True)
    shutil.rmtree(BATCHES, ignore_errors=True)
    os.makedirs(BATCHES)
    os.makedirs(REVIEWS, exist_ok=True)
    book = {p['id']: p for p in st['picks']}
    reviews = st.get('reviews') or {}
    groups, cur, n = [], [], 0
    for i in order:
        k = len(cands[i])
        if cur and (len(cur) >= BATCH_EVENTS or n + k > BATCH_MARKETS):
            groups.append(cur)
            cur, n = [], 0
        cur.append(i)
        n += k
    if cur:
        groups.append(cur)
    out = []
    for g, ids in enumerate(groups, 1):
        rows = []
        for i in ids:
            e = events[i]
            ms = []
            for m in cands[i]:
                r, p = reviews.get(m['id']), book.get(m['id'])
                ms.append(dict(id=m['id'], question=m['q'], option=m['g'] or None, yes_bid=float(m['bid']),
                               yes_ask=float(m['ask']), end=m['end'], days=m['days'], liquidity=round(m['liq']), apr=m['hold'],
                               last_review=dict(fair_yes=r['fair'], why=r['why'], day=r['day'],
                                                **({'settles': r['settles']} if r.get('settles') else {})) if r else None,
                               book=dict(side=p['side'], fair=p.get('fair'), why=p.get('why')) if p else None))
            rows.append(dict(event=i, title=e['title'], url=f"https://polymarket.com/event/{e['slug']}",
                             new=i not in seen, rules=e.get('rules', ''), markets=ms,
                             **({'history': e['history']} if e.get('history') else {})))
        name = f'b{g:03d}'
        write(os.path.join(BATCHES, name + '.json'), rows)
        out.append(dict(id=name, file=os.path.relpath(os.path.join(BATCHES, name + '.json'), ROOT),
                        result=os.path.relpath(os.path.join(REVIEWS, name + '.json'), ROOT),
                        events=len(rows), markets=sum(len(r['markets']) for r in rows),
                        first_end=min(m['end'] for r in rows for m in r['markets'])))
    write(idx_path, dict(day=str(TODAY), batches=out))
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
    sync_positions(st)
    picks = st['picks']
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
    def line(e, ms):
        ms = sorted(ms, key=lambda m: -m['mid'])
        apr = ' APR' if any(m['hold'] for m in e['markets']) else ''
        return (f"{e['id']} [{min(m['days'] for m in ms)}d L${sum(m['liq'] for m in ms) / 1000:.0f}k{apr}] {e['title'][:80]}"
                f" :: {mkts(ms)}")
    # Every candidate, seen before or not, goes to the full review: nearest end first, so a run cut short has done the
    # short-dated ones, where a small edge is a big yearly rate.
    cands = {i: ms for i, e in events.items() for ms in [candidate_markets(e)] if ms}
    order = sorted(cands, key=lambda i: (min(m['days'] for m in cands[i]), i))
    recurring, series = [i for i in order if 'recurring' in events[i]['tags']], {}
    def history(i):
        try:
            events[i]['history'] = series_history(events[i], series)
        except Exception as ex:
            print(f'history {i}: {ex}')
    with ThreadPoolExecutor(4) as ex:
        list(ex.map(history, recurring))
    print(f"series history for {sum(1 for i in recurring if events[i].get('history'))} of {len(recurring)} recurring events")
    new = [i for i in order if i not in seen]
    os.makedirs(WORK, exist_ok=True)
    with open(CANDIDATES, 'w') as fh:
        fh.write(f'# All {len(order)} candidate events ({len(new)} new since the last sweep, marked NEW), nearest end first. '
                 'Format: event id [days to first end, liquidity, APR = pays the holding reward] title :: market YES-mid; ...\n')
        fh.write('\n'.join(('NEW ' if i not in seen else '') + line(events[i], cands[i]) for i in order) + '\n')
    batches = write_batches(st, events, cands, order, seen)
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
        if p.get('held') and p.get('fair') is None and not p.get('closed'):
            h = p['held']
            print(f"FAIR NEEDED {p['id']} {p['name']} | held {p.get('outcome') or p['side']} {h['size']:,.0f} sh @ {h['avg']:.3f}, "
                  f"bid {px(p.get('bid'))} ask {px(p.get('ask'))}, ends {p['end']}")
    for p in picks:
        if p.get('closed') or p.get('fair') is None:
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
        if e and weak(e) and not p.get('held'):
            why.append(f"edge gone ({weak(e)}), dropped at build unless the fair value changes")
        if p.get('held') and p.get('bid') and p['bid'] > p['fair']:
            why.append(f"held, bid {p['bid']:.3g} is above the fair value")
        if why:
            prev = f"{p['prev_ask']:.3g}→" if p.get('prev_ask') else ''
            ask = f"{p['ask']:.3g}" if p.get('ask') else '–'
            ret = f"{e['tot'] * 100:+.0f}% ({e['ann'] * 100:+,.0f}% a year)" if e else '–'
            print(f"REVIEW {p['id']} [{'; '.join(why)}] {p['name']} | {p['side']} ask {prev}{ask} fair {p['fair']:.2f} return {ret}"
                  f"{' | HELD' if p.get('held') else ''}")
    print(f'CANDIDATES {len(order)} events ({len(new)} new) -> {os.path.relpath(CANDIDATES, ROOT)}')
    print(f"BATCHES {len(batches)} for the full review ({sum(b['events'] for b in batches)} events, "
          f"{sum(b['markets'] for b in batches)} markets) -> {os.path.relpath(BATCHES, ROOT)}/index.json")


# ---------------------------------------------------------------- edit the book

def cmd_list(a):
    st = load()
    rows = sorted(st['picks'], key=lambda p: -(econ(p) or {'ann': -9})['ann'])
    for p in rows:
        e = econ(p)
        fair = f"{p['fair']:.3f}" if p.get('fair') is not None else '  –  '
        print(f"{p['id']:>8} {p['side']:3} ask {p.get('ask') or '–':<6} fair {fair} "
              f"{(e['ann'] * 100 if e else 0):+7,.0f}%/yr {(e['tot'] * 100 if e else 0):+5.0f}% {p['end']} "
              f"{'APR ' if p.get('hold') else '    '}{'HELD ' if p.get('held') else '     '}"
              f"{p.get('tag') or '':14.14} {p['name']}")


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
    print(f"set {p['id']} {p['name']}: fair {p['fair']:.3f}, return {e['tot'] * 100:+.0f}% ({e['ann'] * 100:+,.0f}% a year)"
          if e else f"set {p['id']}")


def cmd_drop(a):
    st = load()
    p = find(st, a.id)
    if p.get('held'):
        sys.exit(f"{p['name']} is a held position: it stays in the book while held (change its fair value with `set`)")
    st['picks'].remove(p)
    st['dropped'].append(dict(p, reason=a.reason))
    write(STATE, st)
    print(f"dropped {p['id']} {p['name']}")


def cmd_add(a):
    st = load()
    e = event_by_ref(a.event)
    words = a.question.lower().split()
    ms = ([m for m in e.get('markets') or [] if not m.get('closed') and m['id'] == a.question.strip()] or  # a market id
          [m for m in e.get('markets') or [] if not m.get('closed') and all(w in m['question'].lower() for w in words)])
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
          f"return {x['tot'] * 100:+.0f}%{' incl. APR reward' if p['hold'] else ''} ({x['ann'] * 100:+,.0f}% a year), "
          f"ends {p['end']}" if x else f"added {p['id']} {p['name']} (no ask on the book)")
    if x and weak(x):
        print(f"warning: {weak(x)} is under the bar ({MIN_ANN * 100:.0f}% a year and +{MIN_EDGE * 100:.0f}% to resolution), "
              "so build will drop it again")
    elif x and x['ann'] < MAIN_ANN:
        print(f"note: under {MAIN_ANN * 100:.0f}% a year, so it goes to the appendix, not the main list")


def cmd_note(a):
    st = load()
    st['notes'].append(a.text)
    write(STATE, st)
    print(f"{len(st['notes'])} notes")


# ---------------------------------------------------------------- full review

def settle_date(s, end):
    """A review's expected settlement date as YYYY-MM-DD, kept only if it falls from today up to the day before the end."""
    try:
        d = str(datetime.date.fromisoformat(str(s)[:10])) if s else None
    except ValueError:
        return None
    return d if d and str(TODAY) <= d < end else None


def cmd_ingest(a):
    """Read the research subagents' fair values (hunt/work/reviews/*.json) into the book and print what to act on.

    Each result file is a JSON list of {"market": <market id>, "fair": <probability of YES>, "why": "<dated reason>"},
    plus "settles": "YYYY-MM-DD" where the market will most likely be decided well before its end date (it hangs on a
    scheduled launch, vote or ruling, and the end date is only a backstop); returns are then annualized to that date.
    Prints MISSING for batches with markets not reviewed today (rerun those), FLAG for markets outside the book where
    either side clears the main list's bars at today's prices, and BOOK DIFF where a review disagrees with a pick.
    """
    st = load()
    idx_path = os.path.join(BATCHES, 'index.json')
    if not os.path.exists(idx_path) or read(idx_path).get('day') != str(TODAY):
        sys.exit('no batches from today: run `python3 hunt/hunt.py scan` first')
    wanted = {}  # market id -> (batch id, event row, market row)
    for b in read(idx_path)['batches']:
        for e in read(os.path.join(ROOT, b['file'])):
            for m in e['markets']:
                wanted[str(m['id'])] = (b['id'], e, m)
    reviews = st.setdefault('reviews', {})
    got, bad = 0, []
    for f in sorted(glob.glob(os.path.join(REVIEWS, '*.json'))):
        try:
            rows = read(f)
        except Exception as ex:
            bad.append(f'{os.path.basename(f)}: not JSON ({ex})')
            continue
        for r in rows if isinstance(rows, list) else [rows]:
            try:
                mid, fair, why = str(r['market']), float(r['fair']), str(r['why']).strip()
            except Exception:
                bad.append(f'{os.path.basename(f)}: {str(r)[:120]}')
                continue
            if mid not in wanted or not 0 <= fair <= 1 or not why:
                bad.append(f'{os.path.basename(f)}: {str(r)[:120]}')
                continue
            reviews[mid] = dict(fair=round(min(max(fair, 0.001), 0.999), 4), why=why, day=str(TODAY),
                                event=wanted[mid][1]['event'])
            settles = settle_date(r.get('settles'), wanted[mid][2]['end'])
            if settles:
                reviews[mid]['settles'] = settles
            got += 1
    cutoff = str(TODAY - datetime.timedelta(days=REVIEW_KEEP_DAYS))
    for mid in [k for k, r in reviews.items() if r['day'] < cutoff]:
        del reviews[mid]

    today = {mid: reviews[mid] for mid in wanted if reviews.get(mid, {}).get('day') == str(TODAY)}
    missing = Counter(b for mid, (b, _, _) in wanted.items() if mid not in today)
    size = Counter(b for b, _, _ in wanted.values())
    book = {p['id']: p for p in st['picks']}
    flags, diffs = [], []
    for mid, r in today.items():
        _, e, m = wanted[mid]
        p = book.get(mid)
        if p:
            if p.get('fair') is not None:
                ours = r['fair'] if p['side'] == 'YES' else 1 - r['fair']
                if abs(ours - p['fair']) >= REVIEW_GAP:
                    diffs.append((p, ours, r))
            continue
        if abs(r['fair'] - (m['yes_bid'] + m['yes_ask']) / 2) < REVIEW_GAP:  # a few cents off the mid is the reviewer's noise
            continue
        for side, fair, ask in (('YES', r['fair'], m['yes_ask']), ('NO', 1 - r['fair'], 1 - m['yes_bid'])):
            if not 0 < ask < 1:
                continue
            x = econ(dict(ask=ask, fair=fair, end=r.get('settles') or m['end'], hold=m['apr']))
            if x['ann'] >= MAIN_ANN and x['tot'] >= MIN_EDGE:
                flags.append((x, side, ask, fair, e, m, r))
    flags.sort(key=lambda f: -f[0]['ann'])
    st['review'] = dict(day=str(TODAY), markets=len(today), of=len(wanted),
                        events=len({wanted[mid][1]['event'] for mid in today}),
                        of_events=len({e['event'] for _, e, _ in wanted.values()}), flags=len(flags), diffs=len(diffs))
    write(STATE, st)

    rv = st['review']
    print(f"ingest: {got} results read; reviewed today: {rv['markets']} of {rv['of']} markets in {rv['events']} of "
          f"{rv['of_events']} events; {len(flags)} FLAG, {len(diffs)} BOOK DIFF, {len(bad)} rejected")
    for b in sorted(missing):
        print(f'MISSING {b}: {missing[b]} of {size[b]} markets without a review today (rerun this batch)')
    for line in bad[:20]:
        print(f'REJECTED {line}')
    for x, side, ask, fair, e, m, r in flags:
        s = r.get('settles')
        print(f"FLAG {x['ann'] * 100:+,.0f}%/yr {x['tot'] * 100:+.0f}% {side} ask ~{ask:.3g} fair {fair:.3f} ends {m['end']}"
              f"{f' settles ~{s}' if s else ''}{' APR' if m['apr'] else ''} | add {e['event']} {m['id']}"
              f"{f' --end {s}' if s else ''} | {m['question']} | {r['why']}")
    for p, ours, r in diffs:
        print(f"BOOK DIFF {p['id']} {p['name']}: book {p['side']} fair {p['fair']:.3f}, review {ours:.3f} | {r['why']}")


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

    def holders(p):  # min_balance=1 keeps v1's floor: wallets with dust under one share are left out
        try:
            return p['id'], get(f"{DATA}/v2/holders?condition={p['cid']}&limit=20&min_balance=1", tries=3)['data']
        except Exception:
            return p['id'], None
    with ThreadPoolExecutor(4) as ex:
        H = dict(ex.map(holders, picks))
    seen = {h['proxy_wallet'] for v in H.values() if v for s in v for h in s['holders']} - {ADAPTER}
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
                name = h.get('name') or h.get('pseudonym') or h['proxy_wallet'][:8]
                if h['proxy_wallet'] == ADAPTER or name.lower() in MAKERS:
                    continue
                pnl = (wallets.get(h['proxy_wallet']) or [None])[0]
                if pnl is not None and pnl >= SMART_PNL:
                    sd = 'YES' if h.get('outcome_index') == 0 else 'NO'
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
        if e and weak(e) and not p.get('held'):
            prev = f"{p['prev_ask']:.3g}→" if p.get('prev_ask') else ''
            st['dropped'].append(dict(p, reason=f"Edge gone: ask {prev}{p['ask']:.3g}, fair {p['fair']:.2f}, {weak(e)}"))
            continue
        keep.append(p)
    st['picks'] = keep
    rows = [(p, econ(p)) for p in keep if econ(p) and not weak(econ(p))]
    rows.sort(key=lambda r: -r[1]['ann'])
    main = [r for r in rows if r[1]['ann'] >= MAIN_ANN]
    extra = [r for r in rows if r[1]['ann'] < MAIN_ANN]
    noask = [p for p in keep if not econ(p) and not p.get('held')]
    held = [(p, hold_econ(p)) for p in keep if p.get('held')]
    held.sort(key=lambda r: (r[1] is None, r[1]['ann'] if r[1] else 0))  # bid above fair first, unvalued last
    st['built'] = NOW.isoformat(timespec='seconds')

    os.makedirs(SITE, exist_ok=True)
    with open(os.path.join(SITE, 'index.html'), 'w') as fh:
        fh.write(render(st, main, extra, noask, held))
    write(STATE, st)
    write(os.path.join(SITE, 'state.json'), st)
    with open(WALLETS) as src, open(os.path.join(SITE, 'wallets.json'), 'w') as dst:
        dst.write(src.read())
    short = lambda p, e: dict(name=p['name'], side=p['side'], ask=p['ask'], fair=p['fair'], tot=round(e['tot'], 4),
                              ann=round(e['ann'], 4), end=p['end'], hold=bool(p.get('hold')), url=p['url'])
    summary = dict(day=str(TODAY), built=st['built'], main=len(main), extra=len(extra),
                   hold=sum(1 for p, _ in rows if p.get('hold')), dropped=len(st['dropped']),
                   new=sum(1 for p, _ in rows if p.get('added') == str(TODAY)),
                   top=[short(p, e) for p, e in main[:5]],
                   resolved=[dict(name=p['name'], side=p['side'], won=p['won'], held=bool(p.get('held'))) for p in st['resolved']],
                   notes=st['notes'],
                   positions={p['token']: dict(fair=p['fair'], bid=p.get('bid'), tot=round(h['tot'], 4) if h else None,
                                               edge=round(h['edge'], 2) if h else None, hold=bool(p.get('hold')))
                              for p, h in held},
                   held=len(held), held_above=[p['name'] for p, h in held if h and h['tot'] < 0],
                   held_unvalued=sum(1 for p, _ in held if p.get('fair') is None),
                   held_edge=round(sum(h['edge'] for _, h in held if h), 2),
                   review=st['review'] if (st.get('review') or {}).get('day') == str(TODAY) else None)
    write(os.path.join(SITE, 'summary.json'), summary)
    print(f"built site/hunt/index.html: {len(main)} main, {len(extra)} appendix, {len(noask)} without an ask, "
          f"{summary['hold']} earn the APR reward, {summary['new']} new today, {len(st['dropped'])} dropped, "
          f"{len(st['resolved'])} resolved")
    for p, e in main[:10]:
        print(f"  {e['ann'] * 100:+7,.0f}%/yr {e['tot'] * 100:+4.0f}% {'APR ' if p.get('hold') else '    '}{p['side']} "
              f"{p['ask']:.3g} fair {p['fair']:.2f} ends {p['end']}  {p['name']}")
    print(f"positions: {len(held)} held, {len(summary['held_above'])} with the bid above fair, "
          f"{summary['held_unvalued']} without a fair value, hold-vs-sell edge ${summary['held_edge']:+,.0f}")
    for p, h in held:
        fair = f"{p['fair']:.3f}" if p.get('fair') is not None else '–'
        hvs = f"{h['tot'] * 100:+.1f}% ({h['ann'] * 100:+,.0f}%/yr)" if h else '–'
        print(f"  held {p['held']['size']:>8,.0f} {p.get('outcome') or p['side']:4} bid {px(p.get('bid')):>5} fair {fair:<5} "
              f"hold vs sell {hvs:>16}  {p['name']}")
    for p in st['dropped']:
        print(f"  dropped: {p['name']}: {p['reason']}")
    for p in st['resolved']:
        print(f"  resolved: {p['name']}: our {p['side']} {'won' if p['won'] else 'lost'}")


# ---------------------------------------------------------------- page

def esc(s):
    return html.escape(str(s or ''))


def pct(x):
    return f'{x * 100:+,.0f}%'


def px(x):
    return f'{x:.3g}' if x is not None else '–'


def fv(x):
    """A fair value: two decimals, three near the ends where the third one matters."""
    return f'{x:.3f}' if x >= 0.95 or x <= 0.05 else f'{x:.2f}'


def side_pill(p):
    out = p.get('outcome') or p['side']
    label = out.upper() if out.lower() in ('yes', 'no') else out
    return f'<span class="side {p["side"].lower()}">{esc(label)}</span>'


def smart_cell(p):
    s = p.get('smart') or {}
    if s.get('share') is None:
        return '<span class="faint">–</span>'
    v = s['share'] * 100
    t = 'up' if v >= 60 else 'down' if v <= 40 else ''
    tip = esc('Ours: ' + (', '.join(s['top_ours']) or 'none') + ' · Theirs: ' + (', '.join(s['top_theirs']) or 'none'))
    return f'<b class="{t}" title="{tip}">{v:.0f}%</b><div class="sub">${s["ours"]:,} v ${s["theirs"]:,}</div>'


def live_attrs(p):
    """What the page's Refresh asks button needs to redo a row's math at a live price."""
    fair = '' if p.get('fair') is None else p['fair']
    return f'data-token="{esc(p["token"])}" data-fair="{fair}" data-end="{esc(p["end"])}" data-hold="{1 if p.get("hold") else 0}"'


def row_html(i, p, e):
    side = side_pill(p)
    badges = ''
    if p.get('hold'):
        badges += '<span class="apr">3.25% APR</span>'
    if p.get('held'):
        badges += '<span class="held">Held</span>'
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
        fair_ch = f'<div class="sub">was {fv(p["prev_fair"])}</div>'
    tone = 'r3' if e['ann'] >= 1 else 'r2' if e['ann'] >= 0.5 else 'r1' if e['ann'] >= MAIN_ANN else 'r0'
    bonus = f'<div class="sub gold">incl. +{e["bonus"] * 100:.1f}% reward</div>' if p.get('hold') else ''
    smart = smart_cell(p)
    end = datetime.date.fromisoformat(p['end'])
    return f'''<tr id="m{esc(p['id'])}" {live_attrs(p)} data-ask="{p['ask']}">
  <td class="n">{i}</td>
  <td class="mkt"><a href="{esc(p['url'])}">{esc(p['name'])}</a><div class="meta">{side}{badges}</div><p class="why">{esc(p['why'])}</p></td>
  <td class="num" data-k="Ask"><b>{px(p['ask'])}</b>{move}</td>
  <td class="num" data-k="Fair"><b>{fv(p['fair'])}</b>{fair_ch}</td>
  <td class="num" data-k="Return"><b>{pct(e['tot'])}</b>{bonus}</td>
  <td class="num ret {tone}" data-k="Per yr"><b>{pct(e['ann'])}</b></td>
  <td class="num" data-k="Smart $">{smart}</td>
  <td class="num" data-k="Depth">${p.get('depth') or 0:,}</td>
  <td class="num" data-k="Ends">{end:%-d %b %Y}</td>
</tr>'''


def table_html(rows):
    head = ('<thead><tr><th class="n">#</th><th class="mkt">Market</th><th>Ask</th><th>Fair</th>'
            '<th title="Fair ÷ ask − 1 held to resolution, plus the holding reward">Return</th>'
            '<th title="Return ÷ years to resolution; the list is ranked by it">Per yr ▾</th>'
            '<th title="Share of smart-money dollars on our side">Smart $</th><th>Depth</th><th>Ends</th></tr></thead>')
    body = '\n'.join(row_html(i, p, e) for i, (p, e) in enumerate(rows, 1))
    return f'<div class="table-box"><table class="hunt">{head}<tbody>{body}</tbody></table></div>'


def held_row_html(i, p, h):
    d = p['held']
    badges = ''
    if p.get('hold'):
        badges += '<span class="apr">3.25% APR</span>'
    if p.get('origin') != 'position':
        badges += '<span class="new">Hunt pick</span>'
    if p.get('tag'):
        badges += f'<span class="tag">{esc(p["tag"])}</span>'
    prev = p.get('prev_bid')
    move = ''
    if p.get('bid') and prev is not None and abs(p['bid'] - prev) >= 0.005:  # a higher bid is good news for a holder
        move = f'<div class="sub {"up" if p["bid"] > prev else "down"}">was {px(prev)}</div>'
    if p.get('fair') is None:
        fair = '<span class="faint">–</span><div class="sub">not valued yet</div>'
    else:
        fair = f'<b>{fv(p["fair"])}</b>'
        if p.get('prev_fair') is not None and abs(p['fair'] - p['prev_fair']) >= 0.005:
            fair += f'<div class="sub">was {fv(p["prev_fair"])}</div>'
    if h:
        tone = 'down' if h['tot'] < 0 else 'flat' if h['tot'] < 0.02 else 'up'
        bonus = f'<div class="sub gold">incl. +{h["bonus"] * 100:.1f}% reward</div>' if p.get('hold') else ''
        hvs = f'<b class="{tone}">{h["tot"] * 100:+.1f}%</b>{bonus}'
        per_yr = f'<b class="{tone}">{pct(h["ann"])}</b>'
        edge = f'<b class="{tone}">{"+" if h["edge"] >= 0 else "−"}${abs(h["edge"]):,.0f}</b><div class="sub">on ${h["value"]:,.0f}</div>'
    else:
        hvs = per_yr = edge = '<span class="faint">–</span>'
    why = f'<p class="why">{esc(p["why"])}</p>' if p.get('why') else ''
    end = datetime.date.fromisoformat(p['end'])
    return f'''<tr {live_attrs(p)} data-size="{d['size']}" data-bid="{p.get('bid') or ''}">
  <td class="n">{i}</td>
  <td class="mkt"><a href="{esc(p['url'])}">{esc(p['name'])}</a><div class="meta">{side_pill(p)}{badges}</div>{why}</td>
  <td class="num" data-k="Held"><b>{d['size']:,.0f}</b><div class="sub">@ {d['avg']:.3f}</div></td>
  <td class="num" data-k="Bid"><b>{px(p.get('bid'))}</b>{move}</td>
  <td class="num" data-k="Fair">{fair}</td>
  <td class="num" data-k="Hold vs sell">{hvs}</td>
  <td class="num" data-k="Per yr">{per_yr}</td>
  <td class="num" data-k="$ edge">{edge}</td>
  <td class="num" data-k="Smart $">{smart_cell(p)}</td>
  <td class="num" data-k="Ends">{end:%-d %b %Y}</td>
</tr>'''


def held_table_html(held):
    head = ('<thead><tr><th class="n">#</th><th class="mkt">Position</th><th>Shares</th><th>Bid</th><th>Fair</th>'
            '<th title="Fair ÷ bid − 1, plus the holding reward">Hold vs sell</th>'
            '<th title="Hold vs sell ÷ years to resolution">Per yr</th>'
            '<th title="Shares × (fair + reward − bid)">$ edge</th><th>Smart $</th><th>Ends</th></tr></thead>')
    body = '\n'.join(held_row_html(i, p, h) for i, (p, h) in enumerate(held, 1))
    return f'<div class="table-box"><table class="hunt">{head}<tbody>{body}</tbody></table></div>'


def render(st, main, extra, noask, held):
    rows = main + extra
    n_hold = sum(1 for p, _ in rows if p.get('hold'))
    boxes = []
    if st['notes']:
        boxes.append('<div class="box warn" id="changes"><h3>What changed</h3><ul>' +
                     ''.join(f'<li>{esc(n)}</li>' for n in st['notes']) + '</ul></div>')
    if st['resolved']:
        boxes.append('<div class="box" id="resolved"><h3>Resolved since the last run</h3><ul>' + ''.join(
            f'<li><a href="{esc(p["url"])}">{esc(p["name"])}</a>: our {p["side"]} '
            f'<b class="{"up" if p["won"] else "down"}">{"won" if p["won"] else "lost"}</b>'
            f'{" (held)" if p.get("held") else ""}</li>' for p in st['resolved']) +
            '</ul></div>')
    boxes_html = f'<div class="boxes">{"".join(boxes)}</div>' if boxes else ''
    # the tab bar: one tab per section on the page, in page order, with its count
    tabs = [('Overview', 'top', None)]
    if st['notes']:
        tabs.append(('What changed', 'changes', None))
    if st['resolved']:
        tabs.append(('Resolved', 'resolved', len(st['resolved'])))
    if held:
        tabs.append(('Positions', 'positions', len(held)))
    tabs.append(('Main list', 'main', len(main)))
    if extra:
        tabs.append(('Slower', 'slower', len(extra)))
    if noask:
        tabs.append(('No sellers', 'noask', len(noask)))
    if st['dropped']:
        tabs.append(('Dropped', 'dropped', len(st['dropped'])))
    tabs_html = ('<div class="bar"><nav class="tabs" aria-label="Sections">' + ''.join(
        f'<a href="#{t}">{label}{f"<span>{n}</span>" if n is not None else ""}</a>' for label, t, n in tabs) + '</nav>'
        f'<button type="button" class="refresh" id="refresh" data-apr="{HOLD_APR}" data-min-days="{ANN_MIN_DAYS}" '
        f'data-main="{MAIN_ANN}" data-built="{NOW:%H:%M}" title="Fetch the current order books from Polymarket and redo '
        f'the returns at those prices. Fair values and the ranking stay as built."><span aria-hidden="true">↻</span>'
        f'<span class="lbl">Refresh asks</span><span class="when"></span></button></div>')
    dropped = ''
    if st['dropped']:
        dropped = ('<section id="dropped"><h2>Dropped today</h2><ul class="plain">' + ''.join(
            f'<li><a href="{esc(p["url"])}">{esc(p["name"])}</a> <span class="faint">{p["side"]} '
            f'{px(p.get("prev_ask"))} → {px(p.get("ask"))}</span><div class="li-body">{esc(p["reason"])}</div></li>'
            for p in st['dropped']) + '</ul></section>')
    noask_html = ''
    if noask:
        noask_html = ('<section id="noask"><h2>No sellers today</h2><p class="note">Still in the book, but nobody is offering our side, '
                      'so there is no return to show.</p><ul class="plain">' + ''.join(
                          f'<li {live_attrs(p)}><a href="{esc(p["url"])}">{esc(p["name"])}</a> <span class="faint">{p["side"]}, fair '
                          f'{p["fair"]:.2f}</span><span class="live"></span></li>' for p in noask) + '</ul></section>')
    above = sum(1 for _, h in held if h and h['tot'] < 0)
    unvalued = sum(1 for p, _ in held if p.get('fair') is None)
    held_html = ''
    if held:
        held_html = (f'<section id="positions"><h2>Your open positions: {len(held)}</h2><p class="note">The same analysis on '
                     'what the wallet holds. <b>Hold vs sell</b> is what holding to resolution should earn over selling into the '
                     'best bid now: fair ÷ bid − 1, plus the holding reward where the market pays it. Red means the bid is above '
                     'my fair value. <b>Per yr</b> is the same per year to resolution. <b>$ edge</b> = shares × (fair + reward − bid). '
                     'Sorted by Per yr, lowest first.'
                     f'{f" {unvalued} not valued yet." if unvalued else ""}</p>{held_table_html(held)}</section>')
    main_html = table_html(main) if main else f'<p class="note">Nothing clears {MAIN_ANN * 100:.0f}% a year today.</p>'
    extra_html = (f'<section id="slower"><h2>Slower picks ({MIN_ANN * 100:.0f}–{MAIN_ANN * 100:.0f}% a year)</h2>{table_html(extra)}</section>'
                  if extra else '')
    new = [p for p, _ in rows if p.get('added') == str(TODAY)]
    jump = lambda text, n, href: f'<a href="{href}">{text}</a>' if n else text  # "new" goes to the first new row
    new_href = f'#m{esc(new[0]["id"])}' if new else ''
    changes = (f'<span class="v">{jump(len(new), len(new), new_href)} / {jump(len(st["dropped"]), len(st["dropped"]), "#dropped")}</span>'
               f'<span class="s">{jump("new", len(new), new_href)} / {jump("dropped", len(st["dropped"]), "#dropped")}</span>')
    swept = st.get('swept') or {}
    rv = st.get('review') or {}
    reviewed = (f"<br>{rv['markets']:,} of {rv['of']:,} candidate markets researched today" if rv.get('day') == str(TODAY)
                else '')
    return f'''<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex">
<link rel="icon" type="image/svg+xml" href="{FAVICON}">
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
.summary {{ display: grid; grid-template-columns: repeat(5, minmax(0, 1fr)); border-top: 2px solid var(--ink); border-bottom: 1px solid var(--rule); }}
.stat {{ padding: 14px 20px 16px 0; display: grid; gap: 2px; align-content: start; }}
.stat + .stat {{ padding-left: 20px; border-left: 1px solid var(--rule); }}
.stat .k {{ font-size: 12px; letter-spacing: .06em; text-transform: uppercase; color: var(--muted); font-weight: 600; }}
.stat .v {{ font-family: var(--mono); font-size: 24px; font-weight: 500; }}
.stat .s {{ font-size: 12.5px; color: var(--muted); }}
section, tr, header, .box {{ scroll-margin-top: 64px; }}
@media (prefers-reduced-motion: no-preference) {{ html {{ scroll-behavior: smooth; }} }}
.bar {{ position: sticky; top: 0; z-index: 10; display: flex; align-items: stretch; gap: 10px; margin: 0 -28px; padding: 0 16px; min-width: 0;
        background: var(--ground); box-shadow: inset 0 -1px var(--rule); }}
.tabs {{ flex: 1; min-width: 0; display: flex; gap: 2px; overflow-x: auto; scrollbar-width: none; }}
.refresh {{ flex: none; align-self: center; display: inline-flex; align-items: center; gap: 6px; padding: 5px 12px;
            font: 600 13px var(--body); color: var(--accent); background: var(--surface); cursor: pointer;
            border: 1px solid color-mix(in srgb, var(--accent) 40%, transparent); border-radius: 999px; }}
.refresh:hover {{ background: color-mix(in srgb, var(--accent) 10%, var(--surface)); }}
.refresh:focus-visible {{ outline: 2px solid var(--accent); outline-offset: 2px; }}
.refresh:disabled {{ cursor: progress; opacity: .7; }}
.refresh .when {{ font-family: var(--mono); font-weight: 500; font-size: 12px; color: var(--muted); }}
.refresh .when:empty {{ display: none; }}
.refresh.busy span[aria-hidden] {{ animation: spin 1s linear infinite; }}
.refresh.err {{ color: var(--down); border-color: color-mix(in srgb, var(--down) 50%, transparent); }}
@keyframes spin {{ to {{ transform: rotate(360deg); }} }}
@keyframes flash {{ from {{ background: color-mix(in srgb, var(--accent) 22%, transparent); }} to {{ background: transparent; }} }}
td.flash {{ animation: flash 2.5s ease-out; }}
@media (prefers-reduced-motion: reduce) {{ td.flash, .refresh.busy span[aria-hidden] {{ animation: none; }} }}
.live {{ color: var(--muted); font-size: 13px; }}
dialog.moves {{ width: min(680px, calc(100vw - 32px)); max-height: min(82vh, 760px); padding: 0; color: var(--ink); background: var(--surface);
                border: 1px solid var(--rule); border-radius: 12px; box-shadow: 0 24px 64px rgb(0 0 0 / .3); }}
dialog.moves[open] {{ display: flex; flex-direction: column; }}
dialog.moves::backdrop {{ background: rgb(8 12 20 / .5); }}
.moves-head {{ display: flex; justify-content: space-between; align-items: start; gap: 16px; padding: 18px 20px 12px; border-bottom: 1px solid var(--rule); }}
.moves-head h2 {{ margin: 0; }}
.moves-head p {{ margin: 2px 0 0; font-size: 13px; color: var(--muted); }}
.moves-close {{ flex: none; width: 32px; height: 32px; font-size: 20px; line-height: 1; color: var(--muted); background: none; cursor: pointer;
                border: 1px solid var(--rule); border-radius: 999px; }}
.moves-close:hover {{ color: var(--ink); }}
.moves-close:focus-visible {{ outline: 2px solid var(--accent); outline-offset: 2px; }}
.moves-list {{ list-style: none; margin: 0; padding: 0 20px; overflow-y: auto; }}
.moves-list li {{ display: grid; grid-template-columns: minmax(0, 1fr) auto 64px; gap: 4px 16px; align-items: baseline; padding: 11px 0; border-bottom: 1px solid var(--rule); }}
.moves-list li:last-child {{ border-bottom: 0; }}
.moves-list a {{ font-weight: 600; line-height: 1.35; }}
.moves-list .sub {{ grid-column: 1; }}
.moves-list .fair {{ grid-column: 2 / -1; text-align: right; font-family: var(--mono); font-size: 12px; color: var(--muted); white-space: nowrap; }}
.moves-list .chg, .moves-list .d {{ font-family: var(--mono); font-size: 14px; white-space: nowrap; text-align: right; font-variant-numeric: tabular-nums; }}
.moves-list .chg {{ color: var(--muted); }}
.moves-list .d {{ font-weight: 600; }}
.moves-empty {{ margin: 0; padding: 20px; color: var(--muted); }}
.moves-foot {{ margin: 0; padding: 10px 20px 14px; font-size: 12.5px; color: var(--muted); border-top: 1px solid var(--rule); }}
.moves-foot:empty {{ display: none; }}
.tabs::-webkit-scrollbar {{ display: none; }}
.tabs a {{ flex: none; display: flex; align-items: center; gap: 7px; padding: 12px 12px 10px;
           font-size: 13.5px; font-weight: 600; color: var(--muted); white-space: nowrap; border-bottom: 2px solid transparent; }}
.tabs a:hover {{ color: var(--ink); }}
.tabs a[aria-current] {{ color: var(--ink); border-bottom-color: var(--accent); }}
.tabs a span {{ font-family: var(--mono); font-size: 11.5px; font-weight: 500; padding: 0 6px; border-radius: 999px;
                background: var(--surface-2); color: var(--muted); }}
.tabs a[aria-current] span {{ background: color-mix(in srgb, var(--accent) 18%, transparent); color: var(--accent); }}
tr:target {{ background: color-mix(in srgb, var(--accent) 12%, transparent); }}
.stat.goldk {{ background: var(--gold-bg); padding-left: 20px; }}
.stat.goldk .v {{ color: var(--gold); }}
.boxes {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(340px, 1fr)); gap: 14px; }}
.box {{ border-left: 3px solid var(--accent); background: var(--surface); border-radius: 0 8px 8px 0; padding: 12px 16px; font-size: 14px; }}
.box.warn {{ border-color: var(--warn-ink); }}
.box h3 {{ margin: 0 0 4px; font-size: 12px; letter-spacing: .06em; text-transform: uppercase; color: var(--muted); }}
.box p {{ margin: 0; }}
.box ul {{ margin: 0; padding-left: 18px; display: grid; gap: 4px; }}
.box.warn ul {{ display: block; columns: 2 380px; column-gap: 40px; }}  /* the notes run two columns when the box is wide */
.box.warn li {{ break-inside: avoid; margin-bottom: 6px; }}
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
.held {{ color: var(--surface); background: var(--ink); }}
b.flat {{ color: var(--muted); }}
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
  .bar {{ margin: 0 -16px; padding: 0 4px; gap: 4px; }}
  .refresh {{ padding: 5px 10px; margin-right: 8px; }}
  .refresh .lbl {{ display: none; }}
  .stamp {{ text-align: left; }}
  .summary {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
  .stat, .stat + .stat {{ padding: 12px 12px 12px 0; border-left: 0; border-bottom: 1px solid var(--rule); }}
  .stat.goldk {{ padding-left: 12px; }}
  .boxes {{ grid-template-columns: 1fr; }}
  /* the moves modal: the name takes its own line, the numbers sit under it */
  .moves-list li {{ grid-template-columns: minmax(0, 1fr) auto auto; gap: 3px 12px; }}
  .moves-list a {{ grid-column: 1 / -1; }}
  .moves-list .sub {{ grid-row: 2 / span 2; }}
  .moves-list .chg {{ grid-column: 2; grid-row: 2; }}
  .moves-list .d {{ grid-column: 3; grid-row: 2; }}
  .moves-list .fair {{ grid-row: 3; }}
}}
@media (max-width: 1040px) {{
  table.hunt thead {{ display: none; }}
  table.hunt, table.hunt tbody {{ display: block; }}
  table.hunt tr {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(88px, 1fr)); gap: 8px 12px; padding: 14px 14px 16px; border-bottom: 1px solid var(--rule); }}
  table.hunt tr:last-child {{ border-bottom: 0; }}
  table.hunt td {{ padding: 0; border: 0; }}
  td.n {{ display: none; }}
  td.mkt {{ grid-column: 1 / -1; min-width: 0; width: auto; }}
  td.num {{ text-align: left; white-space: normal; }}
  td.num::before {{ content: attr(data-k); display: block; font-family: var(--body); font-size: 11px; letter-spacing: .06em; text-transform: uppercase; color: var(--faint); font-weight: 600; }}
}}
@media print {{
  @page {{ size: A4 landscape; margin: 10mm; }}
  :root {{ --ground: #fff; --surface: #fff; --surface-2: #f4f4f4; --ink: #111; --muted: #444; --faint: #777; --rule: #ddd;
           --gold: #8F6200; --gold-bg: #FFF4D1; --gold-line: #E9C46A; --up: #177A47; --down: #BD3434; --accent: #2F4BCF; }}
  body {{ font-size: 9pt; }}
  .wrap {{ max-width: none; padding: 0; gap: 12px; }}
  .eyebrow a, .bar {{ display: none; }}
  td {{ padding: 5px 6px; }} th {{ padding: 6px; }}
  td.mkt {{ min-width: 0; width: 42%; }}
  tr {{ break-inside: avoid; }}
  .table-box {{ border: 0; }}
  * {{ -webkit-print-color-adjust: exact; print-color-adjust: exact; }}
}}
</style></head>
<body><div class="wrap">
  <header id="top">
    <div>
      <div class="eyebrow"><a href="/">← Morning brief</a> · Polymarket</div>
      <h1>Mispricing hunt</h1>
      <p class="lede">Markets where my fair value beats the best ask, mostly "nothing ever happens" NO bets on politics and
      geopolitics, ranked by annualized return: a small edge that pays out in weeks beats a big one that takes years.
      Return is fair ÷ ask − 1 held to resolution, plus the holding reward where it applies. Not financial advice.</p>
    </div>
    <div class="stamp"><span id="asks-at">Asks at {NOW:%-d %b %Y, %H:%M}</span><br>{swept.get('events', 0):,} events swept{reviewed}<br>{len(st['picks'])} picks in the book</div>
  </header>
  <section class="summary" aria-label="Summary">
    <div class="stat"><span class="k">≥ {MAIN_ANN * 100:.0f}% a year</span><span class="v"><a href="#main">{len(main)}</a></span><span class="s"><a href="#main">main list</a></span></div>
    <div class="stat"><span class="k">Slower picks</span><span class="v">{jump(len(extra), len(extra), "#slower")}</span><span class="s">{MIN_ANN * 100:.0f}–{MAIN_ANN * 100:.0f}% a year</span></div>
    <div class="stat goldk"><span class="k">Earn 3.25% APR</span><span class="v">{n_hold}</span><span class="s">gold badge</span></div>
    <div class="stat"><span class="k">Your positions</span><span class="v"><a href="#positions">{len(held)}</a></span><span class="s">{above} with the bid above fair</span></div>
    <div class="stat"><span class="k">Changes today</span>{changes}</div>
  </section>
  {tabs_html}
  {boxes_html}
  {held_html}
  <section id="main"><h2>Main list: {len(main)} picks returning at least {MAIN_ANN * 100:.0f}% a year</h2>{main_html}</section>
  {extra_html}
  {noask_html}
  {dropped}
  <footer>
    <p><b>Ask</b> is the best offer for our side now; "was" shows the last run's ask (green = cheaper today).
    <b>Refresh asks</b> refetches every order book from Polymarket and redoes Ask, Bid, Return, Per yr and Depth at those
    prices; "was" then shows the ask when the page was built. It does no research: fair values and the order stay as built. <b>Fair</b> is my
    probability that our side pays out. <b>Per yr</b> is the return as a yearly rate, return ÷ years to resolution: what
    a dollar would make in 12 months if bets like this one kept paying the same (simple, not compounded). The lists are
    ranked by it. Picks ending within {ANN_MIN_DAYS} days count as {ANN_MIN_DAYS} days, so the last days before resolution
    don't blow the rate up. A pick leaves the book below {MIN_ANN * 100:.0f}% a year or below
    +{MIN_EDGE * 100:.0f}% to resolution, which is inside a fair value's own error.
    <b>Depth</b> is the dollars on offer at or within 2¢ of the ask.</p>
    <p><b>Smart $</b> is the share of dollars held by wallets with at least $50k all-time profit (Polymarket leaderboard) among
    the top 20 holders of each side that sits on our side; green ≥ 60%, red ≤ 40%, "–" under $300 of such money. Two
    liquidity-reward makers and the neg-risk adapter are excluded; other makers and basket hedgers are not, so read it as a hint.</p>
    <p>Holding-reward eligibility is Gamma's <i>holdingRewardsEnabled</i> flag, read today. Prices from Polymarket's CLOB.
    Rebuilt every morning by the morning brief routine.</p>
  </footer>
</div>
<dialog class="moves" id="moves" aria-labelledby="moves-title">
  <div class="moves-head"><div><h2 id="moves-title">Biggest moves</h2><p id="moves-since"></p></div>
    <button type="button" class="moves-close" id="moves-close" aria-label="Close">×</button></div>
  <ol class="moves-list" id="moves-list"></ol>
  <p class="moves-foot" id="moves-foot"></p>
</dialog>
<script>
// Light up the tab of the section being read. A clicked tab stays lit until the reader scrolls themselves,
// since a short last section can never scroll up to the bar.
(() => {{
  const nav = document.querySelector('.tabs');
  if (!nav) return;
  const links = [...nav.querySelectorAll('a')];
  const targets = links.map(a => document.getElementById(a.hash.slice(1)));
  let pinned = null, queued = false;
  const light = i => {{
    links.forEach((a, j) => j === i ? a.setAttribute('aria-current', 'true') : a.removeAttribute('aria-current'));
    const a = links[i], l = a.offsetLeft, r = l + a.offsetWidth;  // keep the lit tab in view on a narrow screen
    if (l < nav.scrollLeft || r > nav.scrollLeft + nav.clientWidth) nav.scrollLeft = l - 8;
  }};
  const update = () => {{
    queued = false;
    if (pinned !== null) return light(pinned);
    const line = nav.getBoundingClientRect().bottom + 32;
    let i = 0;
    targets.forEach((t, j) => {{ if (t && t.getBoundingClientRect().top <= line) i = j; }});
    if (nav.getBoundingClientRect().top > 1) i = 0;  // the bar hasn't stuck yet: still on the overview
    else if (scrollY > 0 && innerHeight + scrollY >= document.documentElement.scrollHeight - 2) i = links.length - 1;
    light(i);
  }};
  links.forEach((a, j) => a.addEventListener('click', () => {{ pinned = j; light(j); }}));
  ['wheel', 'touchstart', 'keydown'].forEach(ev => addEventListener(ev, () => {{ pinned = null; }}, {{ passive: true }}));
  addEventListener('scroll', () => {{ if (!queued) {{ queued = true; requestAnimationFrame(update); }} }}, {{ passive: true }});
  const hashed = links.findIndex(a => a.hash === location.hash);
  if (hashed >= 0) pinned = hashed;
  update();
}})();
{REFRESH_JS}
</script>
</body></html>
'''


# The Refresh asks button: live order books from Polymarket's CLOB (it allows any origin), the same math as econ() and
# hold_econ(), written into the rows in place. Nothing is researched and nothing is saved.
REFRESH_JS = r"""
(() => {
  const btn = document.getElementById('refresh');
  if (!btn) return;
  const APR = +btn.dataset.apr, MIN_DAYS = +btn.dataset.minDays, MAIN = +btn.dataset.main;
  const px = x => x == null ? '–' : String(+x.toPrecision(3));
  const fv = x => x >= 0.95 || x <= 0.05 ? x.toFixed(3) : x.toFixed(2);
  const pct = x => (x < 0 ? '-' : '+') + Math.abs(Math.round(x * 100)).toLocaleString('en-US') + '%';
  const usd = x => '$' + Math.round(Math.abs(x)).toLocaleString('en-US');
  const today = () => new Intl.DateTimeFormat('en-CA', { timeZone: 'Europe/Copenhagen' }).format(new Date());
  const span = end => { const d = Math.max(Math.round((Date.parse(end) - Date.parse(today())) / 864e5), 1); return [d, d / 365]; };
  const perYear = (tot, days) => tot * 365 / Math.max(days, MIN_DAYS);
  const econ = (ask, fair, end, hold) => {
    const [days, yrs] = span(end), ret = fair / ask - 1;
    const bonus = hold ? APR * yrs * ((ask + fair) / 2) / ask : 0, tot = ret + bonus;
    return { bonus, tot, ann: perYear(tot, days) };
  };
  const holdEcon = (bid, fair, end, hold, size) => {
    const [days, yrs] = span(end), reward = hold ? APR * yrs * (bid + fair) / 2 : 0, tot = (fair + reward) / bid - 1;
    return { tot, bonus: reward / bid, ann: perYear(tot, days), edge: size * (fair + reward - bid), value: size * bid };
  };
  const cell = (tr, k) => tr.querySelector(`td[data-k="${k}"]`);
  const put = (td, html) => {  // flash the cell only when its number changed, not just its "was" line
    if (!td) return;
    const lead = () => (td.firstElementChild || td).textContent, old = lead();
    td.innerHTML = html;
    if (lead() !== old) { td.classList.remove('flash'); void td.offsetWidth; td.classList.add('flash'); }
  };
  const top = book => {
    const asks = (book.asks || []).map(o => [+o.price, +o.size]).sort((a, b) => a[0] - b[0]);
    const bids = (book.bids || []).map(o => +o.price).sort((a, b) => b - a);
    const ask = asks.length ? asks[0][0] : null;
    const depth = ask == null ? 0 : Math.round(asks.filter(([p]) => p <= ask + 0.02 + 1e-9).reduce((s, [p, z]) => s + p * z, 0));
    return { ask, bid: bids.length ? bids[0] : null, depth };
  };
  const was = (now, then, better) => {
    if (now == null || isNaN(then)) return '';
    if (Math.abs(now - then) < 1e-9) return '<div class="sub">unchanged</div>';
    return `<div class="sub ${better(now, then) ? 'up' : 'down'}">was ${px(then)}</div>`;
  };
  const pick = (tr, b) => {  // a main-list or slower row
    const fair = +tr.dataset.fair, hold = tr.dataset.hold === '1';
    put(cell(tr, 'Ask'), `<b>${px(b.ask)}</b>` + (b.ask == null ? '<div class="sub down">no sellers now</div>'
      : was(b.ask, +tr.dataset.ask, (n, t) => n < t)));
    put(cell(tr, 'Depth'), '$' + b.depth.toLocaleString('en-US'));
    const ret = cell(tr, 'Per yr');
    if (b.ask == null) {
      put(cell(tr, 'Return'), '<span class="faint">–</span>'); put(ret, '<b>–</b>'); ret.className = 'num ret r0'; return;
    }
    const e = econ(b.ask, fair, tr.dataset.end, hold);
    put(cell(tr, 'Return'), `<b>${pct(e.tot)}</b>` + (hold ? `<div class="sub gold">incl. +${(e.bonus * 100).toFixed(1)}% reward</div>` : ''));
    put(ret, `<b>${pct(e.ann)}</b>`);
    ret.className = 'num ret ' + (e.ann >= 1 ? 'r3' : e.ann >= 0.5 ? 'r2' : e.ann >= MAIN ? 'r1' : 'r0');
  };
  const held = (tr, b) => {  // an open position: what selling into the bid gives up
    put(cell(tr, 'Bid'), `<b>${px(b.bid)}</b>` + was(b.bid, parseFloat(tr.dataset.bid), (n, t) => n > t));
    const dash = '<span class="faint">–</span>';
    if (tr.dataset.fair === '' || !b.bid) { ['Hold vs sell', 'Per yr', '$ edge'].forEach(k => put(cell(tr, k), dash)); return; }
    const h = holdEcon(b.bid, +tr.dataset.fair, tr.dataset.end, tr.dataset.hold === '1', +tr.dataset.size);
    const tone = h.tot < 0 ? 'down' : h.tot < 0.02 ? 'flat' : 'up';
    put(cell(tr, 'Hold vs sell'), `<b class="${tone}">${h.tot < 0 ? '-' : '+'}${Math.abs(h.tot * 100).toFixed(1)}%</b>` +
      (tr.dataset.hold === '1' ? `<div class="sub gold">incl. +${(h.bonus * 100).toFixed(1)}% reward</div>` : ''));
    put(cell(tr, 'Per yr'), `<b class="${tone}">${pct(h.ann)}</b>`);
    put(cell(tr, '$ edge'), `<b class="${tone}">${h.edge >= 0 ? '+' : '−'}${usd(h.edge)}</b><div class="sub">on ${usd(h.value)}</div>`);
  };
  const noSeller = (li, b) => {
    const out = li.querySelector('.live');
    if (b.ask == null) { out.textContent = ' · still no sellers'; return; }
    const e = econ(b.ask, +li.dataset.fair, li.dataset.end, li.dataset.hold === '1');
    out.textContent = ` · ask now ${px(b.ask)}, ${pct(e.tot)} to resolution, ${pct(e.ann)} a year`;
  };
  // The modal after a refresh: the prices that moved most since the reader last saw them (the build, then each refresh).
  const dlg = document.getElementById('moves'), seen = new Map(), TOP = 10;
  let sinceText = `the page was built at ${btn.dataset.built}`;
  const esc = s => s.replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' })[c]);
  const num = s => s === '' || s == null || isNaN(+s) ? null : +s;
  const quote = el => {  // which price a row tracks, and what it showed before this refresh
    const kind = el.dataset.size ? 'bid' : 'ask';
    const prev = seen.has(el) ? seen.get(el) : el.tagName === 'LI' ? null : num(kind === 'bid' ? el.dataset.bid : el.dataset.ask);
    return { kind, prev };
  };
  const showMoves = (moves, total) => {
    if (!dlg || !dlg.showModal) return;
    const list = dlg.querySelector('#moves-list');
    // a quote that appeared or vanished outranks any move in cents; then the bigger move first
    moves.sort((a, b) => (b.d == null) - (a.d == null) || Math.abs(b.d || 0) - Math.abs(a.d || 0));
    const seenTok = new Set(), uniq = moves.filter(m => !seenTok.has(m.token) && seenTok.add(m.token));
    dlg.querySelector('#moves-since').textContent = `Since ${sinceText}`;
    list.innerHTML = uniq.slice(0, TOP).map(m => {
      const tone = m.good ? 'up' : 'down';
      const d = m.d == null ? (m.now == null ? 'gone' : 'new')
        : `${m.d > 0 ? '+' : '−'}${Math.abs(m.d * 100).toFixed(1).replace(/\.0$/, '')}¢`;
      const sub = [m.side, m.kind === 'bid' ? 'your position, best bid' : 'best ask', m.ann && m.now != null ? `now ${m.ann} a year` : '']
        .filter(Boolean).join(' · ');
      // the market on Polymarket, in a new tab so the refreshed prices stay on screen
      return `<li><a href="${esc(m.href)}" target="_blank" rel="noopener">${esc(m.name)}</a><span class="chg">${px(m.prev)} → ${px(m.now)}</span>` +
             `<span class="d ${tone}">${d}</span><div class="sub">${esc(sub)}</div>` +
             `<span class="fair">${m.fair == null ? 'no fair value yet' : 'fair ' + fv(m.fair)}</span></li>`;  // as built, never recomputed
    }).join('');
    list.hidden = !uniq.length;
    let empty = dlg.querySelector('.moves-empty');
    if (!uniq.length && !empty) { empty = document.createElement('p'); empty.className = 'moves-empty'; list.after(empty); }
    if (empty) { empty.hidden = !!uniq.length; empty.textContent = 'No price has moved.'; }
    dlg.querySelector('#moves-foot').textContent = uniq.length
      ? `${uniq.length} of ${total} prices moved${uniq.length > TOP ? `; the ${TOP} biggest are shown` : ''}. Green is good for you: a cheaper ask, or a higher bid on a position. Fair is my estimate from the build, not redone.`
      : '';
    if (!dlg.open) dlg.showModal();
    list.scrollTop = 0;
  };
  if (dlg) {
    dlg.querySelector('#moves-close').addEventListener('click', () => dlg.close());
    dlg.addEventListener('click', ev => { if (ev.target === dlg) dlg.close(); });  // a click on the backdrop
  }
  const when = btn.querySelector('.when'), lbl = btn.querySelector('.lbl'), stamp = document.getElementById('asks-at');
  btn.addEventListener('click', async () => {
    const rows = [...document.querySelectorAll('[data-token]')];
    const tokens = [...new Set(rows.map(r => r.dataset.token))];
    btn.disabled = true; btn.classList.add('busy'); btn.classList.remove('err'); lbl.textContent = 'Refreshing…';
    try {
      const chunks = [];
      for (let i = 0; i < tokens.length; i += 100) chunks.push(tokens.slice(i, i + 100));
      const books = (await Promise.all(chunks.map(async c => {
        const r = await fetch('https://clob.polymarket.com/books', { method: 'POST', headers: { 'Content-Type': 'application/json' },
                                                                    body: JSON.stringify(c.map(t => ({ token_id: t }))) });
        if (!r.ok) throw new Error(`Polymarket answered ${r.status}`);
        return r.json();
      }))).flat();
      const by = new Map(books.map(b => [b.asset_id, top(b)]));
      const moves = [];
      for (const el of rows) {
        const b = by.get(el.dataset.token);
        if (!b) continue;
        const { kind, prev } = quote(el), now = b[kind] || null;
        if (el.tagName === 'LI') noSeller(el, b); else if (el.dataset.size) held(el, b); else pick(el, b);
        seen.set(el, now);
        if (prev == null && now == null) continue;
        const d = prev == null || now == null ? null : now - prev;
        if (d != null && Math.abs(d) < 5e-4) continue;
        const up = d == null ? now != null : d > 0;  // the quote rose, or one appeared
        const a = el.querySelector('a'), side = el.querySelector('.side'), yr = el.tagName === 'TR' && cell(el, 'Per yr');
        moves.push({ token: el.dataset.token, kind, prev, now, d, good: kind === 'bid' ? up : d == null ? up : !up,
                     fair: num(el.dataset.fair), href: a ? a.getAttribute('href') : '#',
                     name: a ? a.textContent : '', side: side ? side.textContent : '', ann: yr ? yr.textContent.trim() : '' });
      }
      const t = new Date().toLocaleTimeString('en-GB', { timeZone: 'Europe/Copenhagen', hour: '2-digit', minute: '2-digit', second: '2-digit' });
      showMoves(moves, by.size);
      sinceText = `the last refresh at ${t}`;
      when.textContent = t.slice(0, 5);
      stamp.textContent = `Live asks at ${t} (page built ${btn.dataset.built}, ${by.size} of ${tokens.length} books)`;
      lbl.textContent = 'Refresh asks';
    } catch (err) {
      btn.classList.add('err'); lbl.textContent = 'Retry'; when.textContent = '';
      stamp.textContent = `Couldn't refresh the asks: ${err.message}`;
    } finally {
      btn.disabled = false; btn.classList.remove('busy');
    }
  });
})();
"""


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
    sub.add_parser('ingest').set_defaults(f=cmd_ingest)
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
