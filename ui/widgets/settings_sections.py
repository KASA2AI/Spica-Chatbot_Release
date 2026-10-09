"""Shared visual grouping for the desktop settings pages."""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFormLayout, QFrame, QLabel, QVBoxLayout


def section_form(layout, title):
    card = QFrame()
    card.setObjectName('settingsCard')
    content = QVBoxLayout(card)
    content.setContentsMargins(14, 12, 14, 14)
    content.setSpacing(10)
    heading = QLabel(title, card)
    heading.setObjectName('settingsSection')
    heading.setWordWrap(True)
    content.addWidget(heading)
    form = QFormLayout()
    form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
    form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
    form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
    form.setHorizontalSpacing(12)
    form.setVerticalSpacing(10)
    content.addLayout(form)
    layout.addWidget(card)
    return form
