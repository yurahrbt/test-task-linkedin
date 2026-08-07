import hashlib
import logging
import math
import re
import typing as tp
from collections import Counter
from urllib.parse import unquote, urlsplit, urlunsplit

from playwright.sync_api import Error as PlaywrightError, Locator, Page
from pydantic import BaseModel

import config
from linkedin import selectors
from linkedin.errors import SelectorDriftError
from utils.popup_handler import dismiss_popup_if_present
from utils.rate_limiter import human_delay

logger = logging.getLogger(__name__)

# A run of digits plus whatever group/decimal separators are glued to it: "1,234",
# "1.234", "1 234" (fr, with a non-breaking or thin space), "1.2".
_COUNT_TOKEN = re.compile(r"\d[\d.,\u00a0\u202f\u2009 ]*")
# The magnitude word right after the number, ending at a word boundary so
# "42 Kommentare" is not read as "42 k" = 42 000.
_MAGNITUDE_SUFFIX = re.compile(r"[a-zA-Z]{1,3}(?![a-zA-Z])")
_MAGNITUDES = {
    "k": 1_000,
    "tsd": 1_000,  # de
    "mil": 1_000,  # es / pt
    "m": 1_000_000,
    "mn": 1_000_000,
    "mio": 1_000_000,
    "b": 1_000_000_000,
    "mrd": 1_000_000_000,
}
_SPACES = ("\u00a0", "\u202f", "\u2009", " ")  # nbsp, narrow nbsp, thin space, plain

# The URN shapes that identify a *post*, anchored end to end. The feed is full of other
# URNs (urn:li:fsd_profile, urn:li:digitalmediaAsset, \u2026) and keying on one of those would
# hand two different posts the same identity. Anchoring matters as much as the type list:
# a comment's URN embeds its parent post ("urn:li:comment:(urn:li:activity:9,7)"), so an
# unanchored search would happily key a card on some other card's activity id.
_POST_URN_PATTERN = re.compile(r"urn:li:(?:activity|ugcPost|share):\d+")
# Aggregated updates ("X and 3 others posted") wrap the real post URN.
_AGGREGATE_URN_PATTERN = re.compile(r"urn:li:aggregate:\((urn:li:(?:activity|ugcPost|share):\d+)[,)]")

# Patterns to extract a post URN from <a href> URLs on the card.
_FEED_UPDATE_URL_RE = re.compile(
    r"/feed/update/(urn:li:(?:activity|ugcPost|share):\d+)"
)
_POST_SLUG_ACTIVITY_RE = re.compile(
    r"/posts/[^?#/]+[_-]activity-(\d+)"
)


class ScrapedPost(BaseModel):
    author: str
    text: str
    # None means "could not be read", which is deliberately not the same value as 0.
    # Such a post is never liked and never ranked.
    likes_count: int | None
    comments_count: int | None
    engagement_ratio: float | None
    engagement_score: float | None
    outcome: str
    urn: str | None = None
    # Stable within a run even on feed variants that expose no activity URN.  The
    # liking stage uses it to find the ranked card again in the live feed.
    identity_key: str | None = None
    # Reload-stable identity used to re-find the card in the feed. Unlike the
    # de-dup fingerprint, it deliberately excludes relative timestamps.
    feed_match_key: str | None = None


class CountReading(tp.NamedTuple):
    """A count plus why it has the value it has, so a run-level check can tell a feed
    of genuinely quiet posts apart from a selector that stopped matching."""

    value: int | None
    source: str


# CountReading.source values.
SOURCE_READ = "read"  # a number came off the control
SOURCE_NO_CONTROL = "no-control"  # control absent on a card whose social region rendered: 0
SOURCE_NO_NUMBER = "no-number"  # control there, showing no number: 0
SOURCE_NO_SOCIAL_REGION = "no-social-region"  # no social region at all: unknown
SOURCE_UNREADABLE = "unreadable"  # the read itself failed: unknown


class CountStrategy(tp.NamedTuple):
    """Where a given feed build keeps its engagement numbers. Which one is in play is
    detected against the live feed, never assumed."""

    name: str
    reactions: tp.Callable[[Locator], list[Locator]]
    comments: tp.Callable[[Locator], list[Locator]]


COUNT_STRATEGIES: tuple[CountStrategy, ...] = (
    CountStrategy("summary-row", selectors.reactions_count_candidates, selectors.comments_count_candidates),
    CountStrategy("action-bar", selectors.action_bar_reactions_candidates, selectors.action_bar_comments_candidates),
)


def _parse_count(text: str) -> int | None:
    """Parse an engagement count out of an accessible label or visible text.

    Returns None when the text holds no number at all (e.g. the bare word "Comment"
    from an action button), which callers treat as a scrape error rather than zero.
    """
    if not text:
        return None

    match = _COUNT_TOKEN.search(text)
    if not match:
        return None

    token = match.group(0)
    for space in _SPACES:  # thousands separators in fr/ru/sv locales
        token = token.replace(space, "")
    token = token.rstrip(".,")
    if not token:
        return None

    suffix_match = _MAGNITUDE_SUFFIX.match(text[match.end() :].lstrip("".join(_SPACES)))
    multiplier = _MAGNITUDES.get(suffix_match.group(0).lower(), 1) if suffix_match else 1

    try:
        number = _to_number(token)
    except ValueError:
        return None

    return int(round(number * multiplier))


def _to_number(token: str) -> float:
    """Interpret ',' and '.' in a digit token without knowing the locale.

    "1,234"/"1.234" are 1234 (three trailing digits = grouping), "1.2"/"1,2" are 1.2
    (decimal), and when both separators appear the last one is the decimal point.
    """
    last_comma = token.rfind(",")
    last_dot = token.rfind(".")
    last_sep = max(last_comma, last_dot)
    if last_sep == -1:
        return float(token)

    separator = token[last_sep]
    fraction_digits = len(token) - last_sep - 1
    is_grouping = (
        fraction_digits == 3
        or token.count(separator) > 1
        or (last_comma != -1 and last_dot != -1 and last_sep == min(last_comma, last_dot))
    )
    if is_grouping:
        return float(token.replace(",", "").replace(".", ""))

    whole = token[:last_sep].replace(",", "").replace(".", "")
    return float(f"{whole}.{token[last_sep + 1 :]}")


def _read_number(element: Locator) -> int | None:
    """First number found in the element's accessible label or its text.

    Both are tried because the builds disagree: the summary row puts the count in
    aria-label ("42 comments") and hides the visible span from assistive tech, while the
    action-bar build puts the localized action name in aria-label ("Коментувати") and
    the number in the text. Preferring one over the other loses half the feeds."""
    timeout = config.Timeouts.ELEMENT_ACTION_TIMEOUT_MS
    for source in (
        element.get_attribute("aria-label", timeout=timeout),
        element.inner_text(timeout=timeout),
        element.text_content(timeout=timeout),
    ):
        value = _parse_count(source or "")
        if value is not None:
            return value
    return None


def _extract_count(
    card: Locator,
    candidates_fn: tp.Callable[[Locator], list[Locator]],
    label: str,
    index: int,
) -> CountReading:
    """Read one engagement number off the control the detected layout says holds it,
    keeping a real zero distinguishable from a number we could not read."""
    try:
        matched_any = False
        for locator in candidates_fn(card):
            for element in locator.all()[: config.Scraping.COUNT_CANDIDATE_LIMIT]:
                matched_any = True
                value = _read_number(element)
                if value is not None:
                    return CountReading(value, SOURCE_READ)

        if matched_any:
            # The control is there and shows nothing — on a layout already confirmed to
            # render numbers (see detect_count_strategy), that is a genuine zero.
            return CountReading(0, SOURCE_NO_NUMBER)

        if selectors.like_button(card).count() > 0:
            # No control for this metric, but the card's social region did render: the
            # normal shape for a post with no comments (or no engagement at all) yet.
            return CountReading(0, SOURCE_NO_CONTROL)

        logger.warning("[_extract_count] card %d, no social region at all: cannot read %s", index, label)
        return CountReading(None, SOURCE_NO_SOCIAL_REGION)
    except PlaywrightError as exc:
        logger.warning("[_extract_count] card %d, reading %s failed: %s", index, label, exc)
        return CountReading(None, SOURCE_UNREADABLE)


def _extract_author(card: Locator, index: int) -> str:
    author_link = selectors.post_author_link(card)
    # `inner_text()` waits for the full action timeout when the locator matches
    # nothing.  Ads/widgets are stable non-post list items, so reject them with the
    # immediate count check instead of paying that timeout on every scroll round.
    if author_link.count() == 0:
        return ""
    author_raw = author_link.inner_text(timeout=config.Timeouts.ELEMENT_ACTION_TIMEOUT_MS)
    author_lines = author_raw.strip().splitlines()
    return author_lines[0].strip() if author_lines else ""


def _extract_text(card: Locator) -> str:
    text_locator = selectors.post_text(card)
    if text_locator.count() == 0:
        return ""
    return text_locator.inner_text(timeout=config.Timeouts.ELEMENT_ACTION_TIMEOUT_MS).strip()


def _post_urn(card: Locator) -> str | None:
    """The card's activity URN, or None if the feed does not expose one.

    Checked in order: the card root's own attributes (a Playwright locator only ever
    matches descendants, so the root needs an explicit read), then known descendants.
    Only values that look like a post URN are accepted — the actor block, comments and
    images all carry URNs of their own, and keying on one of those would give two
    different posts the same identity."""
    timeout = config.Timeouts.ELEMENT_ACTION_TIMEOUT_MS

    for attribute in selectors.POST_URN_ATTRIBUTES:
        urn = _as_post_urn(card.get_attribute(attribute, timeout=timeout))
        if urn:
            return urn

    for locator in selectors.post_urn_holders(card):
        values = locator.evaluate_all(
            "(nodes, attrs) => nodes.slice(0, 5).flatMap(n => attrs.map(a => n.getAttribute(a)))",
            list(selectors.POST_URN_ATTRIBUTES),
        )
        for value in values:
            urn = _as_post_urn(value)
            if urn:
                return urn

    # Fallback: extract a post URN from a direct permalink on the card.
    urn = _extract_urn_from_links(card)
    if urn:
        return urn

    return None


def _extract_urn_from_links(card: Locator) -> str | None:
    """Extract a post URN only from a direct post permalink on the card."""
    try:
        hrefs = card.locator("a[href]").evaluate_all(
            "nodes => nodes.slice(0, 30).map(n => n.getAttribute('href') || '')"
        )
    except PlaywrightError:
        return None

    for raw_href in hrefs:
        # URL-decode so percent-encoded URNs (urn%3Ali%3A…) become matchable.
        href = unquote(raw_href)

        match = _FEED_UPDATE_URL_RE.search(href)
        if match:
            return match.group(1)
        match = _POST_SLUG_ACTIVITY_RE.search(href)
        if match:
            return f"urn:li:activity:{match.group(1)}"
        # Do not accept arbitrary query-parameter URNs. Group-highlight links can
        # reference a different update than the card and produced false targets in
        # real runs. Only direct post permalinks above are trustworthy identities.


def _as_post_urn(value: str | None) -> str | None:
    """Normalize an attribute value to a bare post URN, or None if it identifies
    something that is not a post (a profile, a comment, a media asset)."""
    if not value:
        return None

    candidate = value.strip()
    if _POST_URN_PATTERN.fullmatch(candidate):
        return candidate

    aggregate = _AGGREGATE_URN_PATTERN.match(candidate)
    return aggregate.group(1) if aggregate else None


def _card_fingerprint(card: Locator, author: str, text: str) -> str:
    """Fallback identity for a card with no URN. Author + text alone collapses two
    genuinely different posts — most obviously two text-less image posts by the same
    author — so the age label and the card's media/link targets are mixed in too.

    This is a *within-run* de-dup key, not a globally unique post id: it exists to stop
    the virtualized feed handing us the same card twice, and a collision costs a skipped
    post rather than a double like."""
    parts = [author, text]
    try:
        timestamp = selectors.post_timestamp_text(card)
        if timestamp.count() > 0:
            parts.append(timestamp.inner_text(timeout=config.Timeouts.ELEMENT_ACTION_TIMEOUT_MS))
    except PlaywrightError:
        pass

    try:
        media = selectors.post_media_sources(card).evaluate_all(
            "nodes => nodes.slice(0, 4).map(n => n.getAttribute('src') || n.getAttribute('href') || '')"
        )
    except PlaywrightError:
        media = []
    parts.extend(media)

    digest = hashlib.sha1("\x00".join(parts).encode("utf-8")).hexdigest()
    return f"fingerprint:{digest}"


class CardIdentity(tp.NamedTuple):
    key: str
    source: str  # SOURCE_URN (globally unique) or SOURCE_FINGERPRINT (best effort)


SOURCE_URN = "urn"
SOURCE_FINGERPRINT = "fingerprint"


class CardSnapshot(tp.NamedTuple):
    """The identifying content needed by both scraping and in-feed liking."""

    author: str
    text: str
    identity: CardIdentity
    feed_match_key: str


def _feed_match_key(card: Locator, author: str, text: str) -> str:
    """Build a card key that remains stable across a feed reload.

    Relative timestamps can roll from ``59m`` to ``1h`` and signed media URLs can
    rotate query strings, so neither is included verbatim in an action target.
    """
    try:
        raw_sources = selectors.post_media_sources(card).evaluate_all(
            "nodes => nodes.slice(0, 4).map(n => n.getAttribute('src') || n.getAttribute('href') || '')"
        )
    except PlaywrightError:
        raw_sources = []

    stable_sources: list[str] = []
    for source in raw_sources:
        try:
            parsed = urlsplit(source)
            stable_sources.append(urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", "")))
        except (TypeError, ValueError):
            stable_sources.append(str(source))

    parts = [author, text, *stable_sources]
    digest = hashlib.sha1("\x00".join(parts).encode("utf-8")).hexdigest()
    return f"feed-match:{digest}"


def _card_key(card: Locator, author: str, text: str, index: int) -> CardIdentity:
    """Stable identity for a feed card, resilient to the virtualized feed reordering or
    recycling DOM nodes."""
    try:
        urn = _post_urn(card)
    except PlaywrightError as exc:
        logger.warning("[_card_key] card %d, reading the post URN failed: %s", index, exc)
        urn = None

    if urn:
        return CardIdentity(urn, SOURCE_URN)

    logger.debug(
        "[_card_key] card %d (%s): no activity URN on the card or its descendants, "
        "falling back to a content fingerprint",
        index,
        author,
    )
    return CardIdentity(_card_fingerprint(card, author, text), SOURCE_FINGERPRINT)


def inspect_post_card(card: Locator, index: int) -> CardSnapshot | None:
    """Read a feed card without waiting on list items that are not posts."""
    # A real actionable feed post must expose both an author and a Like control.
    # These count checks do not wait, unlike inner_text/click operations.
    if selectors.like_button(card).count() == 0:
        return None

    try:
        author = _extract_author(card, index)
        if not author:
            return None
        text = _extract_text(card)
        identity = _card_key(card, author, text, index)
    except PlaywrightError as exc:
        logger.warning("[inspect_post_card] skipping card %d, inspection failed: %s", index, exc)
        return None

    return CardSnapshot(author, text, identity, _feed_match_key(card, author, text))


def _scrape_single_post(
    card: Locator,
    index: int,
    seen_keys: set[str],
    strategy: CountStrategy,
    run_stats: Counter[str] | None = None,
) -> ScrapedPost | None:
    snapshot = inspect_post_card(card, index)
    if snapshot is None:
        if run_stats is not None:
            run_stats["cards:non-post"] += 1
        return None

    author, text, identity, feed_match_key = snapshot

    # De-dup BEFORE any side-effecting action (like) so a recycled/re-seen card is
    # never liked or scraped twice.
    if run_stats is not None:
        run_stats[f"identity:{identity.source}"] += 1
    if identity.key in seen_keys:
        logger.debug("[_scrape_single_post] skipping card %d, already processed (%s)", index, identity.key)
        return None
    seen_keys.add(identity.key)

    likes = _extract_count(card, strategy.reactions, "reactions", index)
    comments = _extract_count(card, strategy.comments, "comments", index)
    if run_stats is not None:
        run_stats[f"reactions:{likes.source}"] += 1
        run_stats[f"comments:{comments.source}"] += 1

    if likes.value is None or comments.value is None:
        # Engagement is unreadable for this card: ranking it would be a guess and
        # liking it would be a side effect taken on that guess. Record it and move on.
        logger.warning(
            "[_scrape_single_post] card %d (%s): engagement unreadable (reactions=%s, comments=%s), "
            "not liking or ranking it",
            index,
            author,
            likes.source,
            comments.source,
        )
        return ScrapedPost(
            author=author,
            text=text,
            likes_count=likes.value,
            comments_count=comments.value,
            engagement_ratio=None,
            engagement_score=None,
            outcome="skipped: engagement counts unreadable",
            urn=identity.key if identity.source == SOURCE_URN else None,
            identity_key=identity.key,
            feed_match_key=feed_match_key,
        )

    engagement_ratio = comments.value / max(likes.value, 1)
    engagement_score = comments.value / math.sqrt(max(likes.value, 1))
    urn = identity.key if identity.source == SOURCE_URN else None

    return ScrapedPost(
        author=author,
        text=text,
        likes_count=likes.value,
        comments_count=comments.value,
        engagement_ratio=engagement_ratio,
        engagement_score=engagement_score,
        outcome="collected",
        urn=urn,
        identity_key=identity.key,
        feed_match_key=feed_match_key,
    )


def _load_more_step(page: Page) -> None:
    load_more = selectors.load_more_button(page)
    if load_more.count() > 0:
        try:
            load_more.first.click(timeout=config.Timeouts.ELEMENT_ACTION_TIMEOUT_MS)
        except PlaywrightError as exc:
            logger.warning("[_load_more_step] load-more click failed: %s", exc)
    else:
        page.mouse.wheel(0, config.Timeouts.SCROLL_PIXELS)

    human_delay(config.RateLimits.SCROLL_DELAY)
    dismiss_popup_if_present(page, timeout=config.Timeouts.POPUP_RECHECK_TIMEOUT_MS)


def detect_count_strategy(page: Page) -> CountStrategy:
    """Work out where this feed build keeps its engagement numbers, before anything is
    liked, by reading real numbers off real cards.

    A strategy is only accepted once it has produced an actual number for both reactions
    and comments from sampled posts; that is what later licenses treating an empty
    control as a zero. If either metric cannot be verified, the run stops — reporting a
    feed of zeros would rank the wrong posts and still like and comment on them."""
    cards = selectors.feed_post_cards(page)
    card_count = cards.count()
    if card_count == 0:
        raise SelectorDriftError(
            "no feed cards matched at all — check feed_post_cards() in dom_selectors.py "
            "(or the session may have been redirected away from the feed)"
        )

    # Only cards with a like button: `main div[role="listitem"]` also picks up
    # suggestion/ad cards that never carry engagement numbers.
    sampled = []
    for i in range(card_count):
        if len(sampled) >= config.Scraping.PREFLIGHT_CARD_SAMPLE:
            break
        card = cards.nth(i)
        if selectors.like_button(card).count() > 0:
            sampled.append((i, card))

    if not sampled:
        raise SelectorDriftError(
            f"none of the {card_count} feed cards exposed a like button — the post markup "
            "has moved; check like_button() in dom_selectors.py"
        )

    diagnostics: dict[str, str] = {}
    for strategy in COUNT_STRATEGIES:
        numbers_by_metric = {"reactions": 0, "comments": 0}
        controls_by_metric = {"reactions": 0, "comments": 0}
        for index, card in sampled:
            for metric, candidates_fn in (("reactions", strategy.reactions), ("comments", strategy.comments)):
                reading = _extract_count(card, candidates_fn, f"{strategy.name}/{metric}", index)
                numbers_by_metric[metric] += reading.source == SOURCE_READ
                controls_by_metric[metric] += reading.source in (SOURCE_READ, SOURCE_NO_NUMBER)

        diagnostics[strategy.name] = (
            f"reactions={numbers_by_metric['reactions']} number(s), "
            f"comments={numbers_by_metric['comments']} number(s) "
            f"from {controls_by_metric['reactions']}+{controls_by_metric['comments']} control(s)"
        )
        # Accept the strategy when a control was found for both metrics.  A quiet
        # feed where every post has zero comments still exposes the control element
        # (SOURCE_NO_NUMBER); requiring an explicit number (SOURCE_READ) would
        # reject such feeds even though the extraction logic correctly treats the
        # absent number as zero.
        if all(controls_by_metric.values()):
            logger.info(
                "[detect_count_strategy] using the %r layout (%s across %d sampled posts)",
                strategy.name,
                diagnostics[strategy.name],
                len(sampled),
            )
            return strategy

    raise SelectorDriftError(
        f"both reactions and comments controls could not be verified from the first "
        f"{len(sampled)} posts using any known feed layout ({diagnostics}). Every number "
        "this run would report is unverifiable — capture a feed card and refresh the "
        "count selectors in selectors.py."
    )


def _log_run_stats(run_stats: Counter[str], post_count: int) -> None:
    """Flag a metric that never once produced a number. Catches the half-broken case
    ("reactions work, comments are all zero") that a both-counts-zero check misses."""
    logger.info("[scrape_feed_posts] scrape sources: %s", dict(run_stats))

    fingerprinted = run_stats[f"identity:{SOURCE_FINGERPRINT}"]
    if fingerprinted:
        logger.warning(
            "[scrape_feed_posts] %d of %d cards exposed no activity URN and were de-duplicated "
            "by content fingerprint instead — that key is best-effort, so a repeated post may "
            "have been skipped. Check POST_URN_ATTRIBUTES / post_urn_holders() in dom_selectors.py.",
            fingerprinted,
            fingerprinted + run_stats[f"identity:{SOURCE_URN}"],
        )

    if post_count < config.Scraping.MIN_CARDS_FOR_DRIFT_WARNING:
        return

    for metric in ("reactions", "comments"):
        if run_stats[f"{metric}:{SOURCE_READ}"] == 0:
            logger.warning(
                "[scrape_feed_posts] %s was never read off a control across %d posts "
                "(sources: %s) — treat the %s ranking input as unverified and re-check "
                "the %s selectors in dom_selectors.py.",
                metric,
                post_count,
                {k: v for k, v in run_stats.items() if k.startswith(metric)},
                metric,
                metric.upper(),
            )


def scrape_feed_posts(page: Page, limit: int) -> list[ScrapedPost]:
    posts: list[ScrapedPost] = []
    seen_keys: set[str] = set()
    run_stats: Counter[str] = Counter()
    eligible_count = 0
    stagnant_rounds = 0

    # Detect (and prove) where the counts live before the first like, not after the last.
    strategy = detect_count_strategy(page)
    run_stats[f"layout:{strategy.name}"] += 1

    for attempt in range(config.Timeouts.LOAD_MORE_MAX_ATTEMPTS):
        # The feed is virtualized: cards.count() is not monotonic and a positional
        # index does not identify a stable post across scrolls. Re-scan every currently
        # attached card each round; _scrape_single_post de-dups by stable key so
        # re-seeing a card is cheap and never double-processes it.
        cards = selectors.feed_post_cards(page)
        card_count = cards.count()
        identities_before = len(seen_keys)

        for i in range(card_count):
            # Unreadable cards are retained for transparent output but are not eligible
            # for liking or ranking. Keep loading until we have `limit` eligible posts,
            # rather than stopping early after a fixed number of inspected cards.
            if eligible_count >= limit:
                break
            post = _scrape_single_post(cards.nth(i), i, seen_keys, strategy, run_stats)
            if post is not None:
                posts.append(post)
                if post.engagement_score is not None:
                    eligible_count += 1

        if eligible_count >= limit:
            break

        new_identities = len(seen_keys) - identities_before
        if new_identities == 0:
            stagnant_rounds += 1
            logger.info(
                "[scrape_feed_posts] load round %d found no new post identities (%d/%d stagnant)",
                attempt + 1,
                stagnant_rounds,
                config.Scraping.MAX_STAGNANT_LOAD_ATTEMPTS,
            )
            if stagnant_rounds >= config.Scraping.MAX_STAGNANT_LOAD_ATTEMPTS:
                logger.info(
                    "[scrape_feed_posts] feed exhausted: stopping after %d consecutive "
                    "rounds without a new post",
                    stagnant_rounds,
                )
                break
        else:
            stagnant_rounds = 0

        _load_more_step(page)

    unreadable = [p for p in posts if p.engagement_score is None]
    if posts and len(unreadable) / len(posts) > config.Scraping.MAX_UNREADABLE_COUNT_RATIO:
        raise SelectorDriftError(
            f"engagement counts were unreadable on {len(unreadable)} of {len(posts)} posts "
            f"(sources: {dict(run_stats)}) — refresh the social-count selectors in "
            "dom_selectors.py against current LinkedIn markup"
        )
    if unreadable:
        logger.warning(
            "[scrape_feed_posts] %d of %d posts had unreadable engagement counts; they were "
            "neither liked nor ranked",
            len(unreadable),
            len(posts),
        )
    if eligible_count < limit:
        logger.warning(
            "[scrape_feed_posts] found only %d eligible post(s) after %d load attempts; "
            "the feed may have been exhausted",
            eligible_count,
            config.Timeouts.LOAD_MORE_MAX_ATTEMPTS,
        )

    _log_run_stats(run_stats, len(posts))
    logger.info("[scrape_feed_posts] collected %d posts (%d unique cards seen)", len(posts), len(seen_keys))
    return posts
