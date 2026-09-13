"""Transcribe un enlace de YouTube o un archivo local con Whisper a TXT y SRT."""

import argparse
import os
import re
from contextlib import ExitStack
from pathlib import Path
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
from urllib.parse import parse_qs, urlparse


def youtube_id(source):
    """Devuelve el ID de un video; las rutas locales devuelven None."""
    parsed = urlparse(source)
    if parsed.scheme.lower() not in ("http", "https"):
        if "://" in source:
            raise ValueError("Usa un enlace https de YouTube o un archivo local.")
        return None
    host = (parsed.hostname or "").lower()
    parts = parsed.path.strip("/").split("/")
    video_id = ""
    if host in ("youtu.be", "www.youtu.be") and len(parts) == 1:
        video_id = parts[0]
    elif host in ("youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com"):
        if parsed.path == "/watch":
            video_id = parse_qs(parsed.query).get("v", [""])[0]
        elif len(parts) == 2 and parts[0] in ("shorts", "live", "embed"):
            video_id = parts[1]
    if not re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id):
        raise ValueError("Indica un enlace a un video de YouTube (watch, youtu.be o shorts), no una lista o canal.")
    return video_id


def reject_live(info, *, incomplete=False):
    if info.get("is_live") or info.get("live_status") in ("is_live", "is_upcoming"):
        return "Las transmisiones en directo o futuras no están soportadas. Usa un video finalizado."


def download_audio(video_id, directory, ffmpeg, progress_hook=None):
    from yt_dlp import YoutubeDL
    from yt_dlp.utils import DownloadError
    options = {
        "format": "bestaudio/best",
        "noplaylist": True,
        "outtmpl": str(Path(directory) / "audio.%(ext)s"),
        "ffmpeg_location": str(ffmpeg.parent),
        "cachedir": False,
        "socket_timeout": 30,
        "retries": 3,
        "match_filter": reject_live,
        "js_runtimes": {"deno": {}, "node": {}},
    }
    if progress_hook:
        options["progress_hooks"] = [progress_hook]
    try:
        with YoutubeDL(options) as downloader:
            info = downloader.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=True)
            if not info or reject_live(info):
                raise RuntimeError("No se descargó un video finalizado de YouTube.")
            source = Path(downloader.prepare_filename(info))
            if not source.is_file():
                raise RuntimeError("La descarga no produjo un archivo de audio utilizable.")
            return source
    except DownloadError as error:
        raise RuntimeError(f"No se pudo descargar el audio de YouTube: {error}") from error


def configure_ffmpeg(directory=None):
    """Valida FFmpeg y ajusta solo el PATH del proceso actual."""
    candidates = []
    if directory:
        candidates.append(Path(directory) / ("ffmpeg.exe" if os.name == "nt" else "ffmpeg"))
    else:
        located = shutil.which("ffmpeg")
        if located:
            candidates.append(Path(located))
        if os.name == "nt" and os.environ.get("LOCALAPPDATA"):
            packages = Path(os.environ["LOCALAPPDATA"]) / "Microsoft/WinGet/Packages"
            candidates.extend(packages.glob("Gyan.FFmpeg*/*/bin/ffmpeg.exe"))
    for candidate in candidates:
        try:
            subprocess.run([str(candidate), "-version"], check=True,
                           capture_output=True, timeout=10)
        except (OSError, subprocess.SubprocessError):
            continue
        os.environ["PATH"] = str(candidate.parent) + os.pathsep + os.environ.get("PATH", "")
        return candidate
    raise RuntimeError("FFmpeg no se puede ejecutar. Instálalo o indica --ffmpeg-dir con su carpeta bin.")


def timestamp(seconds):
    milliseconds = max(0, round(seconds * 1000))
    hours, milliseconds = divmod(milliseconds, 3_600_000)
    minutes, milliseconds = divmod(milliseconds, 60_000)
    seconds, milliseconds = divmod(milliseconds, 1000)
    return f"{hours:02}:{minutes:02}:{seconds:02},{milliseconds:03}"


def render_outputs(result):
    text = result["text"].strip()
    subtitles = []
    for segment in result["segments"]:
        caption = " ".join(segment["text"].split()).replace("-->", "->")
        if caption:
            subtitles.append(f"{len(subtitles) + 1}\n"
                             f"{timestamp(segment['start'])} --> {timestamp(segment['end'])}\n"
                             f"{caption}\n\n")
    return text + ("\n" if text else ""), "".join(subtitles)


def write_outputs(result, targets):
    for target, content in zip(targets, render_outputs(result)):
        target.write_text(content, encoding="utf-8")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", nargs="?", help="Enlace de YouTube o archivo local de audio/video")
    parser.add_argument("--model", default="tiny", help="Modelo Whisper (predeterminado: tiny)")
    parser.add_argument("--language", default=None, help="Código de idioma, por ejemplo es; omitido: automático")
    parser.add_argument("--device", choices=("auto", "cpu", "cuda"), default="auto")
    parser.add_argument("--output-dir", type=Path, default=Path("outputs"))
    parser.add_argument("--model-dir", type=Path, default=Path("models"))
    parser.add_argument("--ffmpeg-dir", type=Path, help="Carpeta que contiene ffmpeg")
    parser.add_argument("--overwrite", action="store_true", help="Permitir reemplazar resultados existentes")
    parser.add_argument("--check", action="store_true", help="Comprobar entorno sin cargar ni descargar modelos")
    args = parser.parse_args(argv)
    if not args.check and args.input is None:
        parser.error("debes indicar un enlace de YouTube, un archivo local o --check")
    try:
        video_id = youtube_id(args.input) if not args.check else None
        source = Path(args.input) if not args.check and video_id is None else None
        if source is not None and not source.is_file():
            raise ValueError(f"No existe el archivo: {source}")
        targets = None
        if not args.check:
            stem = f"youtube-{video_id}" if video_id else source.stem
            targets = [args.output_dir / f"{stem}.{ext}" for ext in ("txt", "srt")]
            for target in targets:
                if source is not None and target.resolve() == source.resolve():
                    raise ValueError("La salida no puede reemplazar el archivo de entrada.")
                if target.exists() and (not args.overwrite or not target.is_file()):
                    raise ValueError(f"La salida ya existe: {target}. Usa --overwrite para reemplazar archivos.")
        ffmpeg = configure_ffmpeg(args.ffmpeg_dir)
        import torch
        import whisper
        from whisper.tokenizer import LANGUAGES
        cuda = torch.cuda.is_available()
        if args.device == "cuda" and not cuda:
            raise ValueError("CUDA no está disponible; utiliza --device cpu.")
        device = "cuda" if args.device == "auto" and cuda else "cpu" if args.device == "auto" else args.device
        if args.check:
            from yt_dlp.version import __version__ as yt_dlp_version
            print(f"Python: {sys.version.split()[0]}\nWhisper: {whisper.__version__}\n"
                  f"PyTorch: {torch.__version__}\nyt-dlp: {yt_dlp_version}\nCUDA disponible: {cuda}\nFFmpeg: {ffmpeg}")
            if "dev" in torch.__version__:
                print("Aviso: PyTorch es una versión de desarrollo; requirements.txt fija una estable.")
            return 0
        if args.model not in whisper.available_models():
            raise ValueError("Modelo desconocido. Disponibles: " + ", ".join(whisper.available_models()))
        if args.language is not None and args.language not in LANGUAGES:
            raise ValueError("Idioma desconocido. Usa un código como es o en.")
        with ExitStack() as stack:
            if video_id:
                temporary = stack.enter_context(TemporaryDirectory(prefix="video-to-text-"))
                print("Descargando audio de YouTube...", flush=True)
                source = download_audio(video_id, temporary, ffmpeg)
            print(f"Cargando {args.model} en {device}. La primera ejecución puede descargar el modelo.", flush=True)
            model = whisper.load_model(args.model, device=device, download_root=str(args.model_dir))
            result = model.transcribe(str(source.resolve()), language=args.language,
                                      fp16=device == "cuda", verbose=False)
        args.output_dir.mkdir(parents=True, exist_ok=True)
        write_outputs(result, targets)
        for target in targets:
            print(f"Generado: {target.resolve()}")
        return 0
    except (OSError, RuntimeError, ValueError, ImportError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
