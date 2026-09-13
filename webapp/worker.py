"""Un proceso aislado por transcripción, con progreso de audio real."""
import importlib
from pathlib import Path
import subprocess
import sys
import time

from transcribe import configure_ffmpeg, download_audio, write_outputs, youtube_id
from webapp.storage import ROOT, atomic_json, read_json


def run(directory):
    directory = Path(directory)
    item = read_json(directory / "job.json")

    last_progress = ["", 0.0]

    def progress(stage, percent=None):
        current = time.monotonic()
        if stage == last_progress[0] and current - last_progress[1] < 0.25 and percent != 100:
            return
        try:
            atomic_json(directory / "progress.json", {"stage": stage, "progress": percent})
            last_progress[:] = [stage, current]
        except PermissionError:
            # Una actualización de progreso puede omitirse sin perder el trabajo.
            pass

    try:
        progress("Comprobando audio y aceleración")
        ffmpeg = configure_ffmpeg()
        import torch
        import whisper
        from tqdm import tqdm
        device = item["device"]
        if device == "auto":
            device = "cuda" if torch.cuda.is_available() else "cpu"
        if device == "cuda" and not torch.cuda.is_available():
            raise RuntimeError("CUDA no está disponible. Crea una nueva transcripción con CPU o Automático.")
        if item["source_type"] == "youtube":
            progress("Descargando audio de YouTube", 0)

            def downloaded(event):
                total = event.get("total_bytes") or event.get("total_bytes_estimate")
                percent = min(100, round(event.get("downloaded_bytes", 0) * 100 / total)) if total else None
                progress("Descargando audio de YouTube", percent)

            source = download_audio(youtube_id(item["url"]), directory, ffmpeg, downloaded)
        else:
            source = directory / item["input_file"]
        progress("Preparando audio para escucha")
        subprocess.run([str(ffmpeg), "-y", "-i", str(source), "-vn", "-ac", "1", "-ar", "16000",
                        "-c:a", "libmp3lame", "-b:a", "64k", str(directory / "listen.mp3")],
                       check=True, capture_output=True)
        progress(f"Cargando modelo {item['model']} · la primera vez puede descargarse")
        model = whisper.load_model(item["model"], device=device, download_root=str(ROOT / "models"))

        class ProgressBar(tqdm):
            def update(self, amount=1):
                super().update(amount)
                if self.total:
                    progress("Transcribiendo audio", min(100, round(self.n * 100 / self.total)))

        # Solo se cambia tqdm dentro de este proceso de trabajo aislado.
        transcription_module = importlib.import_module("whisper.transcribe")
        transcription_module.tqdm.tqdm = ProgressBar
        progress("Transcribiendo audio", 0)
        result = model.transcribe(str(source), language=None if item["language"] == "auto" else item["language"],
                                  fp16=device == "cuda", verbose=False, beam_size=5,
                                  initial_prompt=item.get("glossary") or None)
        segments = [{"start": round(segment["start"], 3), "end": round(segment["end"], 3),
                     "text": segment["text"].strip(),
                     "uncertain": segment.get("avg_logprob", 0) < -0.8 or segment.get("compression_ratio", 0) > 2.4}
                    for segment in result["segments"]]
        result = {"text": result["text"].strip(), "segments": segments, "language": result["language"]}
        atomic_json(directory / "original.json", result)
        atomic_json(directory / "result.json", result)
        write_outputs(result, [directory / "transcript.txt", directory / "transcript.srt"])
        progress("Lista para revisar", 100)
        return 0
    except Exception as error:
        message = str(error)
        if isinstance(error, subprocess.CalledProcessError):
            message = "No se pudo leer el audio. Comprueba que el archivo contenga una pista de audio válida."
        atomic_json(directory / "error.json", {"error": message})
        return 1


if __name__ == "__main__":
    raise SystemExit(run(sys.argv[1]))
