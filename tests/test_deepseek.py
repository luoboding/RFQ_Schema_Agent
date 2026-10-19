import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from rfq_schema_agent.deepseek import DeepSeekAPIError, DeepSeekModel


class FakeResponse:
    def __init__(self, payload: dict):
        self.buffer = io.BytesIO(json.dumps(payload).encode("utf-8"))

    def __enter__(self):
        return self.buffer

    def __exit__(self, *_):
        self.buffer.close()


class DeepSeekModelTests(unittest.TestCase):
    def test_sends_json_mode_request_and_parses_reply(self):
        completion = {
            "choices": [{"finish_reason": "stop", "message": {"content": '{"rfq_summary":"brackets"}'}}]
        }
        model = DeepSeekModel(api_key="test-key")
        with patch("rfq_schema_agent.deepseek.urlopen", return_value=FakeResponse(completion)) as open_url:
            result = model.generate_json("Output JSON", "Quote brackets")

        self.assertEqual(result, {"rfq_summary": "brackets"})
        request = open_url.call_args.args[0]
        body = json.loads(request.data)
        self.assertEqual(request.full_url, model.endpoint)
        self.assertEqual(body["model"], "deepseek-flash")
        self.assertEqual(body["response_format"], {"type": "json_object"})
        self.assertFalse(body["stream"])
        self.assertEqual(body["messages"][1]["content"], "Quote brackets")
        self.assertEqual(open_url.call_args.kwargs["timeout"], 25)

    def test_rejects_truncated_response(self):
        completion = {
            "choices": [{"finish_reason": "length", "message": {"content": '{"partial":true}'}}]
        }
        with patch("rfq_schema_agent.deepseek.urlopen", return_value=FakeResponse(completion)):
            with self.assertRaises(DeepSeekAPIError):
                DeepSeekModel(api_key="test-key").generate_json("Output JSON", "RFQ")

    def test_rejects_empty_content(self):
        completion = {"choices": [{"finish_reason": "stop", "message": {"content": ""}}]}
        with patch("rfq_schema_agent.deepseek.urlopen", return_value=FakeResponse(completion)):
            with self.assertRaises(DeepSeekAPIError):
                DeepSeekModel(api_key="test-key").generate_json("Output JSON", "RFQ")

    def test_requires_api_key_before_network_request(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(os.environ, {}, clear=True):
                with self.assertRaises(ValueError):
                    DeepSeekModel(env_file=Path(directory) / ".env")

    def test_reads_dotenv_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            env_file = Path(directory) / ".env"
            env_file.write_text(
                "DEEPSEEK_API_KEY=file-key\nDEEPSEEK_BASE_URL=https://proxy.example.com\n"
                "DEEPSEEK_MODEL=deepseek-v4-pro\nDEEPSEEK_TIMEOUT_SECONDS=18\n",
                encoding="utf-8",
            )
            with patch.dict(os.environ, {}, clear=True):
                model = DeepSeekModel(env_file=env_file)
        self.assertEqual(model.api_key, "file-key")
        self.assertEqual(model.endpoint, "https://proxy.example.com/chat/completions")
        self.assertEqual(model.model, "deepseek-v4-pro")
        self.assertEqual(model.timeout_seconds, 18)

    def test_rejects_insecure_endpoint(self):
        with tempfile.TemporaryDirectory() as directory:
            env_file = Path(directory) / ".env"
            env_file.write_text("DEEPSEEK_API_KEY=file-key\nDEEPSEEK_BASE_URL=http://example.com\n", encoding="utf-8")
            with patch.dict(os.environ, {}, clear=True):
                with self.assertRaises(ValueError):
                    DeepSeekModel(env_file=env_file)

    def test_system_environment_overrides_dotenv(self):
        with tempfile.TemporaryDirectory() as directory:
            env_file = Path(directory) / ".env"
            env_file.write_text("DEEPSEEK_API_KEY=file-key\nDEEPSEEK_MODEL=deepseek-v4-pro\n", encoding="utf-8")
            with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "system-key"}, clear=True):
                model = DeepSeekModel(env_file=env_file)
        self.assertEqual(model.api_key, "system-key")
        self.assertEqual(model.model, "deepseek-v4-pro")


if __name__ == "__main__":
    unittest.main()
