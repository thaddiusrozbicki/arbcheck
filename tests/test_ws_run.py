"""Tests for the WebSocket runner's .env loading."""

import os

from poller.ws_run import load_env


def test_load_env_fills_missing_values_only(tmp_path, monkeypatch):
    path = tmp_path / ".env"
    path.write_text("# comment\nARBCHECK_TEST_A=from-file\nARBCHECK_TEST_B = spaced \n\nNOT A LINE\n")
    monkeypatch.setenv("ARBCHECK_TEST_A", "already-set")
    monkeypatch.delenv("ARBCHECK_TEST_B", raising=False)
    load_env(path)
    assert os.environ["ARBCHECK_TEST_A"] == "already-set"
    assert os.environ["ARBCHECK_TEST_B"] == "spaced"
    monkeypatch.delenv("ARBCHECK_TEST_B")


def test_missing_env_file_is_fine(tmp_path):
    load_env(tmp_path / "nope.env")
