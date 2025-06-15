import os
import pytest
from playwright.sync_api import Page


def test_server_setup_instructions_exist(page: Page):
    readme_path = os.path.abspath('README.md')
    page.goto(f'file://{readme_path}')
    content = page.content()
    assert 'python server.py' in content
    assert 'docker-compose up' in content

