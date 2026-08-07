from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=BASE_DIR / ".env", extra="ignore")

    linkedin_email: str
    linkedin_password: str
    openai_api_key: str = ""
    headless: bool = False
    storage_state_max_age_hours: float = 12.0


settings = Settings()


class Paths:
    LOG_DIR = BASE_DIR / "logs"
    DATA_DIR = BASE_DIR / "data"
    STORAGE_STATE_PATH = DATA_DIR / "storage_state.json"
    FEED_POSTS_PATH = DATA_DIR / "feed_posts.json"
    RANKED_POSTS_PATH = DATA_DIR / "ranked.json"
    DRAFTS_PATH = DATA_DIR / "drafts.json"
    POSTS_HISTORY_PATH = DATA_DIR / "posts_history.json"


def ensure_dirs() -> None:
    """Create runtime output directories. Called explicitly from main() so that
    merely importing `config` (e.g. from tests) has no filesystem side effects."""
    Paths.DATA_DIR.mkdir(parents=True, exist_ok=True)
    Paths.LOG_DIR.mkdir(parents=True, exist_ok=True)


class RateLimits:
    LIKE_DELAY = (2, 5)
    SCROLL_DELAY = (1, 3)
    NAV_DELAY = (2, 4)


class Ranking:
    COLLECT_POOL_SIZE = 20
    TOP_LIKE_COUNT = 10
    RANKED_TOP_N = 3
    # Posts shorter than this (after stripping whitespace/emoji) are too thin
    # for the AI to draft a meaningful comment on.
    MIN_COMMENT_TEXT_LENGTH = 30


class Browser:
    ARGS = ["--disable-features=Translate,TranslateUI"]
    LOCALE = "en-US"
    ACCEPT_LANGUAGE_HEADER = "en-US,en;q=0.9"
    LINKEDIN_LANG_COOKIE_VALUE = "v=2&lang=en-us"
    LINKEDIN_COOKIE_DOMAIN = ".linkedin.com"


class Scraping:
    # How many feed cards the pre-like preflight samples for the social-count summary row.
    PREFLIGHT_CARD_SAMPLE = 5
    # At most this many elements are tried per count selector before giving up on a card.
    COUNT_CANDIDATE_LIMIT = 3
    # Above this share of posts with unreadable counts, the run aborts instead of
    # ranking on numbers it could not read.
    MAX_UNREADABLE_COUNT_RATIO = 0.5
    # Below this many posts, "this metric never produced a number" is not yet evidence
    # of selector drift — a small quiet feed looks the same.
    MIN_CARDS_FOR_DRIFT_WARNING = 3


class Timeouts:
    LOGIN_RETRY_MAX_ATTEMPTS = 3
    LOGIN_RETRY_BACKOFF = 1.5
    FEED_LOAD_TIMEOUT_MS = 15000
    # A code field on a challenge URL we don't recognize gets this long to be completed
    # by hand; only the known email-code flow is allowed to wait without a deadline.
    UNRECOGNIZED_CHALLENGE_WAIT_MS = 120_000
    ELEMENT_ACTION_TIMEOUT_MS = 3000
    LIKE_VERIFY_TIMEOUT_MS = 3000
    POPUP_DISMISS_TIMEOUT_MS = 3000
    POPUP_RECHECK_TIMEOUT_MS = 500
    LOAD_MORE_MAX_ATTEMPTS = 15
    SCROLL_PIXELS = 3000


class Commenting:
    MODEL = "gpt-4o-mini"
    TEMPERATURE = 0.9
    MAX_OUTPUT_TOKENS = 120
    # The OpenAI SDK retries transient failures (429 / 5xx / connection / timeout)
    # with exponential backoff and honors the server's Retry-After header. Non-retryable
    # errors (auth, invalid request) raise immediately instead of being retried blindly.
    MAX_RETRIES = 3
    REQUEST_TIMEOUT_S = 30.0
