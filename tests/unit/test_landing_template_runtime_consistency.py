from __future__ import annotations

import re
from pathlib import Path


INDEX_HTML = Path("static/index.html")
CHAT_JS = Path("static/chat.js")


def test_compute_node_last_updated_binding_has_runtime_computed_property():
    index_html = INDEX_HTML.read_text(encoding="utf-8")
    chat_js = CHAT_JS.read_text(encoding="utf-8")

    assert "computeNodeCountLastUpdatedLabel" in index_html
    assert re.search(r"computeNodeCountLastUpdatedLabel\s*\(\)\s*{", chat_js)
    assert "return '';" in chat_js
    assert "Updated ${this.computeNodeCountLastUpdated}" in chat_js


def test_static_index_has_no_raw_vue_mustache_interpolation():
    index_html = INDEX_HTML.read_text(encoding="utf-8")

    assert "{{" not in index_html
    assert "}}" not in index_html


def test_dark_mode_script_and_vue_updated_have_defensive_guards():
    index_html = INDEX_HTML.read_text(encoding="utf-8")
    chat_js = CHAT_JS.read_text(encoding="utf-8")

    assert "if (!toggleModeButton || !body)" in index_html
    assert "typeof this.$el.querySelector !== 'function'" in chat_js
    assert "if (!container)" in chat_js
