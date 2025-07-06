class Llama:
    """Minimal stub of llama_cpp.Llama for testing purposes."""
    def __init__(self, *args, **kwargs):
        pass
    def create_chat_completion(self, *args, **kwargs):
        return {"choices": [{"message": {"role": "assistant", "content": "stub"}}]}
