"""Provides code for saving and loading the history."""

from __future__ import annotations

from dataclasses import dataclass
from json import dumps, loads
from pathlib import Path
from typing import Any

from httpx import URL

from ..utility import is_likely_url
from .data_directory import data_directory

# The keys used when persisting a `NavigationEntry`; keeping them as
# constants makes the backwards/forwards compatibility handling explicit.
_LOCATION = "location"
_TITLE = "title"
_SCROLL_X = "scroll_x"
_SCROLL_Y = "scroll_y"
_CONTENT_VERSION = "content_version"


def history_file() -> Path:
    """Get the location of the history file.

    Returns:
        The location of the history file.
    """
    return data_directory() / "history.json"


@dataclass
class NavigationEntry:
    """A single complete entry in the browsing history.

    A history entry records far more than just the location visited; it also
    holds the title derived from the document, the scroll anchor the user was
    at when they left the document and a version tag for the content that was
    displayed, so that forward/back navigation can restore the view and can
    detect when the underlying document has changed.
    """

    location: Path | URL
    """The canonical location of the document."""

    title: str | None = None
    """The title derived from the document, if one could be found."""

    scroll_x: int = 0
    """The horizontal scroll anchor to restore when revisiting."""

    scroll_y: int = 0
    """The vertical scroll anchor to restore when revisiting."""

    content_version: str | None = None
    """A version tag for the content shown, or `None` if never committed."""

    @property
    def scroll_anchor(self) -> tuple[int, int]:
        """The scroll anchor as a ``(x, y)`` pair."""
        return (self.scroll_x, self.scroll_y)

    def set_scroll_anchor(self, scroll_x: int, scroll_y: int) -> None:
        """Set the scroll anchor.

        Args:
            scroll_x: The horizontal scroll anchor.
            scroll_y: The vertical scroll anchor.
        """
        self.scroll_x = max(0, int(scroll_x))
        self.scroll_y = max(0, int(scroll_y))

    def to_data(self) -> dict[str, Any]:
        """Convert the entry into data suitable for serialising to JSON.

        Returns:
            A dictionary representation of the entry.
        """
        return {
            _LOCATION: str(self.location),
            _TITLE: self.title,
            _SCROLL_X: self.scroll_x,
            _SCROLL_Y: self.scroll_y,
            _CONTENT_VERSION: self.content_version,
        }

    @classmethod
    def from_data(cls, data: str | dict[str, Any]) -> NavigationEntry:
        """Create an entry from persisted data.

        Args:
            data: The data to create the entry from. A plain string is
                treated as a bare location, which keeps history files
                written by older versions of the application readable.

        Returns:
            The constructed navigation entry.
        """
        if isinstance(data, str):
            location: str | dict[str, Any] = data
            return cls(
                URL(location) if is_likely_url(location) else Path(location)
            )
        location = data[_LOCATION]
        return cls(
            location=(
                URL(location) if is_likely_url(location) else Path(location)
            ),
            title=data.get(_TITLE),
            scroll_x=int(data.get(_SCROLL_X, 0) or 0),
            scroll_y=int(data.get(_SCROLL_Y, 0) or 0),
            content_version=data.get(_CONTENT_VERSION),
        )


def save_history(history: list[NavigationEntry]) -> None:
    """Save the given history.

    Args:
        history: The history to save.
    """
    history_file().write_text(
        dumps([entry.to_data() for entry in history], indent=4)
    )


def load_history() -> list[NavigationEntry]:
    """Load the history.

    Returns:
        The history.
    """
    return (
        [NavigationEntry.from_data(entry) for entry in loads(history.read_text())]
        if (history := history_file()).exists()
        else []
    )
