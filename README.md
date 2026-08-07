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

## Choices a teammate should know about

- **Selectors are centralized in `dom_selectors.py`.** LinkedIn's class names are obfuscated/regenerated per build, so nothing there is stable. Where possible we key off `aria-label`/role; where not, off the internal SVG icon `id` (e.g. `thumbs-up-outline-small`) since those are locale-independent, unlike `aria-label` text which changes with the account's UI language. This file is the one place to fix things when LinkedIn's markup shifts.
- **Engagement counts: the layout is detected, never assumed.** LinkedIn serves at least two feed builds, and they keep the numbers in different places:
  - *summary row* — a `X reactions · Y comments` row above the action bar, keyed by semantic BEM classes (`.social-details-social-counts…`). Here the action buttons carry no number, so reading one would be the bug this guards against.
  - *action bar* — no summary row at all; each count sits **inside** its action button next to the icon. Every class name is a per-build hash (`_21c7e3d1`) and every `aria-label` is in the account's UI language, so the only anchor that survives is the icon's `id` (`thumbs-up-*`, `comment-*`).

  `detect_count_strategy()` runs before the first like and picks a layout only once it has read a real number off a real card; that proof is what later licenses treating an empty control as a genuine zero. If no known layout produces a single number, the run aborts with `SelectorDriftError` instead of reporting a feed of zeros — and if counts are unreadable on more than half the scraped posts, it aborts before ranking. A count that can't be read is recorded as `null`, not `0`, and that post is neither liked nor ranked. Numbers are parsed from the accessible label *or* the text, whichever holds one, across `1,234` / `1.2K` / `1 234` / `1.234` and localized forms.
- **Locale is a fight.** LinkedIn's logged-out pages respect `Accept-Language`/a `lang` cookie (which we force to `en-US`); the authenticated feed instead uses the account's saved language preference, which no request header can override. If your feed still renders in another language, change it once in Settings & Privacy → Account preferences → Language — the script can't do this for you without guessing at an unfamiliar settings page.
- **Posts are identified by activity URN, with a fingerprint as backstop.** The feed is virtualized, so the same card is handed to us repeatedly; de-dup happens before any like. The key is the post's activity URN, read from the card root's own attributes first (a Playwright locator only searches *descendants*, so the root needs an explicit read) and then from known descendants — accepting only anchored `urn:li:{activity,ugcPost,share}:<id>` values, since profile, media and comment URNs are scattered across the same card and a comment's URN embeds a *different* post's id. Cards with no URN fall back to a fingerprint of author + text + age + media URLs, which is what keeps two text-less image posts by the same author apart; that path is best-effort and gets logged as such. Heads-up: the action-bar build exposes **no** `data-urn`/`data-id` anywhere on a card, so on that build every post takes the fingerprint path and the run logs one warning saying so.
- **Already-liked posts are detected, not toggled.** The like icon's `id` drops the `outline` suffix once reacted; we check that before clicking, so re-running the script never accidentally un-likes something.
- **2FA/checkpoints are classified by URL *and* page, in that order.** `classify_login_state()` checks the feed URL, then a captcha/bot check, then a code field, then any other checkpoint URL. The captcha check comes before the code field on purpose: a bot-check page can also render `input[name="pin"]`, and matching the code field first meant a captcha could be mistaken for "a human can type their way out of this" and waited on forever. Only the recognized email-code URL waits with no deadline; a code field on an unrecognized URL gets a bounded wait (`UNRECOGNIZED_CHALLENGE_WAIT_MS`, 120s) and then fails as a checkpoint; captchas and other challenges fail immediately with the URL. Headless runs never wait for a code — they say to re-run with `HEADLESS=false`.
- **OpenAI credentials are checked before LinkedIn is touched.** `verify_api_access()` does one metadata request (no tokens) at startup, so an expired or unentitled key costs nothing instead of costing a login, a full scrape and a screenful of likes. If credentials fail *during* drafting, the stage aborts on the first auth error rather than writing `failed: ...` into every record — a `drafts.json` full of those reads like a run that worked. Transient failures (rate limit, 5xx, timeouts) still degrade to a per-post `failed: ...` draft, since those are genuinely per-request.
- **The "2-3 most interesting" rule:** from the 10 feed posts, rank by `engagement_score = comments_count / sqrt(max(likes_count, 1))`. Plain comments/likes ratio over-rewards tiny samples (5 likes/3 comments beats 300 likes/60 comments on ratio alone, despite the second post clearly being more "alive"). Dividing by `sqrt(likes)` instead of `likes` keeps the ratio's signal but stops a handful of replies on a barely-seen post from outranking real engagement. Posts whose counts cannot be read are skipped rather than guessed.
- **Comments are drafted, never posted.** There's no "submit comment" selector anywhere in the codebase — `commenter.py` only calls the OpenAI API and returns text.
- **Rate limiting:** randomized delays between likes (2-5s), scroll steps (1-3s), and nav actions (2-4s) in `config.py` — tuned to look paced, not scripted, not validated against LinkedIn's actual bot-detection thresholds.
