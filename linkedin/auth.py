import enum
import logging
import time

from playwright.sync_api import Browser, BrowserContext, Page, Playwright, TimeoutError as PlaywrightTimeoutError

import config
from linkedin import selectors
from linkedin.errors import LoginCheckpointError, LoginError, LoginFailedError
from utils.rate_limiter import human_delay
from utils.retry import retry

logger = logging.getLogger(__name__)


class LoginState(enum.Enum):
    """What LinkedIn showed us after the sign-in form was submitted."""

    FEED = "feed"
    EMAIL_VERIFICATION = "email-verification"  # known code prompt: a human can finish it
    PIN_CHALLENGE = "pin-challenge"  # code field on a URL we do not recognize
    CAPTCHA = "captcha"  # bot check: unautomatable, and no code will clear it
    CHECKPOINT = "checkpoint"  # some other challenge we cannot drive
    UNKNOWN = "unknown"  # still on the form, or somewhere unexpected


def classify_login_state(page: Page) -> LoginState:
    """Classify the post-submit page by URL *and* content, in the order that matters.

    A captcha page can contain a PIN field, so "there is a PIN field" is not evidence
    that a human can finish this flow — it is checked only after the bot check and only
    on a URL that means what we think it means. Getting this order wrong is the
    difference between failing loudly and blocking on a window nobody can complete."""
    url = page.url
    if url.startswith(selectors.FEED_URL):
        return LoginState.FEED

    if selectors.captcha_marker(page).count() > 0:
        return LoginState.CAPTCHA

    if selectors.verification_pin_input(page).count() > 0:
        if any(marker in url for marker in selectors.EMAIL_VERIFICATION_URL_MARKERS):
            return LoginState.EMAIL_VERIFICATION
        return LoginState.PIN_CHALLENGE

    if any(marker in url for marker in selectors.CHECKPOINT_URL_MARKERS):
        return LoginState.CHECKPOINT

    return LoginState.UNKNOWN


def _storage_state_is_fresh() -> bool:
    if not config.Paths.STORAGE_STATE_PATH.exists():
        return False
    age_hours = (time.time() - config.Paths.STORAGE_STATE_PATH.stat().st_mtime) / 3600
    return age_hours < config.settings.storage_state_max_age_hours


def _force_english_locale(context: BrowserContext) -> None:
    context.add_cookies(
        [
            {
                "name": "lang",
                "value": config.Browser.LINKEDIN_LANG_COOKIE_VALUE,
                "domain": config.Browser.LINKEDIN_COOKIE_DOMAIN,
                "path": "/",
            }
        ]
    )


def _new_context(browser: Browser, storage_state: str | None = None) -> BrowserContext:
    context = browser.new_context(
        storage_state=storage_state,
        locale=config.Browser.LOCALE,
        extra_http_headers={"Accept-Language": config.Browser.ACCEPT_LANGUAGE_HEADER},
    )
    _force_english_locale(context)
    return context


def _is_authenticated(context: BrowserContext) -> bool:
    page = context.new_page()
    try:
        page.goto(selectors.FEED_URL, timeout=config.Timeouts.FEED_LOAD_TIMEOUT_MS)
        return page.url.startswith(selectors.FEED_URL)
    except PlaywrightTimeoutError:
        return False
    finally:
        page.close()


@retry(
    max_attempts=config.Timeouts.LOGIN_RETRY_MAX_ATTEMPTS,
    backoff=config.Timeouts.LOGIN_RETRY_BACKOFF,
    exceptions=(PlaywrightTimeoutError,),
)
def _submit_login_form(page) -> None:
    page.goto(selectors.LOGIN_URL)
    selectors.email_input(page).fill(config.settings.linkedin_email)
    selectors.password_input(page).fill(config.settings.linkedin_password)
    human_delay(config.RateLimits.NAV_DELAY)
    selectors.sign_in_button(page).click()
    page.wait_for_load_state("domcontentloaded")


def _await_manual_code_entry(page: Page, state: LoginState) -> None:
    """Hand the window to the human for a verification code.

    Only the recognized email-code state waits without a deadline. A PIN field on a URL
    we do not recognize gets a bounded wait instead: it may be a code prompt, but it may
    equally be a challenge no amount of typing will clear, and a run that blocks forever
    is indistinguishable from a hung worker."""
    if config.settings.headless:
        raise LoginFailedError(
            f"LinkedIn requires a verification code at {page.url}, but the browser is running "
            "headless so there is no window to enter it in. Re-run with HEADLESS=false to complete "
            "the code prompt interactively."
        )

    if state is LoginState.EMAIL_VERIFICATION:
        logger.warning(
            "[_perform_login] LinkedIn is asking for a verification code at %s. "
            "Enter it in the open browser window — waiting for you to complete it (no timeout).",
            page.url,
        )
        page.wait_for_url(f"{selectors.FEED_URL}*", timeout=0)
        return

    timeout_ms = config.Timeouts.UNRECOGNIZED_CHALLENGE_WAIT_MS
    logger.warning(
        "[_perform_login] unrecognized challenge with a code field at %s. If it is a code prompt, "
        "enter it in the open browser window within %.0fs — otherwise this run gives up.",
        page.url,
        timeout_ms / 1000,
    )
    try:
        page.wait_for_url(f"{selectors.FEED_URL}*", timeout=timeout_ms)
    except PlaywrightTimeoutError:
        raise LoginCheckpointError(
            f"LinkedIn presented an unrecognized challenge at {page.url} that was not completed "
            f"within {timeout_ms / 1000:.0f}s. Resolve it manually in a normal browser and re-run."
        ) from None


def _perform_login(context: BrowserContext) -> None:
    page = context.new_page()
    _submit_login_form(page)

    state = classify_login_state(page)
    logger.info("[_perform_login] post-submit state: %s (%s)", state.value, page.url)

    if state is LoginState.CAPTCHA:
        raise LoginCheckpointError(
            f"LinkedIn presented a captcha / bot check at {page.url}. No verification code will "
            "clear this — solve it manually in a normal browser and re-run."
        )

    if state is LoginState.CHECKPOINT:
        raise LoginCheckpointError(
            f"LinkedIn presented a checkpoint/2FA challenge at {page.url}. "
            "Automated login cannot proceed past this — resolve manually and re-run."
        )

    if state in (LoginState.EMAIL_VERIFICATION, LoginState.PIN_CHALLENGE):
        _await_manual_code_entry(page, state)
    elif state is LoginState.UNKNOWN:
        try:
            page.wait_for_url(f"{selectors.FEED_URL}*", timeout=config.Timeouts.FEED_LOAD_TIMEOUT_MS)
        except PlaywrightTimeoutError:
            # Never reached the feed and no code/checkpoint was shown — almost always wrong
            # credentials or a soft block. Fail with an actionable message instead of leaking
            # a raw PlaywrightTimeoutError out of the pipeline.
            error_text = selectors.login_error_text(page)
            detail = f" LinkedIn said: {error_text!r}." if error_text else ""
            raise LoginFailedError(
                f"Login did not reach the feed (still at {page.url}).{detail} "
                "Check LINKEDIN_EMAIL / LINKEDIN_PASSWORD in .env."
            ) from None

    context.storage_state(path=str(config.Paths.STORAGE_STATE_PATH))
    page.close()


def _get_authenticated_context(browser: Browser) -> BrowserContext:
    if _storage_state_is_fresh():
        logger.info("[_get_authenticated_context] reusing cached storage_state.json")
        context = _new_context(browser, storage_state=str(config.Paths.STORAGE_STATE_PATH))
        if _is_authenticated(context):
            return context
        logger.warning("[_get_authenticated_context] cached session is stale/invalid, logging in fresh")
        context.close()

    logger.info("[_get_authenticated_context] no valid cached session, logging in")
    context = _new_context(browser)
    _perform_login(context)
    return context


def get_authenticated_context(playwright: Playwright) -> tuple[Browser, BrowserContext]:
    browser = playwright.chromium.launch(headless=config.settings.headless, args=config.Browser.ARGS)

    try:
        context = _get_authenticated_context(browser)
    except Exception:
        browser.close()
        raise

    return browser, context
