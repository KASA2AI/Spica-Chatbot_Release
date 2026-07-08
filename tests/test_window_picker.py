"""Stage 3: WindowPickerDialog (Qt offscreen) -- renders the candidates the
galgame_window_candidates event carries and returns the picked window_id. The
modal exec() loop itself is real-machine territory; here we drive the non-modal
API (populate / select / read back)."""

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication  # noqa: E402

from ui.widgets.window_picker_dialog import WindowPickerDialog  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


CANDIDATES = [
    {"window_id": "0x1", "title": "LimeLight Lemonade Jam", "process_name": "wine", "pid": 11, "visible": True},
    {"window_id": "0x2", "title": "Firefox", "process_name": None, "pid": 22, "visible": True},
]


def test_populates_rows_from_candidate_dicts(qapp):
    dialog = WindowPickerDialog(CANDIDATES)
    assert dialog._list.count() == 2
    assert dialog._list.item(0).text() == "LimeLight Lemonade Jam — wine"
    assert dialog._list.item(1).text() == "Firefox"  # no process_name -> title only


def test_returns_selected_window_id(qapp):
    dialog = WindowPickerDialog(CANDIDATES)
    assert dialog.selected_window_id() == "0x1"  # first row preselected
    dialog._list.setCurrentRow(1)
    assert dialog.selected_window_id() == "0x2"


def test_empty_candidates_returns_none(qapp):
    dialog = WindowPickerDialog([])
    assert dialog._list.count() == 0
    assert dialog.selected_window_id() is None
