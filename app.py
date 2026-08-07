"""Application-level orchestration for one LinkedIn engagement run.

This module owns the order and failure boundaries between the domain stages. Browser
automation, ranking, and AI drafting remain separate services; the CLI only handles
configuration, persistence, and presentation.

The pipeline enforces a strict stage order:
  1. **Collect** — scrape a pool of posts from the feed without side effects.
  2. **Rank** — score and select the most interesting posts.
  3. **Like** — find each winning post again in the feed and like it.
  4. **Draft** — generate AI comments for the top-ranked subset.
"""

import logging

from playwright.sync_api import Page

import config
from ai.commenter import CommentDraft, draft_comments
from ai.errors import CommentingAuthError
from domain.ranking import rank_posts
from linkedin.feed_scraper import ScrapedPost, scrape_feed_posts
from linkedin.liker import like_posts

logger = logging.getLogger(__name__)


class EngagementPipeline:
    """Coordinate the collect/rank/like/draft stages without owning browser state.

    Each stage is a separate method so that callers (main.py) can persist
    results incrementally — in particular after the irreversible *like* stage.
    """

    def collect(self, page: Page) -> list[ScrapedPost]:
        """Stage 1 — scrape a pool of posts without side effects."""
        return scrape_feed_posts(page, limit=config.Ranking.COLLECT_POOL_SIZE)

    def rank(self, pool: list[ScrapedPost]) -> list[ScrapedPost]:
        """Stage 2 — score and select the most interesting posts."""
        return rank_posts(pool, top_n=config.Ranking.TOP_LIKE_COUNT)

    def like(self, page: Page, posts: list[ScrapedPost]) -> list[ScrapedPost]:
        """Stage 3 — re-find each selected feed card and like it (irreversible)."""
        return like_posts(page, posts)

    def draft(self, candidates: list[ScrapedPost]) -> list[CommentDraft]:
        """Stage 4 — draft AI comments, walking candidates until target is met.

        Thin posts and AI-returned SKIPs are skipped automatically; pass more
        candidates than ``RANKED_TOP_N`` so the loop can compensate.
        """
        try:
            return draft_comments(
                candidates, target_count=config.Ranking.RANKED_TOP_N,
            )
        except CommentingAuthError:
            # Do not convert credential failures into per-post draft failures;
            # callers need to know that this stage did not complete.
            raise
