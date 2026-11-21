import sys
from unittest.mock import Mock

import pytest

from mydays_bot import cli


@pytest.mark.parametrize(
    ("options", "host"),
    [([], "127.0.0.1"), (["--host", "0.0.0.0"], "0.0.0.0")],
)
def test_serve_preserves_loopback_default_and_accepts_explicit_host(
    monkeypatch: pytest.MonkeyPatch, options: list[str], host: str
):
    app = object()
    serve = Mock()
    monkeypatch.setattr(sys, "argv", ["mydays-bot", "--serve", *options])
    monkeypatch.setattr(cli, "create_app", lambda: app)
    monkeypatch.setattr(cli.uvicorn, "run", serve)

    assert cli.main() == 0
    serve.assert_called_once_with(app, host=host)
