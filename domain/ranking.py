import logging

from linkedin.feed_scraper import ScrapedPost

logger = logging.getLogger(__name__)


def rank_posts(posts: list[ScrapedPost], top_n: int) -> list[ScrapedPost]:
    # A post whose engagement counts could not be read has no score; ranking it as 0
    # would silently place it last, so it is excluded from the run instead.
    scorable = [post for post in posts if post.engagement_score is not None]
    skipped = len(posts) - len(scorable)
    if skipped:
        logger.warning("[rank_posts] excluded %d post(s) with unreadable engagement counts", skipped)

    ranked = sorted(scorable, key=lambda post: post.engagement_score or 0.0, reverse=True)[:top_n]
    logger.info("[rank_posts] ranked %d posts down to top %d", len(scorable), len(ranked))
    return ranked
