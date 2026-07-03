"""Phase 5-c0 characterization: the LLM_CLIENT_NOT_CONFIGURED error path.

Pins the one error branch 5-c1 is about to rewire (``call_llm_node``'s
"no LLM client" guard becomes ``if not deps.llm_ready:``): a services bundle
with ``llm_client=None`` driven through the REAL sync chain must surface the
exact error code + message in the response payload. This path had ZERO test
coverage before -- without this pin, a mechanical flip to ``deps.llm is None``
(which is NEVER None thanks to ``from_services``'s adapter wrapping) would
silently kill the branch and nothing would go red.

Client-level and chain-level on purpose: the test must stay valid across the
Phase 5 flip (both sides read the same services bundle through the bridge).
"""

import tempfile
import unittest
from pathlib import Path

from agent_tools.function_tools import TOOL_SCHEMAS, default_tool_functions
from memory.recent import RecentMemory
from memory.store import SQLiteMemoryStore
from spica.runtime.context import TurnContext, TurnRequest
from spica.runtime.services import AgentServices
from spica.runtime.sync_chain import run_voice_pipeline


def _services(tmpdir: str) -> AgentServices:
    return AgentServices(
        llm_client=None,  # THE condition under test
        tts_adapter=None,
        visual_tool=None,
        memory_store=SQLiteMemoryStore(Path(tmpdir) / "m.sqlite3"),
        recent_memory=RecentMemory(max_turns=3),
        config={
            "model": "fake-model",
            "character_profile": "p",
            "interlocutor_name": "麦",
            "character_id": "spica",
            "recent_context_limit": 3,
            "long_term_memory_limit": 5,
            "max_tool_rounds": 2,
        },
        logger=lambda *a, **k: None,
        tool_functions=default_tool_functions(),
        tool_schemas=TOOL_SCHEMAS,
    )


class LlmClientNotConfiguredTest(unittest.TestCase):
    def test_sync_chain_surfaces_the_error_code_and_message(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = run_voice_pipeline(
                TurnContext(TurnRequest(conversation_id="c1", user_input="你好")),
                _services(tmp),
            )
        error = ctx.response_payload.get("error")
        self.assertIsNotNone(error, "payload must carry the error dict")
        self.assertEqual(error["code"], "LLM_CLIENT_NOT_CONFIGURED")
        self.assertEqual(error["message"], "LLM client 未配置。")


if __name__ == "__main__":
    unittest.main()
