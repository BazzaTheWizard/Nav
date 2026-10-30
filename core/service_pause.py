from __future__ import annotations


class ServicePause(RuntimeError):
    """An outside service is holding the worker back for a while: YouTube blocking
    downloads, Gemini's limit used up, or the clip judge not answering.

    The worker puts the creator back in the queue without using one of its 3
    attempts, shows the pause in the app and waits, instead of burning through
    good creators while the service is down.
    """

    def __init__(self, message: str, retry_after: float, *, service: str, hint: str = "") -> None:
        super().__init__(message)
        self.retry_after = max(60.0, float(retry_after))
        self.service = service
        self.hint = hint
