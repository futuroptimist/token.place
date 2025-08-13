import json
from pathlib import Path


def test_package_has_test_ci_script():
    pkg = json.loads(Path('package.json').read_text())
    scripts = pkg.get('scripts', {})
    assert scripts.get('test:ci') == './run_all_tests.sh'
