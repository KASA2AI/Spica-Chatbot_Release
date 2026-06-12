"""P5 step 3 wiring pins:

- the host tee (D-P5-0): the public companion_sink dispatches to the UI bridge
  AND enqueues into the reaction engine -- enqueue-only on the sink stack;
  engine off -> the tee is a plain forward (zero reaction overhead);
- the two beat readers (D-P5-6): prompt reader EXCLUDES silent reaction beats,
  dedupe reader INCLUDES them (source="spica" only);
- the REAL prompt injection ([COMPANION_CONTEXT] via retrieve_game_context_node)
  consumes the silent-excluding reader.
"""

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from spica.adapters.game_memory.sqlite import GameMemorySqliteAdapter
from spica.config.schema import AppConfig, CharacterConfig
from spica.galgame.models import CompanionBeat, utc_now_iso
from spica.host.app_host import AppHost
from spica.runtime.context import GameContextRequest, PromptBundle, TurnContext, TurnRequest
from spica.runtime.deps import TurnDeps
from spica.runtime.observer import DefaultTurnObserver
from spica.runtime.stages import retrieve_game_context_node
from spica.runtime.tools import RegistryToolSet


def _beat(beat_id, content, *, source="spica", silent=None, game_id="limelight"):
    meta = {} if silent is None else {"silent": silent, "trigger_text": "x"}
    return CompanionBeat(
        beat_id=beat_id, game_id=game_id, type="reaction" if source == "spica" else "shared_observation",
        content=content, source=source, created_at=utc_now_iso(),
        scope={"character_id": "spica", "user_id": "麦", "game_id": game_id},
        meta=meta,
    )


class HostTeeTest(unittest.TestCase):
    def test_dispatcher_forwards_to_ui_and_enqueues_to_engine(self):
        host = AppHost()
        ui_events, enqueued = [], []
        host.attach_companion_sink(ui_events.append)
        host.reaction_engine = SimpleNamespace(enqueue_event=enqueued.append)
        event = object()
        host.companion_sink(event)
        self.assertEqual(ui_events, [event])
        self.assertEqual(enqueued, [event])  # enqueue-only leg (D-P5-0)

    def test_engine_off_is_a_plain_forward(self):
        host = AppHost()  # reaction_engine stays None (mode off)
        ui_events = []
        host.attach_companion_sink(ui_events.append)
        host.companion_sink("evt")
        self.assertEqual(ui_events, ["evt"])

    def test_attach_order_no_longer_matters(self):
        # the public sink is a STABLE dispatcher: a consumer holding it from
        # before attach still reaches the UI bridge attached afterwards
        host = AppHost()
        sink = host.companion_sink
        ui_events = []
        host.attach_companion_sink(ui_events.append)
        sink("late")
        self.assertEqual(ui_events, ["late"])


class BeatReaderSplitTest(unittest.TestCase):
    def _seeded(self, tmp):
        adapter = GameMemorySqliteAdapter(Path(tmp) / "g.sqlite3")
        adapter.add_companion_beat(_beat("b1", "我刚才就觉得她有问题", silent=False))
        adapter.add_companion_beat(_beat("b2", "", silent=True))           # no_comment
        adapter.add_companion_beat(_beat("b3", "", silent=True))           # busy_drop
        adapter.add_companion_beat(_beat("b4", "麦说这条线很喜欢", source="user"))  # note 工具老形态(无 meta)
        return adapter

    def test_prompt_reader_excludes_silent_dedupe_includes(self):
        with TemporaryDirectory() as tmp:
            adapter = self._seeded(tmp)
            visible = adapter.recent_companion_beats_for_prompt("limelight", "麦", "spica")
            dedupe = adapter.recent_reaction_beats_for_dedupe("limelight", "麦", "spica")
        self.assertEqual(sorted(b.beat_id for b in visible), ["b1", "b4"])  # silent 排除,老 user 行保留
        self.assertEqual(sorted(b.beat_id for b in dedupe), ["b1", "b2", "b3"])  # 含 silent,仅 spica

    def test_companion_context_injection_uses_the_silent_excluding_reader(self):
        with TemporaryDirectory() as tmp:
            adapter = self._seeded(tmp)
            request = TurnRequest(
                user_input="刚才那段怎么样",
                conversation_id="galgame::limelight::default",
                game_context_request=GameContextRequest(mode="active", game_id="limelight"),
            )
            ctx = TurnContext(request)
            ctx.prompt = PromptBundle(prompt_input="[CURRENT_USER_INPUT]\n刚才那段怎么样")
            deps = TurnDeps(
                config=AppConfig(character=CharacterConfig(character_id="spica", interlocutor_name="麦")),
                llm=None, tts=None, visual=None, memory=None,
                tools=RegistryToolSet.from_function_table([], {}),
                game_memory=adapter,
                observer=DefaultTurnObserver(ctx.timing),
            )
            retrieve_game_context_node(ctx, None, deps)
            prompt = ctx.prompt.prompt_input
        self.assertIn("[COMPANION_CONTEXT]", prompt)
        self.assertIn("我刚才就觉得她有问题", prompt)  # 她真说过的话在
        self.assertIn("麦说这条线很喜欢", prompt)      # 用户 note 在
        # silent beat 零泄漏: 注入的 reaction 条目恰好 1 条(空 content 的两条没进)
        self.assertEqual(prompt.count('"type": "reaction"'), 1)


if __name__ == "__main__":
    unittest.main()
