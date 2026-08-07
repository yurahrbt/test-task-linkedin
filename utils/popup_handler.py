import logging

from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError

import config
from linkedin import selectors

logger = logging.getLogger(__name__)


def dismiss_popup_if_present(page: Page, timeout: int = config.Timeouts.POPUP_DISMISS_TIMEOUT_MS) -> bool:
    try:
        btn = selectors.any_close_button(page)
        btn.wait_for(state="visible", timeout=timeout)
        btn.click()
        logger.info("[dismiss_popup_if_present] dismissed a popup")
        return True
    except PlaywrightTimeoutError:
        return False
