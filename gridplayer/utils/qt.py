from abc import ABCMeta

from PyQt5.QtCore import QCoreApplication, QObject
from PyQt5.QtWidgets import QApplication, QDialog, QWidget

QT_LOG_IGNORED = ("requestActivate() called for",)

# What a signal carrying milliseconds has to be declared as. PyQt maps a
# plain int to a 32-bit one, which runs out after 24.8 days and wraps
# without raising, quietly turning a time into a negative number. A live
# stream whose clock is counted from the epoch, as DASH manifests have
# it, is three orders of magnitude past that on its first frame.
MILLISECONDS = "qint64"


class QABC(type(QObject), ABCMeta):
    """Meta for abstract classes derived from QObject"""


def is_modal_open():
    return bool(QApplication.activeModalWidget() or QApplication.activePopupWidget())


def belongs_to_a_dialog(event_object) -> bool:
    """Whether an event went to a dialog rather than to the player.

    A modal dialog is out of the players' reach already: Qt sends its events
    nowhere else, and is_modal_open covers the rest. One that has to stay
    open while the videos are moved under it is not modal, though, and a
    press on its buttons reaches the player's event filters like any other.

    Read as a press on the video behind it -- the lookups behind that go by
    where the pointer is, not by what is over it -- it is answered by taking
    the focus to the player. A button that loses focus between the press and
    the release is not a click at all, so the first presses come to nothing,
    and once the player is the active window there is no focus left to take
    and the same presses start working.
    """

    widget = event_object if isinstance(event_object, QWidget) else None

    while widget is not None:
        if isinstance(widget, QDialog):
            return True

        widget = widget.parentWidget()

    return False


def qt_connect(*connections):
    for c_sig, c_slot in connections:
        c_sig.connect(c_slot)


def tr(text):
    return QCoreApplication.translate("@default", text)


def translate(context, text, disambiguation=None):
    return QCoreApplication.translate(context, text, disambiguation)


def is_qt_log_ignored(message):
    for ignored in QT_LOG_IGNORED:
        if ignored in message:
            return True

    return False
