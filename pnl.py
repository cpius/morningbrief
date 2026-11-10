"""Profit/loss chart for the brief: Polymarket's own P/L line, its biggest swings labelled with the positions behind them.

The line is `trade_pnl` from the Data API, the number polymarket.com plots under Profit/Loss. The API only records a
new value on some hours, so a swing runs from one recorded value to another. To say what drove a swing, every position
is rebuilt from the wallet's fills and the Data API's price history and its P/L is compared across the same two moments.
"""
import bisect, datetime, html, json, time, urllib.parse
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor

DATA = 'https://data-api.polymarket.com/v2'
HOUR, DAY = 3600, 86400
PERIODS = [('1D', 'Past day', DAY), ('1W', 'Past week', 7 * DAY), ('1M', 'Past month', 30 * DAY),
           ('1Y', 'Past year', 365 * DAY), ('YTD', 'Year to date', None), ('ALL', 'All time', None)]
SWING = 0.08      # a swing is a run that reverses by less than this share of the period's range
MAX_NOTES = 5     # swings labelled per period
MAX_DRIVERS = 3   # positions named per swing
DRIVER_MIN = 0.05  # ... each at least this share of the swing


def cents(p):
    return f'{p * 100:.1f}'.rstrip('0').rstrip('.') + '¢'


def retrying(get, url, tries=4):
    """The Data API sheds load with 503s; wait a little longer each time."""
    for n in range(tries):
        try:
            return get(url)
        except Exception:
            if n == tries - 1:
                raise
            time.sleep(1.5 * (n + 1))


def swings(vals, share=SWING):
    """Zigzag legs of a series: (start index, end index) pairs between turning points that reverse by the threshold."""
    if len(vals) < 2:
        return []
    thr = (max(vals) - min(vals)) * share
    if thr <= 0:
        return []
    lo = hi = 0
    pivots, up, ext = None, None, None
    for i, v in enumerate(vals):
        if pivots is None:  # until the first move of threshold size, track both extremes
            if v < vals[lo]: lo = i
            if v > vals[hi]: hi = i
            if vals[hi] - vals[lo] >= thr:
                first, ext = (lo, hi) if lo < hi else (hi, lo)
                pivots, up = [first], lo < hi
        elif up:
            if v > vals[ext]: ext = i
            elif vals[ext] - v >= thr: pivots.append(ext); up, ext = False, i
        else:
            if v < vals[ext]: ext = i
            elif v - vals[ext] >= thr: pivots.append(ext); up, ext = True, i
    if pivots is None:
        return []
    pivots.append(ext)
    return list(zip(pivots, pivots[1:]))


class Book:
    """Every position the wallet has held: share count and cash over time, and a price for any moment."""

    def __init__(self, get, pages, wallet, now):
        self.now = now
        self.pages = lambda url: retrying(pages, url, tries=6)
        act = self.pages(f'{DATA}/activity?user={wallet}&limit=500')
        self.events, self.meta = defaultdict(list), {}
        for a in sorted(act, key=lambda a: a['timestamp']):
            tok = a.get('token_id')
            if not tok:
                continue
            if a['type'] == 'TRADE':  # price x size, not usdc_size: trade P/L is before fees
                s = 1 if a['side'] == 'BUY' else -1
                self.events[tok].append((a['timestamp'], s * a['size'], -s * a['price'] * a['size'], 'trade'))
            elif a['type'] == 'REDEEM':
                self.events[tok].append((a['timestamp'], -a['size'], a['usdc_size'], 'redeem'))
            else:
                continue
            self.meta.setdefault(tok, a)
        self.prices = {}
        with ThreadPoolExecutor(3) as ex:  # more in parallel and the Data API answers 503
            for tok, series in ex.map(self._history, list(self.events)):
                self.prices[tok] = series

    def held(self, tok, t0, t1):
        """Whether the wallet held any of this token between t0 and t1."""
        h = 0.0
        for ts, dh, *_ in self.events[tok]:
            if ts > t1:
                break
            if ts >= t0 and (h > 1e-6 or dh > 0):
                return True
            h += dh
        return h > 1e-6

    def _history(self, tok):
        """Price points for one token from the Data API, whose marks the P/L snapshots use: half-hourly over the
        last month, 12-hourly before that. A resolved market's series ends with its payout."""
        first = self.events[tok][0][0]
        queries = []
        if first < self.now - 29 * DAY:
            queries.append('interval=max')
        if self.held(tok, self.now - 31 * DAY, self.now):
            queries.append('interval=1m&bucket_seconds=1800')
        pts = {}
        for q in queries:
            try:
                for x in self.pages(f'{DATA}/prices-history?token_id={tok}&{q}'):
                    pts[x['timestamp']] = x['price']
            except Exception as e:
                print(f"  pnl: no price history for {self.meta[tok]['title'][:40]} ({q}): {e}")
        return tok, sorted(pts.items())

    def price(self, tok, t):
        p = self.prices.get(tok)
        if not p:
            return None
        return p[max(bisect.bisect_right(p, (t, 9)) - 1, 0)][1]  # the bucket that starts at or before t

    def state(self, tok, t):
        """(shares, cash, price, P/L) of one token at time t."""
        h = c = 0.0
        for ts, dh, dc, _ in self.events[tok]:
            if ts > t:
                break
            h += dh; c += dc
        p = self.price(tok, t)
        return h, c, p, c + (h * p if abs(h) > 1e-6 and p is not None else 0.0)

    def drivers(self, t0, t1):
        """Positions ranked by how much their P/L changed between t0 and t1."""
        out = []
        for tok, events in self.events.items():
            h0, _, p0, v0 = self.state(tok, t0)
            h1, _, p1, v1 = self.state(tok, t1)
            if abs(v1 - v0) < 0.005:
                continue
            a = self.meta[tok]
            p0, p1 = (p0 if h0 > 1e-6 else None), (p1 if h1 > 1e-6 else None)
            if any(kind == 'redeem' and t0 < ts <= t1 for ts, _, _, kind in events):
                move = f'{cents(p0)} → redeemed' if p0 is not None else 'bought and redeemed'
            elif p0 is None:
                move = f'new, now {cents(p1)}' if p1 is not None else 'bought and sold'
            else:
                move = f"{cents(p0)} → {cents(p1) if p1 is not None else 'sold'}"
            out.append(dict(d=v1 - v0, title=a['title'], side=a['outcome'], move=move,
                            url=f"https://polymarket.com/event/{a['event_slug']}" if a.get('event_slug') else ''))
        return sorted(out, key=lambda r: -abs(r['d']))


def snapshots(points):
    """The hours on which the API recorded a new value: (time, P/L)."""
    out, block = [], None
    for x in points:
        if x['source_block'] != block:
            out.append((x['timestamp'], x['trade_pnl']))
            block = x['source_block']
    return out


def build(get, pages, wallet, now):
    """Chart data per period: the line, the headline change and the labelled swings."""
    points = retrying(get, f'{DATA}/user-pnl?user={wallet}&interval=all&fidelity=1h')['data']['points']
    points = [x for x in points if x.get('trade_pnl') is not None]
    first_t, last_t, last_v = points[0]['timestamp'], points[-1]['timestamp'], points[-1]['trade_pnl']
    snaps = snapshots(points)
    try:
        book = Book(get, pages, wallet, now)
    except Exception as e:  # the line still stands without the positions behind it
        print(f'  pnl: swings left unexplained ({type(e).__name__}: {e})')
        book = None
    jan1 = datetime.datetime(datetime.datetime.fromtimestamp(now).year, 1, 1).timestamp()
    out = []
    for key, label, span in PERIODS:
        start = jan1 if key == 'YTD' else first_t if span is None else last_t - span + HOUR
        if start <= first_t:  # the wallet is younger than the window: measured from zero, like the site
            start, base = first_t, 0.0
        else:
            start, base = next((x['timestamp'], x['trade_pnl']) for x in points if x['timestamp'] >= start)
        # (shown from, value, recorded at): the window's opening value, then each value recorded inside it
        opening = [s for s in snaps if s[0] <= start][-1]
        series = [(start, opening[1], opening[0])] + [(t, v, t) for t, v in snaps if t > start]
        notes = []
        for a, b in swings([v for _, v, _ in series]):
            d = series[b][1] - series[a][1]
            # pinned on the step that carries the swing past its halfway mark
            step = next(i for i in range(a + 1, b + 1) if abs(series[i][1] - series[a][1]) >= abs(d) / 2)
            note = dict(t0=series[a][0], t1=series[b][0], d=d, drivers=[], rest=None, rebuilt=None,
                        at=series[step][0] - HOUR / 2, v=(series[step][1] + series[step - 1][1]) / 2 - base)
            if book:
                ranked = book.drivers(series[a][2], series[b][2])
                note['rebuilt'] = sum(r['d'] for r in ranked)
                note['drivers'] = [r for r in ranked if r['d'] * d > 0 and abs(r['d']) >= DRIVER_MIN * abs(d)][:MAX_DRIVERS]
                if abs(note['rebuilt'] - d) <= 0.35 * abs(d):  # else the rebuild is too far off to speak for the rest
                    note['rest'] = d - sum(r['d'] for r in note['drivers'])
            notes.append(note)
        notes = sorted(sorted(notes, key=lambda n: -abs(n['d']))[:MAX_NOTES], key=lambda n: n['t0'])
        # plateau-and-ramp line: each recorded value is held until the hour before the next one
        line = []
        for (t, v, _), nxt in zip(series, series[1:] + [(last_t + HOUR, None, None)]):
            line.append((t, v - base))
            if nxt[0] - HOUR > t:
                line.append((nxt[0] - HOUR, v - base))
        out.append(dict(key=key, label=label, t0=start, t1=last_t, change=last_v - base, line=line, notes=notes,
                        recorded=snaps[-1][0], opened=first_t))
    return out


CSS = """
.pnl { background: var(--surface); border: 1px solid var(--rule); border-radius: 10px; padding: 20px 20px 16px; display: grid; gap: 14px; min-width: 0; }
.pnl-head { display: flex; flex-wrap: wrap; justify-content: space-between; align-items: start; gap: 10px 24px; }
.pnl h2 { margin: 0 0 2px; }
.pnl-v { font-family: var(--mono); font-size: 28px; font-weight: 500; letter-spacing: -.02em; }
.pnl-k { font-size: 13px; color: var(--muted); margin-left: 10px; }
.pnl-tabs { display: flex; gap: 2px; }
.pnl-tabs button { font: 600 12.5px var(--mono); color: var(--muted); background: none; border: 0; border-radius: 6px; padding: 5px 10px; cursor: pointer; }
.pnl-tabs button:hover { color: var(--ink); background: var(--surface-2); }
.pnl-tabs button:focus-visible { outline: 2px solid var(--accent); outline-offset: 1px; }
.pnl-tabs button[aria-selected="true"] { color: var(--accent); background: color-mix(in srgb, var(--accent) 12%, transparent); }
.pnl-plot { position: relative; min-width: 0; }
.pnl-plot svg { display: block; width: 100%; overflow: visible; cursor: crosshair; touch-action: pan-y; }
.pnl-plot .grid { stroke: var(--rule); stroke-width: 1; }
.pnl-plot .zero { stroke: var(--faint); stroke-width: 1; }
.pnl-plot .tick { font: 11px var(--mono); fill: var(--muted); font-variant-numeric: tabular-nums; }
.pnl-plot .area { fill: var(--accent); opacity: .1; }
.pnl-plot .line { fill: none; stroke: var(--accent); stroke-width: 2; stroke-linejoin: round; stroke-linecap: round; }
.pnl-plot .end { fill: var(--accent); stroke: var(--surface); stroke-width: 2; }
.pnl-plot .band { fill: var(--accent); opacity: 0; transition: opacity .12s; }
.pnl-plot .band.on { opacity: .1; }
.pnl-plot .leader { stroke: var(--faint); stroke-width: 1; }
.pnl-plot .badge { cursor: default; outline: none; }
.pnl-plot .badge circle { fill: var(--surface); stroke: var(--ink); stroke-width: 1.5; }
.pnl-plot .badge text { font: 600 11px var(--mono); fill: var(--ink); text-anchor: middle; }
.pnl-plot .badge.on circle, .pnl-plot .badge:focus-visible circle { fill: var(--ink); }
.pnl-plot .badge.on text, .pnl-plot .badge:focus-visible text { fill: var(--surface); }
.pnl-plot .callout text { paint-order: stroke; stroke: var(--surface); stroke-width: 3px; stroke-linejoin: round; }
.pnl-plot .callout .amt { font: 600 12px var(--mono); }
.pnl-plot .callout .amt.up { fill: var(--up); } .pnl-plot .callout .amt.down { fill: var(--down); }
.pnl-plot .callout .who { font: 11.5px var(--body); fill: var(--muted); }
.pnl-plot .cross { stroke: var(--muted); stroke-width: 1; }
.pnl-plot .dot { fill: var(--surface); stroke: var(--accent); stroke-width: 2; }
.pnl-plot .hover { visibility: hidden; }
.pnl-plot.on .hover { visibility: visible; }
.pnl-tip { position: absolute; z-index: 5; pointer-events: none; transform: translate(-50%, calc(-100% - 12px)); background: var(--surface); color: var(--ink); border: 1px solid var(--rule); border-radius: 6px; padding: 4px 8px; font: 12px/1.4 var(--mono); font-variant-numeric: tabular-nums; white-space: nowrap; box-shadow: 0 2px 8px rgb(0 0 0 / .12); }
.pnl-tip .t { color: var(--muted); margin-right: 8px; }
.pnl-tip b { font-weight: 600; }
ol.pnl-notes { list-style: none; margin: 0; padding: 0; display: grid; grid-template-columns: repeat(auto-fit, minmax(min(100%, 400px), 1fr)); gap: 0 28px; }
.pnl-notes li { display: grid; grid-template-columns: 22px minmax(0, 1fr); gap: 6px 10px; align-content: start; padding: 12px 8px; border-top: 1px solid var(--rule); }
.pnl-notes li.on { background: var(--surface-2); }
.pnl-notes li.pnl-none { display: block; color: var(--muted); font-size: 14px; }
.pnl-n { width: 20px; height: 20px; border-radius: 50%; border: 1.5px solid var(--ink); font: 600 11px/17px var(--mono); text-align: center; margin-top: 1px; }
.pnl-what { display: flex; flex-wrap: wrap; align-items: baseline; gap: 2px 10px; }
.pnl-what b { font: 600 15px var(--mono); font-variant-numeric: tabular-nums; }
.pnl-when { font-size: 12.5px; color: var(--muted); }
.pnl-why { grid-column: 2; display: grid; gap: 5px; font-size: 14px; min-width: 0; }
.pnl-drv { display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 0 14px; align-items: baseline; }
.pnl-drv a { font-weight: 500; }
.pnl-drv .side { margin-left: 2px; }
.pnl-num { text-align: right; white-space: nowrap; }
.pnl-num b { font: 600 13.5px var(--mono); font-variant-numeric: tabular-nums; }
.pnl-px, .pnl-rest { font: 12.5px var(--mono); color: var(--muted); font-variant-numeric: tabular-nums; }
.pnl-px { display: block; }
@media (max-width: 820px) {
  .pnl { padding: 16px 14px 12px; }
}
@media (max-width: 560px) {
  .pnl-drv { grid-template-columns: minmax(0, 1fr); }
  .pnl-num { text-align: left; }
  .pnl-px { display: inline; margin-left: 8px; }
}
"""

JS = r"""
(() => {
  const root = document.getElementById('pnl');
  if (!root) return;
  const periods = JSON.parse(document.getElementById('pnl-data').textContent);
  const byKey = Object.fromEntries(periods.map(p => [p.key, p]));
  for (const p of periods) if (p.same) Object.assign(p, byKey[p.same], { key: p.key, label: p.label });

  const NS = 'http://www.w3.org/2000/svg';
  const el = (tag, attrs, parent, text) => {
    const e = document.createElementNS(NS, tag);
    for (const k in attrs) e.setAttribute(k, attrs[k]);
    if (text != null) e.textContent = text;
    if (parent) parent.appendChild(e);
    return e;
  };
  const h = (tag, cls, parent, text) => {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    if (parent) parent.appendChild(e);
    return e;
  };
  const dtf = new Intl.DateTimeFormat('en-GB', { timeZone: 'Europe/Copenhagen', weekday: 'short', day: 'numeric', month: 'short', year: 'numeric', hour: '2-digit', minute: '2-digit', hourCycle: 'h23' });
  const parts = t => {
    const o = {};
    for (const { type, value } of dtf.formatToParts(t * 1000)) o[type] = value;
    return { wd: o.weekday, d: +o.day, mon: o.month, y: +o.year, h: +o.hour % 24, hm: `${o.hour}:${o.minute}` };
  };
  const tone = x => Math.abs(x) < 0.005 ? 'flat' : x > 0 ? 'up' : 'down';
  const usd = (x, digits = 2) => {
    const s = '$' + Math.abs(x).toLocaleString('en-US', { minimumFractionDigits: digits, maximumFractionDigits: digits });
    return Math.abs(x) < 0.005 ? s : (x > 0 ? '+' : '−') + s;
  };
  const range = (p, t0, t1) => {
    const a = parts(t0), b = parts(t1);
    if (p.t1 - p.t0 > 45 * 86400) return `${a.d} ${a.mon} – ${b.d} ${b.mon}`;
    if (a.d === b.d && a.mon === b.mon) return `${a.wd} ${a.d} ${a.mon}, ${a.hm}–${b.hm}`;
    return `${a.wd} ${a.d} ${a.mon} ${a.hm} – ${b.wd} ${b.d} ${b.mon} ${b.hm}`;
  };
  // Callout name: the question without "Will", its deadline ("in 2026", "by December 31, 2026") or the question mark.
  const WHEN = /\s+(?:in|by|before|after|until|through|on)\s+(?:the\s+)?(?:end\s+of\s+)?(?:(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|June?|July?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)(?:\s+\d{1,2}(?:st|nd|rd|th)?)?(?:,?\s+\d{4})?|\d{4})$/i;
  const brief = (title, max = 30) => {
    let s = title.trim().replace(/\?$/, '').replace(/^Will\s+(?:the\s+)?/i, '');
    while (WHEN.test(s)) s = s.replace(WHEN, '');
    s = s ? s.charAt(0).toUpperCase() + s.slice(1) : title;
    if (s.length <= max) return s;
    s = s.slice(0, max + 1);
    if (s.lastIndexOf(' ') >= 10) s = s.slice(0, s.lastIndexOf(' '));  // cut between words
    return s.replace(/(?:[\s,;:·-]+(?:a|an|and|as|at|by|for|his|her|in|of|on|or|the|to))*[\s,;:·-]*$/i, '') + '…';
  };
  const niceStep = raw => {
    const k = Math.pow(10, Math.floor(Math.log10(raw))), f = raw / k;
    return (f <= 1 ? 1 : f <= 2 ? 2 : f <= 2.5 ? 2.5 : f <= 5 ? 5 : 10) * k;
  };

  // Time ticks on the Copenhagen clock: hours for a day or two, days for a week, Mondays for a month, months beyond.
  function timeTicks(t0, t1, room) {
    let ticks = rawTicks(t0, t1, room);
    const each = Math.max(...ticks.map(k => k[1].length), 1) * 6.6 + 14;  // 11px mono label plus a gap
    while (ticks.length > 1 && ticks.length * each > room) ticks = ticks.filter((_, i) => i % 2 === 0);
    return ticks;
  }
  function rawTicks(t0, t1, room) {
    const span = t1 - t0, out = [];
    if (span <= 2.2 * 86400) {
      const step = room < 420 ? 6 : 3;
      for (let t = Math.ceil(t0 / 3600) * 3600; t <= t1; t += 3600) {
        const p = parts(t);
        if (p.h % step === 0) out.push([t, p.h === 0 ? `${p.wd} ${p.d}` : p.hm]);
      }
      return out;
    }
    let t = Math.ceil(t0 / 3600) * 3600;
    while (parts(t).h !== 0) t += 3600;
    const days = [];
    while (t <= t1) {
      days.push([t, parts(t)]);
      t += 86400;
      const hr = parts(t).h;  // daylight-saving change: stay on local midnight
      if (hr === 23) t += 3600; else if (hr === 1) t -= 3600;
    }
    if (span <= 10 * 86400) return days.map(([t, p]) => [t, `${p.wd} ${p.d}`]);
    if (span <= 62 * 86400) return days.filter(([, p]) => p.wd === 'Mon').map(([t, p]) => [t, `${p.d} ${p.mon}`]);
    return days.filter(([, p]) => p.d === 1).map(([t, p]) => [t, p.mon === 'Jan' ? `${p.y}` : p.mon]);
  }

  // Smooth line through the points without overshooting them (monotone cubic, as on polymarket.com).
  function smooth(pts) {
    const n = pts.length;
    if (n < 3) return 'M' + pts.map(p => p[0].toFixed(1) + ',' + p[1].toFixed(1)).join('L');
    const d = [], m = new Array(n);
    for (let i = 0; i < n - 1; i++) d.push((pts[i + 1][1] - pts[i][1]) / Math.max(pts[i + 1][0] - pts[i][0], 1e-6));
    m[0] = d[0]; m[n - 1] = d[n - 2];
    for (let i = 1; i < n - 1; i++) m[i] = d[i - 1] * d[i] <= 0 ? 0 : (d[i - 1] + d[i]) / 2;
    for (let i = 0; i < n - 1; i++) {
      if (d[i] === 0) { m[i] = m[i + 1] = 0; continue; }
      const a = m[i] / d[i], b = m[i + 1] / d[i], s = a * a + b * b;
      if (s > 9) { const k = 3 / Math.sqrt(s); m[i] = k * a * d[i]; m[i + 1] = k * b * d[i]; }
    }
    let path = `M${pts[0][0].toFixed(1)},${pts[0][1].toFixed(1)}`;
    for (let i = 0; i < n - 1; i++) {
      const w = (pts[i + 1][0] - pts[i][0]) / 3;
      path += `C${(pts[i][0] + w).toFixed(1)},${(pts[i][1] + m[i] * w).toFixed(1)} ${(pts[i + 1][0] - w).toFixed(1)},${(pts[i + 1][1] - m[i + 1] * w).toFixed(1)} ${pts[i + 1][0].toFixed(1)},${pts[i + 1][1].toFixed(1)}`;
    }
    return path;
  }

  const fig = root.querySelector('.pnl-v'), lab = root.querySelector('.pnl-k');
  const tabs = [...root.querySelectorAll('.pnl-tabs button')];
  const plot = root.querySelector('.pnl-plot'), svg = plot.querySelector('svg'), tip = plot.querySelector('.pnl-tip');
  const list = root.querySelector('.pnl-notes');
  let cur = '1D', geom = null, lit = [];

  function light(i, on) {
    for (const e of lit[i] || []) e.classList.toggle('on', on);
  }

  function drawChart(p) {
    const W = Math.max(plot.clientWidth, 280), H = W < 560 ? 220 : 290;
    const m = { l: 58, r: 14, t: 8, b: 26 };
    svg.setAttribute('viewBox', `0 0 ${W} ${H}`);
    svg.setAttribute('height', H);
    svg.setAttribute('aria-label', `Profit and loss, ${p.label.toLowerCase()}: ${usd(p.change)}`);
    svg.replaceChildren();
    const vs = p.line.map(q => q[1]);
    let lo = Math.min(0, ...vs), hi = Math.max(0, ...vs);
    if (hi - lo < 1) hi = lo + 1;
    const y0 = lo - (hi - lo) * 0.08, y1 = hi + (hi - lo) * (W < 560 ? 0.1 : 0.24);  // headroom for the callouts
    const x = t => m.l + (t - p.t0) / Math.max(p.t1 - p.t0, 1) * (W - m.l - m.r);
    const y = v => m.t + (y1 - v) / (y1 - y0) * (H - m.t - m.b);

    const step = niceStep((y1 - y0) / 5);
    for (let v = Math.ceil(y0 / step) * step; v <= y1; v += step) {
      const zero = Math.abs(v) < step / 1e6;
      el('line', { x1: m.l, x2: W - m.r, y1: y(v), y2: y(v), class: zero ? 'zero' : 'grid' }, svg);
      el('text', { x: m.l - 8, y: y(v) + 4, 'text-anchor': 'end', class: 'tick' }, svg,
        zero ? '$0' : (v > 0 ? '+$' : '−$') + Math.abs(v).toLocaleString('en-US', { maximumFractionDigits: 2 }));
    }
    for (const [t, label] of timeTicks(p.t0, p.t1, W - m.l - m.r)) {
      const tx = x(t);
      el('text', { x: tx, y: H - 8, 'text-anchor': tx < m.l + 18 ? 'start' : tx > W - m.r - 18 ? 'end' : 'middle', class: 'tick' }, svg, label);
    }

    const pts = p.line.map(([t, v]) => [x(t), y(v)]);
    const d = smooth(pts), last = pts[pts.length - 1];
    el('path', { d: `${d}L${last[0].toFixed(1)},${y(0).toFixed(1)}L${pts[0][0].toFixed(1)},${y(0).toFixed(1)}Z`, class: 'area' }, svg);
    const bands = p.notes.map(n => el('rect', { x: x(n.t0), y: m.t, width: Math.max(x(n.t1) - x(n.t0), 2), height: H - m.t - m.b, class: 'band' }, svg));
    el('path', { d, class: 'line' }, svg);
    el('circle', { cx: last[0], cy: last[1], r: 4, class: 'end' }, svg);

    // Height of the drawn line over a stretch of x, so a callout can sit clear of it.
    const lineSpan = (xa, xb) => {
      let mn = Infinity, mx = -Infinity;
      const see = v => { mn = Math.min(mn, v); mx = Math.max(mx, v); };
      for (let i = 0; i < pts.length; i++) {
        const [px, py] = pts[i];
        if (px >= xa && px <= xb) see(py);
        if (i && pts[i - 1][0] < xb && px > xa) {  // segment crossing an edge of the stretch
          const [qx, qy] = pts[i - 1];
          for (const e of [xa, xb]) if (e > qx && e < px) see(qy + (py - qy) * (e - qx) / (px - qx));
        }
      }
      return [mn, mx];
    };

    const lineAt = px => {
      for (let i = 1; i < pts.length; i++) if (pts[i][0] >= px) {
        const [qx, qy] = pts[i - 1], [rx, ry] = pts[i];
        return qy + (ry - qy) * (px - qx) / Math.max(rx - qx, 1e-6);
      }
      return last[1];
    };
    // Badges sit on the line where their swing passes halfway; one that would cover an earlier badge slides along the line.
    const R = 9, spots = [];
    for (const n of p.notes) {
      let ax = x(n.at), ay = y(n.v);
      for (let k = 0; k < 12 && spots.some(s => Math.hypot(s[0] - ax, s[1] - ay) < 2 * R + 3); k++) {
        ax = Math.min(ax + 4, W - m.r); ay = lineAt(ax);
      }
      spots.push([ax, ay]);
    }
    const taken = spots.map(([ax, ay]) => ({ x0: ax - R, y0: ay - R, x1: ax + R, y1: ay + R }));
    const hits = b => taken.some(o => b.x0 < o.x1 + 5 && b.x1 > o.x0 - 5 && b.y0 < o.y1 + 4 && b.y1 > o.y0 - 4);
    const leaders = el('g', {}, svg), badges = el('g', {}, svg), callouts = el('g', {}, svg);
    lit = p.notes.map((n, i) => {
      const [ax, ay] = spots[i];
      const g = el('g', { class: 'badge', tabindex: 0, role: 'img', 'aria-label': `Swing ${i + 1}: ${usd(n.d)}, ${range(p, n.t0, n.t1)}` }, badges);
      el('circle', { cx: ax, cy: ay, r: R }, g);
      el('text', { x: ax, y: ay + 4 }, g, i + 1);
      g.addEventListener('pointerenter', () => light(i, true));
      g.addEventListener('pointerleave', () => light(i, false));
      g.addEventListener('focus', () => light(i, true));
      g.addEventListener('blur', () => light(i, false));
      return [g, bands[i]];
    });
    if (W >= 560) p.notes.forEach((n, i) => {
      // Callout: the swing's size and its biggest driver, at the free spot nearest the badge.
      const [ax, ay] = spots[i];
      const g = el('g', { class: 'callout' }, callouts);
      const amt = el('text', { class: 'amt ' + tone(n.d) }, g, usd(n.d, 0));
      const who = n.drivers.length ? el('text', { class: 'who' }, g, brief(n.drivers[0].title)) : null;
      const w = Math.max(amt.getComputedTextLength(), who ? who.getComputedTextLength() : 0), ht = who ? 30 : 15;
      let best = null;
      for (const bx of [ax - 14 - w, ax + 14, ax - w / 2, ax - 14 - w * 0.75, ax + 14 - w * 0.25]) {
        const [mn, mx] = lineSpan(bx - 4, bx + w + 4);
        for (const by of [ay - ht / 2, ay - 16 - ht, ay + 16, mn - 7 - ht, mx + 7, y(0) - 5 - ht, y(0) + 5]) {
          if (by + ht > mn - 7 && by < mx + 7) continue;  // would sit on the line
          if (by + ht > y(0) - 4 && by < y(0) + 4) continue;  // ... or on the zero line
          const b = { x0: bx, y0: by, x1: bx + w, y1: by + ht };
          if (b.x0 < m.l + 2 || b.x1 > W - m.r - 2 || b.y0 < m.t || b.y1 > H - m.b - 2 || hits(b)) continue;
          const cx = Math.min(Math.max(ax, b.x0), b.x1), cy = Math.min(Math.max(ay, b.y0), b.y1);
          const dist = Math.hypot(cx - ax, cy - ay);
          if (dist <= 110 && (!best || dist < best.dist)) best = { ...b, cx, cy, dist };
        }
      }
      if (!best) { g.remove(); return; }
      taken.push(best);
      amt.setAttribute('x', best.x0); amt.setAttribute('y', best.y0 + 11);
      if (who) { who.setAttribute('x', best.x0); who.setAttribute('y', best.y0 + 26); }
      if (best.dist > R + 6) {
        const k = (R + 2) / best.dist, e = 1 - 3 / best.dist;
        el('line', { x1: ax + (best.cx - ax) * k, y1: ay + (best.cy - ay) * k, x2: ax + (best.cx - ax) * e, y2: ay + (best.cy - ay) * e, class: 'leader' }, leaders);
      }
    });

    const hover = el('g', { class: 'hover' }, svg);
    const cross = el('line', { y1: m.t, y2: H - m.b, class: 'cross' }, hover), dot = el('circle', { r: 4, class: 'dot' }, hover);
    geom = { p, x, y, m, W, cross, dot };
  }

  function drawNotes(p) {
    list.replaceChildren();
    if (!p.notes.length) { h('li', 'pnl-none', list, 'No swing stands out in this period.'); return; }
    p.notes.forEach((n, i) => {
      const li = h('li', '', list);
      h('span', 'pnl-n', li, i + 1);
      const what = h('div', 'pnl-what', li);
      h('b', tone(n.d), what, usd(n.d));
      h('span', 'pnl-when', what, range(p, n.t0, n.t1));
      const why = h('div', 'pnl-why', li);
      for (const r of n.drivers) {
        const row = h('div', 'pnl-drv', why), name = h('div', '', row), num = h('div', 'pnl-num', row);
        const a = h(r.url ? 'a' : 'span', '', name, r.title);
        if (r.url) a.href = r.url;
        name.append(' ');  // keeps the side chip on the title's last line
        h('span', 'side', name, r.side);
        h('b', tone(r.d), num, usd(r.d));
        h('span', 'pnl-px', num, r.move);
      }
      if (n.rest != null && n.drivers.length) h('div', 'pnl-rest', why, `Rest of the book ${usd(n.rest)}`);
      if (!n.drivers.length) h('div', 'pnl-rest', why, n.rest == null ? 'Positions behind it could not be rebuilt today.' : 'Spread across the book; no single position stands out.');
      li.addEventListener('pointerenter', () => light(i, true));
      li.addEventListener('pointerleave', () => light(i, false));
      lit[i].push(li);
    });
  }

  function render() {
    const p = byKey[cur];
    fig.textContent = usd(p.change);
    fig.className = 'pnl-v ' + tone(p.change);
    lab.textContent = p.label;
    tabs.forEach(b => { const on = b.dataset.k === cur; b.setAttribute('aria-selected', on); b.tabIndex = on ? 0 : -1; });
    plot.classList.remove('on'); tip.hidden = true;
    drawChart(p);
    drawNotes(p);
  }

  // Crosshair: snaps to the hour and reads the value the line holds there.
  function point(e) {
    const { p, x, y, m, W, cross, dot } = geom, r = svg.getBoundingClientRect();
    const px = (e.clientX - r.left) / r.width * W;
    let t = p.t0 + (px - m.l) / (W - m.l - m.r) * (p.t1 - p.t0);
    t = Math.min(Math.max(Math.round(t / 3600) * 3600, p.t0), p.t1);
    let v = p.line[0][1];
    for (const q of p.line) { if (q[0] <= t) v = q[1]; else break; }
    const cx = x(t), cy = y(v), c = parts(t);
    cross.setAttribute('x1', cx); cross.setAttribute('x2', cx);
    dot.setAttribute('cx', cx); dot.setAttribute('cy', cy);
    tip.replaceChildren();
    h('span', 't', tip, `${c.wd} ${c.d} ${c.mon} ${c.hm}`);
    h('b', tone(v), tip, usd(v));
    tip.style.left = Math.min(Math.max(cx, 70), W - 70) / W * r.width + 'px';
    tip.style.top = cy / svg.viewBox.baseVal.height * r.height + 'px';
    tip.hidden = false;
    plot.classList.add('on');
  }
  svg.addEventListener('pointermove', point);
  svg.addEventListener('pointerdown', point);
  svg.addEventListener('pointerleave', () => { plot.classList.remove('on'); tip.hidden = true; });

  tabs.forEach((b, i) => {
    b.addEventListener('click', () => { cur = b.dataset.k; render(); });
    b.addEventListener('keydown', e => {
      const step = e.key === 'ArrowRight' ? 1 : e.key === 'ArrowLeft' ? -1 : 0;
      if (!step) return;
      const next = tabs[(i + step + tabs.length) % tabs.length];
      next.focus(); next.click();
    });
  });
  let width = 0;
  new ResizeObserver(() => { if (plot.clientWidth !== width) { width = plot.clientWidth; render(); } }).observe(plot);
  if (document.fonts && document.fonts.ready) document.fonts.ready.then(render);  // callouts are measured in the page's fonts
})();
"""


def section(get, pages, wallet, now):
    """The Profit/Loss card: period tabs, the annotated chart and the list of swings under it."""
    return render(build(get, pages, wallet, now))


def render(periods):
    whole = periods[-1]
    data = []
    for p in periods:
        if p is not whole and (p['t0'], p['change']) == (whole['t0'], whole['change']):
            data.append(dict(key=p['key'], label=p['label'], same=whole['key']))  # the wallet is younger than this window
            continue
        data.append(dict(key=p['key'], label=p['label'], t0=p['t0'], t1=p['t1'], change=round(p['change'], 2),
                         line=[[t, round(v, 2)] for t, v in p['line']],
                         notes=[dict(t0=n['t0'], t1=n['t1'], at=n['at'], v=round(n['v'], 2), d=round(n['d'], 2),
                                     rest=None if n['rest'] is None else round(n['rest'], 2),
                                     drivers=[dict(r, d=round(r['d'], 2)) for r in n['drivers']]) for n in p['notes']]))
    when = lambda t, f='%a %d %b %H:%M': f'{datetime.datetime.fromtimestamp(t):{f}}'
    for p in periods:
        print(f"  pnl {p['key']:3} {p['change']:+,.2f}" + ''.join(
            f"; {n['d']:+,.0f} {when(n['t0'], '%d %b %H:%M')}–{when(n['t1'], '%d %b %H:%M')}"
            + (f" ({n['drivers'][0]['title'][:38]} {n['drivers'][0]['side']} {n['drivers'][0]['d']:+,.0f})" if n['drivers'] else '')
            for n in (p['notes'] if 'same' not in data[periods.index(p)] else [])))
    same = [d['key'] for d in data if 'same' in d]
    opened = datetime.datetime.fromtimestamp(whole['opened'])
    note = ('The line is Polymarket\'s own profit/loss figure, before fees, last recorded '
            f'{when(whole["recorded"])}. Polymarket records a new value on some hours only, so the line runs flat in between. '
            'The numbers mark each period\'s largest swings. The positions listed under a swing are rebuilt from your fills '
            'and Polymarket\'s price history, so they can add up to a few dollars more or less than the swing.')
    if same:
        note += (f' {" and ".join(same)} match ALL because the wallet\'s first trade was on '
                 f'{opened.day} {opened:%b %Y}.')
    tabs = ''.join(f'<button type="button" role="tab" data-k="{p["key"]}" aria-selected="{"true" if p["key"] == "1D" else "false"}">'
                   f'{p["key"]}</button>' for p in periods)
    return (f'<section class="pnl" id="pnl" aria-label="Profit and loss">'
            f'<div class="pnl-head"><div><h2>Profit/Loss</h2><span class="pnl-v"></span><span class="pnl-k"></span></div>'
            f'<div class="pnl-tabs" role="tablist" aria-label="Period">{tabs}</div></div>'
            '<div class="pnl-plot"><svg role="img"></svg><div class="pnl-tip" hidden></div></div>'
            '<ol class="pnl-notes"></ol>'
            f'<p class="wx-note">{html.escape(note)}</p>'
            f'<script type="application/json" id="pnl-data">{json.dumps(data, separators=(",", ":")).replace("<", chr(92) + "u003c")}</script>'
            f'<style>{CSS}</style><script>{JS}</script></section>')


if __name__ == '__main__':  # print the swings, to check them against the site
    import os, urllib.request
    os.environ['TZ'] = 'Europe/Copenhagen'; time.tzset()

    def get(url, timeout=30):
        return json.load(urllib.request.urlopen(urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'}), timeout=timeout))

    def pages(url):
        rows, cursor = [], None
        while True:
            d = get(url + (f'&cursor={urllib.parse.quote(cursor)}' if cursor else ''))
            rows += d['data']
            cursor = d['pagination'].get('next_cursor')
            if not cursor:
                return rows

    when = lambda t: f'{datetime.datetime.fromtimestamp(t):%a %d %b %H:%M}'
    for p in build(get, pages, '0x9d4a9ff98bdce01ff8081362f676749db0c85887', int(time.time())):
        print(f"{p['key']:4} {when(p['t0'])} to {when(p['t1'])}  {p['change']:+,.2f}  ({len(p['line'])} line points)")
        for n in p['notes']:
            print(f"   {when(n['t0'])} to {when(n['t1'])}  {n['d']:+9.2f}  rebuilt {n['rebuilt'] or 0:+9.2f}")
            for r in n['drivers']:
                print(f"        {r['d']:+9.2f}  {r['title'][:60]} {r['side']}  {r['move']}")
