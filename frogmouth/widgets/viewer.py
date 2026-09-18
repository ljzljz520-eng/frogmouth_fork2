"""The markdown viewer itself."""

from __future__ import annotations

from asyncio import CancelledError, Task, create_task, shield
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from hashlib import sha1
from pathlib import Path
from webbrowser import open as open_url

from httpx import URL, AsyncClient, HTTPStatusError, RequestError
from markdown_it import MarkdownIt
from mdit_py_plugins import front_matter
from textual import work
from textual._slug import TrackedSlugs
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.message import Message
from textual.reactive import var
from textual.widget import Widget
from textual.widgets import Markdown
from typing_extensions import Final

from .. import __version__
from ..data import NavigationEntry
from ..dialogs import ErrorDialog
from ..utility.advertising import APPLICATION_TITLE, USER_AGENT

PLACEHOLDER = f"""\
# {APPLICATION_TITLE} {__version__}

Welcome to {APPLICATION_TITLE}!
"""

TableOfContents = list  # list[tuple[int, str, str | None]] as produced by Markdown


class TransactionState(Enum):
    """The lifecycle states a `NavigationTransaction` can be in."""

    LOADING = "loading"
    """The document is still being fetched."""

    STAGED = "staged"
    """The document has been parsed into the invisible staging document."""

    COMMITTED = "committed"
    """The document was atomically committed to the user interface."""

    CANCELLED = "cancelled"
    """A newer navigation superseded this transaction; no side effects allowed."""

    FAILED = "failed"
    """Loading failed; an error may have been reported for the current
    generation only."""

    EXTERNAL = "external"
    """The resource could not be displayed and was handed to the OS."""

    @property
    def terminal(self) -> bool:
        """Is this a terminal state?"""
        return self in (
            TransactionState.COMMITTED,
            TransactionState.CANCELLED,
            TransactionState.FAILED,
            TransactionState.EXTERNAL,
        )


@dataclass
class NavigationTransaction:
    """A single, generation-tracked navigation.

    All of the work for a visit -- fetching, parsing and rendering into the
    invisible staging document -- happens against a transaction. Nothing the
    user can see changes until `commit`, which is only permitted while the
    transaction is both staged and still current. Once a transaction reaches a
    terminal state it can never produce a side effect again.
    """

    generation: int
    """The monotonic generation assigned to this navigation."""

    location: Path | URL
    """The location requested for this navigation."""

    remember: bool = True
    """Should this navigation create a new history entry on commit?"""

    target_entry: NavigationEntry | None = None
    """The existing history entry this navigation commits to, if any."""

    state: TransactionState = TransactionState.LOADING
    """The current lifecycle state of the transaction."""

    canonical_location: Path | URL | None = None
    """The resolved/final-redirect location the content actually came from."""

    anchor: str | None = None
    """An in-document anchor to scroll to after commit, if any."""

    content: str | None = field(default=None, repr=False)
    """The raw document content, once fetched."""

    content_version: str | None = None
    """A version tag derived from the document content."""

    title: str | None = None
    """The title derived from the document."""

    table_of_contents: TableOfContents = field(default_factory=list)
    """The table of contents parsed from the staged document."""

    entry: NavigationEntry | None = None
    """The history entry this transaction committed, once committed."""

    scroll_anchor: tuple[int, int] | None = None
    """The scroll position to restore after commit, or `None` for home."""

    history_changed: bool = False
    """Did committing this transaction change the history data?"""

    @property
    def terminal(self) -> bool:
        """Has this transaction reached a terminal state?"""
        return self.state.terminal

    def is_current(self, generation: int) -> bool:
        """Does this transaction own the given generation and is it live?

        Args:
            generation: The generation to test against.

        Returns:
            `True` only if this transaction is still the latest, live one.
        """
        return self.generation == generation and not self.terminal

    def cancel(self) -> None:
        """Move the transaction to the cancelled terminal state."""
        if not self.terminal:
            self.state = TransactionState.CANCELLED

    def fail(self) -> None:
        """Move the transaction to the failed terminal state."""
        if not self.terminal:
            self.state = TransactionState.FAILED

    def external(self) -> None:
        """Move the transaction to the external-handoff terminal state."""
        if not self.terminal:
            self.state = TransactionState.EXTERNAL

    def commit(self) -> None:
        """Move the transaction to the committed terminal state."""
        self.state = TransactionState.COMMITTED


def content_version_of(content: str) -> str:
    """Calculate a version tag for the given document content.

    Args:
        content: The document content to tag.

    Returns:
        A stable version tag that changes whenever the content changes.
    """
    return "sha1:" + sha1(content.encode("utf-8", "ignore")).hexdigest()


def title_from(table_of_contents: TableOfContents, location: Path | URL) -> str:
    """Derive a document title from its table of contents and location.

    Args:
        table_of_contents: The parsed table of contents.
        location: The location of the document, used as a fallback.

    Returns:
        The best title that could be determined.
    """
    headings = [
        (level, label.strip())
        for level, label, block_id in table_of_contents
        if block_id and label.strip()
    ]
    for level, label in headings:
        if level == 1:
            return label
    if headings:
        return headings[0][1]
    name = Path(location.path).name if isinstance(location, URL) else location.name
    return name or str(location)


class History:
    """Holds the browsing history for the viewer."""

    MAXIMUM_HISTORY_LENGTH: Final[int] = 256
    """The maximum number of items we'll keep in history."""

    def __init__(self, history: list[NavigationEntry] | None = None) -> None:
        """Initialise the history object."""
        self._history: deque[NavigationEntry] = deque(
            history or [], maxlen=self.MAXIMUM_HISTORY_LENGTH
        )
        """The list that holds the history of locations visited."""
        self._current: int = max(len(self._history) - 1, 0)
        """The current location."""

    @property
    def entry(self) -> NavigationEntry | None:
        """The current history entry, if there is one."""
        try:
            return self._history[self._current]
        except IndexError:
            return None

    @property
    def location(self) -> Path | URL | None:
        """The current location in the history."""
        entry = self.entry
        return None if entry is None else entry.location

    @property
    def current(self) -> int | None:
        """The current location in history, or None if there is no current location."""
        return None if self.entry is None else self._current

    @property
    def entries(self) -> list[NavigationEntry]:
        """The entries in the history."""
        return list(self._history)

    def entry_at(self, index: int) -> NavigationEntry | None:
        """Get the entry at the given index, if any.

        Args:
            index: The index to get the entry for.

        Returns:
            The entry, or `None` if the index is out of range.
        """
        if 0 <= index < len(self._history):
            return self._history[index]
        return None

    def entry_before(self) -> NavigationEntry | None:
        """The entry immediately before the current one, if any."""
        return self.entry_at(self._current - 1)

    def entry_after(self) -> NavigationEntry | None:
        """The entry immediately after the current one, if any."""
        return self.entry_at(self._current + 1)

    def remember(self, entry: NavigationEntry) -> None:
        """Remember a new entry in the history.

        Any forward history is discarded, matching normal browser behaviour.

        Args:
            entry: The entry to remember.
        """
        while len(self._history) - 1 > self._current:
            self._history.pop()
        self._history.append(entry)
        self._current = len(self._history) - 1

    def set_current(self, entry: NavigationEntry) -> bool:
        """Move the history cursor to the given entry.

        Args:
            entry: The entry to make current.

        Returns:
            `True` if the cursor moved, `False` if the entry was unknown.
        """
        # Match by identity: distinct visits to equal-looking locations (same
        # path, title, version and scroll) must not collapse onto one another.
        for index, candidate in enumerate(self._history):
            if candidate is entry:
                self._current = index
                return True
        return False

    def __delitem__(self, index: int) -> None:
        del self._history[index]
        if self._history:
            if index <= self._current:
                self._current -= 1
            self._current = max(0, min(self._current, len(self._history) - 1))
        else:
            self._current = 0


class Viewer(VerticalScroll, can_focus=True, can_focus_children=True):
    """The markdown viewer class."""

    DEFAULT_CSS = """
    Viewer {
        width: 1fr;
        scrollbar-gutter: stable;
    }

    Viewer > Markdown.navigation-staging {
        display: none;
    }
    """

    BINDINGS = [
        Binding("w,k", "scroll_up", "", show=False),
        Binding("s,j", "scroll_down", "", show=False),
        Binding("space", "page_down", "", show=False),
        Binding("b", "page_up", "", show=False),
    ]
    """Bindings for the Markdown viewer widget."""

    history: var[History] = var(History)
    """The browsing history."""

    viewing_location: var[bool] = var(False)
    """Is an actual location being viewed?"""

    class ViewerMessage(Message):
        """Base class for viewer messages."""

        def __init__(self, viewer: Viewer) -> None:
            """Initialise the message.

            Args:
                viewer: The viewer sending the message.
            """
            super().__init__()
            self.viewer: Viewer = viewer
            """The viewer that sent the message."""

    class LocationChanged(ViewerMessage):
        """Message sent when the viewer location changes."""

    class HistoryUpdated(ViewerMessage):
        """Message sent when the history is updated."""

    class TableOfContentsUpdated(ViewerMessage):
        """Message sent when the committed document's table of contents changes."""

        def __init__(self, viewer: Viewer, table_of_contents: TableOfContents) -> None:
            """Initialise the message.

            Args:
                viewer: The viewer sending the message.
                table_of_contents: The freshly committed table of contents.
            """
            super().__init__(viewer)
            self.table_of_contents: TableOfContents = table_of_contents
            """The table of contents of the committed document."""

    def __init__(self, *args, **kwargs) -> None:
        """Initialise the viewer."""
        super().__init__(*args, **kwargs)
        self._generation: int = 0
        """The monotonic generation of the latest navigation."""
        self._transaction: NavigationTransaction | None = None
        """The latest navigation transaction, if any."""
        self._staging_chain: Task | None = None
        """The chained task that serialises staging-document updates."""
        self._table_of_contents: dict[int, TableOfContents] = {}
        """The latest table of contents parsed for each document buffer."""
        self._active: Markdown | None = None
        """The document buffer currently on display."""
        self._staging: Markdown | None = None
        """The invisible document buffer that loads are staged into."""

    @staticmethod
    def _make_parser() -> MarkdownIt:
        """Make the Markdown parser used by every document buffer."""
        return MarkdownIt("gfm-like").use(front_matter.front_matter_plugin)

    def compose(self) -> ComposeResult:
        """Compose the markdown viewer.

        Two document buffers are mounted: the visible one starts with the
        placeholder content, while the other is hidden and is used as the
        invisible staging document for in-flight navigations.
        """
        self._active = Markdown(
            PLACEHOLDER, parser_factory=self._make_parser
        )
        self._staging = Markdown(
            parser_factory=self._make_parser, classes="navigation-staging"
        )
        yield self._active
        yield self._staging

    @property
    def document(self) -> Markdown:
        """The currently visible markdown document."""
        if self._active is None:
            return self.query_one(Markdown)
        return self._active

    @property
    def location(self) -> Path | URL | None:
        """The location that is currently being visited."""
        return self.history.location if self.viewing_location else None

    def scroll_to_block(self, block_id: str) -> None:
        """Scroll the document to the given block ID.

        Args:
            block_id: The ID of the block to scroll to.
        """
        self.scroll_to_widget(self.document.query_one(f"#{block_id}"), top=True)

    def _on_markdown_table_of_contents_updated(
        self, event: Markdown.TableOfContentsUpdated
    ) -> None:
        """Remember the table of contents parsed for each buffer.

        Updates for the invisible staging document are stopped here so that
        the rest of the application only ever sees a table of contents that
        was committed atomically with its document.

        Args:
            event: The table of contents update event.
        """
        table_of_contents = list(event.table_of_contents)
        self._table_of_contents[id(event.markdown)] = table_of_contents
        event.stop()
        if self._active is not None and event.markdown is self._active:
            self.post_message(self.TableOfContentsUpdated(self, table_of_contents))

    def _next_generation(self) -> int:
        """Begin a new navigation generation.

        Any live transaction from an earlier generation is moved to the
        cancelled terminal state.

        Returns:
            The new generation number.
        """
        self._generation += 1
        if self._transaction is not None and not self._transaction.terminal:
            self._transaction.cancel()
        return self._generation

    def _capture_scroll_anchor(self) -> None:
        """Record the viewer's current scroll position on the current entry."""
        if self._active is not None and (entry := self.history.entry) is not None:
            entry.set_scroll_anchor(int(self.scroll_x), int(self.scroll_y))

    def visit(
        self,
        location: Path | URL,
        remember: bool = True,
        target_entry: NavigationEntry | None = None,
    ) -> None:
        """Visit a location.

        Args:
            location: The location to visit.
            remember: Should this visit be added to the history?
            target_entry: The existing history entry this visit commits to
                (used for back/forward and reload); `None` means the current
                entry when not remembering.
        """
        # Normalise the requested location up front; the canonical location
        # (following redirects, etc.) is attached to the transaction at commit.
        if isinstance(location, Path):
            location = location.expanduser().resolve()
        elif not isinstance(location, URL):
            raise ValueError("Unknown location type passed to the Markdown viewer")

        self._capture_scroll_anchor()

        if target_entry is None and not remember:
            target_entry = self.history.entry

        transaction = NavigationTransaction(
            generation=self._next_generation(),
            location=location,
            remember=remember,
            target_entry=target_entry,
        )
        self._transaction = transaction
        self._navigate(transaction)

    @work(exclusive=True)
    async def _navigate(self, transaction: NavigationTransaction) -> None:
        """Run a navigation transaction through to a terminal state.

        Args:
            transaction: The transaction to run.
        """
        try:
            if isinstance(transaction.location, Path):
                content = await self._load_local(transaction)
            else:
                content = await self._load_remote(transaction)
            if content is None or transaction.terminal:
                return
            if not transaction.is_current(self._generation):
                transaction.cancel()
                return
            await self._stage(transaction, content)
            if not transaction.is_current(self._generation):
                transaction.cancel()
                return
            self._commit(transaction)
        except CancelledError:
            # The exclusive worker was superseded by a newer navigation; this
            # is a terminal state and must produce no further side effects.
            transaction.cancel()
            raise
        except (OSError, RequestError, HTTPStatusError) as error:
            self._abort(
                transaction, "Error loading document", self._error_text(error)
            )
        except Exception as error:  # pylint:disable=broad-except
            # Parsing or layout failures must also terminate cleanly, never
            # leaving half-applied UI state behind.
            self._abort(
                transaction, "Error loading document", self._error_text(error)
            )

    @staticmethod
    def _error_text(error: BaseException) -> str:
        """Turn an exception into error dialog text."""
        return str(error) or error.__class__.__name__

    def _abort(
        self, transaction: NavigationTransaction, title: str, message: str
    ) -> None:
        """Terminate a failed transaction.

        The error dialog is only shown while the transaction owns the current
        generation; failures of superseded transactions are silenced.

        Args:
            transaction: The transaction to abort.
            title: The error dialog title.
            message: The error dialog message.
        """
        if transaction.is_current(self._generation):
            transaction.fail()
            self.app.push_screen(ErrorDialog(title, message))
        else:
            transaction.cancel()

    async def _load_local(
        self, transaction: NavigationTransaction
    ) -> str | None:
        """Fetch a local document for the transaction.

        Args:
            transaction: The navigation transaction.

        Returns:
            The document content, or `None` if the transaction is finished.
        """
        path = transaction.location
        anchor = None
        # Preserve the `path#anchor` behaviour that Markdown.load() provided.
        file_part, _, anchor_part = str(path).partition("#")
        if anchor_part:
            path = Path(file_part)
            anchor = anchor_part
        content = path.read_text(encoding="utf-8")
        if not transaction.is_current(self._generation):
            transaction.cancel()
            return None
        transaction.canonical_location = path
        transaction.anchor = anchor
        return content

    async def _load_remote(
        self, transaction: NavigationTransaction
    ) -> str | None:
        """Fetch a remote document for the transaction.

        Args:
            transaction: The navigation transaction.

        Returns:
            The document content, or `None` if the transaction is finished.
        """
        try:
            async with AsyncClient() as client:
                response = await client.get(
                    transaction.location,
                    follow_redirects=True,
                    headers={"user-agent": USER_AGENT},
                )
        except RequestError as error:
            self._abort(transaction, "Error getting document", str(error))
            return None

        try:
            response.raise_for_status()
        except HTTPStatusError as error:
            self._abort(transaction, "Error getting document", str(error))
            return None

        if not transaction.is_current(self._generation):
            transaction.cancel()
            return None

        # Final content-type check: if the server didn't hand us plain text or
        # Markdown, hand the location off to the OS instead.
        content_type = response.headers.get("content-type", "")
        if not any(
            content_type.startswith(f"text/{sub_type}")
            for sub_type in ("plain", "markdown", "x-markdown")
        ):
            if transaction.is_current(self._generation):
                transaction.external()
                open_url(str(transaction.location))
            else:
                transaction.cancel()
            return None

        final_location = response.url
        transaction.anchor = (
            transaction.location.fragment or final_location.fragment or None
        )
        if final_location.fragment:
            final_location = final_location.copy_with(fragment="")
        transaction.canonical_location = final_location
        return response.text

    async def _stage(self, transaction: NavigationTransaction, content: str) -> None:
        """Parse and render content into the invisible staging document.

        Staging updates are serialised (and shielded from worker cancellation)
        so that a superseded transaction can never leave the staging document
        half-built; its parsed result simply goes uncommitted.

        Args:
            transaction: The navigation transaction.
            content: The document content to stage.
        """
        staging = self._staging
        assert staging is not None
        previous = self._staging_chain

        async def stage_job() -> None:
            """Wait for any earlier stage to settle, then replace the buffer."""
            if previous is not None and not previous.done():
                try:
                    await shield(previous)
                except BaseException:  # pylint:disable=broad-except
                    # A poisoned earlier stage must not break the chain; the
                    # update below removes all old blocks before mounting new
                    # ones anyway.
                    pass
            await staging.update(content)

        task = create_task(stage_job())
        self._staging_chain = task
        await shield(task)

        table_of_contents = self._table_of_contents.get(id(staging))
        if table_of_contents is None:
            # Fallback for the pinned Textual API: the parsed table of
            # contents is held on the widget while the update message is
            # still in flight.
            table_of_contents = list(
                getattr(staging, "_table_of_contents", None) or []
            )
        transaction.content = content
        transaction.content_version = content_version_of(content)
        transaction.table_of_contents = table_of_contents
        assert transaction.canonical_location is not None
        transaction.title = title_from(
            table_of_contents, transaction.canonical_location
        )
        transaction.state = TransactionState.STAGED

    def _commit(self, transaction: NavigationTransaction) -> None:
        """Atomically commit a staged, current transaction to the UI.

        This is the only place visible state changes: document content,
        canonical location, title, history cursor, scroll anchor and the
        table of contents all switch over together.

        Args:
            transaction: The transaction to commit.
        """
        if transaction.state is not TransactionState.STAGED or not (
            staging := self._staging
        ) or not (outgoing := self._active):
            transaction.cancel()
            return
        if not transaction.is_current(self._generation):
            transaction.cancel()
            return

        version_changed = False
        if transaction.remember or transaction.target_entry is None:
            entry = NavigationEntry(
                transaction.canonical_location,
                title=transaction.title,
                content_version=transaction.content_version,
            )
            self.history.remember(entry)
            history_changed = True
        else:
            entry = transaction.target_entry
            version_changed = (
                entry.content_version is not None
                and entry.content_version != transaction.content_version
            )
            history_changed = (
                entry.location != transaction.canonical_location
                or entry.title != transaction.title
                or version_changed
            )
            entry.location = transaction.canonical_location
            entry.title = transaction.title
            entry.content_version = transaction.content_version
            self.history.set_current(entry)

        # Decide the scroll anchor for the commit: an explicit in-document
        # anchor is handled post-layout; a revisited document restores its
        # saved anchor unless the content has changed versions, in which case
        # it returns to the top.
        if (
            not transaction.anchor
            and not transaction.remember
            and not version_changed
            and any(entry.scroll_anchor)
        ):
            transaction.scroll_anchor = entry.scroll_anchor
        else:
            transaction.scroll_anchor = None

        transaction.entry = entry
        transaction.history_changed = history_changed

        with self.app.batch_update():
            outgoing.add_class("navigation-staging")
            staging.remove_class("navigation-staging")
            self._active = staging
            self._staging = outgoing
            self.viewing_location = True

        transaction.commit()

        if history_changed:
            self.post_message(self.HistoryUpdated(self))
        self.post_message(self.LocationChanged(self))
        self.post_message(
            self.TableOfContentsUpdated(self, transaction.table_of_contents)
        )
        self.call_after_refresh(self._restore_scroll, transaction)

    _MAX_SCROLL_RESTORE_ATTEMPTS = 4

    def _anchor_block(self, transaction: NavigationTransaction) -> Widget | None:
        """Resolve a transaction's fragment anchor to its rendered block.

        Headings render with generated block IDs; the fragment is matched via
        the same slug walk that `Markdown.goto_anchor` uses.

        Args:
            transaction: The committed transaction carrying the anchor.

        Returns:
            The heading widget, or `None` if the anchor matches no heading.
        """
        anchor = transaction.anchor
        if not anchor:
            return None
        table_of_contents = self._table_of_contents.get(id(self.document))
        if not table_of_contents:
            return None
        slugs = TrackedSlugs()
        for _, title, block_id in table_of_contents:
            if slugs.slug(title) == anchor:
                blocks = self.document.query(f"#{block_id}")
                return blocks[0] if blocks else None
        return None

    def _restore_scroll(
        self,
        transaction: NavigationTransaction,
        attempt: int = 1,
    ) -> None:
        """Restore the committed transaction's scroll anchor after layout.

        Runs after a refresh (so the newly visible document has been laid out)
        and is a no-op unless the transaction is both committed and current.
        Because revealing the staging buffer can take more than one refresh to
        reflow, `_verify_scroll_restore` re-applies the anchor on following
        refreshes until it sticks.

        Args:
            transaction: The transaction whose scroll should be restored.
            attempt: The number of this restoration attempt.
        """
        if (
            transaction.state is not TransactionState.COMMITTED
            or transaction.generation != self._generation
        ):
            return
        anchor_applied = False
        if transaction.anchor:
            anchor_applied = self.document.goto_anchor(transaction.anchor)
        if not anchor_applied:
            anchor = transaction.scroll_anchor
            if anchor is not None:
                target_x, target_y = anchor
                self.scroll_to(x=target_x, y=target_y, animate=False)
            else:
                target_x = target_y = 0
                self.scroll_home(animate=False)
        else:
            target_x = target_y = 0
        self.call_after_refresh(
            self._verify_scroll_restore,
            transaction,
            attempt,
            anchor_applied,
            target_x,
            target_y,
        )

    def _verify_scroll_restore(
        self,
        transaction: NavigationTransaction,
        attempt: int,
        anchor_applied: bool,
        target_x: float,
        target_y: float,
    ) -> None:
        """Confirm a restored scroll anchor landed, retrying while layout settles.

        Args:
            transaction: The transaction whose scroll was restored.
            attempt: The restoration attempt that was made.
            anchor_applied: Whether a fragment anchor was jumped to.
            target_x: The horizontal position that was requested.
            target_y: The vertical position that was requested.
        """
        if (
            transaction.state is not TransactionState.COMMITTED
            or transaction.generation != self._generation
        ):
            return
        if anchor_applied:
            block = self._anchor_block(transaction)
            # If the heading cannot be located we cannot verify further;
            # otherwise it must intersect with the visible scroll region.
            settled = block is None or (
                block.region.y <= self.region.bottom - 1
                and block.region.bottom >= self.region.y
            )
        else:
            range_settled = (
                self.max_scroll_y >= target_y - 0.5
                and self.max_scroll_x >= target_x - 0.5
            )
            at_target = (
                abs(self.scroll_y - min(target_y, self.max_scroll_y)) <= 0.5
                and abs(self.scroll_x - min(target_x, self.max_scroll_x)) <= 0.5
            )
            settled = range_settled and at_target
        if not settled and attempt < self._MAX_SCROLL_RESTORE_ATTEMPTS:
            # The buffer reveal may only just have produced the final scroll
            # range; apply the anchor again on the next refresh.
            self.call_after_refresh(self._restore_scroll, transaction, attempt + 1)

    def reload(self) -> None:
        """Reload the current location."""
        if self.location is not None:
            self.visit(self.location, remember=False)

    def show(self, content: str) -> None:
        """Show some direct text in the viewer.

        Args:
            content: The text to show.
        """
        # Direct content isn't a navigation, but it does supersede anything
        # still loading.
        self._capture_scroll_anchor()
        self._next_generation()
        self._transaction = None
        self.viewing_location = False
        if self._active is not None:
            self._active.update(content)
        self.scroll_home(animate=False)

    def _jump(self, target: NavigationEntry | None) -> None:
        """Jump to a specific existing history entry.

        Args:
            target: The entry to jump to, if any.
        """
        if target is not None:
            self.visit(target.location, remember=False, target_entry=target)

    def back(self) -> None:
        """Go back in the viewer history."""
        self._jump(self.history.entry_before())

    def forward(self) -> None:
        """Go forward in the viewer history."""
        self._jump(self.history.entry_after())

    def goto_history_id(self, history_id: int) -> None:
        """Go to an entry in the history by its index.

        Args:
            history_id: The index of the history entry to visit.
        """
        target = self.history.entry_at(history_id)
        if target is None:
            return
        if target is self.history.entry:
            self.reload()
        else:
            self.visit(target.location, remember=False, target_entry=target)

    def load_history(self, history: list[NavigationEntry]) -> None:
        """Load up a history list from the given history.

        Args:
            history: The history load up from.
        """
        self.history = History(history)
        self.post_message(self.HistoryUpdated(self))

    def delete_history(self, history_id: int) -> None:
        """Delete an item from the history.

        Args:
            history_id: The ID of the history item to delete.
        """
        try:
            del self.history[history_id]
        except IndexError:
            pass
        else:
            self.post_message(self.HistoryUpdated(self))

    def clear_history(self) -> None:
        """Clear down the whole of history."""
        self.load_history([])
