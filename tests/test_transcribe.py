import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import transcribe


class TranscriptionTests(unittest.TestCase):
    def test_youtube_link_variants(self):
        for url in (
            "https://www.youtube.com/watch?v=BaW_jenozKc&list=ignored",
            "https://youtu.be/BaW_jenozKc?t=3",
            "https://www.youtube.com/shorts/BaW_jenozKc",
            "https://m.youtube.com/watch?v=BaW_jenozKc",
        ):
            with self.subTest(url=url):
                self.assertEqual(transcribe.youtube_id(url), "BaW_jenozKc")
        self.assertIsNone(transcribe.youtube_id(r"C:\videos\sample.mp4"))

    def test_invalid_links_and_playlists(self):
        for url in ("https://youtube.com/playlist?list=123", "https://example.com/watch?v=BaW_jenozKc",
                    "https://youtube.com/watch?v=../bad", "ftp://youtu.be/BaW_jenozKc"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                transcribe.youtube_id(url)

    def test_live_video_is_rejected(self):
        self.assertTrue(transcribe.reject_live({"is_live": True}))
        self.assertTrue(transcribe.reject_live({"live_status": "is_upcoming"}))
        self.assertIsNone(transcribe.reject_live({"live_status": "was_live"}))

    def test_downloader_produces_local_audio(self):
        with tempfile.TemporaryDirectory() as folder, patch("yt_dlp.YoutubeDL") as factory:
            audio = Path(folder) / "audio.webm"
            audio.touch()
            downloader = factory.return_value.__enter__.return_value
            downloader.extract_info.return_value = {"id": "BaW_jenozKc"}
            downloader.prepare_filename.return_value = str(audio)
            self.assertEqual(transcribe.download_audio("BaW_jenozKc", folder, Path("ffmpeg")), audio)
            downloader.extract_info.assert_called_once_with(
                "https://www.youtube.com/watch?v=BaW_jenozKc", download=True)

    def test_download_error_is_actionable(self):
        from yt_dlp.utils import DownloadError
        with patch("yt_dlp.YoutubeDL") as factory:
            factory.return_value.__enter__.return_value.extract_info.side_effect = DownloadError("Video unavailable")
            with self.assertRaisesRegex(RuntimeError, "No se pudo descargar"):
                transcribe.download_audio("BaW_jenozKc", ".", Path("ffmpeg"))

    def test_url_to_outputs_and_temporary_cleanup(self):
        downloaded = []

        def download(video_id, folder, ffmpeg):
            audio = Path(folder) / "audio.webm"
            audio.touch()
            downloaded.append(audio)
            return audio

        model = Mock()
        model.transcribe.return_value = {"text": "Hola", "segments": [{"start": 0, "end": 1, "text": "Hola"}]}
        whisper = SimpleNamespace(available_models=lambda: ["tiny"], load_model=Mock(return_value=model))
        modules = {"torch": SimpleNamespace(cuda=SimpleNamespace(is_available=lambda: False)),
                   "whisper": whisper, "whisper.tokenizer": SimpleNamespace(LANGUAGES={"es": "Spanish"})}
        with tempfile.TemporaryDirectory() as folder, patch.dict("sys.modules", modules), \
                patch("transcribe.configure_ffmpeg", return_value=Path("ffmpeg")), \
                patch("transcribe.download_audio", side_effect=download) as downloader, \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            args = ["https://youtu.be/BaW_jenozKc", "--language", "es", "--output-dir", folder]
            self.assertEqual(transcribe.main(args), 0)
            self.assertEqual((Path(folder) / "youtube-BaW_jenozKc.txt").read_text(encoding="utf-8"), "Hola\n")
            self.assertIn("00:00:00,000 --> 00:00:01,000", (Path(folder) / "youtube-BaW_jenozKc.srt").read_text())
            self.assertFalse(downloaded[0].parent.exists())
            self.assertEqual(transcribe.main(args), 1)
            self.assertEqual(downloader.call_count, 1)
            model.transcribe.side_effect = RuntimeError("Transcription failed")
            self.assertEqual(transcribe.main(args + ["--overwrite"]), 1)
            self.assertFalse(downloaded[-1].parent.exists())

    def test_timestamps_carry_milliseconds(self):
        self.assertEqual(transcribe.timestamp(59.9996), "00:01:00,000")
        self.assertEqual(transcribe.timestamp(3661.234), "01:01:01,234")

    def test_utf8_and_srt_skip_empty_segments(self):
        with tempfile.TemporaryDirectory() as folder:
            targets = [Path(folder) / f"result.{ext}" for ext in ("txt", "srt")]
            transcribe.write_outputs({"text": " Hola, ñ. ", "segments": [
                {"start": 0, "end": 1, "text": " "},
                {"start": 1, "end": 2.25, "text": "Hola,\nñ."},
            ]}, targets)
            self.assertEqual(targets[0].read_text(encoding="utf-8"), "Hola, ñ.\n")
            self.assertEqual(targets[1].read_text(encoding="utf-8"),
                             "1\n00:00:01,000 --> 00:00:02,250\nHola, ñ.\n\n")

    def test_existing_output_is_protected_before_loading_model(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "sample.wav"
            source.touch()
            existing = Path(folder) / "sample.txt"
            existing.write_text("original", encoding="utf-8")
            with patch("transcribe.configure_ffmpeg") as ffmpeg, contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(transcribe.main([str(source), "--output-dir", folder]), 1)
                ffmpeg.assert_not_called()
            self.assertEqual(existing.read_text(encoding="utf-8"), "original")

    def test_missing_input_does_not_load_dependencies(self):
        with tempfile.TemporaryDirectory() as folder:
            with patch("transcribe.configure_ffmpeg") as ffmpeg, contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(transcribe.main([str(Path(folder) / "missing.wav")]), 1)
                ffmpeg.assert_not_called()


if __name__ == "__main__":
    unittest.main()
