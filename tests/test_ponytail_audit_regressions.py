"""User-visible failures from the Ponytail audit, exercised at their callers."""

import base64
import json
import io
import sqlite3
from pathlib import Path

import pytest

import app_updater
import session_history
from accessible_ai.models import MessageAttachment
from accessible_ai.providers.protocols import attachment_content_part
from test_session_history import _write_opencode


@pytest.mark.parametrize("machine", ["arm64", "x86_64"])
def test_mac_updater_discovers_the_asset_published_by_release_workflow(machine):
    workflow = Path(".github/workflows/release.yml").read_text(encoding="utf-8")
    asset = "BlindPilot-macOS-universal2.zip"
    assert f"asset: {asset}" in workflow
    payload = {
        "tag_name": "v99.0.0",
        "assets": [
            {
                "name": asset,
                "size": 1,
                "digest": "sha256:" + "a" * 64,
                "browser_download_url": "https://github.com/example/update.zip",
            }
        ],
    }

    release = app_updater.fetch_latest_release(
        "1.0.0",
        system="Darwin",
        machine=machine,
        opener=lambda *a, **k: io.BytesIO(json.dumps(payload).encode()),
    )
    assert release.asset_name == asset


def test_text_attachment_never_allocates_unused_base64(monkeypatch):
    def unused(_data):
        raise AssertionError("text does not use base64")

    monkeypatch.setattr(base64, "b64encode", unused)
    part = attachment_content_part(
        MessageAttachment(filename="notes.txt", mime_type="text/plain", data=b"hello")
    )
    assert part == {"type": "text", "text": "[Attached file: notes.txt]\nhello"}


def test_opencode_file_only_question_stays_before_its_answer(monkeypatch, tmp_path):
    _write_opencode(
        tmp_path, "image", "/project", "Image", [("user", ""), ("assistant", "A red square.")]
    )
    database = tmp_path / ".local/share/opencode/opencode.db"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO part VALUES (?,?,?,?,?)",
            (
                "image-part",
                "msg_image_0",
                "image",
                99,
                json.dumps({"type": "file", "filename": "shot.png"}),
            ),
        )
    monkeypatch.setenv("OPENCODE_DATA", str(database.parent))
    monkeypatch.setattr(session_history, "_home", lambda: tmp_path)
    entry = session_history.HistoryEntry("opencode", "image", "Image", "", 0)
    turns = session_history.load_turns(entry)
    assert [(turn.prompt, turn.response) for turn in turns] == [
        ("[Attached: shot.png]", "A red square.")
    ]
