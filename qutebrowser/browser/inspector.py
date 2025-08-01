# SPDX-FileCopyrightText: Florian Bruhin (The Compiler) <mail@qutebrowser.org>
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Base class for a QtWebKit/QtWebEngine web inspector."""

import base64
import binascii
import enum
from typing import cast, Optional, Any

from qutebrowser.qt.widgets import QWidget
from qutebrowser.qt.core import pyqtSignal, pyqtSlot, QObject, QEvent
from qutebrowser.qt.gui import QCloseEvent

from qutebrowser.browser import eventfilter
from qutebrowser.config import configfiles, config
from qutebrowser.utils import log, usertypes, qtutils
from qutebrowser.keyinput import modeman
from qutebrowser.misc import miscwidgets


# FIXME:mypy How to annotate this properly without running into Liskov issues?
_WidgetType = Any


class Position(enum.Enum):

    """Where the inspector is shown."""

    right = enum.auto()
    left = enum.auto()
    top = enum.auto()
    bottom = enum.auto()
    window = enum.auto()


class Error(Exception):

    """Raised when the inspector could not be initialized."""


class _EventFilter(QObject):

    """Event filter to enter insert mode when inspector was clicked.

    We need to use this with a ChildEventFilter (rather than just overriding
    mousePressEvent) for two reasons:

    - For QtWebEngine, we need to listen for mouse events on its focusProxy(),
      which can change when another page loads (which might be possible with an
      inspector as well?)

    - For QtWebKit, we need to listen for mouse events on the QWebView used by
      the QWebInspector.
    """

    clicked = pyqtSignal()

    def eventFilter(self, _obj: Optional[QObject], event: Optional[QEvent]) -> bool:
        """Translate mouse presses to a clicked signal."""
        assert event is not None
        if event.type() == QEvent.Type.MouseButtonPress:
            self.clicked.emit()
        return False


class AbstractWebInspector(QWidget):

    """Base class for QtWebKit/QtWebEngine inspectors.

    Attributes:
        _position: position of the inspector (right/left/top/bottom/window)
        _splitter: InspectorSplitter where the inspector can be placed.

    Signals:
        recreate: Emitted when the inspector should be recreated.
    """

    recreate = pyqtSignal()

    def __init__(self, splitter: 'miscwidgets.InspectorSplitter',
                 win_id: int,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._widget = cast(_WidgetType, None)
        self._layout = miscwidgets.WrapperLayout(self)
        self._splitter = splitter
        self._position: Optional[Position] = None
        self._win_id = win_id

        self._event_filter = _EventFilter(parent=self)
        self._event_filter.clicked.connect(self._on_clicked)
        self._child_event_filter = eventfilter.ChildEventFilter(
            eventfilter=self._event_filter,
            parent=self)

    def _set_widget(self, widget: _WidgetType) -> None:
        self._widget = widget
        self._widget.setWindowTitle("Web Inspector")
        self._widget.installEventFilter(self._child_event_filter)
        self._layout.wrap(self, self._widget)

    def _load_position(self) -> Position:
        """Get the last position the inspector was in."""
        pos = configfiles.state['inspector'].get('position', 'right')
        return Position[pos]

    def _save_position(self, position: Position) -> None:
        """Save the last position the inspector was in."""
        configfiles.state['inspector']['position'] = position.name

    def _needs_recreate(self) -> bool:
        """Whether the inspector needs recreation when detaching to a window.

        This is done due to an unknown QtWebEngine bug which sometimes prevents
        inspector windows from showing up.

        Needs to be overridden by subclasses.
        """
        return False

    @pyqtSlot()
    def _on_clicked(self) -> None:
        """Enter insert mode if a docked inspector was clicked."""
        if (self._position != Position.window and
                config.val.input.insert_mode.auto_enter):
            modeman.enter(self._win_id, usertypes.KeyMode.insert,
                          reason='Inspector clicked', only_if_normal=True)

    def set_position(self, position: Optional[Position]) -> None:
        """Set the position of the inspector.

        If the position is None, the last known position is used.
        """
        if position is None:
            position = self._load_position()
        else:
            self._save_position(position)

        if position == self._position:
            self.toggle()
            return

        if (position == Position.window and
                self._position is not None and
                self._needs_recreate()):
            # Detaching to window
            self.recreate.emit()
            self.shutdown()
            return
        elif position == Position.window:
            self.setParent(qtutils.QT_NONE)
            self._load_state_geometry()
        else:
            self._splitter.set_inspector(self, position)

        self._position = position

        self._widget.show()
        self.show()

    def toggle(self) -> None:
        """Toggle visibility of the inspector."""
        if self.isVisible():
            self.hide()
        else:
            self.show()

    def _load_state_geometry(self) -> None:
        """Load the geometry from the state file."""
        try:
            data = configfiles.state['inspector']['window']
            geom = base64.b64decode(data, validate=True)
        except KeyError:
            # First start
            pass
        except binascii.Error:
            log.misc.exception("Error while reading geometry")
        else:
            log.init.debug("Loading geometry from {!r}".format(geom))
            ok = self._widget.restoreGeometry(geom)
            if not ok:
                log.init.warning("Error while loading geometry.")

    def closeEvent(self, _e: Optional[QCloseEvent]) -> None:
        """Save the geometry when closed."""
        data = self._widget.saveGeometry().data()
        geom = base64.b64encode(data).decode('ASCII')
        configfiles.state['inspector']['window'] = geom

    # FIXME:mypy How to annotate 'page' properly without running into Liskov issues?
    def inspect(self, page: Any) -> None:
        """Inspect the given QWeb(Engine)Page."""
        raise NotImplementedError

    @pyqtSlot()
    def shutdown(self) -> None:
        """Clean up the inspector."""
        self.close()
        self.deleteLater()
