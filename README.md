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
  runs the hunt, reruns the script and deploys.

## Mispricing hunt (`hunt/`)

A book of Polymarket picks, mostly "nothing ever happens" NO bets on politics and geopolitics, each with a fair
value and a one-line reason. Every morning it is repriced against the live order books and published at `/hunt/`.
Return is fair ÷ ask − 1 held to resolution, plus Polymarket's 3.25% APR holding reward on markets that pay it
(Gamma flag `holdingRewardsEnabled`; those rows get a gold badge).

    python3 hunt/hunt.py pull     # book from the live site's /hunt/state.json, else hunt/seed.json
    python3 hunt/hunt.py scan     # reprice, sweep ~60 politics/geo tags; prints RESOLVED/REVIEW lines, writes hunt/work/candidates.txt
    python3 hunt/hunt.py peek <event>   # a candidate event: markets, prices, rules
    python3 hunt/hunt.py set|drop|add|note ...   # judgement: fair values, reasons, new picks, "what changed" lines
    python3 hunt/hunt.py smart    # smart-money share (top-20 holders scored by leaderboard PnL)
    python3 hunt/hunt.py build    # site/hunt/{index.html,state.json,wallets.json,summary.json}

State lives in the deployment, not in git: `build` writes `site/hunt/state.json` (picks, fair values, notes, seen
event ids) and `site/hunt/wallets.json` (leaderboard PnL cache), `deploy.py` publishes them, and the next `pull`
reads them back. A failed run therefore leaves the last good book in place. `hunt/seed.json` is the 30 Oct 2026
book, used only when the live copy is missing. Working files go to `hunt/work/` (ignored).

The wallet's open positions are part of the book. `scan` marks picks the wallet holds and adds any held market that
is missing, without a fair value (`FAIR NEEDED` lines; value them with `set`). The page's "Your open positions" table
shows each one's "hold vs sell": fair ÷ best bid − 1 plus the reward, i.e. what holding to resolution should earn over
selling into the bid now, and the $ edge (shares × (fair + reward − bid)). Held positions are never auto-dropped and
leave the book once sold, unless they are also hunt picks. The brief shows the same figure as a chip on each position.

Build drops picks whose return fell below +5% and moves resolved markets to "Resolved". The page prints well as
A4 landscape (browser Print → PDF).
