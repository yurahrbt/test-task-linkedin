"""Errors raised by the AI integration."""


class CommentingAuthError(RuntimeError):
    """OpenAI rejected the credentials or model access."""
