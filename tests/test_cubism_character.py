"""Native presentation lifecycle without loading the optional SDK in unit tests."""
import pytest
pytest.importorskip('PySide6')
from PySide6.QtWidgets import QApplication
from ui.widgets.cubism_character import CubismCanvas
from spica.core.cubism import CubismModel
from spica.core.cubism_motion import CubismPlayback


class Model:
    def __init__(self):
        self.expression = None
        self.started = []
        self.finished = False
    def SetExpression(self, expression): self.expression = expression
    def ResetExpressions(self): self.expression = None
    def StopAllMotions(self): self.finished = True
    def ResetAllParameters(self): pass
    def IsMotionFinished(self): return self.finished
    def StartMotion(self, group, index, priority):
        self.started.append(index)
        self.finished = False


def test_same_model_costume_updates_bindings_without_reloading_assets(tmp_path, monkeypatch):
    from unittest.mock import Mock

    app = QApplication.instance() or QApplication([])
    canvas = CubismCanvas()
    (tmp_path / 'model.model3.json').write_text('{"FileReferences":{"Moc":"model.moc3"}}')
    (tmp_path / 'model.moc3').write_bytes(b'MOC3')
    first = CubismModel(model='model.model3.json', idle='wait', motions={
        'wait': {'group': 'Idle', 'index': 0, 'kind': 'idle'}},
        parameters={'fixed': {'ParamHat': 1}})
    second = CubismModel(model=first.model, idle='wait', motions={
        'wait': {'group': 'Idle', 'index': 1, 'kind': 'idle'}},
        parameters={'fixed': {'ParamHat': 0}}, scale=1.2, offset=(.1, .2))
    model = Mock()
    model.GetParameterIds.return_value = ['ParamHat']
    model.HasMocConsistencyFromFile.return_value = True
    model.IsMotionFinished.return_value = False
    canvas._native = Mock()
    canvas._native.Model.return_value = model
    for method in ('makeCurrent', 'doneCurrent'):
        monkeypatch.setattr(canvas, method, lambda: None)
    monkeypatch.setattr(canvas, 'isValid', lambda: True)
    try:
        canvas.set_model(tmp_path, first)
        canvas.paintGL()
        model.SetParameterValue.assert_called_with(0, 1)
        canvas.set_model(tmp_path, second)
        canvas.paintGL()
        model.SetParameterValue.assert_called_with(0, 0)
        model.SetScale.assert_called_with(1.2)
        model.SetOffset.assert_called_with(.1, .2)
        model.StartMotion.assert_called_with('Idle', 1, 3)
        assert canvas._playback.spec == second
        canvas._native.Model.assert_called_once()
        model.LoadModelJson.assert_called_once()
        calls = model.StopAllMotions.call_count
        canvas.set_model(tmp_path, second.model_copy(deep=True))
        assert model.StopAllMotions.call_count == calls
    finally:
        canvas.release()
        canvas.close()


def test_normal_completion_keeps_mood_until_idle_timeout_and_interrupt_clears_actions(monkeypatch):
    app = QApplication.instance() or QApplication([])
    canvas = CubismCanvas()
    spec = CubismModel(model='model.model3.json', idle='wait', motions={
        'wait': {'group': '', 'index': 0, 'kind': 'idle'},
        'smiling-wait': {'group': '', 'index': 1, 'kind': 'idle'},
        'happy': {'group': '', 'index': 2},
    }, expressions={'smile': 'smile'}, neutral_expression='neutral', idle_motions={'smile':'smiling-wait'})
    canvas.spec = spec
    canvas.model = Model()
    canvas._playback = CubismPlayback(spec)
    canvas.reset_performance()
    canvas.apply_cue({'turn':'one','index':0,'intent':'thanks','expression':'smile','motion':'happy'})
    canvas._advance_director(10)
    assert canvas.model.expression == 'smile'
    canvas.model.finished = True
    canvas._advance_idle(13)
    assert canvas.model.started[-1] == 1
    monkeypatch.setattr('ui.widgets.cubism_character.time.monotonic', lambda: 14)
    canvas.finish_performance()
    assert canvas.model.expression == 'smile'
    canvas._advance_idle(20)
    assert canvas.model.expression == 'smile'
    canvas._advance_idle(40)
    assert canvas.model.expression == 'neutral'
    canvas.apply_cue({'turn':'two','index':0,'intent':'thanks','expression':'smile','motion':'happy'})
    canvas._advance_director(41)
    canvas._effect = ('old effect', 41)
    canvas._mouth = .8
    canvas.reset_performance(preserve_expression=True)
    assert canvas.model.expression == 'smile'
    assert canvas._mouth == 0 and canvas._effect is None
    assert canvas._playback.advance(50) is None
    # No GL context was created; the injected model is a boundary double.
    canvas.model = None
    canvas.close()


def test_costume_group_selects_variants_and_all_selectors_respect_busy_state():
    from ui.widgets.settings_panel import SettingsPanel
    app = QApplication.instance() or QApplication([])
    panel = SettingsPanel()
    selected = []
    panel.costume_changed.connect(selected.append)
    panel.set_costume_groups({'法袍': ['classic', 'hatless']}, {'classic': '原版', 'hatless': '无帽', 'festival': '庆典'})
    panel.set_costumes(['classic', 'hatless', 'festival'], 'hatless')
    assert panel.costume_box.count() == 2
    assert panel.costume_box.currentText() == '法袍'
    assert panel.costume_variant_box.currentData() == 'hatless'
    assert selected == []  # Restoring preferences must not overwrite the selection.
    panel.costume_variant_box.setCurrentIndex(0)
    panel.costume_variant_box.activated.emit(0)
    assert selected == ['classic']
    panel.costume_box.setCurrentIndex(1)
    panel.costume_box.activated.emit(1)
    assert selected[-1] == 'festival'
    assert panel.costume_variant_box.isHidden()
    panel.set_costumes(['classic', 'hatless', 'festival'], 'hatless')
    panel.set_costume_enabled(False)
    assert not panel.costume_box.isEnabled() and not panel.costume_variant_box.isEnabled()
    panel.set_costume_enabled(True)
    assert panel.costume_variant_box.isEnabled()
    panel.close()
    legacy = SettingsPanel()
    legacy.set_costumes(['sana', 'spica'], 'sana')
    assert legacy.costume_variant_box is None
    legacy.costume_changed.connect(selected.append)
    legacy.costume_box.setCurrentIndex(1)
    legacy.costume_box.activated.emit(1)
    assert selected[-1] == 'spica'
    legacy.close()


def test_short_final_clause_keeps_its_mood_without_starting_its_pending_action(monkeypatch):
    app = QApplication.instance() or QApplication([])
    canvas = CubismCanvas()
    spec = CubismModel(model='model.model3.json', idle='wait', motions={
        'wait': {'group': '', 'index': 0, 'kind': 'idle'},
        'smiling-wait': {'group': '', 'index': 1, 'kind': 'idle'},
        'happy': {'group': '', 'index': 2},
    }, expressions={'sad': 'sad', 'smile': 'smile'}, idle_motions={'smile': 'smiling-wait'})
    canvas.spec, canvas.model = spec, Model()
    canvas._playback = CubismPlayback(spec, .85)
    canvas.reset_performance()
    canvas.apply_cue({'turn': 'one', 'index': 0, 'intent': 'sad', 'expression': 'sad', 'priority': 3})
    canvas._advance_director(10)
    canvas.apply_cue({'turn': 'one', 'index': 1, 'intent': 'thanks', 'expression': 'smile',
                      'priority': 1, 'motion': 'happy', 'effect': 'glow'})
    canvas._advance_director(10.4)
    assert canvas.model.expression == 'sad'  # Last clause is inside the face hold.
    monkeypatch.setattr('ui.widgets.cubism_character.time.monotonic', lambda: 10.5)
    canvas.finish_performance()
    assert canvas.model.expression == 'smile'
    assert canvas.model.started[-1] == 1
    assert 2 not in canvas.model.started  # A finished turn cannot start a queued gesture.
    assert canvas._effect is None
    canvas._advance_director(11)
    assert canvas.model.expression == 'smile'
    canvas.model = None
    canvas.close()
