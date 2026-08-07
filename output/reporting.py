from ai.commenter import CommentDraft
from linkedin.feed_scraper import ScrapedPost


def print_posts(posts: list[ScrapedPost]) -> None:
    for i, post in enumerate(posts, start=1):
        print(f"{i}. {post.author}: {post.text[:200]} — outcome: {post.outcome}")


def print_drafts(drafts: list[CommentDraft]) -> None:
    for i, draft in enumerate(drafts, start=1):
        print(f"\n{i}. {draft.post.author}: {draft.post.text[:200]}")
        print(f"   Draft comment: {draft.comment}")
