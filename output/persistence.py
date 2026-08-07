import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from pydantic import BaseModel

logger = logging.getLogger(__name__)


def save_json(path: Path, models: list[BaseModel]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps([model.model_dump() for model in models], ensure_ascii=False, indent=2)

    # Write to a temp file in the same directory, then atomically replace, so a crash
    # mid-write can never leave a truncated/corrupt artifact behind.
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as tmp:
            tmp.write(payload)
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise


def load_json(path: Path) -> list[dict]:
    """Load a JSON array from *path*, returning [] if the file is missing or corrupt."""
    if not path.exists():
        return []
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return data
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("[load_json] could not read %s: %s", path, exc)
    return []


def merge_into_history(history_path: Path, new_posts: list[BaseModel]) -> int:
    """Append *new_posts* to the cumulative post history, dedup, and sort by score.

    Returns the total number of posts in the history after merging.
    Each post entry is enriched with a ``last_seen`` timestamp so that
    successive runs build a useful engagement log.
    """
    from linkedin.feed_scraper import ScrapedPost  # local to avoid circular import

    existing_raw = load_json(history_path)

    # Build a map keyed by URN (globally unique) or author+text prefix (best effort).
    seen: dict[str, dict] = {}
    for item in existing_raw:
        key = item.get("urn") or f"{item.get('author', '')}:{(item.get('text', ''))[:100]}"
        seen[key] = item

    now = datetime.now(timezone.utc).isoformat()
    for post_model in new_posts:
        post = post_model if isinstance(post_model, ScrapedPost) else post_model
        data = post.model_dump() if isinstance(post, BaseModel) else dict(post)
        key = data.get("urn") or f"{data.get('author', '')}:{(data.get('text', ''))[:100]}"
        data["last_seen"] = now
        if key in seen:
            # Preserve best-known outcome across runs; update everything else.
            prev_outcome = seen[key].get("outcome", "")
            if prev_outcome == "liked" and data.get("outcome") != "liked":
                data["outcome"] = prev_outcome
        seen[key] = data

    # Sort descending by engagement score (None → bottom).
    merged = sorted(
        seen.values(),
        key=lambda p: p.get("engagement_score") or 0.0,
        reverse=True,
    )

    # Save using the same atomic-write pattern.
    history_path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(merged, ensure_ascii=False, indent=2)
    fd, tmp_name = tempfile.mkstemp(
        dir=history_path.parent, prefix=f".{history_path.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as tmp:
            tmp.write(payload)
        os.replace(tmp_name, history_path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise

    logger.info("[merge_into_history] history now contains %d posts at %s", len(merged), history_path)
    return len(merged)
