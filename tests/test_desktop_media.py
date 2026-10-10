import json
from pathlib import Path
import pytest
from video_to_summary.desktop import media
from video_to_summary.web.tasks import JobSuspendedError


@pytest.fixture
def source(tmp_path, monkeypatch):
    original = tmp_path / "中文 空格.mp4"
    original.write_bytes(b"original")
    monkeypatch.setattr(media, "binary_path", lambda name: name)
    return media.DesktopMediaSource(original, tmp_path / "products", "mp3", "source-id")


def test_prepare_atomically_and_cache(source, monkeypatch):
    calls = []
    def runner(command, check):
        calls.append(command)
        if command[0] == "ffprobe":
            return 0, json.dumps({"streams": [{"codec_type": "audio"}]}), ""
        Path(command[-1]).write_bytes(b"converted")
        return 0, "", ""
    monkeypatch.setattr(media, "run_media", runner)
    audio, meta = source.resolve()
    assert audio.read_bytes() == b"converted"
    assert source.path.read_bytes() == b"original"
    assert meta.source_id == "source-id"
    source.resolve()
    assert len(calls) == 2
    assert "-vn" in calls[-1]


def test_no_audio_actionable(source, monkeypatch):
    monkeypatch.setattr(media, "run_media", lambda *a: (0, '{"streams": []}', ""))
    with pytest.raises(ValueError, match="没有音轨"):
        source.resolve()
    assert not list(source.output_dir.iterdir())


def test_cancel_cannot_publish_partial_cache(source, monkeypatch):
    def runner(command, check):
        if command[0] == "ffprobe":
            return 0, '{"streams": [{"codec_type": "audio"}]}', ""
        Path(command[-1]).write_bytes(b"partial")
        raise JobSuspendedError()
    monkeypatch.setattr(media, "run_media", runner)
    with pytest.raises(JobSuspendedError):
        source.resolve()
    assert not list(source.output_dir.iterdir())
    assert source.path.read_bytes() == b"original"
