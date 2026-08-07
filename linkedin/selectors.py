import re

from playwright.sync_api import Locator, Page

LOGIN_URL = "https://www.linkedin.com/login"
FEED_URL = "https://www.linkedin.com/feed/"
CHECKPOINT_URL_MARKERS = ("checkpoint/challenge", "checkpoint/rp", "checkpoint/lg")
# Checkpoints that are specifically the "we emailed you a code" flow — the only state a
# human can finish by typing into the window, and so the only one worth waiting on
# indefinitely. Every other checkpoint URL is unautomatable and must fail loudly.
EMAIL_VERIFICATION_URL_MARKERS = (
    "checkpoint/challenge/verify",
    "checkpoint/challenge/email",
    "checkpoint/challengesV2",
)


def email_input(page: Page) -> Locator:
    return page.locator('input[autocomplete="username webauthn"]:visible')


def password_input(page: Page) -> Locator:
    return page.locator('input[autocomplete="current-password"]:visible')


def sign_in_button(page: Page) -> Locator:
    return page.get_by_role("button", name="Sign in", exact=True)


def verification_pin_input(page: Page) -> Locator:
    return page.locator('input[name="pin"]')


def captcha_marker(page: Page) -> Locator:
    """Any sign of a captcha/bot check. A captcha page can *also* render a PIN field, so
    this is checked before the PIN field: entering a code will never clear a captcha, and
    waiting for a human to do so is waiting forever."""
    return page.locator(
        'iframe[src*="captcha" i], iframe[title*="captcha" i], iframe[src*="arkoselabs" i], '
        'iframe[src*="funcaptcha" i], #captcha-internal, [id*="captcha" i], [class*="captcha" i]'
    )


def login_error_text(page: Page) -> str:
    """Inline validation/error banner shown on a failed login (wrong password, etc).
    Returns the trimmed message, or '' if none is visible."""
    banner = page.locator(
        '[role="alert"]:visible, #error-for-password:visible, '
        "#error-for-username:visible, .form__label--error:visible"
    ).first
    try:
        if banner.count() > 0:
            return banner.inner_text(timeout=1000).strip()
    except Exception:
        pass
    return ""


def any_close_button(page: Page) -> Locator:
    dialog = page.locator('[role="dialog"]:visible')
    return (
        dialog.get_by_role("button", name="Dismiss")
        .or_(dialog.get_by_role("button", name="Close"))
        .or_(dialog.locator('button:has(svg[id^="close-"]):visible'))
    ).first


def feed_post_cards(page: Page) -> Locator:
    return page.locator('main div[role="listitem"]')


def post_text(post: Locator) -> Locator:
    return post.locator('[data-testid="expandable-text-box"]').first


def post_author_link(post: Locator) -> Locator:
    candidates = post.locator('a[href*="/in/"], a[href*="/company/"]')
    return candidates.filter(has_text=re.compile(r"\S")).last


def load_more_button(page: Page) -> Locator:
    return page.get_by_role("button", name="Load more", exact=True)


def like_button(post: Locator) -> Locator:
    return post.locator('button:has(svg[id^="thumbs-up-"])').first


def like_icon(post: Locator) -> Locator:
    return post.locator('svg[id^="thumbs-up-"]').first


# --- Engagement counts ----------------------------------------------------------
# LinkedIn serves more than one feed build, and they put the numbers in different
# places. The scraper detects which one it is looking at (scraper.detect_count_strategy)
# rather than assuming, because assuming is how counts silently become zeros.
#
#   "summary row"  — a "X reactions · Y comments" row above the action bar, keyed by
#                    semantic BEM classes. Here the action buttons carry no number, so
#                    everything is scoped to the row and can never read "Like" as 0.
#   "action bar"   — no summary row at all: each count is rendered *inside* its own
#                    action button next to the icon. This build hashes every class name
#                    (`_21c7e3d1`) and localizes every aria-label, so the only usable
#                    anchor is the icon's `id` — neither hashed nor translated.
SOCIAL_COUNTS_BAR = ".social-details-social-counts"

# Ordered most-specific-first; the caller reads them in order and takes the first
# candidate whose accessible label / text actually contains a number.
REACTIONS_COUNT_SELECTORS: tuple[str, ...] = (
    ".social-details-social-counts__reactions-count",
    ".social-details-social-counts__social-proof-fallback-number",
    ".social-details-social-counts__reactions button[aria-label]",
    ".social-details-social-counts__reactions",
    '[aria-label*="reaction" i]',
)

COMMENTS_COUNT_SELECTORS: tuple[str, ...] = (
    ".social-details-social-counts__comments button[aria-label]",
    ".social-details-social-counts__comments",
    '[aria-label*="comment" i]',
)


def social_counts_bar(post: Locator) -> Locator:
    """The "X reactions · Y comments" summary row. Absent on a post with no engagement
    at all, so callers must tell "no bar because nothing happened yet" apart from
    "no bar because the markup moved" — see `like_button` presence as the tie-breaker."""
    return post.locator(SOCIAL_COUNTS_BAR).first


def reactions_count_candidates(post: Locator) -> list[Locator]:
    bar = social_counts_bar(post)
    return [bar.locator(selector) for selector in REACTIONS_COUNT_SELECTORS]


def comments_count_candidates(post: Locator) -> list[Locator]:
    bar = social_counts_bar(post)
    return [bar.locator(selector) for selector in COMMENTS_COUNT_SELECTORS]


# The "action bar" build. Icon ids (thumbs-up-outline-small, comment-small, repost-small)
# survive both the class hashing and the UI language, which nothing else on these cards
# does. The count sits inside the button as plain text; the aria-label is the localized
# action name ("Коментувати"), so the number has to come from the text.
REACTIONS_ICON_PREFIX = "thumbs-up-"
COMMENTS_ICON_PREFIX = "comment-"


def action_bar_reactions_candidates(post: Locator) -> list[Locator]:
    return [post.locator(f'button:has(svg[id^="{REACTIONS_ICON_PREFIX}"])')]


def action_bar_comments_candidates(post: Locator) -> list[Locator]:
    return [post.locator(f'button:has(svg[id^="{COMMENTS_ICON_PREFIX}"])')]


# --- Post identity --------------------------------------------------------------
# The activity URN is the only globally unique post id the feed exposes. It sits on
# the card root on some builds and on the inner update container on others, and
# unrelated URNs (profiles, comments, images) sit on other descendants — so callers
# read the root's own attributes first, then descendants, and accept only values
# matching a post URN.
POST_URN_ATTRIBUTES: tuple[str, ...] = ("data-urn", "data-id")

POST_URN_HOLDER_SELECTORS: tuple[str, ...] = (
    ".feed-shared-update-v2[data-urn], .feed-shared-update-v2[data-id]",
    '[data-urn*="urn:li:activity"], [data-id*="urn:li:activity"]',
    '[data-urn*="urn:li:ugcPost"], [data-id*="urn:li:ugcPost"]',
    "[data-urn], [data-id]",
)


def post_urn_holders(post: Locator) -> list[Locator]:
    """Descendants that may carry the post's activity URN, most-specific-first. The
    card root is NOT included: Playwright locator searches are descendant-scoped, so
    the caller must read the root's attributes itself."""
    return [post.locator(selector) for selector in POST_URN_HOLDER_SELECTORS]


def post_timestamp_text(post: Locator) -> Locator:
    """Relative age ("2h", "1d • Edited"). Weak alone, useful as one component of the
    fallback identity used when no URN is exposed."""
    return post.locator(".update-components-actor__sub-description, time").first


def post_media_sources(post: Locator) -> Locator:
    """Image/video/link targets on the card. These are what tell two text-less image
    posts by the same author apart when there is no URN to key on."""
    return post.locator('img[src], video[src], a[href^="https://"]')
