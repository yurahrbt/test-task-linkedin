import logging

from openai import (
    AuthenticationError,
    NotFoundError,
    OpenAI,
    OpenAIError,
    PermissionDeniedError,
)
from pydantic import BaseModel

import config
from ai.errors import CommentingAuthError
from linkedin.feed_scraper import ScrapedPost

logger = logging.getLogger(__name__)

# Errors no retry and no other post will fix: the key is wrong, revoked, or not entitled
# to this model. Everything else (rate limits, 5xx, timeouts) is per-request and already
# retried by the SDK.
_CREDENTIAL_ERRORS = (AuthenticationError, PermissionDeniedError, NotFoundError)


_SYSTEM_PROMPT = (
    "Write a natural 1-2 sentence LinkedIn comment in the post's language. "
    "Mention one concrete detail or question; avoid generic praise, hashtags, and emoji lists. "
    "Use only the post as evidence. If it is too thin for a genuine comment, return exactly SKIP. "
    "Return only the comment or SKIP."
)


class CommentDraft(BaseModel):
    post: ScrapedPost
    comment: str


def _build_user_prompt(post: ScrapedPost) -> str:
    return (
        f"Author: {post.author}\n\n"
        f"Post (use only this evidence; do not invent profile facts):\n{post.text}\n\n"
        "Draft one natural comment that a peer might actually leave."
    )


def _generate_comment(client: OpenAI, post: ScrapedPost) -> str:
    response = client.chat.completions.create(
        model=config.Commenting.MODEL,
        temperature=config.Commenting.TEMPERATURE,
        max_tokens=config.Commenting.MAX_OUTPUT_TOKENS,
        messages=[
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": _build_user_prompt(post)},
        ],
    )
    return (response.choices[0].message.content or "").strip()


def build_client() -> OpenAI:
    # The SDK retries only transient failures (429 / 5xx / connection / timeout) with
    # exponential backoff and honors Retry-After; auth/invalid-request errors raise
    # immediately rather than being retried blindly.
    return OpenAI(
        api_key=config.settings.openai_api_key,
        max_retries=config.Commenting.MAX_RETRIES,
        timeout=config.Commenting.REQUEST_TIMEOUT_S,
    )


def verify_api_access(client: OpenAI | None = None) -> None:
    """Preflight the OpenAI credentials with one metadata request — no tokens, no
    generation. Called before LinkedIn is touched so an expired key costs nothing
    instead of costing a login, a scrape and a screenful of likes.

    A transient/network failure is logged and tolerated: it is not evidence the key is
    bad, and the drafting stage will surface it if it persists."""
    client = client or build_client()
    try:
        client.models.retrieve(config.Commenting.MODEL)
    except _CREDENTIAL_ERRORS as exc:
        raise CommentingAuthError(
            f"OpenAI rejected the credentials for model {config.Commenting.MODEL!r}: {exc}. "
            "Check OPENAI_API_KEY in .env (and that the key's project has access to the model)."
        ) from exc
    except OpenAIError as exc:
        logger.warning(
            "[verify_api_access] preflight could not confirm API access (%s) — continuing; "
            "the drafting stage will report it if it persists.",
            exc,
        )
        return

    logger.info("[verify_api_access] OpenAI credentials accepted for %s", config.Commenting.MODEL)


def draft_comments(
    posts: list[ScrapedPost],
    client: OpenAI | None = None,
    *,
    target_count: int = 3,
) -> list[CommentDraft]:
    """Draft up to *target_count* real comments by walking *posts* in order.

    Posts without enough text for a meaningful comment are skipped, and so are
    AI responses that return ``SKIP``.  The caller should pass more candidates
    than *target_count* so the loop can keep going when thin posts or SKIPs
    reduce the usable set.
    """
    from domain.ranking import has_commentable_text  # local to avoid circular import

    client = client or build_client()
    drafts: list[CommentDraft] = []

    for post in posts:
        if len(drafts) >= target_count:
            break

        if not has_commentable_text(post):
            logger.info(
                "[draft_comments] skipping %s — post text too thin for a meaningful comment",
                post.author,
            )
            continue

        try:
            comment = _generate_comment(client, post)
        except _CREDENTIAL_ERRORS as exc:
            raise CommentingAuthError(
                f"OpenAI rejected the credentials while drafting comment {len(drafts) + 1} of "
                f"{len(posts)}: {exc}. {len(drafts)} draft(s) were discarded. Check OPENAI_API_KEY."
            ) from exc
        except Exception as exc:
            logger.warning("[draft_comments] failed to draft comment for %s: %s", post.author, exc)
            continue

        if comment.upper().strip() == "SKIP":
            logger.info(
                "[draft_comments] AI returned SKIP for %s — moving to next candidate",
                post.author,
            )
            continue

        drafts.append(CommentDraft(post=post, comment=comment))

    if len(drafts) < target_count:
        logger.warning(
            "[draft_comments] only %d usable draft(s) obtained from %d candidate(s) "
            "(target was %d)",
            len(drafts), len(posts), target_count,
        )

    return drafts
