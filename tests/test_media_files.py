"""Sound file rules: media folder scan, safe names and the sync plan (no Home Assistant needed)."""

from __future__ import annotations

import json
from types import SimpleNamespace

from custom_components.iotopen.media_files import (
    MEDIA_LIST_MAX,
    RemoteFile,
    local_names,
    media_list_meta,
    plan_sync,
    remote_files,
    safe_file_name,
    scan_media,
)


def test_scan_media(tmp_path):
    (tmp_path / "larm").mkdir()
    (tmp_path / "larm" / "brand.mp3").write_bytes(b"x")
    (tmp_path / "Doorbell.WAV").write_bytes(b"x")
    (tmp_path / "notes.txt").write_text("x")
    (tmp_path / ".hidden.mp3").write_bytes(b"x")
    (tmp_path / ".trash").mkdir()
    (tmp_path / ".trash" / "old.mp3").write_bytes(b"x")
    assert scan_media(str(tmp_path)) == ["Doorbell.WAV", "larm/brand.mp3"]
    assert scan_media(str(tmp_path / "missing")) == []
    assert scan_media("") == []


def test_media_list_meta():
    assert media_list_meta([]) is None
    assert json.loads(media_list_meta(["a.mp3", "b/ö.mp3"])) == ["a.mp3", "b/ö.mp3"]
    many = media_list_meta([f"sound-{i:04}.mp3" for i in range(500)])
    assert len(many) <= MEDIA_LIST_MAX and json.loads(many)[0] == "sound-0000.mp3"


def test_safe_file_name():
    assert safe_file_name("Dörrklocka 2.mp3", 1) == "Dörrklocka 2.mp3"
    assert safe_file_name("../../etc/passwd", 2) == "passwd"
    assert safe_file_name("..\\secrets.mp3", 3) == "secrets.mp3"
    assert safe_file_name(".hidden.mp3", 4) == "hidden.mp3"
    assert safe_file_name("..", 5) == "file-5"
    assert safe_file_name("a:b*c?.mp3", 6) == "a_b_c_.mp3"
    long = safe_file_name("x" * 300 + ".mp3", 7)
    assert len(long) == 100 and long.endswith(".mp3")


def test_local_names_resolve_clashes():
    names = local_names([RemoteFile(2, "Bell.mp3", "b"), RemoteFile(1, "bell.mp3", "a"), RemoteFile(3, "x.mp3", "c")])
    assert {n: f.id for n, f in names.items()} == {"bell.mp3": 1, "Bell-2.mp3": 2, "x.mp3": 3}


def test_plan_sync():
    remote = [RemoteFile(1, "a.mp3", "h1"), RemoteFile(2, "b.mp3", "h2new")]
    manifest = {"a.mp3": "h1", "b.mp3": "h2old", "gone.mp3": "h3", "gone-too.mp3": "h4"}
    plan = plan_sync(remote, manifest, ["a.mp3", "b.mp3", "gone.mp3", "someone-elses.mp3"])
    assert set(plan.download) == {"b.mp3"}  # changed in IoT Open
    assert plan.delete == ["gone.mp3"]  # ours and on disk; someone-elses.mp3 is never touched
    assert plan.keep == {"a.mp3": "h1", "b.mp3": "h2new"}

    again = plan_sync(remote, plan.keep, ["b.mp3"])  # a.mp3 was removed locally: fetch it again
    assert set(again.download) == {"a.mp3"} and again.delete == []


def test_remote_files_keeps_audio_only():
    entries = [
        SimpleNamespace(id=1, name="a.mp3", hash="ABC", mime="audio/mpeg"),
        SimpleNamespace(id=2, name="layout.json", hash="def", mime="application/json"),
        SimpleNamespace(id=3, name="b.mp3", hash="", mime="audio/mpeg"),
    ]
    assert remote_files(entries) == [RemoteFile(1, "a.mp3", "abc")]
