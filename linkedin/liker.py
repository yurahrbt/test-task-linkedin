"""Dedicated liking stage — finds ranked posts again and likes them in the feed.

Separated from feed collection so that only posts that survive ranking are liked,
not whichever posts happened to appear first in the feed.
"""

import logging
import time
import typing as tp

from playwright.sync_api import Error as PlaywrightError, Locator, Page

import config
from linkedin import selectors
from linkedin.feed_scraper import ScrapedPost, _load_more_step, inspect_post_card
from utils.popup_handler import dismiss_popup_if_present
from utils.rate_limiter import human_delay

logger = logging.getLogger(__name__)


REACTION_NONE = "none"
REACTION_LIKE = "like"
REACTION_OTHER = "other"
REACTION_UNKNOWN = "unknown"


def _reaction_from_icon_id(icon_id: str | None) -> str:
    """Interpret the reaction-state icon without depending on the UI language."""
    normalized = (icon_id or "").lower()
    if normalized.startswith("like-consumption-"):
        return REACTION_LIKE
    if "-consumption-" in normalized:
        return REACTION_OTHER
    if normalized.startswith("thumbs-up-") and "outline" in normalized:
        return REACTION_NONE
    return REACTION_UNKNOWN


def _read_reaction_state(scope: Locator, index: int) -> str:
    try:
        icon_id = selectors.like_icon(scope).get_attribute(
            "id", timeout=config.Timeouts.ELEMENT_ACTION_TIMEOUT_MS
        )
    except PlaywrightError as exc:
        logger.debug("[_read_reaction_state] post %d, state icon unavailable: %s", index, exc)
        return REACTION_UNKNOWN

    return _reaction_from_icon_id(icon_id)


def _find_card_by_match_key(page: Page, match_key: str) -> Locator | None:
    """Reacquire a card after LinkedIn re-renders the feed following a reaction."""
    cards = selectors.feed_post_cards(page)
    for card_index in range(cards.count()):
        card = cards.nth(card_index)
        snapshot = inspect_post_card(card, card_index)
        if snapshot is not None and snapshot.feed_match_key == match_key:
            return card
    return None


def _wait_for_liked(page: Page, scope: Locator, match_key: str, index: int) -> bool:
    """Poll until the matched card reports the explicit Like reaction."""
    deadline = time.monotonic() + config.Timeouts.LIKE_VERIFY_TIMEOUT_MS / 1000
    current_scope = scope
    last_state = REACTION_UNKNOWN

    while time.monotonic() < deadline:
        last_state = _read_reaction_state(current_scope, index)
        if last_state == REACTION_LIKE:
            return True

        # The reaction update can replace or reorder the card node. Reacquire it
        # by the same reload-stable identity used before the click.
        replacement = _find_card_by_match_key(page, match_key)
        if replacement is not None:
            current_scope = replacement
        page.wait_for_timeout(200)

    logger.warning(
        "[_wait_for_liked] post %d did not reach Like state within %.1fs (last state: %s)",
        index,
        config.Timeouts.LIKE_VERIFY_TIMEOUT_MS / 1000,
        last_state,
    )
    return False


def _like_card(page: Page, scope: Locator, match_key: str, index: int) -> str:
    """Click and verify Like on a card already matched to the ranked post."""
    state = _read_reaction_state(scope, index)
    if state == REACTION_LIKE:
        return "already_liked"
    if state == REACTION_OTHER:
        return "skipped: post already has a different reaction"
    if state == REACTION_UNKNOWN:
        return "failed: reaction state could not be read"

    dismiss_popup_if_present(page, timeout=config.Timeouts.POPUP_RECHECK_TIMEOUT_MS)

    # In this LinkedIn feed build, clicking the thumbs-up/count control does not
    # select Like. Hovering it opens the reaction picker; choose Like explicitly.
    try:
        selectors.like_button(scope).hover(timeout=config.Timeouts.ELEMENT_ACTION_TIMEOUT_MS)
        like_option = selectors.like_reaction_option(page)
        like_option.wait_for(state="visible", timeout=config.Timeouts.ELEMENT_ACTION_TIMEOUT_MS)
        like_option.click(timeout=config.Timeouts.ELEMENT_ACTION_TIMEOUT_MS)
    except PlaywrightError as exc:
        logger.warning("[_like_card] post %d, selecting Like failed: %s", index, exc)
        return f"failed: could not select Like reaction ({exc})"

    if not _wait_for_liked(page, scope, match_key, index):
        return "failed: Like reaction not confirmed"

    human_delay(config.RateLimits.LIKE_DELAY)
    return "liked"


def _post_key(post: ScrapedPost) -> str | None:
    return post.feed_match_key or post.identity_key or post.urn


def like_posts(page: Page, posts: list[ScrapedPost]) -> list[ScrapedPost]:
    """Re-scan the feed and Like ranked posts by URN or content fingerprint.

    Most current feed cards expose no activity URN. Collection therefore stores a
    within-run fingerprint, and this stage returns to the feed and matches that same
    identity before clicking. Ambiguous/unseen cards are never clicked.
    """
    results: list[ScrapedPost | None] = [None] * len(posts)
    pending: dict[str, tuple[int, ScrapedPost]] = {}
    ambiguous_keys: set[str] = set()
    for index, post in enumerate(posts):
        key = _post_key(post)
        if key is None:
            results[index] = post.model_copy(update={"outcome": "skipped: no card identity available"})
        elif key in pending:
            previous_index, previous_post = pending.pop(key)
            ambiguous_keys.add(key)
            results[previous_index] = previous_post.model_copy(
                update={"outcome": "skipped: ambiguous feed identity"}
            )
            results[index] = post.model_copy(update={"outcome": "skipped: ambiguous feed identity"})
        elif key in ambiguous_keys:
            results[index] = post.model_copy(update={"outcome": "skipped: ambiguous feed identity"})
        else:
            pending[key] = (index, post)

    if pending:
        try:
            page.goto(selectors.FEED_URL, wait_until="domcontentloaded")
            human_delay(config.RateLimits.NAV_DELAY)
            dismiss_popup_if_present(page, timeout=config.Timeouts.POPUP_RECHECK_TIMEOUT_MS)
        except PlaywrightError as exc:
            logger.warning("[like_posts] could not reopen the feed: %s", exc)
            return [
                result
                if result is not None
                else post.model_copy(update={"outcome": f"failed: feed navigation error ({exc})"})
                for result, post in zip(results, posts)
            ]

    seen_card_keys: set[str] = set()
    stagnant_rounds = 0
    for attempt in range(config.Timeouts.LOAD_MORE_MAX_ATTEMPTS):
        if not pending:
            break

        seen_before = len(seen_card_keys)
        cards = selectors.feed_post_cards(page)
        for card_index in range(cards.count()):
            card = cards.nth(card_index)
            snapshot = inspect_post_card(card, card_index)
            if snapshot is None:
                continue
            key = snapshot.feed_match_key
            seen_card_keys.add(key)
            target = pending.pop(key, None)
            if target is None:
                continue

            post_index, post = target
            outcome = _like_card(page, card, key, post_index)
            results[post_index] = post.model_copy(update={"outcome": outcome})
            logger.info("[like_posts] post %d (%s): %s", post_index + 1, post.author, outcome)

        if not pending:
            break

        if len(seen_card_keys) == seen_before:
            stagnant_rounds += 1
            if stagnant_rounds >= config.Scraping.MAX_STAGNANT_LOAD_ATTEMPTS:
                logger.info(
                    "[like_posts] feed exhausted with %d selected post(s) still not found",
                    len(pending),
                )
                break
        else:
            stagnant_rounds = 0

        _load_more_step(page)

    for index, post in enumerate(posts):
        if results[index] is None:
            results[index] = post.model_copy(update={"outcome": "skipped: selected post not found again in feed"})

    final_results = [tp.cast(ScrapedPost, result) for result in results]

    liked_count = sum(1 for p in final_results if p.outcome == "liked")
    skipped_count = sum(1 for p in final_results if p.outcome.startswith("skipped:"))
    failed_count = sum(1 for p in final_results if p.outcome.startswith("failed:"))
    logger.info(
        "[like_posts] results: %d liked, %d already_liked, %d skipped, %d failed out of %d",
        liked_count,
        sum(1 for p in final_results if p.outcome == "already_liked"),
        skipped_count,
        failed_count,
        len(final_results),
    )
    return final_results
