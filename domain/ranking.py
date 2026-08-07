import logging
import re

import config
from linkedin.feed_scraper import ScrapedPost

logger = logging.getLogger(__name__)

# Strip emoji and other non-text symbols so that an emoji-only post registers as empty.
_EMOJI_RE = re.compile(
    "["
    "\U0001F600-\U0001F64F"  # emoticons
    "\U0001F300-\U0001F5FF"  # symbols & pictographs
    "\U0001F680-\U0001F6FF"  # transport & map
    "\U0001F1E0-\U0001F1FF"  # flags
    "\U00002702-\U000027B0"  # dingbats
    "\U0000FE00-\U0000FE0F"  # variation selectors
    "\U0001F900-\U0001F9FF"  # supplemental symbols
    "\U0001FA00-\U0001FA6F"  # chess symbols
    "\U0001FA70-\U0001FAFF"  # symbols extended-A
    "\U00002600-\U000026FF"  # misc symbols
    "\U0000200D"             # zero-width joiner
    "]+",
    flags=re.UNICODE,
)


def has_commentable_text(post: ScrapedPost) -> bool:
    """True if the post carries enough text for the AI to draft a meaningful comment."""
    stripped = _EMOJI_RE.sub("", post.text).strip()
    return len(stripped) >= config.Ranking.MIN_COMMENT_TEXT_LENGTH


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
