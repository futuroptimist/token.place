import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
BRIDGE = REPO_ROOT / "desktop-tauri" / "src-tauri" / "python" / "inference_bridge.py"


class DesktopInferenceBridgeTests(unittest.TestCase):
    def _bridge_cmd(self, *, model_path: Path, prompt: str):
        return [
            sys.executable,
            str(BRIDGE),
            "--model",
            str(model_path),
            "--mode",
            "cpu",
            "--prompt",
            prompt,
        ]

    def test_happy_path_streams_ndjson_events(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            model_path = Path(temp_dir) / "model.gguf"
            model_path.write_bytes(b"stub")

            env = os.environ.copy()
            env["USE_MOCK_LLM"] = "1"

            proc = subprocess.Popen(
                self._bridge_cmd(model_path=model_path, prompt="What is token.place?"),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=env,
                cwd=str(REPO_ROOT),
            )

            events = []
            self.assertIsNotNone(proc.stdout)
            for line in proc.stdout:
                text = line.strip()
                if text:
                    events.append(json.loads(text))

            return_code = proc.wait(timeout=30)
            if proc.stdout:
                proc.stdout.close()
            if proc.stderr:
                proc.stderr.close()
            self.assertEqual(return_code, 0)
            self.assertGreaterEqual(len(events), 3)

            event_types = [event["type"] for event in events]
            self.assertEqual(event_types[0], "started")
            self.assertEqual(event_types[-1], "done")

            token_payload = "".join(
                event.get("text", "") for event in events if event["type"] == "token"
            )
            self.assertIn("Mock Response", token_payload)
            self.assertNotEqual(token_payload.strip(), "What is token.place?")

    def test_cancel_emits_canceled_event(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            model_path = Path(temp_dir) / "model.gguf"
            model_path.write_bytes(b"stub")

            env = os.environ.copy()
            env["USE_MOCK_LLM"] = "1"
            env["TOKEN_PLACE_SIDECAR_TOKEN_DELAY_SECONDS"] = "0.05"

            proc = subprocess.Popen(
                self._bridge_cmd(model_path=model_path, prompt="Cancel this response please"),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=env,
                cwd=str(REPO_ROOT),
            )

            self.assertIsNotNone(proc.stdout)
            self.assertIsNotNone(proc.stdin)

            first_event = json.loads(proc.stdout.readline().strip())
            self.assertEqual(first_event["type"], "started")

            proc.stdin.write('{"type":"cancel"}\n')
            proc.stdin.flush()

            remaining_events = []
            for line in proc.stdout:
                text = line.strip()
                if text:
                    remaining_events.append(json.loads(text))

            return_code = proc.wait(timeout=30)
            proc.stdin.close()
            proc.stdout.close()
            if proc.stderr:
                proc.stderr.close()
            self.assertEqual(return_code, 0)

            event_types = [event["type"] for event in remaining_events]
            self.assertIn("canceled", event_types)
            self.assertNotIn("done", event_types)


if __name__ == "__main__":
    unittest.main()
