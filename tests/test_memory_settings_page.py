"""The desktop editor writes through the same memory entrance used by recall."""
import time

import pytest

from memory.recent import RecentMemory
from memory.store import SQLiteMemoryStore
from spica.config.schema import AppConfig, CharacterConfig, MemoryConfig
from spica.core.chat_engine import ChatEngine
from spica.runtime.services import AgentServices


def test_editor_loads_older_memory_for_revision_and_deletion(qapp, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QMessageBox
    from ui.widgets.memory_settings_page import MemorySettingsPage
    engine = ChatEngine(AgentServices(llm_client=None, tts_adapter=None, visual_tool=None,
        memory_store=SQLiteMemoryStore(tmp_path / 'pages.sqlite3'), recent_memory=RecentMemory(), config={}),
        AppConfig(character=CharacterConfig(character_id='spica', profile_override='test')))
    old_id = engine.remember('我喝咖啡一直加糖。')
    for index in range(500):
        engine.remember(f'我的项目档案编号{index}。')
    page = MemorySettingsPage()
    page.surface_provider = lambda: engine
    def idle():
        deadline = time.monotonic() + 3
        while page.worker is not None and time.monotonic() < deadline:
            qapp.processEvents()
            time.sleep(.005)
        assert page.worker is None
    try:
        page.refresh()
        idle()
        assert page.list.count() == 500
        assert old_id not in {row['id'] for row in page.rows}
        page.more_button.click()
        idle()
        assert page.list.count() == 501 and not page.more_button.isEnabled()
        assert len({row['id'] for row in page.rows}) == 501
        page.list.setCurrentRow(500)
        assert page._selected()['id'] == old_id
        page.editor.setPlainText('我喝咖啡一直不加糖，之前记错了。')
        page.save_button.click()
        idle()
        corrected = page._selected()['id']
        assert corrected != old_id and '不加糖' in page._selected()['content']
        monkeypatch.setattr(QMessageBox, 'exec', lambda dialog: 0)
        monkeypatch.setattr(QMessageBox, 'clickedButton', lambda dialog: next(button for button in dialog.buttons()
            if dialog.buttonRole(button) == QMessageBox.ButtonRole.DestructiveRole))
        page.delete_button.click()
        idle()
        assert corrected not in {row['id'] for row in engine.list_memory(limit=1000)}
    finally:
        page.close();page.deleteLater();qapp.processEvents()


def test_editor_revision_and_history_use_live_memory(qapp, tmp_path):
    from ui.widgets.memory_settings_page import MemorySettingsPage
    store = SQLiteMemoryStore(tmp_path / 'memory.sqlite3')
    engine = ChatEngine(AgentServices(llm_client=None, tts_adapter=None, visual_tool=None,
        memory_store=store, recent_memory=RecentMemory(), config={}),
        AppConfig(character=CharacterConfig(character_id='spica', profile_override='test')))
    entry_id = engine.remember('喜欢甜咖啡。')
    page = MemorySettingsPage()
    page.surface_provider = lambda: engine

    def idle():
        deadline = time.monotonic() + 3
        while page.worker is not None and time.monotonic() < deadline:
            qapp.processEvents()
            time.sleep(.005)
        assert page.worker is None

    try:
        page.refresh()
        idle()
        assert page.list.count() == 1
        page.list.setCurrentRow(0)
        assert '喜欢甜咖啡' in page.sources.toPlainText()
        page.editor.setPlainText('现在咖啡不加糖。')
        page.reason.setCurrentIndex(1)
        page.save_button.click()
        idle()
        assert engine.list_memory()[0]['content'] == '现在咖啡不加糖。'
        assert page._selected()['id'] != entry_id
        page.history.setChecked(True)
        idle()
        assert page.list.count() == 2
        past = next(page.list.item(i) for i in range(2)
                    if page.list.item(i).text().startswith('过去'))
        page.list.setCurrentItem(past)
        assert page.save_button.isEnabled()
        assert not page.reason.isEnabled() and page.reason.currentData() == 'correction'
        assert '真实的过去状态' in page.sources.toPlainText()
    finally:
        page.close()
        page.deleteLater()
        qapp.processEvents()


@pytest.fixture
def qapp():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def test_editor_does_not_label_expired_phase_as_current(qapp):
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QListWidgetItem
    from ui.widgets.memory_settings_page import MemorySettingsPage
    page = MemorySettingsPage()
    try:
        item = QListWidgetItem('这周忙，暂时不看动漫。',page.list)
        item.setData(Qt.ItemDataRole.UserRole,dict(id=1,content=item.text(),status='active',revision=1,
                     kind='state',sources=[],valid_until=1577836800.0))
        page.list.setCurrentItem(item)
        assert '已过有效期' in page.sources.toPlainText()
        assert '2020' in page.sources.toPlainText()
        assert '当前有效' not in page.sources.toPlainText()
    finally:
        page.close();page.deleteLater();qapp.processEvents()


def test_editor_shows_held_processing_and_requires_button_for_new_attempts(qapp, tmp_path):
    from spica.ports.memory import MemoryScope
    from ui.widgets.memory_settings_page import MemorySettingsPage
    engine = ChatEngine(AgentServices(llm_client=None, tts_adapter=None, visual_tool=None,
        memory_store=SQLiteMemoryStore(tmp_path / 'held-ui.sqlite3'), recent_memory=RecentMemory(), config={}),
        AppConfig(character=CharacterConfig(character_id='spica', profile_override='test'), memory=MemoryConfig(consolidation_enabled=True)))
    memory = engine.deps.memory
    scope = MemoryScope('spica', 'owner', 'default')
    memory.record_evidence(scope, event_id='held:user', kind='user', source='desktop', content='保留这句。')
    memory.journal.hold_processing(scope, memory.journal.snapshot(scope), 'input_budget')
    page = MemorySettingsPage()
    page.surface_provider = lambda: engine
    def idle():
        deadline = time.monotonic()+2
        while page.worker is not None and time.monotonic() < deadline:
            qapp.processEvents()
            time.sleep(.005)
        assert page.worker is None
    try:
        page.refresh()
        idle()
        assert '暂停 1' in page.processing_status.text()
        assert memory.maintenance_status(scope)['held'] == 1
        assert page.resume_processing_button.isEnabled()
        page.resume_processing_button.click()
        idle()
        assert memory.maintenance_status(scope)['held'] == 0
    finally:
        page.close();page.deleteLater();qapp.processEvents()
