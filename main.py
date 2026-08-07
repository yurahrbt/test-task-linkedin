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
            try:
                result = pipeline.collect_and_rank(session.page)
            except SelectorDriftError as exc:
                # Stop before ranking/commenting: every downstream decision would be
                # built on engagement numbers the scraper could not actually read.
                logger.error("[main] LinkedIn markup no longer matches the scraper: %s", exc)
                raise SystemExit(1)

            print_posts(result.posts)
            save_json(config.Paths.FEED_POSTS_PATH, result.posts)
            logger.info("[main] saved %d posts to %s", len(result.posts), config.Paths.FEED_POSTS_PATH)

            save_json(config.Paths.RANKED_POSTS_PATH, result.ranked)
            logger.info("[main] saved top %d ranked posts to %s", len(result.ranked), config.Paths.RANKED_POSTS_PATH)

            try:
                drafts = pipeline.draft(result.ranked)
            except CommentingAuthError as exc:
                # The credentials worked at preflight and stopped working mid-stage
                # (revoked, quota removed). Leave drafts.json untouched rather than
                # writing a half-real one.
                logger.error("[main] %s", exc)
                raise SystemExit(1)

            print_drafts(drafts)
            save_json(config.Paths.DRAFTS_PATH, drafts)
            logger.info("[main] saved %d comment drafts to %s", len(drafts), config.Paths.DRAFTS_PATH)
        finally:
            session.close()


if __name__ == "__main__":
    main()
