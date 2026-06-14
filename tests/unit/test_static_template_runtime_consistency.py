from pathlib import Path
import re


def test_index_referenced_compute_timestamp_computed_exists():
    index_html = Path("static/index.html").read_text(encoding="utf-8")
    chat_js = Path("static/chat.js").read_text(encoding="utf-8")

    assert "computeNodeCountLastUpdatedLabel" in index_html
    assert re.search(r"computeNodeCountLastUpdatedLabel\s*\(\)\s*{", chat_js)


def test_static_index_has_no_raw_mustache_interpolation():
    index_html = Path("static/index.html").read_text(encoding="utf-8")

    assert "{{" not in index_html
    assert "}}" not in index_html


def test_dark_mode_script_and_updated_hook_have_defensive_guards():
    index_html = Path("static/index.html").read_text(encoding="utf-8")
    chat_js = Path("static/chat.js").read_text(encoding="utf-8")

    assert "if (!toggleModeButton || !body)" in index_html
    assert 'typeof this.$el.querySelector !== "function"' in chat_js
    assert "if (!container)" in chat_js
