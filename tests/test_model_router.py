"""ModelRouter unit contract (OO migration Phase 6b, 方案 A-ii).

Pins the router's four promises:
1. constructor is INERT -- stores the host ref, reads nothing, no I/O;
2. role_model fallbacks are byte-identical to the historical per-site
   expressions (summary_model / reaction_judge_model each falling back to the
   dialogue model independently);
3. for_role("summary"/"dialogue") binds the MAIN resolved adapter;
4. for_role("judge") takes its adapter THROUGH ``host._judge_llm_adapter()``
   (facade contract) -- ``patch.object(AppHost, "_judge_llm_adapter", ...)``
   must keep intercepting real construction (router-level patch-validity).

The judge endpoint fallback tree itself (key/base_url/reasoning) stays pinned
by tests/test_reaction_judge.py::JudgeKeySplitTest, which drives the SAME code
through the AppHost delegate with zero changes -- deliberately not duplicated
here; this file adds only the no-key sharing case for direct router coverage.
"""

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from spica.config.schema import AppConfig, GalgameConfig, LLMConfig
from spica.host.app_host import AppHost
from spica.host.model_router import ModelRouter
from spica.ports.model import BoundModel


class _ExplodingHost:
    """Any attribute access at construction time is a contract violation."""

    def __getattribute__(self, name):
        raise AssertionError(f"ModelRouter constructor touched host.{name}")


class ConstructorInertTest(unittest.TestCase):
    def test_constructor_reads_nothing_from_the_host(self):
        ModelRouter(_ExplodingHost())  # must not raise


def _stub_host(*, summary_model=None, judge_model=None, dialogue="dlg-m"):
    return SimpleNamespace(
        config=AppConfig(
            llm=LLMConfig(model=dialogue),
            galgame=GalgameConfig(
                summary_model=summary_model, reaction_judge_model=judge_model
            ),
        ),
        services=SimpleNamespace(llm_adapter=SimpleNamespace(name="main")),
        secrets=None,
    )


class RoleModelFallbackTest(unittest.TestCase):
    def test_summary_prefers_summary_model_else_dialogue(self):
        self.assertEqual(
            ModelRouter(_stub_host(summary_model="sum-m")).role_model("summary"), "sum-m"
        )
        self.assertEqual(ModelRouter(_stub_host()).role_model("summary"), "dlg-m")

    def test_judge_prefers_judge_model_else_dialogue(self):
        self.assertEqual(
            ModelRouter(_stub_host(judge_model="jdg-m")).role_model("judge"), "jdg-m"
        )
        self.assertEqual(ModelRouter(_stub_host()).role_model("judge"), "dlg-m")

    def test_dialogue_is_the_config_model(self):
        self.assertEqual(ModelRouter(_stub_host()).role_model("dialogue"), "dlg-m")


class ForRoleTest(unittest.TestCase):
    def test_summary_binds_the_main_adapter(self):
        host = _stub_host(summary_model="sum-m")
        bound = ModelRouter(host).for_role("summary")
        self.assertIsInstance(bound, BoundModel)
        self.assertIs(bound.adapter, host.services.llm_adapter)
        self.assertEqual(bound.model, "sum-m")

    def test_judge_with_no_key_shares_the_main_adapter(self):
        # Direct router coverage of the tree's no-key branch (the full tree is
        # pinned through the AppHost delegate by JudgeKeySplitTest, unchanged).
        host = _stub_host(judge_model="jdg-m")
        host._judge_llm_adapter = lambda: ModelRouter(host).judge_adapter()
        bound = ModelRouter(host).for_role("judge")
        self.assertIs(bound.adapter, host.services.llm_adapter)  # secrets=None -> share
        self.assertEqual(bound.model, "jdg-m")


class RouterPatchValidityTest(unittest.TestCase):
    """Router-level patch-validity: the judge adapter must flow through the
    AppHost delegate so the historical patch seam keeps intercepting."""

    def test_for_role_judge_takes_adapter_through_the_host_delegate(self):
        host = AppHost()
        host.config = AppConfig(galgame=GalgameConfig(reaction_judge_enabled=True))
        host.services = SimpleNamespace(llm_adapter=object())
        sentinel_adapter = object()
        with patch.object(AppHost, "_judge_llm_adapter", return_value=sentinel_adapter):
            bound = host.model_router.for_role("judge")
        self.assertIs(bound.adapter, sentinel_adapter)  # sentinel arrived
        self.assertEqual(bound.model, host.config.llm.model)  # judge_model unset -> dialogue


if __name__ == "__main__":
    unittest.main()
