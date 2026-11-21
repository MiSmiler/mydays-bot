import sys
from unittest.mock import Mock

import pytest

from mydays_bot import cli


def test_serve_binds_to_loopback(monkeypatch: pytest.MonkeyPatch):
    app = object()
    serve = Mock()
    monkeypatch.setattr(sys, "argv", ["mydays-bot", "--serve"])
    monkeypatch.setattr(cli, "create_app", lambda: app)
    monkeypatch.setattr(cli.uvicorn, "run", serve)

    assert cli.main() == 0
    serve.assert_called_once_with(app, host="127.0.0.1", port=8000)


def test_serve_rejects_a_host_override(monkeypatch: pytest.MonkeyPatch):
    serve = Mock()
    monkeypatch.setattr(sys, "argv", ["mydays-bot", "--serve", "--host", "0.0.0.0"])
    monkeypatch.setattr(cli.uvicorn, "run", serve)

    with pytest.raises(SystemExit) as rejected:
        cli.main()

    assert rejected.value.code == 2
    serve.assert_not_called()
