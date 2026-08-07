"""Dedicated liking stage — navigates to each post by URN and likes it.

Separated from feed collection so that only posts that survive ranking are liked,
not whichever posts happened to appear first in the feed.
"""

import logging

from playwright.sync_api import Error as PlaywrightError, Locator, Page

import config
from linkedin import selectors
from linkedin.feed_scraper import ScrapedPost
from utils.popup_handler import dismiss_popup_if_present
from utils.rate_limiter import human_delay

logger = logging.getLogger(__name__)

POST_URL_TEMPLATE = "https://www.linkedin.com/feed/update/{}/"


def _resolve_url(post: ScrapedPost) -> str | None:
    """Return a navigable URL for the post, or None if no URN is available."""
    if post.urn:
        return POST_URL_TEMPLATE.format(post.urn)
    return None


def _verify_liked(scope: Locator, index: int) -> bool:
    """Return True if the like icon has transitioned to the filled (liked) state."""
    try:
        icon_id = selectors.like_icon(scope).get_attribute(
            "id", timeout=config.Timeouts.LIKE_VERIFY_TIMEOUT_MS
        )
    except PlaywrightError as exc:
        logger.warning("[_verify_liked] post %d, icon unreadable after click: %s", index, exc)
        return False

    # A filled icon does NOT contain "outline" in its id.
    return "outline" not in (icon_id or "")


def _like_on_current_page(page: Page, index: int) -> str:
    """Click the like button on the main post of the current page and verify the result."""
    cards = selectors.feed_post_cards(page)
    scope = cards.first if cards.count() > 0 else page.locator("main")

    try:
        icon_id = selectors.like_icon(scope).get_attribute(
            "id", timeout=config.Timeouts.ELEMENT_ACTION_TIMEOUT_MS
        )
    except PlaywrightError as exc:
        logger.warning("[_like_on_current_page] post %d, no like icon found: %s", index, exc)
        return f"failed: no like icon ({exc})"

    if "outline" not in (icon_id or ""):
        return "already_liked"

    dismiss_popup_if_present(page, timeout=config.Timeouts.POPUP_RECHECK_TIMEOUT_MS)

    try:
        selectors.like_button(scope).click(timeout=config.Timeouts.ELEMENT_ACTION_TIMEOUT_MS)
    except PlaywrightError as exc:
        logger.warning("[_like_on_current_page] post %d, like click failed: %s", index, exc)
        return f"failed: {exc}"

    human_delay(config.RateLimits.LIKE_DELAY)

    # Verify the icon actually changed to the filled (liked) state.
    if not _verify_liked(scope, index):
        logger.warning("[_like_on_current_page] post %d, like not confirmed after click", index)
        return "failed: like not confirmed (icon still in outline state)"

    return "liked"


def like_posts(page: Page, posts: list[ScrapedPost]) -> list[ScrapedPost]:
    """Navigate to each post by URN and like it.

    Returns a new list of posts with the ``outcome`` field updated to reflect
    the result of the like attempt.
    """
    results: list[ScrapedPost] = []
    for i, post in enumerate(posts):
        url = _resolve_url(post)
        if not url:
            logger.warning(
                "[like_posts] post %d (%s): no URN, cannot navigate to like",
                i + 1, post.author,
            )
            results.append(post.model_copy(update={"outcome": "skipped: no URN available"}))
            continue

        try:
            page.goto(url, wait_until="domcontentloaded")
            human_delay(config.RateLimits.NAV_DELAY)
            dismiss_popup_if_present(page, timeout=config.Timeouts.POPUP_RECHECK_TIMEOUT_MS)
            outcome = _like_on_current_page(page, i)
        except PlaywrightError as exc:
            logger.warning(
                "[like_posts] post %d (%s): navigation failed: %s",
                i + 1, post.author, exc,
            )
            outcome = f"failed: navigation error ({exc})"

        results.append(post.model_copy(update={"outcome": outcome}))
        logger.info("[like_posts] post %d (%s): %s", i + 1, post.author, outcome)

    liked_count = sum(1 for p in results if p.outcome == "liked")
    skipped_count = sum(1 for p in results if p.outcome.startswith("skipped:"))
    failed_count = sum(1 for p in results if p.outcome.startswith("failed:"))
    logger.info(
        "[like_posts] results: %d liked, %d already_liked, %d skipped, %d failed out of %d",
        liked_count,
        sum(1 for p in results if p.outcome == "already_liked"),
        skipped_count,
        failed_count,
        len(results),
    )
    return results
