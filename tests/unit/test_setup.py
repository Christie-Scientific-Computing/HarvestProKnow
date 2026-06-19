"""
Unit tests for utils/setup.py.

init_config() hardcodes a relative path ('./config.toml'), so these tests
chdir into a temp directory holding a fabricated config.toml rather than
touching the real one in the repo.
"""
import logging
from pathlib import Path

import pytest

from utils.setup import init_config, init_logger


def write_config(tmp_path, body):
    (tmp_path / "config.toml").write_text(body)


def test_init_config_coerces_types(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    write_config(
        tmp_path,
        """
        path_to_csv = './data/ids.csv'
        log-to-file = true
        log-dir = './logs/'
        log-level = "DEBUG"
        """,
    )
    config = init_config()
    assert config["path_to_csv"] == Path("./data/ids.csv")
    assert config["log-dir"] == Path("./logs/")
    assert config["log-to-file"] is True
    assert config["log-level"] == "DEBUG"


def test_init_config_warns_but_keeps_raw_value_on_bad_type(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    # Path() raises TypeError on a list; the current code catches that,
    # prints a warning, and leaves the raw (uncoerced) value in place.
    write_config(
        tmp_path,
        """
        path_to_csv = [1, 2, 3]
        log-to-file = true
        log-dir = './logs/'
        log-level = "INFO"
        """,
    )
    config = init_config()
    # Path([1, 2, 3]) raises TypeError -> current code prints a warning and
    # leaves the raw (uncoerced) value in place.
    assert config["path_to_csv"] == [1, 2, 3]
    captured = capsys.readouterr()
    assert "Warning: Could not convert key 'path_to_csv'" in captured.out


def test_init_logger_writes_to_file_when_enabled(tmp_path):
    log_dir = tmp_path / "logs"
    config = {
        "log-to-file": True,
        "log-dir": log_dir,
        "log-level": "DEBUG",
    }
    init_logger(config)
    assert log_dir.exists()
    log_files = list(log_dir.glob("logfile_*.log"))
    assert len(log_files) == 1

    root = logging.getLogger()
    assert root.level == logging.DEBUG
    assert any(isinstance(h, logging.FileHandler) for h in root.handlers)
    assert logging.getLogger("httpx").level == logging.WARNING


def test_init_logger_does_not_touch_filesystem_when_disabled(tmp_path):
    log_dir = tmp_path / "logs-not-created"
    config = {
        "log-to-file": False,
        "log-dir": log_dir,
        "log-level": "INFO",
    }
    init_logger(config)
    assert not log_dir.exists()

    root = logging.getLogger()
    assert root.level == logging.INFO
    assert not any(isinstance(h, logging.FileHandler) for h in root.handlers)
