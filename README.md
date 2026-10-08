# Morning brief

Daily dashboard for the Polymarket wallet `0x9d4a9ff98bdce01ff8081362f676749db0c85887` (mdorup),
plus a Copenhagen weather line and a Saturn observing forecast for the east balcony.
Live at https://morningbrief.dorup.dk (Vercel project `nothing-ever-happens-book`, team `oxean-x`).

- `python3 refresh.py` fetches Polymarket positions/activity/prices and Open-Meteo forecasts, then writes
  `portfolio.html` and `site/index.html`. It uses only the standard library.
- `research.json` holds "Why it moved" notes, keyed by asset id. The script prints `research MISSING: ...` for
  each position that moved at least 2¢ or $10 without a note for the current 24h window.
- `podcast.json` holds today's spoken script for the "Listen to today's brief" button:
  `{"as_of": "YYYY-MM-DD", "script": "..."}`, paragraphs separated by blank lines. It is written fresh each run
  (gitignored). `refresh.py` prints `podcast ok`, `podcast MISSING` (no script for today) or `podcast NO AUDIO`
  (script changed since the last recording) and shows the button only when it is `ok`.
- `python3 podcast.py` reads the script aloud with Piper (local neural TTS, voice `en_US-ryan-high`) and writes
  `site/brief.mp3`. A fresh container installs `piper-tts` with pip and downloads the voice from Hugging Face
  (about 120 MB, ~30 s in all). Run `refresh.py` again afterwards so the page picks up the recording.
- `python3 deploy.py` posts `site/index.html` to the Vercel API as a production deployment. It sends no token:
  the cloud environment's "Vercel API Token" credential adds the Authorization header for api.vercel.com.
  To test locally, pipe `python3 deploy.py --body-only` into `vercel api "/v13/deployments?teamId=team_45ZNkd6F5HMXNUu6jGjrKr44" -X POST --input -`.
- A daily Claude Code cloud routine (environment `morningbrief`) runs the script, researches the missing movers,
  writes the podcast script, runs `podcast.py`, reruns `refresh.py` and deploys.

## Writing the podcast script

A spoken summary, not a reading of the page: about 1.5 to 3 minutes (250 to 450 words), addressed to Mads.
- Open with the day and the headline: value, P&L and the 24h change in round numbers.
- Spend time only where there is a story: a real mover with a catalyst from research.json, a resolution,
  a market that ended, something to redeem, a trade. On a quiet day, say it was quiet and keep it short.
- Group small moves into one sentence; skip positions that did nothing.
- Finish with Copenhagen's weather, tonight's Saturn verdict and the best Saturn night this week.
- Write for the ear: round numbers in words ("about eight thousand three hundred dollars", "ninety-three
  and a half cents"), no symbols, tables, asset ids or URLs, short sentences. No buy or sell advice.
