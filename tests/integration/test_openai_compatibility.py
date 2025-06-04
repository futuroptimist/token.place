import os
import time
import subprocess
import requests
import openai
from contextlib import contextmanager

# We reuse the port used in other tests
API_PORT = 5055
BASE_URL = f"http://localhost:{API_PORT}"

@contextmanager
def start_relay_with_mock():
    env = os.environ.copy()
    env["USE_MOCK_LLM"] = "1"
    cmd = ["python", "relay.py", "--port", str(API_PORT)]
    proc = subprocess.Popen(cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        # wait for server
        for _ in range(10):
            try:
                r = requests.get(f"{BASE_URL}/v1/health")
                if r.status_code == 200:
                    break
            except Exception:
                pass
            time.sleep(1)
        else:
            raise RuntimeError("relay failed to start")
        yield
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


def test_openai_client_compatibility():
    with start_relay_with_mock():
        client = openai.OpenAI(base_url=f"{BASE_URL}/v1", api_key="test")

        # list models via OpenAI client
        models = client.models.list()
        assert len(models.data) > 0

        # retrieve a single model
        model_id = models.data[0].id
        model = client.models.retrieve(model_id)
        assert model.id == model_id

        # chat completion
        chat_resp = client.chat.completions.create(
            model=model_id,
            messages=[{"role": "user", "content": "Hello"}]
        )
        assert chat_resp.choices[0].message.content

        # legacy completions endpoint
        comp_resp = client.completions.create(
            model=model_id,
            prompt="Hello"
        )
        assert comp_resp.choices[0].text

