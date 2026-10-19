# Morning brief

Daily dashboard for the Polymarket wallet `0x9d4a9ff98bdce01ff8081362f676749db0c85887` (mdorup),
plus a Copenhagen weather line and a Saturn observing forecast for the east balcony.
Live at https://morningbrief.dorup.dk (Vercel project `nothing-ever-happens-book`, team `oxean-x`).

- `python3 refresh.py` fetches Polymarket positions/activity/prices and Open-Meteo forecasts, then writes
  `portfolio.html` and `site/index.html`. It uses only the standard library.
- `research.json` holds "Why it moved" notes, keyed by asset id. The script prints `research MISSING: ...` for
  each position that moved at least 2¢ or $10 without a note for the current 24h window.
- `python3 deploy.py` posts `site/index.html` to the Vercel API as a production deployment. It sends no token:
  the cloud environment's "Vercel API Token" credential adds the Authorization header for api.vercel.com.
  To test locally, pipe `python3 deploy.py --body-only` into `vercel api "/v13/deployments?teamId=team_45ZNkd6F5HMXNUu6jGjrKr44" -X POST --input -`.
- The page's **Refresh now** button calls `api/refresh.py`, a Vercel Python function shipped with every deployment
  (`bundle.py` lists the files). It reruns `refresh.py` with the deployed `research.json` and redeploys the site, so
  "Why it moved" notes are not researched for new movers. It refuses (429) while the newest deployment is under
  5 minutes old, which covers the daily routine too; the button counts down until then. It needs a Vercel token
  in the project's `VERCEL_TOKEN` environment variable (Production, Sensitive); without it the button stays hidden.
  `python3 deploy.py --preview` deploys a preview, whose refresh function only creates previews.
- A daily Claude Code cloud routine (environment `morningbrief`) runs the script, researches the missing movers,
  reruns it and deploys.
