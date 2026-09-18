"""Provides the history navigation pane."""

from __future__ import annotations

from functools import partial
from pathlib import Path

from httpx import URL
from rich.markup import escape
from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.message import Message
from textual.widgets import OptionList
from textual.widgets.option_list import Option

from ...data import NavigationEntry
from ...dialogs import YesNoDialog
from .navigation_pane import NavigationPane


class Entry(Option):
    """An entry in the history."""

    def __init__(self, history_id: int, entry: NavigationEntry) -> None:
        """Initialise the history entry item.

        Args:
            history_id: The ID of the item of history.
            entry: The navigation entry being added to history.
        """
        super().__init__(self._as_prompt(entry))
        self.history_id = history_id
        """The ID of the item of history."""
        self.entry = entry
        """The navigation entry for this item of history."""

    @property
    def location(self) -> Path | URL:
        """The location for this entry in the history."""
        return self.entry.location

    @staticmethod
    def _as_prompt(entry: NavigationEntry) -> Text:
        """Depict the navigation entry as a decorated prompt.

        Args:
            entry: The entry to depict.

        Returns:
            A prompt with icon, title and location.
        """
        location = entry.location
        if isinstance(location, Path):
            icon = ":page_facing_up:"
            title = entry.title or location.name
            detail = str(location.parent)
        else:
            icon = ":globe_with_meridians:"
            title = entry.title or Path(location.path).name
            detail = f"{Path(location.path).parent}\n{location.host}"
        return Text.from_markup(
            f"{icon} [bold]{escape(title)}[/]\n[dim]{escape(detail)}[/]",
            overflow="ellipsis",
        )


class History(NavigationPane):
    """History navigation pane."""

    DEFAULT_CSS = """
    History {
        height: 100%;
    }

    History > OptionList {
        background: $panel;
        border: none;
        height: 1fr;
    }

    History > OptionList:focus {
        border: none;
    }
    """

    BINDINGS = [
        Binding("delete", "delete", "Delete the history item"),
        Binding("backspace", "clear", "Clean the history"),
    ]
    """The bindings for the history navigation pane."""

    def __init__(self) -> None:
        """Initialise the history navigation pane."""
        super().__init__("History")

    def compose(self) -> ComposeResult:
        """Compose the child widgets."""
        yield OptionList()

    def set_focus_within(self) -> None:
        """Focus the option list."""
        self.query_one(OptionList).focus(scroll_visible=False)

    def update_from(self, entries: list[NavigationEntry]) -> None:
        """Update the history from the given list of entries.

        Args:
            entries: A list of entries to update the history with.

        This call removes any existing history and sets it to the given
        value.
        """
        option_list = self.query_one(OptionList).clear_options()
        for history_id, entry in reversed(list(enumerate(entries))):
            option_list.add_option(Entry(history_id, entry))

    class Goto(Message):
        """Message that requests the viewer goes to a given history entry."""

        def __init__(self, history_id: int) -> None:
            """Initialise the history goto message.

            Args:
                history_id: The ID of the history entry to go to.
            """
            super().__init__()
            self.history_id = history_id
            """The ID of the history entry to go to."""

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        """Handle an entry in the history being selected.

        Args:
            event: The event to handle.
        """
        event.stop()
        assert isinstance(event.option, Entry)
        self.post_message(self.Goto(event.option.history_id))

    class Delete(Message):
        """Message that requests the viewer to delete an item of history."""

        def __init__(self, history_id: int) -> None:
            """initialise the history delete message.

            args:
                history_id: The ID of the item of history to delete.
            """
            super().__init__()
            self.history_id = history_id
            """The ID of the item of history to delete."""

    def delete_history(self, history_id: int, delete_it: bool) -> None:
        """Delete a given history entry.

        Args:
            history_id: The ID of the item of history to delete.
            delete_it: Should it be deleted?
        """
        if delete_it:
            self.post_message(self.Delete(history_id))

    def action_delete(self) -> None:
        """Delete the highlighted item from history."""
        history = self.query_one(OptionList)
        if (item := history.highlighted) is not None:
            assert isinstance(entry := history.get_option_at_index(item), Entry)
            self.app.push_screen(
                YesNoDialog(
                    "Delete history entry?",
                    "Are you sure you want to delete the history entry?",
                ),
                partial(self.delete_history, entry.history_id),
            )

    class Clear(Message):
        """Message that requests that the history be cleared."""

    def clear_history(self, clear_it: bool) -> None:
        """Perform a history clear.

        Args:
            clear_it: Should it be cleared?
        """
        if clear_it:
            self.post_message(self.Clear())

    def action_clear(self) -> None:
        """Clear out the whole history."""
        self.app.push_screen(
            YesNoDialog(
                "Clear history?",
                "Are you sure you want to clear everything out of history?",
            ),
            self.clear_history,
        )
