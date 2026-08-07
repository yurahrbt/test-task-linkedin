import logging
from dataclasses import dataclass

from playwright.sync_api import Browser, Page, Playwright

from linkedin import selectors
from linkedin.auth import get_authenticated_context
from utils.popup_handler import dismiss_popup_if_present

logger = logging.getLogger(__name__)


@dataclass
class FeedSession:
    """Authenticated browser session used by one run.

    The browser owns the context and page, so closing the browser is the single
    cleanup operation callers need to perform.
    """

    browser: Browser
    page: Page

    def close(self) -> None:
        self.browser.close()


def open_feed(playwright: Playwright) -> FeedSession:
    browser, context = get_authenticated_context(playwright)
    page = context.pages[0] if context.pages else context.new_page()
    page.goto(selectors.FEED_URL)
    dismiss_popup_if_present(page)
    logger.info("[open_feed] authenticated, feed loaded at %s", page.url)
    return FeedSession(browser=browser, page=page)
