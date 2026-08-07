"""Errors raised while authenticating or scraping LinkedIn."""


class LoginError(Exception):
    """Base class for unrecoverable login failures."""


class LoginCheckpointError(LoginError):
    """LinkedIn presented a captcha or checkpoint the script cannot automate."""


class LoginFailedError(LoginError):
    """Login did not reach the feed and no interactive challenge was detected."""


class SelectorDriftError(RuntimeError):
    """LinkedIn markup no longer matches the scraper's expected structure."""
