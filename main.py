import logging

from playwright.sync_api import sync_playwright

import config
from ai.commenter import verify_api_access
from ai.errors import CommentingAuthError
from app import EngagementPipeline
from linkedin.errors import LoginError, SelectorDriftError
from linkedin.session import open_feed
from output.persistence import save_json
from utils.logging_setup import setup_logging
from output.reporting import print_drafts, print_posts

logger = logging.getLogger("main")


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

            # Stage 3: Like (irreversible mutation).
            # Persist immediately so every attempted like is recorded even if
            # the process crashes or the drafting stage fails later.
            liked = pipeline.like(session.page, top_posts)
            print_posts(liked)
            save_json(config.Paths.FEED_POSTS_PATH, liked)
            logger.info("[main] saved %d liked posts to %s", len(liked), config.Paths.FEED_POSTS_PATH)

            save_json(config.Paths.RANKED_POSTS_PATH, liked)
            logger.info("[main] saved top %d ranked posts to %s", len(liked), config.Paths.RANKED_POSTS_PATH)

            # Stage 4: Draft comments (no side effects on LinkedIn).
            # Pass all liked posts as candidates so the commenter can skip thin
            # posts and AI-returned SKIPs while still reaching the target count.
            try:
                drafts = pipeline.draft(liked)
            except CommentingAuthError as exc:
                logger.error("[main] %s", exc)
                raise SystemExit(1)

            print_drafts(drafts)
            save_json(config.Paths.DRAFTS_PATH, drafts)
            logger.info("[main] saved %d comment drafts to %s", len(drafts), config.Paths.DRAFTS_PATH)
        finally:
            session.close()


if __name__ == "__main__":
    main()
