# Morning brief

Daily dashboard for the Polymarket wallet `0x9d4a9ff98bdce01ff8081362f676749db0c85887` (mdorup),
plus a Copenhagen weather line, a Saturn observing forecast for the east balcony, and the mispricing hunt.
Live at https://morningbrief.dorup.dk (Vercel project `nothing-ever-happens-book`, team `oxean-x`).

- `python3 refresh.py` fetches Polymarket positions/activity/prices and Open-Meteo forecasts, then writes
  `portfolio.html` and `site/index.html`. It uses only the standard library. If `hunt/hunt.py build` ran today,
  the page also gets a short "Mispricing hunt" section linking to `/hunt/`.
- `research.json` holds "Why it moved" notes, keyed by asset id. The script prints `research MISSING: ...` for
  each position that moved at least 2¢ or $10 without a note for the current 24h window.
- `python3 deploy.py` posts everything under `site/` to the Vercel API as a production deployment
  (`--preview` for a preview). It sends no token: the cloud environment's "Vercel API Token" credential adds the
  Authorization header for api.vercel.com. To test locally, pipe `python3 deploy.py --preview --body-only` into
  `vercel api "/v13/deployments?teamId=team_45ZNkd6F5HMXNUu6jGjrKr44" -X POST --input -`.
- A daily Claude Code cloud routine (environment `morningbrief`) runs the script, researches the missing movers,
  updates the hunt's book, reruns the script and deploys. It then runs the hunt's full review (below), which takes
  a few hours, and builds and deploys again, so a slow or failed review never holds up the brief.

## Mispricing hunt (`hunt/`)

A book of Polymarket picks, mostly "nothing ever happens" NO bets on politics and geopolitics, each with a fair
value and a one-line reason. Every morning it is repriced against the live order books and published at `/hunt/`.
Return is fair ÷ ask − 1 held to resolution, plus Polymarket's 3.25% APR holding reward on markets that pay it
(Gamma flag `holdingRewardsEnabled`; those rows get a gold badge). The book is ranked by that return as a yearly rate,
return × 365 / days (simple, with days counted as at least 14), so a small edge that resolves in weeks beats a big one
that takes years. The main list is 30% a year and up, the appendix 10–30%.

    python3 hunt/hunt.py pull     # book from the live site's /hunt/state.json, else hunt/seed.json
    python3 hunt/hunt.py scan     # reprice, sweep ~64 tags; prints RESOLVED/REVIEW lines, writes hunt/work/candidates.txt
                                  # (every candidate event, new ones marked) and the full review's batches
    python3 hunt/hunt.py peek <event>   # a candidate event: markets, prices, rules
    python3 hunt/hunt.py set|drop|add|note ...   # judgement: fair values, reasons, new picks, "what changed" lines
    python3 hunt/hunt.py ingest   # full review: read the research subagents' fair values, print MISSING/FLAG/BOOK DIFF
    python3 hunt/hunt.py smart    # smart-money share (top-20 holders scored by leaderboard PnL)
    python3 hunt/hunt.py build    # site/hunt/{index.html,state.json,wallets.json,summary.json}

### Full review

Every candidate market is researched every day, not only new events: about 2,200 markets in about 970 events
(7 Nov 2026). A candidate is a live market with a spread of 15¢ or less, a YES price of 5–95¢ and 4–900 days to go,
in an event under one of the swept tags that isn't sports, crypto or noise (`candidate_markets`, `NOISE`).

1. `scan` splits them into `hunt/work/batches/bNNN.json` (15 events or 30 markets each, nearest end first) and lists
   them in `hunt/work/batches/index.json`. Each market carries its rules, prices, its last review and, if it is in the
   book, the book's side and fair value.
2. The routine gives each batch to a research subagent (Sonnet, several at a time). It reads the rules, searches the
   news (since the last review, if there is one) and writes `hunt/work/reviews/bNNN.json`:
   `[{"market": "<id>", "fair": <probability of YES>, "why": "<dated reason>"}, ...]`.
3. `ingest` reads them into the book's `reviews` (kept 30 days, so the next day's review can look only at what's new)
   and prints `MISSING` batches to rerun, `FLAG` lines for markets outside the book where either side clears the main
   list's bars at today's prices (30% a year and +3% to resolution, and at least 5¢ off the mid), and `BOOK DIFF` where a review disagrees with a
   pick's fair value by 5¢ or more. The routine checks each FLAG itself before it `add`s it (`add <event> <market id>`).

The page's stamp shows how many candidate markets were researched that day.

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

Build drops picks that fell below 10% a year or +3% to resolution and moves resolved markets to "Resolved". The page prints well as
A4 landscape (browser Print → PDF).
