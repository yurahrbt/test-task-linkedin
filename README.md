# LinkedIn Engagement Automation

**Level reached:** Level 2 (read + like + AI-drafted comments). **Time spent:** ~1.5 hours.

Level 3 was intentionally not attempted: the implementation does not visit author
profiles, so it does not claim profile-aware context it did not collect.

## How to run

```
cd linkedin-automation
cp .env.example .env
```

Fill in `.env`:

```
LINKEDIN_EMAIL=you@example.com
LINKEDIN_PASSWORD=your-password
OPENAI_API_KEY=sk-...
```

Then:

```
uv sync
uv run playwright install chromium
uv run python main.py
```

First run opens a visible (headed) Chromium window and logs in. If LinkedIn shows an email verification code, the script pauses and waits — enter it in that window, the run continues on its own. Subsequent runs reuse the cached session (`data/storage_state.json`) until it's 12h old.

## Output

- stdout: for up to 10 eligible unique feed posts — author, first 200 chars, and like outcome (`liked` / `already_liked` / `failed: ...`); cards with unreadable counts are retained in the output for transparency but skipped rather than liked, and the scraper continues loading until it finds 10 eligible posts or exhausts its attempts; then the top 2-3 scorable picks with their AI-drafted comments.
- `data/feed_posts.json`, `data/ranked.json`, `data/drafts.json` — intermediate artifacts of each pipeline stage.
- `logs/run_<timestamp>.log` — full run log (retries, skips, warnings).

## Code layout

- `main.py` is the CLI entry point and owns only startup, error presentation, and output persistence.
- `app.py` coordinates the scrape → rank → draft stages.
- `linkedin/` owns authentication, browser sessions, selectors, feed scraping, and `errors.py` for LinkedIn failures.
- `domain/ranking.py` contains the post-selection rule; `ai/commenter.py` and `ai/errors.py` contain the OpenAI integration and its failures.
- `output/` contains JSON persistence and terminal reporting; `utils/` contains generic timing, retry, popup, and logging helpers.