# Morning brief

Daily dashboard for the Polymarket wallet `0x9d4a9ff98bdce01ff8081362f676749db0c85887` (mdorup),
plus a Copenhagen weather line, a Saturn observing forecast for the east balcony, and the mispricing hunt.
Live at https://morningbrief.dorup.dk (Vercel project `nothing-ever-happens-book`, team `oxean-x`).

- `python3 refresh.py` fetches Polymarket positions/activity/prices and Open-Meteo forecasts, then writes
  `portfolio.html` and `site/index.html`. It uses only the standard library. The page is the summary, the
  Profit/Loss card, the positions table, the Mispricing hunt (if `hunt/hunt.py build` ran today, its section from
  `hunt/work/section.html`), the weather and positions closed in the last 24h, with a link to the run log at the foot.
  A menu between the header and the summary (`top_menu()`) has a tab per section, the hunt's parts among them (from
  `hunt/work/nav.json`), and the hunt's Refresh prices button. It sticks to the top of the window, and `NAV_JS` lights
  the tab of the section being read. Every part of the page shares one column, up to 1304px wide.
  Trades and the biggest movers are printed (`trade:` and `mover:` lines) for the routine's summary, not shown.
- `pnl.py` builds the Profit/Loss card under the summary: the line polymarket.com plots (`trade_pnl` from
  `/v2/user-pnl`) for 1D, 1W, 1M, 1Y, YTD and ALL, with each period's largest swings numbered on the chart and
  listed below with the positions that drove them. A swing ends where the line reverses (`SWING`) or holds flat for a
  while (`QUIET`), so a day that climbs in steps gets a note per step. The drivers are rebuilt from the wallet's fills and `/v2/prices-history`,
  the marks the P/L snapshots use. `python3 pnl.py` prints the swings; if the API fails, the brief builds without the card.
  The card's Refresh button redoes `build()` in the browser (a JavaScript copy in `pnl.py`'s `JS`, fed the same
  constants, rules and labels through `#pnl-cfg`), so a change to the Python build needs the same change there.
  The chart's callouts use the short names in `labels.json` (market question without its deadline → name, e.g.
  "Supreme Court vacancy"). A market missing from it gets a rule-made name and a `pnl: callout ... was made by rule`
  line in the output; add an entry to fix a clumsy one.
- `research.json` holds "Why it moved" notes, keyed by asset id. The script prints `research MISSING: ...` for
  each position that moved at least 2¢ or $10 without a note for the current 24h window.
- `python3 deploy.py` posts everything under `site/` to the Vercel API as a production deployment
  (`--preview` for a preview). It sends no token: the cloud environment's "Vercel API Token" credential adds the
  Authorization header for api.vercel.com. To test locally, pipe `python3 deploy.py --preview --body-only` into
  `vercel api "/v13/deployments?teamId=team_45ZNkd6F5HMXNUu6jGjrKr44" -X POST --input -`.
- A daily Claude Code cloud routine (environment `morningbrief`) runs the script, researches the missing movers,
  updates the hunt's book, reruns the script and deploys. It then runs the hunt's full review (below), which takes
  a few hours, and builds and deploys again, so a slow or failed review never holds up the brief.

## Run log (`runlog.py`, `/log/`)

Each routine run is logged and published at https://morningbrief.dorup.dk/log/ (linked at the foot of the brief).
For every stage it records who ran it (a script, the main agent or research subagents), the model and effort level,
start time, time spent, exit code, counts and notes, plus the start and end of a script's output. For every subagent
it records model, effort, time, tokens, tool uses, web searches and the markets it wrote. The account's weekly and
5-hour Claude plan usage is read at the start, at every stage boundary and at the end, so the page shows how much of
the weekly plan was left at each point and what each stage used.

    python3 runlog.py start --model claude-opus-5-5 --effort xhigh
    python3 runlog.py run refresh "Refresh brief" -- python3 refresh.py    # times a script; output passes through
    python3 runlog.py begin research "Why it moved research"             # a stage the agent does itself ...
    python3 runlog.py end research --metric missing=15 --note "..."       # ... and its end
    python3 runlog.py jobs review --model claude-opus-5-5 --effort high < lines   # one line per subagent
    python3 runlog.py note|metric|finish|usage ...

- Data: `site/log/index.json` (one summary per run, last 180) and `site/log/runs/<id>.json` (full log, last 30); the
  page `site/log/index.html` is copied from `runlog.html` and renders them in the browser. Like the hunt's book, the
  history lives in the deployment: `start` pulls the live log back before the run adds to it.
- Plan usage: cloud sessions sign Claude Code in through the platform and have no token a script can use, so the
  `morningbrief` environment keeps one as the secret variable `RUNLOG_OAUTH_TOKEN`, a long-lived token from
  `claude setup-token` (valid for a year; renew it before it runs out). That kind of token may only run the model, so
  each reading makes one tiny Haiku request through Claude Code with it and takes the weekly (and, when reported,
  5-hour) utilization from Claude Code's rate-limit report. Locally, a full sign-in (`CLAUDE_CODE_OAUTH_TOKEN` or
  `~/.claude/.credentials.json`) reads the usage endpoint instead. Without either, each reading records why and the
  page says usage was unavailable. `RUNLOG_USAGE_FILE=<json>` feeds fixed readings for testing. Readings within 30
  seconds of each other are shared.
- The effort level is read from the session's `CLAUDE_EFFORT` variable when it is set, else from `--effort`; the model
  comes from `--model`, so keep those flags in the routine prompt in step with the routine's settings.
- While a run is going, the routine also redeploys after each group of 10 review subagents, and the page re-reads the
  log every minute, so /log/ follows the run with a lag of minutes rather than hours.
- Logging never stops a run: each command catches its own errors and `run` always runs (and exits with) the command.
  Working files go to `.runlog/` (ignored).

## Mispricing hunt (`hunt/`)

A book of Polymarket picks, mostly "nothing ever happens" NO bets on politics and geopolitics, each with a fair
value and a one-line reason. Every morning it is repriced against the live order books and published as the brief's
Mispricing hunt section (https://morningbrief.dorup.dk/#hunt). It was a page of its own at `/hunt/` until 12 Nov 2026;
that address now forwards to the section.
Return is fair ÷ ask − 1 held to resolution, plus Polymarket's 3.25% APR holding reward on markets that pay it
(Gamma flag `holdingRewardsEnabled`; those rows get a gold badge). The book is ranked by that return as a yearly rate,
return × 365 / days (simple, with days counted as at least 14), so a small edge that resolves in weeks beats a big one
that takes years. The main list is 30% a year and up, the appendix 10–30%. Picks added today open the main list
whatever their rate (slower ones included, sorted by rate among themselves) and fall into place the next day.

    python3 hunt/hunt.py pull     # book from the live site's /hunt/state.json, else hunt/seed.json
    python3 hunt/hunt.py scan     # reprice, sweep ~64 tags; prints RESOLVED/REVIEW lines, writes hunt/work/candidates.txt
                                  # (every candidate event, new ones marked) and the full review's batches
    python3 hunt/hunt.py peek <event>   # a candidate event: markets, prices, rules
    python3 hunt/hunt.py set|drop|add|note ...   # judgement: fair values, reasons, new picks, "what changed" lines
    python3 hunt/hunt.py ingest   # full review: read the research subagents' fair values, print MISSING/FLAG/BOOK DIFF
    python3 hunt/hunt.py smart    # smart-money share (top-20 holders scored by leaderboard PnL)
    python3 hunt/hunt.py build    # hunt/work/section.html (refresh.py puts it on the brief), site/hunt/*.json

### Full review

Every candidate market is researched every day, not only new events: about 2,200 markets in about 970 events
(7 Nov 2026). A candidate is a live market with a spread of 15¢ or less, a YES price of 5–95¢ and 4–900 days to go,
in an event under one of the swept tags that isn't sports, crypto or noise (`candidate_markets`, `NOISE`). Count
words ("how many", "number of") are noise in a market's question but not in its event's title, so a ladder like "How
many Starship launches reach space in 2026?" stays in: its "less than 5" rung is a nothing-ever-happens bet.

1. `scan` splits them into `hunt/work/batches/bNNN.json` (15 events or 30 markets each, nearest end first) and lists
   them in `hunt/work/batches/index.json`. Each market carries its rules, prices, its last review and, if it is in the
   book, the book's side and fair value. Events tagged `recurring` also carry `history`: how the last 4 resolved events
   in their Gamma series went (Starship flights 11–14, past RBNZ decisions, last year's "before 2026" question), as the
   reviewer's base rate.
2. The routine gives each batch to a research subagent (Opus 5.5 at high effort, 10 at a time). It reads the rules, searches the
   news (since the last review, if there is one) and writes `hunt/work/reviews/bNNN.json`:
   `[{"market": "<id>", "fair": <probability of YES>, "why": "<dated reason>", "settles": "<YYYY-MM-DD>"}, ...]`.
   `settles` is optional: the day the market will most likely be decided when that is well before its end date (a
   Starship flight's booster market ends 1 Jul 2027 but is decided at launch).
3. `ingest` reads them into the book's `reviews` (kept 30 days, so the next day's review can look only at what's new)
   and prints `MISSING` batches to rerun, `FLAG` lines for markets outside the book where either side clears the main
   list's bars at today's prices (30% a year and +3% to resolution, and at least 5¢ off the mid), and `BOOK DIFF` where a review disagrees with a
   pick's fair value by 5¢ or more. A FLAG with a `settles` date is annualized to that date and its `add` command
   carries `--end <date>`, so the pick is ranked the same way. The routine checks each FLAG itself before it `add`s it
   (`add <event> <market id>`).

The hunt section's stamp shows how many candidate markets were researched that day.

Claude Code caps WebSearch at 200 calls by default, shared by the main agent and every subagent, which the review
outgrew (on 9 Nov 2026 the subagents ran out partway and later batches kept the previous day's values).
`.claude/settings.json` raises the cap to 4,000 with `CLAUDE_CODE_MAX_WEB_SEARCHES_PER_SESSION`. The run log shows each
subagent's searches, so check there whether the cap still binds.

State lives in the deployment, not in git: `build` writes `site/hunt/state.json` (picks, fair values, notes, seen
event ids, the full review's last fair value per market) and `site/hunt/wallets.json` (leaderboard PnL cache), `deploy.py` publishes them, and the next `pull`
reads them back. A failed run therefore leaves the last good book in place. `hunt/seed.json` is the 30 Oct 2026
book, used only when the live copy is missing. Working files go to `hunt/work/` (ignored).

To change fair values outside the routine (for example after a deep dive), add entries to `hunt/updates.json`
(`id`, `at` timestamp, `fair`, `why`). The next `pull` applies each one once, to the pick whose last review is older.

The wallet's open positions are part of the book. `scan` marks picks the wallet holds and adds any held market that
is missing, without a fair value (`FAIR NEEDED` lines; value them with `set`). The page's "Your open positions" table
shows each one's "hold vs sell": fair ÷ best bid − 1 plus the reward, i.e. what holding to resolution should earn over
selling into the bid now, and the $ edge (shares × (fair + reward − bid)). Held positions are never auto-dropped and
leave the book once sold, unless they are also hunt picks. The brief shows the same figure as a chip on each position.

Every row has a Rec column (`advice()`). A held market (in the positions table, or a held hunt pick) gets **Sell** when
the bid is at or above fair plus the reward, **Reduce** when what is left over selling into the bid is under +3% or 10%
a year (the bars a pick needs to stay in the book), **Expand** when buying more at the ask would make the main list, and
**Keep** otherwise. Any other pick gets **Bigger/Smaller/Same edge**: its return to resolution against the last run.
The hunt section's **Refresh prices** button refetches every order book, updates both the best bid and the best ask on each
row (the other side sits under the row's price) and redoes the returns and Rec at those prices, with Bigger/Smaller
edge then measured against the build. The button's JavaScript (`REFRESH_JS`) repeats `econ()`, `hold_econ()` and
`advice()`, so a rule change needs the same change there.

Build drops picks that fell below 10% a year or +3% to resolution and moves resolved markets to "Resolved".

The section's styles are nested under `#hunt` (CSS nesting) so they stay out of the rest of the brief, and its first
rules undo the brief's own table and list rules inside it. Its anchors are `hunt-` prefixed (`#hunt-positions`,
`#hunt-main`, ...); the brief's "fair · hold vs sell" chips link to `#hunt-positions`. `build` also writes
`hunt/work/nav.json`: the section's tabs (label, anchor, count) and its Refresh prices button, which refresh.py puts in
the page's top menu, which also holds the button's styles (its script stays in the section). The brief's column
(up to 1304px) is wide enough for the hunt's tables; below 1040px they
turn into one card per row, and in between they scroll inside their boxes.
