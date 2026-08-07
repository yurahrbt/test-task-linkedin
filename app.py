"""Application-level orchestration for one LinkedIn engagement run.

This module owns the order and failure boundaries between the domain stages. Browser
automation, ranking, and AI drafting remain separate services; the CLI only handles
configuration, persistence, and presentation.
"""

from dataclasses import dataclass
import logging

from playwright.sync_api import Page

import config
from ai.commenter import CommentDraft, draft_comments
from ai.errors import CommentingAuthError
from domain.ranking import rank_posts
from linkedin.feed_scraper import ScrapedPost, scrape_feed_posts

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RankedFeed:
    """The feed posts and the smaller set selected for comment drafting."""

    posts: list[ScrapedPost]
    ranked: list[ScrapedPost]


class EngagementPipeline:
    """Coordinate the read/react/rank/draft stages without owning browser state."""

    def collect_and_rank(self, page: Page) -> RankedFeed:
        posts = scrape_feed_posts(page, limit=config.Ranking.TOP_LIKE_COUNT)
        ranked = rank_posts(posts, top_n=config.Ranking.RANKED_TOP_N)
        logger.info("[pipeline] collected %d posts and selected %d for drafting", len(posts), len(ranked))
        return RankedFeed(posts=posts, ranked=ranked)

    def draft(self, ranked: list[ScrapedPost]) -> list[CommentDraft]:
        """Draft comments only after collection/ranking has completed."""
        try:
            return draft_comments(ranked)
        except CommentingAuthError:
            # Do not convert credential failures into per-post draft failures; callers
            # need to know that this stage did not complete.
            raise
