# Code Review

Date: 2026-08-07  
Branch: `main`  
Scope: current working tree, including the uncommitted refactor from the flat modules into `linkedin/`, `ai/`, `domain/`, `output/`, and `utils/`.

## Summary

The implementation is appropriately limited to the Level 2 task scope: it signs in, reads up to 10 feed cards, likes readable cards, ranks candidates, and prints AI-generated comment drafts without posting comments or visiting author profiles.

The main risks are selector correctness and testability rather than architecture. The most important follow-ups are to make the “10 posts” contract explicit, tighten author/count extraction, and add unit tests for parsing and ranking before relying on the browser against live LinkedIn markup.

## Findings

### Resolved — the scraper could return fewer than 10 liked posts

`linkedin/feed_scraper.py:520-553` previously stopped when it had collected 10 `ScrapedPost` objects, but unreadable-count cards are also appended as posts and are deliberately not liked. The sample run therefore processed 10 cards but liked only 7.

The scraper now continues loading until it has liked/confirmed 10 eligible posts or exhausted its configured load attempts. Unreadable cards remain in the returned output and are logged as skipped.

Follow-up: add a test covering unreadable cards between readable cards.

### High — count-layout detection can accept a partially broken selector set

`linkedin/feed_scraper.py:452-470` selects a layout as soon as either reactions or comments produces one readable number. Later, `_extract_count()` can classify a missing metric control as a genuine zero when a like button exists (`linkedin/feed_scraper.py:183-194`).

This means a selector regression affecting only comments, for example, can silently turn real comment counts into zero and change ranking while the run continues. The existing warning in `_log_run_stats()` is not fatal and does not prevent drafting from bad ranking input.

Recommended action: validate both metric regions/controls during preflight, or make the run abort when a metric is structurally expected but never yields a trustworthy reading. Add fixtures for “reactions work, comments selector broken.”

### Medium — author extraction may select a link from the post body instead of the author

`linkedin/selectors.py:76-78` collects every `/in/` or `/company/` link inside the card and takes `.last`. A post containing a tagged person, company link, article CTA, or other linked content can therefore be attributed to the wrong author.

Recommended action: scope the selector to the actor/header region and prefer the first author link there. Add a DOM fixture with an author link plus a later company/body link.

### Medium — broad exception handling hides programming and API-shape defects

`ai/commenter.py:99-113` converts every non-credential exception into a `failed: ...` draft. This is resilient for transient API failures, but it also masks unexpected bugs such as malformed responses, schema changes, or coding errors and makes the overall run look partially successful.

Recommended action: catch the specific transient OpenAI/network exceptions that are safe to degrade per post, log the exception with traceback, and let unexpected exceptions fail the stage. Consider an explicit draft status instead of embedding errors in the comment string.

### Medium — count parsing has ambiguous locale behavior

`linkedin/feed_scraper.py:90-144` intentionally guesses whether `.` and `,` are grouping or decimal separators. That is reasonable for common forms such as `1,234` and `1.2K`, but ambiguous forms can be rounded into the wrong integer; for example, `1.234,5` is converted to `1234` after rounding. The parser also only recognizes ASCII magnitude suffixes.

Recommended action: keep the supported-format list narrow and documented, or parse based on the account/feed locale. Add table-driven tests for every advertised format and for malformed/ambiguous values.

### Medium — live browser behavior is effectively untested

There are no automated tests (`pytest` reports “no tests ran”). The most fragile code is exactly the code that needs tests: selector composition, count parsing, deduplication, ranking, login-state classification, and output formatting.

Recommended action: add fast unit tests using mocked Playwright locators or small HTML fixtures. At minimum cover `_parse_count()`, `_to_number()`, `rank_posts()`, `classify_login_state()`, already-liked detection, unreadable-count handling, and the 10-post/10-eligible-post decision.

### Low — scraping uses many synchronous locator round trips

`linkedin/feed_scraper.py:166-197` tries multiple locators and up to three elements per candidate, with separate accessibility/text calls for each element. This is acceptable for a 10-post prototype, but it can become slow and brittle as selector candidates grow. The scraper also performs serial human delays between actions.

Recommended action: leave the pacing as-is for this task, but reduce redundant locator calls if the feed grows. Keep the selector candidate limit and measure a real run before optimizing further.

### Low — feed loading has weak readiness/error handling

`linkedin/session.py:28-33` navigates to the feed without an explicit timeout or a readiness assertion. A redirect, consent page, or slow feed can reach scraping with no usable cards and produce a selector-drift error that does not explain the navigation state.

Recommended action: use the configured feed timeout, wait for a feed-specific readiness signal, and include the final URL/page state in the failure message.

## Scope review

No out-of-scope user-facing behavior was found:

- No profile visits or profile-aware comments; Level 3 is not claimed.
- No comment submission selector or posting action.
- No connect, follow, message, share, or delete actions.
- Extra logging, retries, session caching, rate limiting, and JSON artifacts support the requested workflow and do not expand its product scope.

The repository does contain runtime artifacts and local files in the working tree (`.DS_Store`, generated caches, logs, and data). `.gitignore` covers the important generated directories and secrets, but cleanup should be done before submission if those files are intended to be excluded from the GitHub repository.

## Performance assessment

The dominant cost is deliberate pacing: 2–5 seconds after each successful like, 1–3 seconds per scroll/load-more step, and 2–4 seconds during navigation. For 10 posts this is acceptable and aligned with the prototype’s human-paced behavior. The potential performance issue is the number of Playwright sync calls per card, not Python computation.

Do not parallelize likes or browser actions: that would increase race conditions and bot-detection risk. Optimize only after measuring selector round trips in a real run.

## Verification performed

- Python bytecode compilation: passed with `compileall`.
- `pytest -q`: no tests were found.
- Count-parser smoke checks were run for common forms such as `1,234`, `1.2K`, `1 234`, and zero counts.
- Static scope inspection found no profile navigation or comment-posting action.

## Prioritized follow-up plan

1. Decide whether “10” means 10 inspected cards or 10 eligible liked posts; align code, README, and sample output.
2. Harden count preflight so a partially broken metric cannot silently rank posts with fabricated zeros.
3. Fix author-link scoping and add a representative card fixture.
4. Add unit tests for count parsing, ranking, deduplication, login states, and scraper stopping behavior.
5. Replace broad AI exception handling with explicit transient-error handling and traceback logging.
6. Add feed readiness checks and clean generated artifacts before submitting the repository.
