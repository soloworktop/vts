"""Prepare local audio/video without modifying or owning the user's source file."""
import json
import os
import subprocess
import tempfile
from pathlib import Path

from ..schemas import AudioMeta
from ..utils import subprocess_no_window_kwargs
from .paths import binary_path


def run_media(command, cancel_check):
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               text=True, encoding="utf-8", errors="replace", **subprocess_no_window_kwargs())
    try:
        while True:
            cancel_check()
            try:
                stdout, stderr = process.communicate(timeout=0.2)
                return process.returncode, stdout, stderr
            except subprocess.TimeoutExpired:
                continue
    except BaseException:
        process.terminate()
        try:
            process.communicate(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()
        raise


class DesktopMediaSource:
    def __init__(self, path, output_dir, audio_format, source_id, title=None):
        self.path = Path(path).expanduser().resolve()
        self.output_dir = Path(output_dir)
        self.audio_format = audio_format
        self.source_id = source_id
        self.title = title or self.path.stem
        self.meta = None
        self.cancel_check = lambda: None

    def resolve(self):
        self.cancel_check()
        if not self.path.is_file() or not os.access(self.path, os.R_OK):
            raise ValueError("本地文件不存在或不可读取，请重新选择文件")
        if self.audio_format not in ("mp3", "wav"):
            raise ValueError("本地媒体输出格式必须为 MP3 或 WAV")
        self.output_dir.mkdir(parents=True, exist_ok=True)
        audio = self.output_dir / ("prepared." + self.audio_format)
        if not audio.is_file():
            code, stdout, _ = run_media([binary_path("ffprobe"), "-v", "error", "-show_streams", "-of", "json", str(self.path)], self.cancel_check)
            if code:
                raise ValueError("无法读取音视频文件；请确认文件完整且格式受 FFmpeg 支持")
            streams = json.loads(stdout).get("streams", [])
            if not any(stream.get("codec_type") == "audio" for stream in streams):
                raise ValueError("文件没有音轨，请选择含声音的视频或音频文件")
            fd, temporary = tempfile.mkstemp(dir=self.output_dir, prefix=".prepared-", suffix="." + self.audio_format)
            os.close(fd)
            try:
                codec = "libmp3lame" if self.audio_format == "mp3" else "pcm_s16le"
                code, _, _ = run_media([binary_path("ffmpeg"), "-nostdin", "-y", "-v", "error", "-i", str(self.path),
                    "-map", "0:a:0", "-vn", "-ac", "1", "-ar", "16000", "-c:a", codec, temporary], self.cancel_check)
                if code or not Path(temporary).stat().st_size:
                    raise ValueError("音轨转换失败；请确认磁盘空间及源文件完整性后重试")
                self.cancel_check()
                os.replace(temporary, audio)
            finally:
                Path(temporary).unlink(missing_ok=True)
        self.meta = AudioMeta(source_id=self.source_id, title=self.title, source_url=None,
            duration=None, uploader=None, upload_date=None, audio_path=audio)
        return audio, self.meta


def local_source_factory(settings, job):
    return DesktopMediaSource(job.payload.get("audio_path") or job.payload.get("file_path"),
                              settings.output_dir, settings.audio_format, job.job_id, job.payload.get("title"))
