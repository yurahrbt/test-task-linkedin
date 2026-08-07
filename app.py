"""Application-level orchestration for one LinkedIn engagement run.

This module owns the order and failure boundaries between the domain stages. Browser
automation, ranking, and AI drafting remain separate services; the CLI only handles
configuration, persistence, and presentation.

The pipeline enforces a strict stage order:
  1. **Collect** — scrape a pool of posts from the feed without side effects.
  2. **Rank** — score and select the most interesting posts.
  3. **Like** — navigate to each winning post and like it.
  4. **Draft** — generate AI comments for the top-ranked subset.
"""

from dataclasses import dataclass
import logging

from playwright.sync_api import Page

import config
from ai.commenter import CommentDraft, draft_comments
from ai.errors import CommentingAuthError
from domain.ranking import rank_posts
from linkedin.feed_scraper import ScrapedPost, scrape_feed_posts
from linkedin.liker import like_posts

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RankedFeed:
    """The feed posts and the smaller set selected for comment drafting."""

    posts: list[ScrapedPost]
    ranked: list[ScrapedPost]


class EngagementPipeline:
    """Coordinate the collect/rank/like/draft stages without owning browser state."""

    def collect_and_rank(self, page: Page) -> RankedFeed:
        # Stage 1 — Collect a pool of posts without liking any of them.
        pool = scrape_feed_posts(page, limit=config.Ranking.COLLECT_POOL_SIZE)

        # Stage 2 — Rank the pool and select the top N most interesting.
        top_posts = rank_posts(pool, top_n=config.Ranking.TOP_LIKE_COUNT)

        # Stage 3 — Like only the posts that survived ranking.
        liked = like_posts(page, top_posts)

        # The top RANKED_TOP_N of the liked posts are selected for comment drafting.
        ranked = liked[: config.Ranking.RANKED_TOP_N]

        logger.info(
            "[pipeline] collected %d posts, liked %d, selected %d for drafting",
            len(pool), len(liked), len(ranked),
        )
        return RankedFeed(posts=liked, ranked=ranked)

    def draft(self, ranked: list[ScrapedPost]) -> list[CommentDraft]:
        """Draft comments only after collection/ranking has completed."""
        try:
            return draft_comments(ranked)
        except CommentingAuthError:
            # Do not convert credential failures into per-post draft failures; callers
            # need to know that this stage did not complete.
            raise
