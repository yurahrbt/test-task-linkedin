import logging

from playwright.sync_api import sync_playwright

import config
from ai.commenter import verify_api_access
from ai.errors import CommentingAuthError
from app import EngagementPipeline
from linkedin.errors import LoginError, SelectorDriftError
from linkedin.session import open_feed
from output.persistence import merge_into_history, save_json
from utils.logging_setup import setup_logging
from output.reporting import print_drafts, print_posts

logger = logging.getLogger("main")


def _like_outcome_summary(posts: list) -> tuple[int, int, int]:
    """Return (liked, skipped, failed) counts from post outcomes."""
    liked = sum(1 for p in posts if p.outcome in ("liked", "already_liked"))
    skipped = sum(1 for p in posts if p.outcome.startswith("skipped:"))
    failed = sum(1 for p in posts if p.outcome.startswith("failed:"))
    return liked, skipped, failed


def main() -> None:
    config.ensure_dirs()
    setup_logging()
    pipeline = EngagementPipeline()

    # Both checks run before LinkedIn is touched. A key that is missing, expired or not
    # entitled to the model would otherwise be discovered only after a login, a full
    # human-paced scrape and a screenful of likes — with "failed: ..." written into every
    # draft record, which reads like a run that worked.
    if not config.settings.openai_api_key.strip():
        logger.error("[main] OPENAI_API_KEY is not set — set it in .env before running.")
        raise SystemExit(1)

    try:
        verify_api_access()
    except CommentingAuthError as exc:
        logger.error("[main] %s", exc)
        raise SystemExit(1)

    like_failure = False  # tracks whether the like stage had a total failure

    with sync_playwright() as playwright:
        try:
            session = open_feed(playwright)
        except LoginError as exc:
            logger.error("[main] %s", exc)
            raise SystemExit(1)

        try:
            # Stage 1-2: Collect and rank (no side effects on LinkedIn).
            # SelectorDriftError fires here, before any likes.
            try:
                pool = pipeline.collect(session.page)
                top_posts = pipeline.rank(pool)
            except SelectorDriftError as exc:
                logger.error("[main] LinkedIn markup no longer matches the scraper: %s", exc)
                raise SystemExit(1)

            # Persist the full collected pool to cumulative history (sorted by score).
            # This builds a useful engagement log across runs even when individual
            # stages fail later.
            merge_into_history(config.Paths.POSTS_HISTORY_PATH, pool)

            # Stage 3: Like (irreversible mutation).
            # Persist immediately so every attempted like is recorded even if
            # the process crashes or the drafting stage fails later.
            liked = pipeline.like(session.page, top_posts)
            print_posts(liked)
            save_json(config.Paths.FEED_POSTS_PATH, liked)
            logger.info("[main] saved %d posts to %s", len(liked), config.Paths.FEED_POSTS_PATH)

            save_json(config.Paths.RANKED_POSTS_PATH, liked)
            logger.info("[main] saved top %d ranked posts to %s", len(liked), config.Paths.RANKED_POSTS_PATH)

            # Update history with like outcomes.
            merge_into_history(config.Paths.POSTS_HISTORY_PATH, liked)

            # Report partial/total like failure clearly.
            liked_count, skipped_count, failed_count = _like_outcome_summary(liked)
            total = len(liked)
            if total == 0:
                logger.error(
                    "[main] TOTAL LIKE FAILURE: the exhausted feed produced no eligible posts."
                )
                like_failure = True
            elif liked_count == 0:
                logger.error(
                    "[main] TOTAL LIKE FAILURE: 0 of %d posts were liked "
                    "(%d skipped, %d failed). The run performed no engagement.",
                    total, skipped_count, failed_count,
                )
                like_failure = True
            elif liked_count < total:
                logger.warning(
                    "[main] PARTIAL LIKE FAILURE: only %d of %d posts were liked "
                    "(%d skipped, %d failed).",
                    liked_count, total, skipped_count, failed_count,
                )

            # Stage 4: Draft comments (no side effects on LinkedIn).
            # Only posts that were actually liked are valid comment candidates.
            # Drafting for failed/skipped posts would produce polished comments
            # for content the user never engaged with — misleading output.
            successfully_liked = [p for p in liked if p.outcome in ("liked", "already_liked")]

            if like_failure:
                logger.error(
                    "[main] Level 1 (Like) did not complete — skipping Level 2 (Draft). "
                    "No comments will be generated because no posts were liked.",
                )
                drafts: list = []
            elif not successfully_liked:
                logger.warning(
                    "[main] No successfully-liked posts available for comment drafting.",
                )
                drafts = []
            else:
                if len(successfully_liked) < len(liked):
                    logger.info(
                        "[main] %d of %d posts were successfully liked — "
                        "only those will be used for comment drafting.",
                        len(successfully_liked), len(liked),
                    )
                try:
                    drafts = pipeline.draft(successfully_liked)
                except CommentingAuthError as exc:
                    logger.error("[main] %s", exc)
                    raise SystemExit(1)

            print_drafts(drafts)
            save_json(config.Paths.DRAFTS_PATH, drafts)
            logger.info("[main] saved %d comment drafts to %s", len(drafts), config.Paths.DRAFTS_PATH)
        finally:
            session.close()

    # Exit non-zero when the like stage performed no engagement at all, so that
    # callers (CI, cron) can detect that the run was ineffective.
    if like_failure:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
