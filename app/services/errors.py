"""Exception types shared by services and workers."""

from __future__ import annotations


class OperationCanceled(Exception):
    """Raised inside a worker when the user canceled the operation."""


class FriendlyError(Exception):
    """An error with a short human explanation and optional technical details."""

    def __init__(self, message: str, details: str = "", hint: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.details = details
        self.hint = hint          # what could fix it: "signin" | "cookies" | "js" | "update" | ""

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.message


class FFmpegNotFound(FriendlyError):
    """FFmpeg or FFprobe could not be located."""

    def __init__(self, what: str = "FFmpeg") -> None:
        super().__init__(
            f"{what} was not found. Install FFmpeg and add it to PATH, or set its location "
            "in Settings > FFmpeg executable (see README, section 'Installing FFmpeg').")
