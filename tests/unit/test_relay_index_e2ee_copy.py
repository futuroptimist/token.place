from pathlib import Path


def test_relay_index_e2ee_copy_states_mandatory_encryption():
    index_html = Path("static/index.html").read_text(encoding="utf-8")
    assert "End-to-End Encryption (Always On)" in index_html
    assert "enabled by design for all client↔relay↔server communication paths" in index_html
    assert "not an optional privacy mode" in index_html
    assert "For enhanced privacy, you can use end-to-end encryption with the API:" not in index_html
