#!/usr/bin/env python3
"""Rebuild the Polymarket portfolio dashboard (Nothing Ever Happens Book).

Fetches live positions and the last 24h of wallet activity, computes a
trade-adjusted 24h change per position, and writes portfolio/portfolio.html.
"""
import datetime, html, json, os, time, urllib.request
from collections import defaultdict

os.environ['TZ'] = 'Europe/Copenhagen'  # cloud runners are on UTC; all times on the page are Copenhagen
time.tzset()

WALLET = '0x9d4a9ff98bdce01ff8081362f676749db0c85887'
HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, 'portfolio.html')
RESEARCH = os.path.join(HERE, 'research.json')  # "why it moved" notes, keyed by asset id
MAX_MOVE = 3.0  # cents; full width of the 24h bar
SIG_MOVE = 2.0  # cents; a position needs research when its price moves this much ...
SIG_USD = 10.0  # ... or its 24h $ change is at least this
UA = {'User-Agent': 'Mozilla/5.0'}


def get(url):
    req = urllib.request.Request(url, headers=UA)
    return json.load(urllib.request.urlopen(req, timeout=30))


NOW = int(time.time())
T0 = NOW - 86400
SNAP = datetime.datetime.fromtimestamp(NOW)
WIN = datetime.datetime.fromtimestamp(T0)
TODAY = SNAP.date()


def price_24h_ago(asset):
    """(price at T0, last price, 24h series) from CLOB history; Nones when the market has no recent history."""
    h = get(f'https://clob.polymarket.com/prices-history?market={asset}'
            f'&startTs={T0 - 7200}&endTs={NOW}&fidelity=10').get('history', [])
    if not h:
        return None, None, []
    before = [x for x in h if x['t'] <= T0]
    start = before[-1] if before else h[0]
    series = [(T0, start['p'])] + [(x['t'], x['p']) for x in h if x['t'] > T0]
    return start['p'], h[-1]['p'], series


# ---- fetch ---------------------------------------------------------------
positions = get(f'https://data-api.polymarket.com/positions?user={WALLET}&limit=500&sizeThreshold=0.1')
activity = [a for a in get(f'https://data-api.polymarket.com/activity?user={WALLET}&limit=500')
            if a['timestamp'] >= T0]

flows = defaultdict(lambda: dict(buy_sh=0.0, buy_cash=0.0, sell_sh=0.0, sell_cash=0.0))
meta = {}
for a in activity:
    if a['type'] == 'TRADE':
        f = flows[a['asset']]
        if a['side'] == 'BUY':
            f['buy_sh'] += a['size']; f['buy_cash'] += a['usdcSize']
        else:
            f['sell_sh'] += a['size']; f['sell_cash'] += a['usdcSize']
        meta.setdefault(a['asset'], a)
    elif a['type'] == 'REDEEM' and a.get('conditionId'):
        # Redeeming pays the winning outcome; treat it as a sale at the payout.
        try:
            m = get(f"https://gamma-api.polymarket.com/markets?condition_ids={a['conditionId']}&closed=true")[0]
            prices = [float(x) for x in json.loads(m['outcomePrices'])]
            tokens = json.loads(m['clobTokenIds'])
            idx = prices.index(max(prices))
            asset = tokens[idx]
            f = flows[asset]
            f['sell_sh'] += a['size']; f['sell_cash'] += a['usdcSize']; f['redeemed'] = True
            meta.setdefault(asset, dict(a, outcome=json.loads(m['outcomes'])[idx]))
        except Exception:
            pass

rows = []
held = set()
for p in positions:
    held.add(p['asset'])
    p24, _, series = price_24h_ago(p['asset'])
    rows.append(dict(asset=p['asset'], title=p['title'], outcome=p['outcome'], event=p['eventSlug'],
                     end=p['endDate'], size=p['size'], avg=p['avgPrice'], now=p['curPrice'], p24=p24,
                     traded=p['initialValue'], towin=p['size'], value=p['currentValue'],
                     pnl=p['cashPnl'], pct=p['percentPnl'], redeemable=p['redeemable'], closed=False,
                     series=series))
for asset, a in meta.items():
    if asset in held:
        continue
    p24, last, series = price_24h_ago(asset)
    rows.append(dict(asset=asset, title=a['title'], outcome=a['outcome'], event=a.get('eventSlug'),
                     end=None, size=0, avg=None, now=last if last is not None else 0, p24=p24,
                     traded=0, towin=0, value=0, pnl=0, pct=0, redeemable=False, closed=True,
                     series=series))

for r in rows:
    f = flows.get(r['asset'], dict(buy_sh=0, buy_cash=0, sell_sh=0, sell_cash=0))
    r['flows'] = f
    r['sh24'] = r['size'] - f['buy_sh'] + f['sell_sh']
    base = r['p24'] if r['p24'] is not None else r['now']
    # value now - value 24h ago - cash in + cash out (fees are inside usdcSize)
    r['d24'] = r['size'] * r['now'] - r['sh24'] * base - f['buy_cash'] + f['sell_cash']
    r['move'] = (r['now'] - base) * 100
    r['sig'] = abs(r['move']) >= SIG_MOVE or abs(r['d24']) >= SIG_USD

rows.sort(key=lambda r: -r['value'])
open_rows = [r for r in rows if not r['redeemable'] and not r['closed']]
closed = [r for r in rows if r['closed']]
resolved = [r for r in rows if r['redeemable']]

try:
    with open(RESEARCH) as fh:
        research = json.load(fh)
except FileNotFoundError:
    research = {}


# ---- formatting ----------------------------------------------------------
def cents(p):
    return f"{p*100:.2f}".rstrip('0').rstrip('.') + '¢'


def usd(x, sign=False):
    s = f"${abs(x):,.2f}"
    if not sign:
        return s
    if round(x, 2) == 0:
        return '$0.00'
    return ('+' if x > 0 else '−') + s


def tone(x):
    x = round(x, 2)
    return 'up' if x > 0 else 'down' if x < 0 else 'flat'


def fmt_date(d):
    return f"{d.day} {d.strftime('%b')} {d.year}"


def end_chip(end):
    if not end or end.startswith('1970'):
        return ''
    d = datetime.date.fromisoformat(end[:10])
    if d.month == 1 and d.day == 1:  # Polymarket lists "by 31 Dec" markets as ending 1 Jan
        d -= datetime.timedelta(days=1)
    if d < TODAY:
        return f'<span class="chip-warn">ended {fmt_date(d)} · awaiting resolution</span>'
    if d == TODAY:
        return '<span class="chip-warn">ends today</span>'
    return f'<span>ends {fmt_date(d)}</span>'


def bar(dc):
    frac = max(-1, min(1, dc / MAX_MOVE))
    w = abs(frac) * 50
    pos = f"left:50%;width:{w:.1f}%" if frac >= 0 else f"right:50%;width:{w:.1f}%"
    return f'<span class="rail" aria-hidden="true"><span class="fill {tone(dc)}" style="{pos}"></span></span>'


def spark(series, now, p24):
    """24h price line as inline SVG; dashed line marks the price 24h ago."""
    pts = series + [(NOW, now)] if series else [(T0, now), (NOW, now)]
    W, H, pad = 112, 30, 3
    ps = [p for _, p in pts]
    lo, hi = min(ps), max(ps)
    if hi - lo < 0.01:  # keep flat-ish lines from filling the box
        mid = (hi + lo) / 2
        lo, hi = mid - 0.005, mid + 0.005
    x = lambda t: (t - T0) / (NOW - T0) * W
    y = lambda p: pad + (hi - p) / (hi - lo) * (H - 2 * pad)
    line = ' '.join(f"{x(t):.1f},{y(p):.1f}" for t, p in pts)
    b = y(p24 if p24 is not None else now)
    return (f'<svg class="spark {tone(now - (p24 if p24 is not None else now))}" viewBox="0 0 {W} {H}" '
            f'width="{W}" height="{H}" role="img" aria-label="24h price, {cents(lo)} to {cents(hi)}">'
            f'<line x1="0" x2="{W}" y1="{b:.1f}" y2="{b:.1f}" class="base"/>'
            f'<polyline points="{line}"/><circle cx="{x(NOW):.1f}" cy="{y(now):.1f}" r="2"/></svg>')


def why(r):
    """Research row for a significant mover, from research.json when it covers this window."""
    n = research.get(r['asset'])
    if not n or n.get('as_of', '') < f"{WIN:%Y-%m-%d}":
        return ''
    src = ' · '.join(f'<a href="{html.escape(s["url"])}">{html.escape(s["label"])}</a>' for s in n.get('sources', []))
    return (f'<tr class="why"><td colspan="4"><div class="why-box"><span class="why-k">Why it moved</span>'
            f'<p>{html.escape(n["summary"])}</p>{f"<div class=why-src>{src}</div>" if src else ""}</div></td></tr>')


def side(outcome):
    return f'<span class="side">{html.escape(outcome)}</span>'


def short(title):
    t = title[5:] if title.startswith('Will ') else title
    return t.rstrip('?')


tot_value = sum(r['value'] for r in open_rows)
tot_cost = sum(r['traded'] for r in open_rows)
tot_pnl = sum(r['pnl'] for r in open_rows)
tot_win = sum(r['towin'] for r in open_rows)
tot_d24 = sum(r['d24'] for r in open_rows)
closed_d24 = sum(r['d24'] for r in closed) + sum(r['d24'] for r in resolved)
d24_all = tot_d24 + closed_d24
redeem_value = sum(r['value'] for r in resolved)

trs = []
for r in open_rows:
    f = r['flows']
    notes = []
    if f['buy_sh']: notes.append(f"bought {f['buy_sh']:,.0f} in last 24h")
    if f['sell_sh']: notes.append(f"sold {f['sell_sh']:,.0f} in last 24h")
    dc = r['move']
    move_lbl = f"{'+' if dc > 0.05 else '−' if dc < -0.05 else '±'}{abs(dc):.1f}¢"
    p24 = r['p24'] if r['p24'] is not None else r['now']
    link = f"https://polymarket.com/event/{html.escape(r['event'])}" if r['event'] else '#'
    trs.append(f'''
<tr>
  <td class="mkt"><a href="{link}">{html.escape(r['title'])}</a>
    <div class="meta">{side(r['outcome'])}<span>{usd(r['traded'])} traded</span>{end_chip(r['end'])}</div></td>
  <td class="num">{cents(r['avg'])}<span class="arrow">→</span><b>{cents(r['now'])}</b><div class="sub {tone(r['pnl'])}">{usd(r['pnl'], True)} ({r['pct']:+.1f}%)</div></td>
  <td class="chart">{spark(r['series'], r['now'], r['p24'])}</td>
  <td class="d24">
    <div class="d24-top"><b class="{tone(r['d24'])}">{usd(r['d24'], True)}</b>{bar(dc)}</div>
    <div class="sub">{cents(p24)} → {cents(r['now'])} <span class="{tone(dc)}">{move_lbl}</span></div>
    {''.join(f'<div class="note">{n}</div>' for n in notes)}
  </td>
</tr>{why(r) if r['sig'] else ''}''')

movers = sorted([r for r in open_rows + closed if round(r['d24'], 2) != 0], key=lambda r: -abs(r['d24']))[:3]


def closed_item(r):
    f = r['flows']
    verb = 'Redeemed' if f.get('redeemed') else 'Sold'
    parts = [f"{verb} {f['sell_sh']:,.1f} shares for {usd(f['sell_cash'])} after fees."]
    if f['buy_sh']:
        parts.insert(0, f"Bought {f['buy_sh']:,.1f} for {usd(f['buy_cash'])}.")
    if r['p24'] is not None:
        parts.append(f"Price 24h ago was {cents(r['p24'])}, so this "
                     f"{'added' if r['d24'] >= 0 else 'cost'} <b class=\"{tone(r['d24'])}\">{usd(r['d24'], True)}</b> for the day.")
    return (f'<li><span class="li-title">{html.escape(r["title"])} {side(r["outcome"])}</span>'
            f'<span class="li-body">{" ".join(parts)}</span></li>')


# ---- weather: Saturn nights from the east balcony + today in Copenhagen -----
# Method from the ASICAP sessions: cloud per model over the hours Saturn is above ~25°,
# and seeing ranked by 925 hPa wind (sharp night 09-26: 4-6 m/s, blurry 09-27: 16-19 m/s).
LAT, LON = 55.69, 12.56
MODELS = [('ecmwf_ifs025', 'ECMWF'), ('icon_seamless', 'ICON'), ('gfs_seamless', 'GFS'),
          ('dmi_seamless', 'DMI'), ('metno_seamless', 'MetNo')]
LOCAL_ONLY_DAYS = 3  # DMI and MetNo repeat ECMWF after ~2.5 days, so only count them for the first nights
# (evening date, roof clearance, passes az 195°), minutes after that evening's midnight
SATURN = [(datetime.date(2026, 9, 16), 1271, 1625), (datetime.date(2026, 10, 4), 1201, 1550),
          (datetime.date(2026, 11, 1), 1031, 1373), (datetime.date(2026, 12, 1), 988, 1251),
          (datetime.date(2027, 1, 20), 1022, 1058)]
WMO = {0: 'clear', 1: 'mostly clear', 2: 'partly cloudy', 3: 'overcast', 45: 'fog', 48: 'fog',
       51: 'drizzle', 53: 'drizzle', 55: 'drizzle', 61: 'light rain', 63: 'rain', 65: 'heavy rain',
       80: 'showers', 81: 'showers', 82: 'heavy showers', 95: 'thunderstorms', 96: 'thunderstorms', 99: 'thunderstorms'}


def saturn_window(d):
    """(start, end) when Saturn is above ~25° and still inside the east balcony's clear arc, or None."""
    for (d0, c0, e0), (d1, c1, e1) in zip(SATURN, SATURN[1:]):
        if d0 <= d <= d1:
            f = (d - d0).days / (d1 - d0).days
            start = c0 + (c1 - c0) * f + 120  # ~2 h after clearing the roof
            end = e0 + (e1 - e0) * f
            if start >= end:
                return None
            midnight = datetime.datetime.combine(d, datetime.time())
            return midnight + datetime.timedelta(minutes=start), midnight + datetime.timedelta(minutes=end)
    return None


def fetch_weather():
    base = (f'https://api.open-meteo.com/v1/forecast?latitude={LAT}&longitude={LON}'
            '&timezone=Europe/Copenhagen&wind_speed_unit=ms')
    hourly = get(base + '&forecast_days=8&models=' + ','.join(m for m, _ in MODELS) +
                 '&hourly=cloud_cover,wind_speed_925hPa,temperature_2m,dew_point_2m')['hourly']
    today = get(base + '&forecast_days=1&daily=weather_code,temperature_2m_max,temperature_2m_min,'
                'precipitation_sum,precipitation_probability_max,wind_speed_10m_max,sunset')['daily']
    idx = {t: i for i, t in enumerate(hourly['time'])}
    nights = []
    for n in range(7):
        d = TODAY + datetime.timedelta(days=n)
        w = saturn_window(d)
        if not w or w[1] <= SNAP:
            continue
        hours, h = [], w[0].replace(minute=0)
        while h < w[1]:
            if f'{h:%Y-%m-%dT%H:00}' in idx:
                hours.append(idx[f'{h:%Y-%m-%dT%H:00}'])
            h += datetime.timedelta(hours=1)
        if not hours:
            continue
        use = [(m, lbl) for m, lbl in MODELS if n < LOCAL_ONLY_DAYS or m not in ('dmi_seamless', 'metno_seamless')]
        mean = lambda k: sum(hourly[k][i] for i in hours) / len(hours)
        cloud = {lbl: mean(f'cloud_cover_{m}') for m, lbl in use}
        wind = sorted(mean(f'wind_speed_925hPa_{m}') for m, _ in use)
        spread = sorted(min(hourly[f'temperature_2m_{m}'][i] - hourly[f'dew_point_2m_{m}'][i] for i in hours)
                        for m, _ in use)
        cl = sorted(cloud.values())
        nights.append(dict(date=d, start=w[0], end=w[1], cloud=cloud, cloud_med=cl[len(cl) // 2],
                           wind=wind[len(wind) // 2], wind_lo=wind[0], wind_hi=wind[-1],
                           dew=spread[len(spread) // 2]))
    return nights, today


def verdict(n):
    if n['cloud_med'] <= 30:
        if n['wind'] <= 7:
            return 'up', 'Go: clear, steady air'
        if n['wind'] <= 12:
            return 'flat', 'Clear, fair seeing'
        return 'flat', 'Clear, poor seeing'
    if n['cloud_med'] <= 60:
        return 'flat', 'Maybe'
    return 'down', 'Out'


def weather_section():
    try:
        nights, today = fetch_weather()
    except Exception as e:  # the portfolio must still build when the forecast is down
        print(f"  weather: skipped ({e})")
        return ''
    day = (f"{WMO.get(today['weather_code'][0], 'mixed')}, "
           f"{today['temperature_2m_min'][0]:.0f}–{today['temperature_2m_max'][0]:.0f} °C, "
           f"rain {today['precipitation_sum'][0]:.1f} mm ({today['precipitation_probability_max'][0]}%), "
           f"wind up to {today['wind_speed_10m_max'][0]:.0f} m/s · sunset {today['sunset'][0][11:]}")
    print(f"  weather today: {day}")
    rows = []
    for n in nights:
        t, label = verdict(n)
        cells = ''
        for _, lbl in MODELS:
            v = n['cloud'].get(lbl)
            cells += (f'<td class="num cc" style="--c:{min(v, 100):.0f}">{v:.0f}%</td>' if v is not None
                      else '<td class="num faint" title="Repeats ECMWF this far out">–</td>')
        dew = ' <span class="chip-warn">dew</span>' if n['dew'] <= 2 else ''
        rows.append(f'<tr><td class="mkt"><b>{n["date"]:%a} {n["date"].day} {n["date"]:%b}</b>'
                    f'<div class="sub">{n["start"]:%H:%M}–{n["end"]:%H:%M}</div></td>{cells}'
                    f'<td class="num"><b>{n["wind"]:.0f} m/s</b><div class="sub">{n["wind_lo"]:.0f}–{n["wind_hi"]:.0f}</div></td>'
                    f'<td><span class="verdict {t}">{label}</span>{dew}</td></tr>')
        print(f"  saturn night {n['date']:%a %d %b} {n['start']:%H:%M}-{n['end']:%H:%M}: "
              f"cloud median {n['cloud_med']:.0f}% {dict((k, round(v)) for k, v in n['cloud'].items())} "
              f"925hPa {n['wind']:.0f} m/s, dew spread {n['dew']:.1f} °C -> {label}")
    heads = ''.join(f'<th>{lbl}</th>' for _, lbl in MODELS)
    if rows:
        table = ('<div class="table-box"><table class="wx"><thead><tr><th>Night · Saturn &gt;25°</th>'
                 f'{heads}<th>Wind 925 hPa</th><th style="text-align:left">Verdict</th></tr></thead>'
                 f'<tbody>{"".join(rows)}</tbody></table></div>')
    else:
        table = '<p class="wx-note">Saturn is outside the east balcony window this week.</p>'
    return ('<section class="weather"><h2>Weather</h2>'
            f'<p class="wx-today"><b>Copenhagen today:</b> {html.escape(day)}</p>'
            f'<h3>Saturn nights from the east balcony</h3>{table}'
            '<p class="wx-note">Cloud is each model\'s average over the hours Saturn is above ~25° and inside the '
            'balcony\'s clear arc. Wind at 925 hPa (~800 m up) sets the seeing: 4–6 m/s gave the sharp 26 Sep night, '
            '16–19 m/s the blurry 27 Sep one. "Dew" means the air comes within 2 °C of its dew point. '
            'DMI and MetNo repeat ECMWF after about 2.5 days, so they are left out beyond that. Forecasts: Open-Meteo.</p>'
            '</section>')


weather_html = weather_section()

lower = []
if closed:
    lower.append(f'<section><h2>Closed in the last 24h</h2><ul>{"".join(closed_item(r) for r in closed)}</ul></section>')

value_sub = f"{len(open_rows)} open positions"
if redeem_value > 0.005:
    value_sub += f" · +{usd(redeem_value)} to redeem"

page = f'''<title>Nothing Ever Happens Book</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Bricolage+Grotesque:opsz,wght@12..96,600;12..96,700&family=IBM+Plex+Mono:wght@400;500;600&family=Schibsted+Grotesk:wght@400;500;600&display=swap">
<style>
:root {{
  --ground: #F3F5F9;
  --surface: #FFFFFF;
  --surface-2: #EEF1F7;
  --ink: #141A26;
  --muted: #5D6679;
  --faint: #8A93A6;
  --rule: #DCE1EA;
  --accent: #2F4BCF;
  --up: #177A47;
  --down: #BD3434;
  --warn-bg: #FFF1D6;
  --warn-ink: #8A5300;
  --rail: #E3E7EF;
  --display: "Bricolage Grotesque", "Schibsted Grotesk", system-ui, sans-serif;
  --body: "Schibsted Grotesk", system-ui, -apple-system, "Segoe UI", sans-serif;
  --mono: "IBM Plex Mono", ui-monospace, "SF Mono", Menlo, monospace;
}}
@media (prefers-color-scheme: dark) {{
  :root:not([data-theme="light"]) {{
    --ground: #0D111A; --surface: #141A25; --surface-2: #1A2130; --ink: #E7EAF1; --muted: #9AA3B5;
    --faint: #6E7789; --rule: #262E3D; --accent: #8CA0FF; --up: #4FC98C; --down: #F27A7A;
    --warn-bg: #3A2A0B; --warn-ink: #F5C165; --rail: #232B3A;
  }}
}}
:root[data-theme="dark"] {{
  --ground: #0D111A; --surface: #141A25; --surface-2: #1A2130; --ink: #E7EAF1; --muted: #9AA3B5;
  --faint: #6E7789; --rule: #262E3D; --accent: #8CA0FF; --up: #4FC98C; --down: #F27A7A;
  --warn-bg: #3A2A0B; --warn-ink: #F5C165; --rail: #232B3A;
}}
* {{ box-sizing: border-box; }}
body {{ background: var(--ground); color: var(--ink); font-family: var(--body); font-size: 15px; line-height: 1.5; }}
.wrap {{ max-width: 1180px; margin: 0 auto; padding: 40px 28px 64px; display: grid; gap: 32px; }}
a {{ color: inherit; text-decoration: none; }}
a:hover {{ color: var(--accent); }}
a:focus-visible {{ outline: 2px solid var(--accent); outline-offset: 2px; border-radius: 2px; }}
.up {{ color: var(--up); }} .down {{ color: var(--down); }} .flat {{ color: var(--muted); }}

header {{ display: flex; flex-wrap: wrap; justify-content: space-between; align-items: end; gap: 12px 32px; }}
.eyebrow {{ font-size: 12px; letter-spacing: .08em; text-transform: uppercase; color: var(--muted); font-weight: 600; }}
h1 {{ font-family: var(--display); font-weight: 700; font-size: clamp(30px, 4vw, 42px); line-height: 1.05; letter-spacing: -.02em; margin: 6px 0 0; text-wrap: balance; }}
.stamp {{ font-family: var(--mono); font-size: 12.5px; color: var(--muted); text-align: right; line-height: 1.6; }}

.summary {{ display: grid; grid-template-columns: repeat(5, minmax(0, 1fr)); border-top: 2px solid var(--ink); border-bottom: 1px solid var(--rule); }}
.stat {{ padding: 16px 20px 18px 0; display: grid; gap: 4px; align-content: start; }}
.stat + .stat {{ padding-left: 20px; border-left: 1px solid var(--rule); }}
.stat .k {{ font-size: 12px; letter-spacing: .06em; text-transform: uppercase; color: var(--muted); font-weight: 600; }}
.stat .v {{ font-family: var(--mono); font-size: 24px; font-weight: 500; letter-spacing: -.02em; font-variant-numeric: tabular-nums; }}
.stat .s {{ font-family: var(--mono); font-size: 12.5px; color: var(--muted); }}
.stat.hot {{ background: var(--surface-2); padding-left: 20px; }}

.movers {{ display: flex; flex-wrap: wrap; gap: 8px 28px; font-size: 14px; color: var(--muted); }}
.movers b {{ font-family: var(--mono); font-weight: 500; }}
.movers span.lbl {{ color: var(--ink); font-weight: 600; }}

.table-box {{ background: var(--surface); border: 1px solid var(--rule); border-radius: 10px; overflow-x: auto; }}
table {{ width: 100%; border-collapse: collapse; min-width: 760px; }}
th {{ text-align: right; font-size: 12px; letter-spacing: .06em; text-transform: uppercase; color: var(--muted); font-weight: 600; padding: 14px 16px 10px; border-bottom: 1px solid var(--rule); white-space: nowrap; }}
th:first-child {{ text-align: left; padding-left: 20px; }}
th.d24h {{ text-align: left; color: var(--accent); }}
td {{ padding: 14px 16px; border-bottom: 1px solid var(--rule); vertical-align: top; }}
tr:last-child td {{ border-bottom: 0; }}
tbody tr:hover td {{ background: var(--surface-2); }}
td.mkt {{ padding-left: 20px; max-width: 340px; }}
td.mkt a {{ font-weight: 500; line-height: 1.35; display: block; }}
.meta {{ display: flex; flex-wrap: wrap; align-items: center; gap: 4px 10px; margin-top: 6px; font-size: 12.5px; color: var(--muted); }}
.side {{ font-family: var(--mono); font-size: 11.5px; font-weight: 600; padding: 1px 7px; border-radius: 4px; border: 1px solid var(--rule); color: var(--ink); background: var(--surface-2); }}
.chip-warn {{ background: var(--warn-bg); color: var(--warn-ink); padding: 1px 7px; border-radius: 4px; font-weight: 600; }}
td.num {{ text-align: right; font-family: var(--mono); font-size: 14px; font-variant-numeric: tabular-nums; white-space: nowrap; color: var(--muted); }}
td.num b {{ color: var(--ink); font-weight: 500; }}
.arrow {{ color: var(--faint); margin: 0 5px; }}
.sub {{ font-family: var(--mono); font-size: 12.5px; margin-top: 3px; color: var(--muted); font-variant-numeric: tabular-nums; white-space: nowrap; }}
td.d24 {{ background: color-mix(in srgb, var(--accent) 4%, transparent); min-width: 210px; }}
.d24-top {{ display: flex; align-items: center; gap: 12px; font-family: var(--mono); font-size: 14px; font-variant-numeric: tabular-nums; }}
.d24-top b {{ font-weight: 600; min-width: 72px; }}
.rail {{ position: relative; width: 72px; height: 8px; background: var(--rail); border-radius: 2px; flex: none; }}
.rail::after {{ content: ""; position: absolute; left: 50%; top: -3px; bottom: -3px; width: 1px; background: var(--faint); }}
.fill {{ position: absolute; top: 0; bottom: 0; border-radius: 1px; }}
.fill.up {{ background: var(--up); }} .fill.down {{ background: var(--down); }}
td.chart {{ width: 140px; }}
.spark {{ display: block; overflow: visible; }}
.spark polyline {{ fill: none; stroke: currentColor; stroke-width: 1.6; stroke-linejoin: round; stroke-linecap: round; }}
.spark circle {{ fill: currentColor; }}
.spark .base {{ stroke: var(--faint); stroke-width: 1; stroke-dasharray: 2 3; }}
.spark.flat {{ color: var(--faint); }}
tr.why td {{ padding: 0 16px 16px 20px; }}
tr:has(+ tr.why) td {{ border-bottom: 0; }}
tbody tr.why:hover td {{ background: transparent; }}
.why-box {{ border-left: 3px solid var(--accent); background: var(--surface-2); border-radius: 0 6px 6px 0; padding: 10px 14px; display: grid; gap: 4px; max-width: 90ch; }}
.why-k {{ font-size: 11.5px; letter-spacing: .06em; text-transform: uppercase; color: var(--accent); font-weight: 600; }}
.why-box p {{ margin: 0; font-size: 14px; }}
.why-src {{ font-size: 12.5px; color: var(--muted); }}
.why-src a {{ text-decoration: underline; text-underline-offset: 2px; }}
.note {{ display: inline-block; margin-top: 6px; font-size: 12px; color: var(--accent); font-weight: 500; }}
tfoot td {{ border-top: 2px solid var(--ink); border-bottom: 0; font-weight: 600; background: var(--surface-2); }}
tfoot td.num {{ color: var(--ink); }}

.weather {{ display: grid; gap: 10px; }}
.weather h2 {{ margin: 0; }}
.weather h3 {{ font-size: 15px; font-weight: 600; margin: 8px 0 0; }}
.wx-today {{ margin: 0; font-size: 14.5px; }}
.wx-note {{ margin: 0; color: var(--muted); font-size: 13px; max-width: 90ch; }}
table.wx {{ min-width: 640px; }}
table.wx td {{ padding: 10px 14px; vertical-align: middle; }}
td.cc {{ background: color-mix(in srgb, var(--faint) calc(var(--c) * 0.35%), transparent); }}
td.faint {{ color: var(--faint); }}
.verdict {{ font-weight: 600; font-size: 13.5px; white-space: nowrap; }}
.lower {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); gap: 32px; }}
h2 {{ font-family: var(--display); font-size: 19px; font-weight: 600; letter-spacing: -.01em; margin: 0 0 10px; }}
ul {{ list-style: none; margin: 0; padding: 0; display: grid; gap: 12px; }}
li {{ display: grid; gap: 3px; padding-top: 12px; border-top: 1px solid var(--rule); }}
.li-title {{ font-weight: 500; }}
.li-body {{ color: var(--muted); font-size: 14px; max-width: 62ch; }}
.li-body b {{ font-family: var(--mono); font-weight: 600; }}
code {{ font-family: var(--mono); font-size: 12.5px; }}

@media (max-width: 820px) {{
  .summary {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
  .stat, .stat + .stat {{ padding: 14px 14px 14px 0; border-left: 0; border-bottom: 1px solid var(--rule); }}
  .stat.hot {{ padding-left: 14px; grid-column: 1 / -1; order: -1; }}
  .movers .lbl {{ flex-basis: 100%; }}
  .stamp {{ text-align: left; }}
}}
</style>

<div class="wrap">
  <header>
    <div>
      <div class="eyebrow">Polymarket positions · mdorup</div>
      <h1>Nothing Ever Happens Book</h1>
      <!--BRIEF-->
    </div>
    <div class="stamp">Snapshot {fmt_date(SNAP)}, {SNAP:%H:%M}<br>24h window from {WIN.day} {WIN:%b}, {WIN:%H:%M}<br>0x9d4A…5887</div>
  </header>

  <section class="summary" aria-label="Portfolio summary">
    <div class="stat"><span class="k">Value</span><span class="v">{usd(tot_value)}</span><span class="s">{value_sub}</span></div>
    <div class="stat"><span class="k">Traded</span><span class="v">{usd(tot_cost)}</span><span class="s">cost basis</span></div>
    <div class="stat"><span class="k">Unrealized P&amp;L</span><span class="v {tone(tot_pnl)}">{usd(tot_pnl, True)}</span><span class="s">{tot_pnl/tot_cost*100:+.2f}% on cost</span></div>
    <div class="stat"><span class="k">To win</span><span class="v">{usd(tot_win)}</span><span class="s">if every position pays $1</span></div>
    <div class="stat hot"><span class="k">24h change</span><span class="v {tone(d24_all)}">{usd(d24_all, True)}</span><span class="s">open {usd(tot_d24, True)} · closed {usd(closed_d24, True)}</span></div>
  </section>

  <div class="movers">
    <span class="lbl">Biggest 24h moves</span>
    {''.join(f'<span>{html.escape(short(r["title"]))} {r["outcome"]} <b class="{tone(r["d24"])}">{usd(r["d24"], True)}</b></span>' for r in movers)}
  </div>

  <div class="table-box">
    <table>
      <thead><tr><th>Market</th><th>Avg → Now</th><th style="text-align:left">24h chart</th><th class="d24h">Last 24h</th></tr></thead>
      <tbody>{''.join(trs)}</tbody>
      <tfoot><tr>
        <td class="mkt">Total, {len(open_rows)} open positions</td>
        <td class="num">{usd(tot_value)}<div class="sub {tone(tot_pnl)}">{usd(tot_pnl, True)}</div></td><td></td>
        <td class="d24"><div class="d24-top"><b class="{tone(tot_d24)}">{usd(tot_d24, True)}</b></div><div class="sub">open positions only</div></td>
      </tr></tfoot>
    </table>
  </div>

  {weather_html}

  {f'<div class="lower">{"".join(lower)}</div>' if lower else ''}

</div>
'''

with open(OUT, 'w') as fh:
    fh.write(page)

# "The Morning Report" button, Vercel site only (the artifact sandbox may block YouTube frames).
# Plays the first seconds of the licensed YouTube upload in YouTube's own player; nothing is hosted here.
BRIEF_VIDEO, BRIEF_END = 'J0CASZfnVS8', 20
BRIEF = f"""<div class="brief">
        <button type="button" id="brief-play">▶ The Morning Report</button>
        <div id="brief-player" hidden><div class="brief-frame"></div><button type="button" id="brief-close" aria-label="Close player">×</button></div>
      </div>
      <style>
      .brief {{ margin-top: 12px; display: grid; gap: 10px; justify-items: start; }}
      #brief-play {{ font: 600 13px var(--body); color: var(--accent); background: color-mix(in srgb, var(--accent) 10%, transparent); border: 1px solid color-mix(in srgb, var(--accent) 30%, transparent); border-radius: 999px; padding: 5px 14px; cursor: pointer; }}
      #brief-play:hover {{ background: color-mix(in srgb, var(--accent) 18%, transparent); }}
      #brief-player {{ position: relative; }}
      #brief-player iframe {{ display: block; width: 356px; max-width: calc(100vw - 32px); aspect-ratio: 356 / 200; border: 0; border-radius: 8px; }}
      #brief-close {{ position: absolute; top: -10px; right: -10px; width: 24px; height: 24px; border-radius: 50%; border: 1px solid var(--rule); background: var(--surface); color: var(--ink); cursor: pointer; line-height: 1; }}
      </style>
      <script>
      (() => {{
        const box = document.getElementById('brief-player'), frame = box.querySelector('.brief-frame');
        document.getElementById('brief-play').addEventListener('click', () => {{
          frame.innerHTML = '<iframe src="https://www.youtube-nocookie.com/embed/{BRIEF_VIDEO}?autoplay=1&start=0&end={BRIEF_END}&rel=0&playsinline=1" title="The Morning Report" allow="autoplay; encrypted-media"></iframe>';
          box.hidden = false;
        }});
        document.getElementById('brief-close').addEventListener('click', () => {{ frame.innerHTML = ''; box.hidden = true; }});
      }})();
      </script>"""

# Standalone copy for the Vercel site; the artifact adds this document skeleton itself.
os.makedirs(os.path.join(HERE, 'site'), exist_ok=True)
with open(os.path.join(HERE, 'site', 'index.html'), 'w') as fh:
    fh.write('<!doctype html>\n<html lang="en"><head><meta charset="utf-8">\n'
             '<meta name="viewport" content="width=device-width,initial-scale=1">\n'
             '<meta name="robots" content="noindex">\n<style>body { margin: 0; }</style>\n'
             f"{page.replace('<!--BRIEF-->', BRIEF)}\n</html>\n")

print(f"wrote {OUT}")
print(f"snapshot {SNAP:%Y-%m-%d %H:%M}  value {usd(tot_value)}  pnl {usd(tot_pnl, True)}  24h {usd(d24_all, True)} "
      f"(open {usd(tot_d24, True)}, closed {usd(closed_d24, True)})")
for r in resolved:
    verdict = f"won, redeem for {usd(r['value'])}" if r['now'] >= 0.5 else f"lost, cost {usd(r['traded'])}"
    print(f"  resolved: {r['title']} {r['outcome']} ({verdict}, 24h {usd(r['d24'], True)})")
for r in movers:
    print(f"  mover: {short(r['title'])} {r['outcome']} {usd(r['d24'], True)}")
for r in open_rows + closed:
    if r['sig']:
        state = 'ok' if research.get(r['asset'], {}).get('as_of', '') >= f"{WIN:%Y-%m-%d}" else 'MISSING'
        print(f"  research {state}: {r['asset']}  {r['title']} {r['outcome']} {r['move']:+.1f}¢ {usd(r['d24'], True)}")
for r in open_rows:
    print(f"  {r['title'][:50]:50} {r['outcome']:3} {cents(r['p24'] if r['p24'] is not None else r['now']):>7} -> {cents(r['now']):>7}  24h {usd(r['d24'], True):>9}  value {usd(r['value']):>9}")
