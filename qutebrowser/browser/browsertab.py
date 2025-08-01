# SPDX-FileCopyrightText: Florian Bruhin (The Compiler) <mail@qutebrowser.org>
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Base class for a wrapper over WebView/WebEngineView."""

import enum
import pathlib
import itertools
import functools
import dataclasses
from typing import (cast, TYPE_CHECKING, Any, Optional, Union,TypeVar)
from collections.abc import Iterable, Sequence, Callable

from qutebrowser.qt import machinery
from qutebrowser.qt.core import (pyqtSignal, pyqtSlot, QUrl, QObject, QSizeF, Qt,
                          QEvent, QPoint, QRect, QTimer, QByteArray)
from qutebrowser.qt.gui import QKeyEvent, QIcon, QPixmap
from qutebrowser.qt.widgets import QApplication, QWidget
from qutebrowser.qt.printsupport import QPrintDialog, QPrinter
from qutebrowser.qt.network import QNetworkAccessManager

# if TYPE_CHECKING:
#     from qutebrowser.qt.webkit import QWebHistory, QWebHistoryItem
#     from qutebrowser.qt.webkitwidgets import QWebPage
#     from qutebrowser.qt.webenginecore import (
#         QWebEngineHistory, QWebEngineHistoryItem, QWebEnginePage)

from PyQt6.QtCore import QUrl
from PyQt6.QtWidgets import (QApplication, QMainWindow, QTabWidget, QToolBar, 
                            QLineEdit, QVBoxLayout, QWidget)
# from PyQt6.QtWebEngineWidgets import QWebEngineView
from PyQt6.QtWebEngineCore import QWebEnginePage, QWebEngineHistory,QWebEngineHistoryItem


from qutebrowser.keyinput import modeman
from qutebrowser.config import config, websettings
from qutebrowser.utils import (utils, objreg, usertypes, log, qtutils,
                               urlutils, message, jinja, version)
from qutebrowser.misc import miscwidgets, objects, sessions
from qutebrowser.browser import eventfilter, inspector
from qutebrowser.qt import sip

if TYPE_CHECKING:
    from qutebrowser.browser import webelem
    from qutebrowser.browser.inspector import AbstractWebInspector
    from qutebrowser.browser.webengine.webview import WebEngineView
    from qutebrowser.browser.webkit.webview import WebView

tab_id_gen = itertools.count(0)
_WidgetType = Union["WebView", "WebEngineView"]


def create(win_id: int,
           private: bool,
           parent: QWidget | None = None) -> 'AbstractTab':
    """Get a QtWebKit/QtWebEngine tab object.

    Args:
        win_id: The window ID where the tab will be shown.
        private: Whether the tab is a private/off the record tab.
        parent: The Qt parent to set.
    """
    # Importing modules here so we don't depend on QtWebEngine without the
    # argument and to avoid circular imports.
    mode_manager = modeman.instance(win_id)
    if objects.backend == usertypes.Backend.QtWebEngine:
        from qutebrowser.browser.webengine import webenginetab
        tab_class: type[AbstractTab] = webenginetab.WebEngineTab
    elif objects.backend == usertypes.Backend.QtWebKit:
        from qutebrowser.browser.webkit import webkittab
        tab_class = webkittab.WebKitTab
    else:
        raise utils.Unreachable(objects.backend)
    return tab_class(win_id=win_id, mode_manager=mode_manager, private=private,
                     parent=parent)


class WebTabError(Exception):

    """Base class for various errors."""


class UnsupportedOperationError(WebTabError):

    """Raised when an operation is not supported with the given backend."""


class TerminationStatus(enum.Enum):

    """How a QtWebEngine renderer process terminated.

    Also see QWebEnginePage::RenderProcessTerminationStatus
    """

    #: Unknown render process status value gotten from Qt.
    unknown = -1
    #: The render process terminated normally.
    normal = 0
    #: The render process terminated with with a non-zero exit status.
    abnormal = 1
    #: The render process crashed, for example because of a segmentation fault.
    crashed = 2
    #: The render process was killed, for example by SIGKILL or task manager kill.
    killed = 3


@dataclasses.dataclass
class TabData:

    """A simple namespace with a fixed set of attributes.

    Attributes:
        keep_icon: Whether the (e.g. cloned) icon should not be cleared on page
                   load.
        inspector: The QWebInspector used for this webview.
        viewing_source: Set if we're currently showing a source view.
                        Only used when sources are shown via pygments.
        open_target: Where to open the next link.
                     Only used for QtWebKit.
        override_target: Override for open_target for fake clicks (like hints).
                         Only used for QtWebKit.
        pinned: Flag to pin the tab.
        fullscreen: Whether the tab has a video shown fullscreen currently.
        netrc_used: Whether netrc authentication was performed.
        input_mode: current input mode for the tab.
        splitter: InspectorSplitter used to show inspector inside the tab.
    """

    keep_icon: bool = False
    viewing_source: bool = False
    inspector: Optional['AbstractWebInspector'] = None
    open_target: usertypes.ClickTarget = usertypes.ClickTarget.normal
    override_target: Optional[usertypes.ClickTarget] = None
    pinned: bool = False
    fullscreen: bool = False
    netrc_used: bool = False
    input_mode: usertypes.KeyMode = usertypes.KeyMode.normal
    last_navigation: Optional[usertypes.NavigationRequest] = None
    splitter: Optional[miscwidgets.InspectorSplitter] = None

    def should_show_icon(self) -> bool:
        return (config.val.tabs.favicons.show == 'always' or
                config.val.tabs.favicons.show == 'pinned' and self.pinned)


class AbstractAction:

    """Attribute ``action`` of AbstractTab for Qt WebActions."""

    action_base: type['QWebEnginePage.WebAction']

    def __init__(self, tab: 'AbstractTab') -> None:
        self._widget = cast(_WidgetType, None)
        self._tab = tab

    def exit_fullscreen(self) -> None:
        """Exit the fullscreen mode."""
        raise NotImplementedError

    def save_page(self) -> None:
        """Save the current page."""
        raise NotImplementedError

    def run_string(self, name: str) -> None:
        """Run a webaction based on its name."""
        try:
            member = getattr(self.action_base, name)
        except AttributeError:
            raise WebTabError(f"{name} is not a valid web action!")
        self._widget.triggerPageAction(member)

    def show_source(self, pygments: bool = False) -> None:
        """Show the source of the current page in a new tab."""
        raise NotImplementedError

    def _show_html_source(self, html: str) -> None:
        """Show the given HTML as source page."""
        tb = objreg.get('tabbed-browser', scope='window', window=self._tab.win_id)
        new_tab = tb.tabopen(background=False, related=True)
        new_tab.set_html(html, self._tab.url())
        new_tab.data.viewing_source = True

    def _show_source_fallback(self, source: str) -> None:
        """Show source with pygments unavailable."""
        html = jinja.render(
            'pre.html',
            title='Source',
            content=source,
            preamble="Note: The optional Pygments dependency wasn't found - "
            "showing unhighlighted source.",
        )
        self._show_html_source(html)

    def _show_source_pygments(self) -> None:

        def show_source_cb(source: str) -> None:
            """Show source as soon as it's ready."""
            try:
                import pygments
                import pygments.lexers
                import pygments.formatters
            except ImportError:
                # Pygments is an optional dependency
                self._show_source_fallback(source)
                return

            try:
                lexer = pygments.lexers.HtmlLexer()
                formatter = pygments.formatters.HtmlFormatter(
                    full=True, linenos='table')
            except AttributeError:
                # Remaining namespace package from Pygments
                self._show_source_fallback(source)
                return

            html = pygments.highlight(source, lexer, formatter)
            self._show_html_source(html)

        self._tab.dump_async(show_source_cb)


class AbstractPrinting(QObject):

    """Attribute ``printing`` of AbstractTab for printing the page."""

    printing_finished = pyqtSignal(bool)
    pdf_printing_finished = pyqtSignal(str, bool)  # filename, ok

    def __init__(self, tab: 'AbstractTab', parent: QWidget | None | None = None) -> None:
        super().__init__(parent)
        self._widget = cast(_WidgetType, None)
        self._tab = tab
        self._dialog: Optional[QPrintDialog] = None
        self.printing_finished.connect(self._on_printing_finished)
        self.pdf_printing_finished.connect(self._on_pdf_printing_finished)

    @pyqtSlot(bool)
    def _on_printing_finished(self, ok: bool) -> None:
        # Only reporting error here, as the user has feedback from the dialog
        # (and probably their printer) already.
        if not ok:
            message.error("Printing failed!")
        if self._dialog is not None:
            self._dialog.deleteLater()
            self._dialog = None

    @pyqtSlot(str, bool)
    def _on_pdf_printing_finished(self, path: str, ok: bool) -> None:
        if ok:
            message.info(f"Printed to {path}")
        else:
            message.error(f"Printing to {path} failed!")

    def check_pdf_support(self) -> None:
        """Check whether writing to PDFs is supported.

        If it's not supported (by the current Qt version), a WebTabError is
        raised.
        """
        raise NotImplementedError

    def check_preview_support(self) -> None:
        """Check whether showing a print preview is supported.

        If it's not supported (by the current Qt version), a WebTabError is
        raised.
        """
        raise NotImplementedError

    def to_pdf(self, path: pathlib.Path) -> None:
        """Print the tab to a PDF with the given filename."""
        raise NotImplementedError

    def to_printer(self, printer: QPrinter) -> None:
        """Print the tab.

        Args:
            printer: The QPrinter to print to.
        """
        raise NotImplementedError

    def _do_print(self) -> None:
        assert self._dialog is not None
        printer = self._dialog.printer()
        assert printer is not None
        self.to_printer(printer)

    def show_dialog(self) -> None:
        """Print with a QPrintDialog."""
        self._dialog = QPrintDialog(self._tab)
        self._dialog.open(self._do_print)
        # Gets cleaned up in on_printing_finished


@dataclasses.dataclass
class SearchMatch:

    """The currently highlighted search match.

    Attributes:
        current: The currently active search match on the page.
                 0 if no search is active or the feature isn't available.
        total: The total number of search matches on the page.
               0 if no search is active or the feature isn't available.
    """

    current: int = 0
    total: int = 0

    def reset(self) -> None:
        """Reset match counter information.

        Stale information could lead to next_result or prev_result misbehaving.
        """
        self.current = 0
        self.total = 0

    def is_null(self) -> bool:
        """Whether the SearchMatch is set to zero."""
        return self.current == 0 and self.total == 0

    def at_limit(self, going_up: bool) -> bool:
        """Whether the SearchMatch is currently at the first/last result."""
        return (
            self.total != 0 and
            (
                going_up and self.current == 1 or
                not going_up and self.current == self.total
            )
        )

    def __str__(self) -> str:
        return f"{self.current}/{self.total}"


class SearchNavigationResult(enum.Enum):

    """The outcome of calling prev_/next_result."""

    found = enum.auto()
    not_found = enum.auto()

    wrapped_bottom = enum.auto()
    wrap_prevented_bottom = enum.auto()

    wrapped_top = enum.auto()
    wrap_prevented_top = enum.auto()


class AbstractSearch(QObject):

    """Attribute ``search`` of AbstractTab for doing searches.

    Attributes:
        text: The last thing this view was searched for.
        search_displayed: Whether we're currently displaying search results in
                          this view.
        match: The currently active search match.
        _flags: The flags of the last search (needs to be set by subclasses).
        _widget: The underlying WebView widget.

    Signals:
        finished: A search has finished. True if the text was found, false otherwise.
        match_changed: The currently active search match has changed.
                       Emits SearchMatch(0, 0) if no search is active.
                       Will not be emitted if search matches are not available.
        cleared: An existing search was cleared.
    """

    finished = pyqtSignal(bool)
    match_changed = pyqtSignal(SearchMatch)
    cleared = pyqtSignal()

    _Callback = Callable[[bool], None]
    _NavCallback = Callable[[SearchNavigationResult], None]

    def __init__(self, tab: 'AbstractTab', parent: QWidget | None | None = None):
        super().__init__(parent)
        self._tab = tab
        self._widget = cast(_WidgetType, None)
        self.text: Optional[str] = None
        self.search_displayed = False
        self.match = SearchMatch()

    def _is_case_sensitive(self, ignore_case: usertypes.IgnoreCase) -> bool:
        """Check if case-sensitivity should be used.

        This assumes self.text is already set properly.

        Arguments:
            ignore_case: The ignore_case value from the config.
        """
        assert self.text is not None
        mapping = {
            usertypes.IgnoreCase.smart: not self.text.islower(),
            usertypes.IgnoreCase.never: True,
            usertypes.IgnoreCase.always: False,
        }
        return mapping[ignore_case]

    def search(self, text: str, *,
               ignore_case: usertypes.IgnoreCase = usertypes.IgnoreCase.never,
               reverse: bool = False,
               result_cb: _Callback | None = None) -> None:
        """Find the given text on the page.

        Args:
            text: The text to search for.
            ignore_case: Search case-insensitively.
            reverse: Reverse search direction.
            result_cb: Called with a bool indicating whether a match was found.
        """
        raise NotImplementedError

    def clear(self) -> None:
        """Clear the current search."""
        raise NotImplementedError

    def prev_result(self, *, wrap: bool = False, callback: _NavCallback | None = None) -> None:
        """Go to the previous result of the current search.

        Args:
            wrap: Allow wrapping at the top or bottom of the page.
            callback: Called with a SearchNavigationResult.
        """
        raise NotImplementedError

    def next_result(self, *, wrap: bool = False, callback: _NavCallback | None = None) -> None:
        """Go to the next result of the current search.

        Args:
            wrap: Allow wrapping at the top or bottom of the page.
            callback: Called with a SearchNavigationResult.
        """
        raise NotImplementedError


class AbstractZoom(QObject):

    """Attribute ``zoom`` of AbstractTab for controlling zoom."""

    def __init__(self, tab: 'AbstractTab', parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._tab = tab
        self._widget = cast(_WidgetType, None)
        # Whether zoom was changed from the default.
        self._default_zoom_changed = False
        self._init_neighborlist()
        config.instance.changed.connect(self._on_config_changed)
        self._zoom_factor = float(config.val.zoom.default) / 100

    @pyqtSlot(str)
    def _on_config_changed(self, option: str) -> None:
        if option in ['zoom.levels', 'zoom.default']:
            if not self._default_zoom_changed:
                factor = float(config.val.zoom.default) / 100
                self.set_factor(factor)
            self._init_neighborlist()

    def _init_neighborlist(self) -> None:
        """Initialize self._neighborlist.

        It is a NeighborList with the zoom levels."""
        levels = config.val.zoom.levels
        self._neighborlist: usertypes.NeighborList = usertypes.NeighborList(
            levels, mode=usertypes.NeighborList.Modes.edge)
        self._neighborlist.fuzzyval = config.val.zoom.default

    def apply_offset(self, offset: int) -> float:
        """Increase/Decrease the zoom level by the given offset.

        Args:
            offset: The offset in the zoom level list.

        Return:
            The new zoom level.
        """
        level = self._neighborlist.getitem(offset)
        self.set_factor(float(level) / 100, fuzzyval=False)
        return level

    def _set_factor_internal(self, factor: float) -> None:
        raise NotImplementedError

    def set_factor(self, factor: float, *, fuzzyval: bool = True) -> None:
        """Zoom to a given zoom factor.

        Args:
            factor: The zoom factor as float.
            fuzzyval: Whether to set the NeighborLists fuzzyval.
        """
        if fuzzyval:
            self._neighborlist.fuzzyval = int(factor * 100)
        if factor < 0:
            raise ValueError("Can't zoom to factor {}!".format(factor))

        default_zoom_factor = float(config.val.zoom.default) / 100
        self._default_zoom_changed = factor != default_zoom_factor

        self._zoom_factor = factor
        self._set_factor_internal(factor)

    def factor(self) -> float:
        return self._zoom_factor

    def apply_default(self) -> None:
        self._set_factor_internal(float(config.val.zoom.default) / 100)

    def reapply(self) -> None:
        self._set_factor_internal(self._zoom_factor)


class SelectionState(enum.Enum):

    """Possible states of selection in caret mode.

    NOTE: Names need to line up with SelectionState in caret.js!
    """

    none = enum.auto()
    normal = enum.auto()
    line = enum.auto()


class AbstractCaret(QObject):

    """Attribute ``caret`` of AbstractTab for caret browsing."""

    #: Signal emitted when the selection was toggled.
    selection_toggled = pyqtSignal(SelectionState)
    #: Emitted when a ``follow_selection`` action is done.
    follow_selected_done = pyqtSignal()

    def __init__(self,
                 tab: 'AbstractTab',
                 mode_manager: modeman.ModeManager,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._widget = cast(_WidgetType, None)
        self._mode_manager = mode_manager
        mode_manager.entered.connect(self._on_mode_entered)
        mode_manager.left.connect(self._on_mode_left)
        self._tab = tab

    def _on_mode_entered(self, mode: usertypes.KeyMode) -> None:
        raise NotImplementedError

    def _on_mode_left(self, mode: usertypes.KeyMode) -> None:
        raise NotImplementedError

    def move_to_next_line(self, count: int = 1) -> None:
        raise NotImplementedError

    def move_to_prev_line(self, count: int = 1) -> None:
        raise NotImplementedError

    def move_to_next_char(self, count: int = 1) -> None:
        raise NotImplementedError

    def move_to_prev_char(self, count: int = 1) -> None:
        raise NotImplementedError

    def move_to_end_of_word(self, count: int = 1) -> None:
        raise NotImplementedError

    def move_to_next_word(self, count: int = 1) -> None:
        raise NotImplementedError

    def move_to_prev_word(self, count: int = 1) -> None:
        raise NotImplementedError

    def move_to_start_of_line(self) -> None:
        raise NotImplementedError

    def move_to_end_of_line(self) -> None:
        raise NotImplementedError

    def move_to_start_of_next_block(self, count: int = 1) -> None:
        raise NotImplementedError

    def move_to_start_of_prev_block(self, count: int = 1) -> None:
        raise NotImplementedError

    def move_to_end_of_next_block(self, count: int = 1) -> None:
        raise NotImplementedError

    def move_to_end_of_prev_block(self, count: int = 1) -> None:
        raise NotImplementedError

    def move_to_start_of_document(self) -> None:
        raise NotImplementedError

    def move_to_end_of_document(self) -> None:
        raise NotImplementedError

    def toggle_selection(self, line: bool = False) -> None:
        raise NotImplementedError

    def drop_selection(self) -> None:
        raise NotImplementedError

    def selection(self, callback: Callable[[str], None]) -> None:
        raise NotImplementedError

    def reverse_selection(self) -> None:
        raise NotImplementedError

    def _follow_enter(self, tab: bool) -> None:
        """Follow a link by faking an enter press."""
        if tab:
            self._tab.fake_key_press(Qt.Key.Key_Enter, modifier=Qt.KeyboardModifier.ControlModifier)
        else:
            self._tab.fake_key_press(Qt.Key.Key_Enter)

    def follow_selected(self, *, tab: bool = False) -> None:
        raise NotImplementedError


class AbstractScroller(QObject):

    """Attribute ``scroller`` of AbstractTab to manage scroll position."""

    #: Signal emitted when the scroll position changed (int, int)
    perc_changed = pyqtSignal(int, int)
    #: Signal emitted before the user requested a jump.
    #: Used to set the special ' mark so the user can return.
    before_jump_requested = pyqtSignal()

    def __init__(self, tab: 'AbstractTab', parent: QWidget | None = None):
        super().__init__(parent)
        self._tab = tab
        self._widget = cast(_WidgetType, None)
        if 'log-scroll-pos' in objects.debug_flags:
            self.perc_changed.connect(self._log_scroll_pos_change)

    @pyqtSlot()
    def _log_scroll_pos_change(self) -> None:
        log.webview.vdebug(  # type: ignore[attr-defined]
            "Scroll position changed to {}".format(self.pos_px()))

    def _init_widget(self, widget: _WidgetType) -> None:
        self._widget = widget

    def pos_px(self) -> QPoint:
        raise NotImplementedError

    def pos_perc(self) -> tuple[int, int]:
        raise NotImplementedError

    def to_perc(self, x: float | None = None, y: float | None = None) -> None:
        raise NotImplementedError

    def to_point(self, point: QPoint) -> None:
        raise NotImplementedError

    def to_anchor(self, name: str) -> None:
        raise NotImplementedError

    def delta(self, x: int = 0, y: int = 0) -> None:
        raise NotImplementedError

    def delta_page(self, x: float = 0, y: float = 0) -> None:
        raise NotImplementedError

    def up(self, count: int = 1) -> None:
        raise NotImplementedError

    def down(self, count: int = 1) -> None:
        raise NotImplementedError

    def left(self, count: int = 1) -> None:
        raise NotImplementedError

    def right(self, count: int = 1) -> None:
        raise NotImplementedError

    def top(self) -> None:
        raise NotImplementedError

    def bottom(self) -> None:
        raise NotImplementedError

    def page_up(self, count: int = 1) -> None:
        raise NotImplementedError

    def page_down(self, count: int = 1) -> None:
        raise NotImplementedError

    def at_top(self) -> bool:
        raise NotImplementedError

    def at_bottom(self) -> bool:
        raise NotImplementedError


class AbstractHistoryPrivate:

    """Private API related to the history."""

    _history: QWebEngineHistory

    def serialize(self) -> QByteArray:
        """Serialize into an opaque format understood by self.deserialize."""
        raise NotImplementedError

    def deserialize(self, data: QByteArray) -> None:
        """Deserialize from a format produced by self.serialize."""
        raise NotImplementedError

    def load_items(self, items: Sequence[sessions.TabHistoryItem]) -> None:
        """Deserialize from a list of TabHistoryItems."""
        raise NotImplementedError


class AbstractHistory:

    """The history attribute of a AbstractTab."""

    def __init__(self, tab: 'AbstractTab') -> None:
        self._tab = tab
        self._history:QWebEngineHistory|None = None 
        # self._history:QWebEngineHistory | None = None
        self.private_api = AbstractHistoryPrivate()

    def __len__(self) -> int:
        raise NotImplementedError

    def __iter__(self) -> Iterable[QWebEngineHistoryItem]:
        raise NotImplementedError

    def _check_count(self, count: int) -> None:
        """Check whether the count is positive."""
        if count < 0:
            raise WebTabError("count needs to be positive!")

    def current_idx(self) -> int:
        raise NotImplementedError

    def current_item(self) -> QWebEngineHistoryItem:
        raise NotImplementedError

    def back(self, count: int = 1) -> None:
        """Go back in the tab's history."""
        self._check_count(count)
        idx = self.current_idx() - count
        if idx >= 0:
            self._go_to_item(self._item_at(idx))
        else:
            self._go_to_item(self._item_at(0))
            raise WebTabError("At beginning of history.")

    def forward(self, count: int = 1) -> None:
        """Go forward in the tab's history."""
        self._check_count(count)
        idx = self.current_idx() + count
        if idx < len(self):
            self._go_to_item(self._item_at(idx))
        else:
            self._go_to_item(self._item_at(len(self) - 1))
            raise WebTabError("At end of history.")

    def can_go_back(self) -> bool:
        raise NotImplementedError

    def can_go_forward(self) -> bool:
        raise NotImplementedError

    def _item_at(self, i: int) -> Any:
        raise NotImplementedError

    def _go_to_item(self, item: Any) -> None:
        raise NotImplementedError

    def back_items(self) -> list[Any]:
        raise NotImplementedError

    def forward_items(self) -> list[Any]:
        raise NotImplementedError


class AbstractElements:

    """Finding and handling of elements on the page."""

    _MultiCallback = Callable[[Sequence['webelem.AbstractWebElement']], None]
    _SingleCallback = Callable[[Optional['webelem.AbstractWebElement']], None]
    _ErrorCallback = Callable[[Exception], None]

    def __init__(self, tab: 'AbstractTab') -> None:
        self._widget = cast(_WidgetType, None)
        self._tab = tab

    def find_css(self, selector: str,
                 callback: _MultiCallback,
                 error_cb: _ErrorCallback, *,
                 only_visible: bool = False) -> None:
        """Find all HTML elements matching a given selector async.

        If there's an error, the callback is called with a webelem.Error
        instance.

        Args:
            callback: The callback to be called when the search finished.
            error_cb: The callback to be called when an error occurred.
            selector: The CSS selector to search for.
            only_visible: Only show elements which are visible on screen.
        """
        raise NotImplementedError

    def find_id(self, elem_id: str, callback: _SingleCallback) -> None:
        """Find the HTML element with the given ID async.

        Args:
            callback: The callback to be called when the search finished.
                      Called with a WebEngineElement or None.
            elem_id: The ID to search for.
        """
        raise NotImplementedError

    def find_focused(self, callback: _SingleCallback) -> None:
        """Find the focused element on the page async.

        Args:
            callback: The callback to be called when the search finished.
                      Called with a WebEngineElement or None.
        """
        raise NotImplementedError

    def find_at_pos(self, pos: QPoint, callback: _SingleCallback) -> None:
        """Find the element at the given position async.

        This is also called "hit test" elsewhere.

        Args:
            pos: The QPoint to get the element for.
            callback: The callback to be called when the search finished.
                      Called with a WebEngineElement or None.
        """
        raise NotImplementedError


class AbstractAudio(QObject):

    """Handling of audio/muting for this tab."""

    muted_changed = pyqtSignal(bool)
    recently_audible_changed = pyqtSignal(bool)

    def __init__(self, tab: 'AbstractTab', parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._widget = cast(_WidgetType, None)
        self._tab = tab

    def set_muted(self, muted: bool, override: bool = False) -> None:
        """Set this tab as muted or not.

        Arguments:
            muted: Whether the tab is currently muted.
            override: If set to True, muting/unmuting was done manually and
                      overrides future automatic mute/unmute changes based on
                      the URL.
        """
        raise NotImplementedError

    def is_muted(self) -> bool:
        raise NotImplementedError

    def is_recently_audible(self) -> bool:
        """Whether this tab has had audio playing recently."""
        raise NotImplementedError


class AbstractTabPrivate:

    """Tab-related methods which are only needed in the core.

    Those methods are not part of the API which is exposed to extensions, and
    should ideally be removed at some point in the future.
    """

    def __init__(self, mode_manager: modeman.ModeManager,
                 tab: 'AbstractTab') -> None:
        self._widget = cast(_WidgetType, None)
        self._tab = tab
        self._mode_manager = mode_manager

    def event_target(self) -> Optional[QWidget]:
        """Return the widget events should be sent to."""
        raise NotImplementedError

    def handle_auto_insert_mode(self, ok: bool) -> None:
        """Handle `input.insert_mode.auto_load` after loading finished."""
        if not ok or not config.cache['input.insert_mode.auto_load']:
            return

        cur_mode = self._mode_manager.mode
        if cur_mode == usertypes.KeyMode.insert:
            return

        def _auto_insert_mode_cb(
                elem: Optional['webelem.AbstractWebElement']
        ) -> None:
            """Called from JS after finding the focused element."""
            if elem is None:
                log.webview.debug("No focused element!")
                return
            if elem.is_editable():
                modeman.enter(self._tab.win_id, usertypes.KeyMode.insert,
                              'load finished', only_if_normal=True)

        # There seems to be a race between loadFinished being called,
        # and the autoload attribute on websites actually focusing anything.
        # Thus, we delay this by a bit. Locally, a delay of 13ms caused no races
        # with 5000 test reruns (even with simultaneous CPU stress testing),
        # so 65ms should be a safe bet and still not be too noticeable.
        QTimer.singleShot(
            65, lambda: self._tab.elements.find_focused(_auto_insert_mode_cb))

    def clear_ssl_errors(self) -> None:
        raise NotImplementedError

    def networkaccessmanager(self) -> Optional[QNetworkAccessManager]:
        """Get the QNetworkAccessManager for this tab.

        This is only implemented for QtWebKit.
        For QtWebEngine, always returns None.
        """
        raise NotImplementedError

    def shutdown(self) -> None:
        raise NotImplementedError

    def run_js_sync(self, code: str) -> Any:
        """Run javascript sync.

        Result will be returned when running JS is complete.
        This is only implemented for QtWebKit.
        For QtWebEngine, always raises UnsupportedOperationError.
        """
        raise NotImplementedError

    def _recreate_inspector(self) -> None:
        """Recreate the inspector when detached to a window.

        This is needed to circumvent a QtWebEngine bug (which wasn't
        investigated further) which sometimes results in the window not
        appearing anymore.
        """
        self._tab.data.inspector = None
        self.toggle_inspector(inspector.Position.window)

    def toggle_inspector(self, position: Optional[inspector.Position]) -> None:
        """Show/hide (and if needed, create) the web inspector for this tab."""
        tabdata = self._tab.data
        if tabdata.inspector is None:
            assert tabdata.splitter is not None
            tabdata.inspector = self._init_inspector(
                splitter=tabdata.splitter,
                win_id=self._tab.win_id)
            self._tab.shutting_down.connect(tabdata.inspector.shutdown)
            tabdata.inspector.recreate.connect(self._recreate_inspector)
            tabdata.inspector.inspect(self._widget.page())
        tabdata.inspector.set_position(position)

    def _init_inspector(self, splitter: 'miscwidgets.InspectorSplitter',
           win_id: int,
           parent: QWidget | None = None) -> 'AbstractWebInspector':
        """Get a WebKitInspector/WebEngineInspector.

        Args:
            splitter: InspectorSplitter where the inspector can be placed.
            win_id: The window ID this inspector is associated with.
            parent: The Qt parent to set.
        """
        raise NotImplementedError


class AbstractTab(QWidget):

    """An adapter for WebView/WebEngineView representing a single tab."""

    #: Signal emitted when a website requests to close this tab.
    window_close_requested = pyqtSignal()
    #: Signal emitted when a link is hovered (the hover text)
    link_hovered = pyqtSignal(str)
    #: Signal emitted when a page started loading
    load_started = pyqtSignal()
    #: Signal emitted when a page is loading (progress percentage)
    load_progress = pyqtSignal(int)
    #: Signal emitted when a page finished loading (success as bool)
    load_finished = pyqtSignal(bool)
    #: Signal emitted when a page's favicon changed (icon as QIcon)
    icon_changed = pyqtSignal(QIcon)
    #: Signal emitted when a page's title changed (new title as str)
    title_changed = pyqtSignal(str)
    #: Signal emitted when this tab was pinned/unpinned (new pinned state as bool)
    pinned_changed = pyqtSignal(bool)
    #: Signal emitted when a new tab should be opened (url as QUrl)
    new_tab_requested = pyqtSignal(QUrl)
    #: Signal emitted when a page's URL changed (url as QUrl)
    url_changed = pyqtSignal(QUrl)
    #: Signal emitted when a tab's content size changed
    #: (new size as QSizeF)
    contents_size_changed = pyqtSignal(QSizeF)
    #: Signal emitted when a page requested full-screen (bool)
    fullscreen_requested = pyqtSignal(bool)
    #: Signal emitted before load starts (URL as QUrl)
    before_load_started = pyqtSignal(QUrl)

    # Signal emitted when a page's load status changed
    # (argument: usertypes.LoadStatus)
    load_status_changed = pyqtSignal(usertypes.LoadStatus)
    # Signal emitted before shutting down
    shutting_down = pyqtSignal()
    # Signal emitted when a history item should be added
    history_item_triggered = pyqtSignal(QUrl, QUrl, str)
    # Signal emitted when the underlying renderer process terminated.
    # arg 0: A TerminationStatus member.
    # arg 1: The exit code.
    renderer_process_terminated = pyqtSignal(TerminationStatus, int)

    # Hosts for which a certificate error happened. Shared between all tabs.
    #
    # Note that we remember hosts here, without scheme/port:
    # QtWebEngine/Chromium also only remembers hostnames, and certificates are
    # for a given hostname anyways.
    _insecure_hosts: set[str] = set()

    # Sub-APIs initialized by subclasses
    history: AbstractHistory
    scroller: AbstractScroller
    caret: AbstractCaret
    zoom: AbstractZoom
    search: AbstractSearch
    printing: AbstractPrinting
    action: AbstractAction
    elements: AbstractElements
    audio: AbstractAudio
    private_api: AbstractTabPrivate
    settings: websettings.AbstractSettings

    def __init__(self, *, win_id: int,
                 mode_manager: 'modeman.ModeManager',
                 private: bool,
                 parent: QWidget | None = None) -> None:
        utils.unused(mode_manager)  # needed for mypy
        self.is_private = private
        self.win_id = win_id
        self.tab_id = next(tab_id_gen)
        super().__init__(parent)

        self.registry = objreg.ObjectRegistry()
        tab_registry = objreg.get('tab-registry', scope='window',
                                  window=win_id)
        tab_registry[self.tab_id] = self
        objreg.register('tab', self, registry=self.registry)

        self.data = TabData()
        self._layout = miscwidgets.WrapperLayout(self)
        self._widget = cast(_WidgetType, None)
        self._progress = 0
        self._load_status = usertypes.LoadStatus.none
        self._tab_event_filter = eventfilter.TabEventFilter(
            self, parent=self)
        self.backend: Optional[usertypes.Backend] = None

        # If true, this tab has been requested to be removed (or is removed).
        self.pending_removal = False
        self.shutting_down.connect(functools.partial(
            setattr, self, 'pending_removal', True))

        self.before_load_started.connect(self._on_before_load_started)

    def _set_widget(self, widget: _WidgetType) -> None:
        # pylint: disable=protected-access
        self._widget = widget
        # FIXME:v4 ignore needed for QtWebKit
        self.data.splitter = miscwidgets.InspectorSplitter(
            win_id=self.win_id,
            main_webview=widget,  # type: ignore[arg-type,unused-ignore]
        )
        self._layout.wrap(self, self.data.splitter)
        self.history._history = widget.history()
        self.history.private_api._history = widget.history()
        self.scroller._init_widget(widget)
        self.caret._widget = widget
        self.zoom._widget = widget
        self.search._widget = widget
        self.printing._widget = widget
        self.action._widget = widget
        self.elements._widget = widget
        self.audio._widget = widget
        self.private_api._widget = widget
        self.settings._settings = widget.settings()

        self._install_event_filter()
        self.zoom.apply_default()

    def _install_event_filter(self) -> None:
        raise NotImplementedError

    def _set_load_status(self, val: usertypes.LoadStatus) -> None:
        """Setter for load_status."""
        if not isinstance(val, usertypes.LoadStatus):
            raise TypeError("Type {} is no LoadStatus member!".format(val))
        log.webview.debug("load status for {}: {}".format(repr(self), val))
        self._load_status = val
        self.load_status_changed.emit(val)

    def send_event(self, evt: QEvent) -> None:
        """Send the given event to the underlying widget.

        The event will be sent via QApplication.postEvent.
        Note that a posted event must not be re-used in any way!
        """
        # This only gives us some mild protection against re-using events, but
        # it's certainly better than a segfault.
        if getattr(evt, 'posted', False):
            raise utils.Unreachable("Can't re-use an event which was already "
                                    "posted!")

        recipient = self.private_api.event_target()
        if recipient is None:
            # https://github.com/qutebrowser/qutebrowser/issues/3888
            log.webview.warning("Unable to find event target!")
            return

        evt.posted = True  # type: ignore[attr-defined]
        QApplication.postEvent(recipient, evt)

    def navigation_blocked(self) -> bool:
        """Test if navigation is allowed on the current tab."""
        return self.data.pinned and config.val.tabs.pinned.frozen

    @pyqtSlot(QUrl)
    def _on_before_load_started(self, url: QUrl) -> None:
        """Adjust the title if we are going to visit a URL soon."""
        qtutils.ensure_valid(url)
        url_string = url.toDisplayString()
        log.webview.debug("Going to start loading: {}".format(url_string))
        self.title_changed.emit(url_string)

    @pyqtSlot(QUrl)
    def _on_url_changed(self, url: QUrl) -> None:
        """Update title when URL has changed and no title is available."""
        if url.isValid() and not self.title():
            self.title_changed.emit(url.toDisplayString())
        self.url_changed.emit(url)

    @pyqtSlot()
    def _on_load_started(self) -> None:
        self._progress = 0
        self.data.viewing_source = False
        self._set_load_status(usertypes.LoadStatus.loading)
        self.load_started.emit()

    @pyqtSlot(usertypes.NavigationRequest)
    def _on_navigation_request(
            self,
            navigation: usertypes.NavigationRequest
    ) -> None:
        """Handle common acceptNavigationRequest code."""
        url = utils.elide(navigation.url.toDisplayString(), 100)
        log.webview.debug(
            f"navigation request: url {url} (current {self.url().toDisplayString()}), "
            f"type {navigation.navigation_type.name}, "
            f"is_main_frame {navigation.is_main_frame}"
        )

        if navigation.is_main_frame:
            self.data.last_navigation = navigation

        if not navigation.url.isValid():
            if navigation.navigation_type == navigation.Type.link_clicked:
                msg = urlutils.get_errstring(navigation.url,
                                             "Invalid link clicked")
                message.error(msg)
                self.data.open_target = usertypes.ClickTarget.normal

            log.webview.debug("Ignoring invalid URL {} in "
                              "acceptNavigationRequest: {}".format(
                                  navigation.url.toDisplayString(),
                                  navigation.url.errorString()))
            navigation.accepted = False

        # WORKAROUND for QtWebEngine >= 6.2 not allowing form requests from
        # qute:// to outside domains.
        needs_load_workarounds = (
            objects.backend == usertypes.Backend.QtWebEngine and
            version.qtwebengine_versions().webengine >= utils.VersionNumber(6, 2)
        )
        if (
            needs_load_workarounds and
            self.url() == QUrl("qute://start/") and
            navigation.navigation_type == navigation.Type.form_submitted and
            navigation.url.matches(
                QUrl(config.val.url.searchengines['DEFAULT']),
                urlutils.FormatOption.REMOVE_QUERY)
        ):
            log.webview.debug(
                "Working around qute://start loading issue for "
                f"{navigation.url.toDisplayString()}")
            navigation.accepted = False
            self.load_url(navigation.url)

        if (
            needs_load_workarounds and
            self.url() == QUrl("qute://bookmarks/") and
            navigation.navigation_type == navigation.Type.back_forward
        ):
            log.webview.debug(
                "Working around qute://bookmarks loading issue for "
                f"{navigation.url.toDisplayString()}")
            navigation.accepted = False
            self.load_url(navigation.url)

    @pyqtSlot(bool)
    def _on_load_finished(self, ok: bool) -> None:
        assert self._widget is not None
        if self.is_deleted():
            # https://github.com/qutebrowser/qutebrowser/issues/3498
            return

        if sessions.session_manager is not None:
            sessions.session_manager.save_autosave()

        self.load_finished.emit(ok)

        if not self.title():
            self.title_changed.emit(self.url().toDisplayString())

        self.zoom.reapply()

    def _update_load_status(self, ok: bool) -> None:
        """Update the load status after a page finished loading.

        Needs to be called by subclasses to trigger a load status update, e.g.
        as a response to a loadFinished signal.
        """
        url = self.url()
        is_https = url.scheme() == 'https'

        if not ok:
            loadstatus = usertypes.LoadStatus.error
        elif is_https and url.host() in self._insecure_hosts:
            loadstatus = usertypes.LoadStatus.warn
        elif is_https:
            loadstatus = usertypes.LoadStatus.success_https
        else:
            loadstatus = usertypes.LoadStatus.success

        self._set_load_status(loadstatus)

    @pyqtSlot()
    def _on_history_trigger(self) -> None:
        """Emit history_item_triggered based on backend-specific signal."""
        raise NotImplementedError

    @pyqtSlot(int)
    def _on_load_progress(self, perc: int) -> None:
        self._progress = perc
        self.load_progress.emit(perc)

    def url(self, *, requested: bool = False) -> QUrl:
        raise NotImplementedError

    def progress(self) -> int:
        return self._progress

    def load_status(self) -> usertypes.LoadStatus:
        return self._load_status

    def _load_url_prepare(self, url: QUrl) -> None:
        qtutils.ensure_valid(url)
        self.before_load_started.emit(url)

    def load_url(self, url: QUrl) -> None:
        raise NotImplementedError

    def reload(self, *, force: bool = False) -> None:
        raise NotImplementedError

    def stop(self) -> None:
        raise NotImplementedError

    def fake_key_press(self,
                       key: Qt.Key,
                       modifier: Qt.KeyboardModifier = Qt.KeyboardModifier.NoModifier) -> None:
        """Send a fake key event to this tab."""
        press_evt = QKeyEvent(QEvent.Type.KeyPress, key, modifier, 0, 0, 0)
        release_evt = QKeyEvent(QEvent.Type.KeyRelease, key, modifier,
                                0, 0, 0)
        self.send_event(press_evt)
        self.send_event(release_evt)

    def dump_async(self,
                   callback: Callable[[str], None], *,
                   plain: bool = False) -> None:
        """Dump the current page's html asynchronously.

        The given callback will be called with the result when dumping is
        complete.
        """
        raise NotImplementedError

    def run_js_async(
            self,
            code: str,
            callback: Callable[[Any], None] | None = None, *,
            world: Union[usertypes.JsWorld, int] | None = None
    ) -> None:
        """Run javascript async.

        The given callback will be called with the result when running JS is
        complete.

        Args:
            code: The javascript code to run.
            callback: The callback to call with the result, or None.
            world: A world ID (int or usertypes.JsWorld member) to run the JS
                   in the main world or in another isolated world.
        """
        raise NotImplementedError

    def title(self) -> str:
        raise NotImplementedError

    def icon(self) -> QIcon:
        raise NotImplementedError

    def set_html(self, html: str, base_url: QUrl = QUrl()) -> None:
        raise NotImplementedError

    def set_pinned(self, pinned: bool) -> None:
        self.data.pinned = pinned
        self.pinned_changed.emit(pinned)

    def renderer_process_pid(self) -> Optional[int]:
        """Get the PID of the underlying renderer process.

        Returns None if the PID can't be determined or if getting the PID isn't
        supported.
        """
        raise NotImplementedError

    def grab_pixmap(self, rect: QRect | None = None) -> Optional[QPixmap]:
        """Grab a QPixmap of the displayed page.

        Returns None if we got a null pixmap from Qt.
        """
        if rect is None:
            pic = self._widget.grab()
        else:
            qtutils.ensure_valid(rect)
            # FIXME:v4 ignore needed for QtWebKit
            pic = self._widget.grab(rect)  # type: ignore[arg-type,unused-ignore]

        if pic.isNull():
            return None

        if machinery.IS_QT6:
            # FIXME:v4 cast needed for QtWebKit
            pic = cast(QPixmap, pic)

        return pic

    def __repr__(self) -> str:
        try:
            qurl = self.url()
            url = qurl.toDisplayString(urlutils.FormatOption.ENCODE_UNICODE)
        except (AttributeError, RuntimeError) as exc:
            url = '<{}>'.format(exc.__class__.__name__)
        else:
            url = utils.elide(url, 100)
        return utils.get_repr(self, tab_id=self.tab_id, url=url)

    def is_deleted(self) -> bool:
        """Check if the tab has been deleted."""
        assert self._widget is not None
        # FIXME:v4 cast needed for QtWebKit
        if machinery.IS_QT6:
            widget = cast(QWidget, self._widget)
        else:
            widget = self._widget
        return sip.isdeleted(widget)