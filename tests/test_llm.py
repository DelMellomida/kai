"""The Ollama client: prompt assembly, the persona file, and the GPU-placement probe.

Moved out of tests/test_voice_assistant.py with ai/llm.py. The tests that drive a request through
VoiceAssistant (TestCallOllama, TestEnsureLlmWarm) stayed there — they are about the assistant's
use of this module, not about the module.
"""

import unittest
from unittest.mock import MagicMock, patch

import requests

from ai.llm import _DEFAULT_PERSONA, build_chat_messages, load_persona, log_model_placement
from config.voice import MAX_HISTORY_TURNS, OLLAMA_MODEL


class TestBuildChatMessages(unittest.TestCase):
    def test_system_prompt_first(self):
        msgs = build_chat_messages("sys", [], "hello")
        self.assertEqual(msgs[0], {"role": "system", "content": "sys"})

    def test_appends_user_turn_last(self):
        msgs = build_chat_messages("sys", [], "hello")
        self.assertEqual(msgs[-1], {"role": "user", "content": "hello"})

    def test_includes_history_in_order(self):
        history = [
            {"role": "user", "content": "a"},
            {"role": "assistant", "content": "b"},
        ]
        msgs = build_chat_messages("sys", history, "c")
        self.assertEqual(msgs, [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "a"},
            {"role": "assistant", "content": "b"},
            {"role": "user", "content": "c"},
        ])

    def test_truncates_to_max_history_turns(self):
        history = []
        for i in range(MAX_HISTORY_TURNS + 5):
            history.append({"role": "user", "content": f"u{i}"})
            history.append({"role": "assistant", "content": f"a{i}"})
        msgs = build_chat_messages("sys", history, "new")
        # system + capped history + new user turn
        self.assertEqual(len(msgs), 1 + MAX_HISTORY_TURNS * 2 + 1)


class TestLoadPersona(unittest.TestCase):
    def test_reads_custom_content(self):
        mock_path = MagicMock()
        mock_path.read_text.return_value = "Custom persona text.\n"
        with patch("ai.llm.PERSONA_PATH", mock_path):
            self.assertEqual(load_persona(), "Custom persona text.")

    def test_missing_file_falls_back_to_default(self):
        mock_path = MagicMock()
        mock_path.read_text.side_effect = OSError("no such file")
        with patch("ai.llm.PERSONA_PATH", mock_path):
            self.assertEqual(load_persona(), _DEFAULT_PERSONA)

    def test_empty_file_falls_back_to_default(self):
        mock_path = MagicMock()
        mock_path.read_text.return_value = "   \n"
        with patch("ai.llm.PERSONA_PATH", mock_path):
            self.assertEqual(load_persona(), _DEFAULT_PERSONA)


class TestLogModelPlacement(unittest.TestCase):
    def _resp(self, payload):
        r = MagicMock()
        r.json.return_value = payload
        return r

    def test_reports_a_full_gpu_offload(self):
        payload = {"models": [{"name": f"{OLLAMA_MODEL}", "size": 2000, "size_vram": 2000}]}
        with patch("ai.llm.requests.get", return_value=self._resp(payload)):
            out = log_model_placement()
        self.assertEqual(out["gpu_pct"], 100.0)

    def test_reports_a_partial_offload(self):
        """The ~2x slowdown this whole probe exists to make visible."""
        payload = {"models": [{"name": f"{OLLAMA_MODEL}", "size": 2000, "size_vram": 900}]}
        with patch("ai.llm.requests.get", return_value=self._resp(payload)):
            out = log_model_placement()
        self.assertEqual(out["gpu_pct"], 45.0)

    def test_never_raises_when_ollama_is_unreachable(self):
        with patch("ai.llm.requests.get",
                   side_effect=requests.exceptions.ConnectionError()):
            self.assertEqual(log_model_placement(), {})

    def test_handles_model_not_loaded(self):
        with patch("ai.llm.requests.get", return_value=self._resp({"models": []})):
            self.assertEqual(log_model_placement(), {})


if __name__ == "__main__":
    unittest.main()
