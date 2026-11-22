"""The hunt's Kalshi leg: sweep Kalshi's open markets, pick the candidates, price Kalshi's fee, and pair each Kalshi
market with the Polymarket candidate that asks about the same thing (its "twin").

Kalshi (kalshi.com) is a US exchange regulated by the CFTC. Its contracts pay $1 like Polymarket's, but it keeps balances
in USD on its own books, so there are no wallets or holders to read. Its market data needs no account:
api.elections.kalshi.com/trade-api/v2 serves every exchange market, not only elections. hunt.py uses this module in
`scan` (the candidates go into the full review's batches, next to their Polymarket twins), `ingest` (KFLAG and PAIR
lines) and `build` (the hunt's Kalshi part). Nothing here buys anything. Only the standard library is used.

Matching is by words and dates: the words of the event title and outcome, weighted by how rare they are across both
venues (a name like "hegseth" counts for much more than "trump"), and end dates within MATCH_DAYS of each other. A
match only proposes a twin: the research subagent reads both rule texts and says whether they ask the same thing
("same"), whether the Kalshi YES covers more cases ("wider") or fewer ("narrower"), or "different". Only same, wider and
narrower twins make a cross-venue pair.
"""
import datetime, json, math, re, time, unicodedata, urllib.error, urllib.parse, urllib.request
from collections import Counter, defaultdict

API = 'https://api.elections.kalshi.com/trade-api/v2'
WEB = 'https://kalshi.com/markets'
UA = {'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/120 Safari/537.36'}

# Kalshi's own event categories. Sports, Crypto, Financials (index and FX ranges), Commodities and Mentions (what a
# speaker says) are left out, like the Polymarket sweep's sports, crypto and mention markets.
CATEGORIES = {'Politics', 'World', 'Elections', 'Economics', 'Companies', 'Science and Technology', 'Health', 'AI',
              'Social', 'Business', 'Entertainment', 'Climate and Weather'}
MIN_OI = 5000         # a market goes to the full review with at least this many contracts open (Kalshi publishes no
                      # $ liquidity; 5,000 keeps about 700 markets, a quarter more than the Polymarket review) ...
MATCH_MIN_OI = 1000   # ... or with this many if it has a Polymarket twin, which can make a cross-venue pair
MAX_SPREAD = 0.15     # same bars as hunt.candidate_markets(): two-sided, spread at most 15¢ ...
MID_RANGE = (0.05, 0.95)  # ... mid between 5% and 95% ...
DAYS_RANGE = (4, 900)     # ... and 4 days to about 2.5 years to go
TAKER = 0.07          # Kalshi's taker fee per contract: TAKER x fee_multiplier x price x (1 - price) ...
MATCH_DAYS = 7        # twins end within this many days of each other ...
MATCH_SCORE = 0.5     # ... their words other than the outcome's overlap at least this much (see match()) ...
SUBJECT_COVER = 0.8   # ... and the outcome's own words (a name) appear on the other side


def get(url, tries=4, timeout=30):
    for i in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code in (400, 404) or i == tries - 1:
                raise
            time.sleep(2 * (i + 1) if e.code == 429 else 1 + i)
        except Exception:
            if i == tries - 1:
                raise
            time.sleep(1 + i)


def fee(price, mult=1.0):
    """Kalshi's taker fee per contract bought at `price` (dollars). The exchange rounds an order's total fee up to the
    next cent, which only matters for orders of a few contracts; this is the per-contract rate for a real-sized order."""
    return TAKER * mult * price * (1 - price)


def cost(price, mult=1.0):
    """What one contract costs at `price` with the taker fee."""
    return price + fee(price, mult)


def url(series, event):
    return f'{WEB}/{series.lower()}/{event.lower()}' if event else f'{WEB}/{series.lower()}'


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _day(s):
    try:
        return str(datetime.datetime.fromisoformat(str(s).replace('Z', '+00:00')).date())
    except ValueError:
        return None


def sweep():
    """Every open Kalshi event in CATEGORIES with its markets, plus each series' fee multiplier.
    Returns (events, fee multiplier by series ticker, number of open events of all categories)."""
    events, cursor, total = [], '', 0
    while True:
        d = get(f'{API}/events?status=open&with_nested_markets=true&limit=200'
                + (f'&cursor={urllib.parse.quote(cursor)}' if cursor else ''), timeout=60)
        total += len(d.get('events') or [])
        events += [e for e in d.get('events') or [] if e.get('category') in CATEGORIES]
        cursor = d.get('cursor')
        if not cursor or not d.get('events'):
            break
    mult = {}
    try:  # one call lists every series with its fee type; quadratic_with_maker_fees has the same taker fee
        for s in get(f'{API}/series', timeout=60).get('series') or []:
            mult[s['ticker']] = float(s.get('fee_multiplier') if s.get('fee_multiplier') is not None else 1)
    except Exception as e:
        print(f'kalshi: series fees unavailable ({e}); assuming the standard fee')
    return events, mult, total


def slim(e, m, mult):
    """One Kalshi market in the hunt's terms. id is "K:<ticker>", so it never collides with a Polymarket market id."""
    yb, ya, nb, na = (_f(m.get(k)) for k in ('yes_bid_dollars', 'yes_ask_dollars', 'no_bid_dollars', 'no_ask_dollars'))
    rules = (m.get('rules_primary') or '').strip()
    return dict(id='K:' + m['ticker'], ticker=m['ticker'], event=e['event_ticker'], series=e.get('series_ticker') or '',
                title=e.get('title') or '', sub=e.get('sub_title') or '', option=m.get('yes_sub_title') or '',
                q=m.get('title') or e.get('title') or '', rules=rules[:900],
                fine=(m.get('rules_secondary') or '').strip()[:900],
                end=_day(m.get('close_time')),  # the deadline; Kalshi's expected_expiration_time is often earlier than it, so unused
                yes_bid=yb, yes_ask=ya, no_bid=nb, no_ask=na, mult=mult.get(e.get('series_ticker'), 1.0),
                oi=round(_f(m.get('open_interest_fp')) or 0), vol=round(_f(m.get('volume_fp')) or 0),
                category=e.get('category') or '', url=url(e.get('series_ticker') or '', e['event_ticker']))


def candidates(events, mult, today, noise_title, noise_q, min_oi=MATCH_MIN_OI):
    """Kalshi markets worth a look, by the same bars as the Polymarket candidates: live, two-sided, spread at most
    MAX_SPREAD, mid in MID_RANGE, DAYS_RANGE to go, not noise, plus at least min_oi contracts open.
    Returns {event ticker: (event, [slim market, ...])}."""
    out = {}
    for e in events:
        if noise_title.search(e.get('title') or ''):
            continue
        ms = []
        for m in e.get('markets') or []:
            if m.get('status') not in ('active', 'open'):
                continue
            k = slim(e, m, mult)
            if None in (k['yes_bid'], k['yes_ask'], k['end']) or not k['yes_bid'] or not k['yes_ask']:
                continue
            mid = (k['yes_bid'] + k['yes_ask']) / 2
            days = (datetime.date.fromisoformat(k['end']) - today).days
            text = ' '.join((k['q'], k['option']))
            if (k['yes_ask'] - k['yes_bid'] > MAX_SPREAD or not MID_RANGE[0] <= mid <= MID_RANGE[1]
                    or not DAYS_RANGE[0] <= days <= DAYS_RANGE[1] or k['oi'] < min_oi or noise_q.search(text)):
                continue
            ms.append(dict(k, mid=mid, days=days))
        if ms:
            out[e['event_ticker']] = (e, ms)
    return out


# ---------------------------------------------------------------- matching

_MONTHS = ('january february march april may june july august september october november december '
           'jan feb mar apr jun jul aug sep sept oct nov dec').split()
_STOP = set(('will the a an of in on at to by be is are was for and or as with before after end its it this that than '
             'from who what which when how does do did any new next his her their there yes no market markets win '
             'winner happen occur during year officially publicly announce announced announces').split()) | set(_MONTHS)
_SYN = {'leave': 'out', 'leaves': 'out', 'left': 'out', 'depart': 'out', 'departs': 'out', 'departure': 'out',
        'exit': 'out', 'exits': 'out', 'resign': 'out', 'resigns': 'out', 'removed': 'out', 'ousted': 'out',
        'u': 'us', 'usa': 'us', 'america': 'us', 'american': 'us', 'states': 'us', 'united': 'us',
        'pm': 'minister', 'prime': 'minister', 'premier': 'minister', 'secdef': 'defense', 'shut': 'shutdown',
        'impeached': 'impeach', 'impeachment': 'impeach', 'invoked': 'invoke', 'invokes': 'invoke',
        'nominee': 'nominate', 'nominated': 'nominate', 'nomination': 'nominate', 'confirmed': 'confirm',
        'arrested': 'arrest', 'indicted': 'charge', 'charged': 'charge', 'truce': 'ceasefire',
        'hike': 'increase', 'hikes': 'increase', 'raise': 'increase', 'raises': 'increase', 'cut': 'decrease',
        'cuts': 'decrease', 'lower': 'decrease', 'lowers': 'decrease', 'maintain': 'hold', 'unchanged': 'hold',
        'bp': 'bps', 'administration': 'admin', 'secretary': 'sec'}


def words(*texts):
    """The words that say what a market is about: accents folded, dates, stop words and plural s taken out."""
    s = unicodedata.normalize('NFKD', ' '.join(t or '' for t in texts)).encode('ascii', 'ignore').decode().lower()
    s = re.sub(r"'s\b|’s\b", '', s)
    toks = re.findall(r'[a-z]+|\d+(?:\.\d+)?%?', s)
    out, prev = set(), ''
    for t in toks:
        month_day = prev in _MONTHS and re.fullmatch(r'\d{1,2}', t)
        prev = t
        if t in _STOP or month_day or re.fullmatch(r'20[2-3]\d', t) or re.fullmatch(r'\d{1,2}(st|nd|rd|th)', t):
            continue
        t = _SYN.get(t, t)
        if len(t) > 4 and t.endswith('s') and not t.endswith('ss'):
            t = t[:-1]
        if len(t) > 1 or t.isdigit():
            out.add(t)
    return out


def match(kalshi, poly):
    """Propose a Polymarket twin for each Kalshi market.

    kalshi: [slim Kalshi market]; poly: [dict(id, title, q, g, end)] for the Polymarket candidate markets.
    Returns {Kalshi id: (Polymarket id, score)} for each Kalshi market's best match. Words are weighted by rarity,
    log(N / markets using it) over both venues. A match must:
      - end within MATCH_DAYS of it;
      - share the subject: the outcome's own words (a name, "Hike 25bps"; not a date or "Yes") on either side appear,
        by weight, at least SUBJECT_COVER on the other;
      - share the frame: the rest of the words, subjects taken out, overlap by at least MATCH_SCORE (weighted Jaccard).
        This keeps "Kentucky Democratic primary: Newsom" off "Newsom wins the Democratic nomination", and
        "Hegseth out as Secretary of Defense" off "Hegseth leaves the Trump administration"."""
    kdocs = {k['id']: words(k['title'], k['sub'], k['option'], k['q']) for k in kalshi}
    ksub = {k['id']: words(k['option']) for k in kalshi}
    pdocs = {p['id']: words(p['title'], p['q'], p['g']) for p in poly}
    psub = {p['id']: words(p['g']) for p in poly}
    df = Counter(t for d in list(kdocs.values()) + list(pdocs.values()) for t in d)
    n = len(kdocs) + len(pdocs)
    idf = {t: math.log(n / c) for t, c in df.items()}
    w = lambda ws: sum(idf.get(t, 0) for t in ws)
    index = defaultdict(set)
    for pid, d in pdocs.items():
        for t in d:
            if idf[t] > 2:  # look only at Polymarket markets sharing at least one rare-ish word
                index[t].add(pid)
    pend = {p['id']: datetime.date.fromisoformat(p['end']) for p in poly if p.get('end')}
    out = {}
    for k in kalshi:
        kd, ks = kdocs[k['id']], ksub[k['id']]
        end = datetime.date.fromisoformat(k['end'])
        best = None
        for pid in set().union(*(index.get(t, ()) for t in kd)) if kd else ():
            if pid not in pend or abs((end - pend[pid]).days) > MATCH_DAYS:
                continue
            pd, ps = pdocs[pid], psub[pid]
            if (ks and w(ks & pd) < SUBJECT_COVER * w(ks)) or (ps and w(ps & kd) < SUBJECT_COVER * w(ps)):
                continue
            kf, pf = kd - ks - ps, pd - ks - ps
            score = w(kf & pf) / (w(kf | pf) or 1)
            if score >= MATCH_SCORE and (best is None or score > best[1]):
                best = (pid, round(score, 3))
        if best:
            out[k['id']] = best
    return out


def quotes(tickers):
    """Live prices for some Kalshi markets: {ticker: dict(yes_bid, yes_ask, no_bid, no_ask, status, result)}."""
    out, tickers = {}, sorted(set(tickers))
    for i in range(0, len(tickers), 50):
        part = ','.join(tickers[i:i + 50])
        for m in get(f'{API}/markets?tickers={urllib.parse.quote(part, safe=",")}&limit=100').get('markets') or []:
            out[m['ticker']] = dict(yes_bid=_f(m.get('yes_bid_dollars')), yes_ask=_f(m.get('yes_ask_dollars')),
                                    no_bid=_f(m.get('no_bid_dollars')), no_ask=_f(m.get('no_ask_dollars')),
                                    status=m.get('status'), result=m.get('result') or '')
    return out
