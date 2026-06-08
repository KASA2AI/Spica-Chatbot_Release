"""C7 unit tests for ScreenAnalysisPort + the local adapter.

The adapter is a thin, behaviour-preserving pass-through to the existing
``analyze_screen_image_local`` engine -- the formalization that lets the
inspect_screen tool and the manual-attachment stage share one analysis adapter.
"""

import unittest
from unittest.mock import patch

from spica.adapters.screen import LocalMoondreamScreenAnalysis
from spica.ports.screen import ScreenAnalysisPort


class ScreenAnalysisAdapterTest(unittest.TestCase):
    def test_adapter_conforms_to_port(self):
        self.assertIsInstance(LocalMoondreamScreenAnalysis(), ScreenAnalysisPort)

    def test_analyze_image_delegates_to_the_local_engine(self):
        observation = {"schema_version": "screen_observation.v1"}
        with patch(
            "spica.adapters.screen.local_moondream.analyze_screen_image_local",
            return_value=observation,
        ) as engine:
            out = LocalMoondreamScreenAnalysis().analyze_image(
                "IMG",
                "full_screen",
                "屏幕上有什么",
                config="CFG",
                capture={"source": "x"},
                performance={"capture_ms": 1.0},
                question_type="general_observation",
            )
        self.assertIs(out, observation)
        engine.assert_called_once_with(
            "IMG",
            "full_screen",
            "屏幕上有什么",
            config="CFG",
            capture={"source": "x"},
            performance={"capture_ms": 1.0},
            question_type="general_observation",
        )


if __name__ == "__main__":
    unittest.main()
