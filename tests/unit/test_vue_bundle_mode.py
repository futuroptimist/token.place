from pathlib import Path


def test_static_index_defaults_to_production_vue_bundle():
    index_html = Path("static/index.html").read_text(encoding="utf-8")

    assert "vue.min.js" in index_html, "production Vue bundle should be referenced in template"
    assert "params.get('vue') === 'dev'" in index_html, "dev override should be explicit"
    assert "isLocalhost" in index_html, "localhost-only dev ergonomics should be documented in code"


def test_static_index_keeps_local_development_override():
    index_html = Path("static/index.html").read_text(encoding="utf-8")

    assert "['localhost', '127.0.0.1', '::1']" in index_html
    assert "params.get('vue') === 'prod'" in index_html
