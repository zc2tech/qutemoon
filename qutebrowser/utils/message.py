# SPDX-FileCopyrightText: Florian Bruhin (The Compiler) <mail@qutebrowser.org>
#
# SPDX-License-Identifier: GPL-3.0-or-later

# Because every method needs to have a log_stack argument
# and because we use *args a lot
# pylint: disable=unused-argument,differing-param-doc

"""Message singleton so we don't have to define unneeded signals."""

import dataclasses
import traceback
from typing import Any, Union, Optional
from collections.abc import Iterable, Callable

from qutebrowser.qt.core import pyqtSignal, pyqtBoundSignal, QObject

from qutebrowser.utils import usertypes, log


@dataclasses.dataclass
class MessageInfo:

    """Information associated with a message to be displayed."""

    level: usertypes.MessageLevel
    text: str
    replace: Optional[str] = None
    rich: bool = False


def _log_stack(typ: str, stack: str) -> None:
    """Log the given message stacktrace.

    Args:
        typ: The type of the message.
        stack: An optional stacktrace.
    """
    lines = stack.splitlines()
    stack_text = '\n'.join(line.rstrip() for line in lines)
    log.message.debug("Stack for {} message:\n{}".format(typ, stack_text))


def error(
    message: str, *,
    stack: str = "",
    replace: str = "",
    rich: bool = False,
) -> None:
    """Display an error message.

    Args:
        message: The message to show.
        stack: The stack trace to show (if any).
        replace: Replace existing messages which are still being shown.
        rich: Show message as rich text.
    """
    if stack is None:
        stack = ''.join(traceback.format_stack())
        typ = 'error'
    else:
        typ = 'error (from exception)'
    _log_stack(typ, stack)
    log.message.error(message)
    global_bridge.show(
        level=usertypes.MessageLevel.error,
        text=message,
        replace=replace,
        rich=rich,
    )


def warning(message: str, *, replace: str = "", rich: bool = False) -> None:
    """Display a warning message.

    Args:
        message: The message to show.
        replace: Replace existing messages which are still being shown.
        rich: Show message as rich text.
    """
    _log_stack('warning', ''.join(traceback.format_stack()))
    log.message.warning(message)
    global_bridge.show(
        level=usertypes.MessageLevel.warning,
        text=message,
        replace=replace,
        rich=rich,
    )


def info(message: str, *, replace: str = "", rich: bool = False) -> None:
    """Display an info message.

    Args:
        message: The message to show.
        replace: Replace existing messages which are still being shown.
        rich: Show message as rich text.
    """
    log.message.info(message)
    global_bridge.show(
        level=usertypes.MessageLevel.info,
        text=message,
        replace=replace,
        rich=rich,
    )


def _build_question(title: str,
                    text: str = "", *,
                    mode: usertypes.PromptMode,
                    default: Union[None, bool, str] = None,
                    abort_on: Iterable[pyqtBoundSignal] = (),
                    url: str = "",
                    option: bool | None = None) -> usertypes.Question:
    """Common function for ask/ask_async."""
    question = usertypes.Question()
    question.title = title
    question.text = text
    question.mode = mode
    question.default = default
    question.url = url

    if option is not None:
        if mode != usertypes.PromptMode.yesno:
            raise ValueError("Can only 'option' with PromptMode.yesno")
        if url is None:
            raise ValueError("Need 'url' given when 'option' is given")
    question.option = option

    for sig in abort_on:
        sig.connect(question.abort)
    return question


def ask(*args: Any, **kwargs: Any) -> Any:
    """Ask a modular question in the statusbar (blocking).

    Args:
        title: The message to display to the user.
        mode: A PromptMode.
        default: The default value to display.
        text: Additional text to show
        option: The option for always/never question answers.
                Only available with PromptMode.yesno.
        abort_on: A list of signals which abort the question if emitted.

    Return:
        The answer the user gave or None if the prompt was cancelled.
    """
    question = _build_question(*args, **kwargs)
    global_bridge.ask(question, blocking=True)
    answer = question.answer
    question.deleteLater()
    return answer


def ask_async(title: str,
              mode: usertypes.PromptMode,
              handler: Callable[[Any], None],
              **kwargs: Any) -> None:
    """Ask an async question in the statusbar.

    Args:
        title: The message to display to the user.
        mode: A PromptMode.
        handler: The function to get called with the answer as argument.
        default: The default value to display.
        text: Additional text to show.
    """
    question = _build_question(title, mode=mode, **kwargs)
    question.answered.connect(handler)
    question.completed.connect(question.deleteLater)
    global_bridge.ask(question, blocking=False)


_ActionType = Callable[[], Any]


def confirm_async(*, yes_action: _ActionType,
                  no_action: _ActionType| None = None,
                  cancel_action: _ActionType | None = None,
                  **kwargs: Any) -> usertypes.Question:
    """Ask a yes/no question to the user and execute the given actions.

    Args:
        title: The message to display to the user.
        yes_action: Callable to be called when the user answered yes.
        no_action: Callable to be called when the user answered no.
        cancel_action: Callable to be called when the user cancelled the
                       question.
        default: True/False to set a default value, or None.
        option: The option for always/never question answers.
        text: Additional text to show.

    Return:
        The question object.
    """
    kwargs['mode'] = usertypes.PromptMode.yesno
    question = _build_question(**kwargs)
    question.answered_yes.connect(yes_action)
    if no_action is not None:
        question.answered_no.connect(no_action)
    if cancel_action is not None:
        question.cancelled.connect(cancel_action)

    question.completed.connect(question.deleteLater)
    global_bridge.ask(question, blocking=False)
    return question


class GlobalMessageBridge(QObject):

    """Global (not per-window) message bridge for errors/infos/warnings.

    Attributes:
        _connected: Whether a slot is connected and we can show messages.
        _cache: Messages shown while we were not connected.

    Signals:
        show_message: Show a message
                      arg 0: A MessageLevel member
                      arg 1: The text to show
                      arg 2: A message ID (as string) to replace, or None.
        prompt_done: Emitted when a prompt was answered somewhere.
        ask_question: Ask a question to the user.
                      arg 0: The Question object to ask.
                      arg 1: Whether to block (True) or ask async (False).

                      IMPORTANT: Slots need to be connected to this signal via
                                 a Qt.ConnectionType.DirectConnection!
        mode_left: Emitted when a keymode was left in any window.
    """

    show_message = pyqtSignal(MessageInfo)
    prompt_done = pyqtSignal(usertypes.KeyMode)
    ask_question = pyqtSignal(usertypes.Question, bool)
    mode_left = pyqtSignal(usertypes.KeyMode)
    clear_messages = pyqtSignal()

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._connected = False
        self._cache: list[MessageInfo] = []

    def ask(self, question: usertypes.Question,
            blocking: bool, *,
            log_stack: bool = False) -> None:
        """Ask a question to the user.

        Note this method doesn't return the answer, it only blocks. The caller
        needs to construct a Question object and get the answer.

        Args:
            question: A Question object.
            blocking: Whether to return immediately or wait until the
                      question is answered.
            log_stack: ignored
        """
        self.ask_question.emit(question, blocking)

    def show(
        self,
        level: usertypes.MessageLevel,
        text: str,
        replace: str = "",
        rich: bool = False,
    ) -> None:
        """Show the given message."""
        msg = MessageInfo(level=level, text=text, replace=replace, rich=rich)
        if self._connected:
            self.show_message.emit(msg)
        else:
            self._cache.append(msg)

    def flush(self) -> None:
        """Flush messages which accumulated while no handler was connected.

        This is so we don't miss messages shown during some early init phase.
        It needs to be called once the show_message signal is connected.
        """
        self._connected = True
        for msg in self._cache:
            self.show(**dataclasses.asdict(msg))
        self._cache = []


global_bridge = GlobalMessageBridge()
