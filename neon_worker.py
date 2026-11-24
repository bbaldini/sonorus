"""Ponte verso il motore di Neon Minutes. Va eseguito con il Python di Neon:
    <Neon>\\runtime\\python.exe -u neon_worker.py '{"video": ..., "out_root": ..., ...}'
Stampa una riga JSON per ogni evento: start, log, progress, done, error."""
import json
import os
import sys
import time
import traceback
from pathlib import Path

NEON = Path(os.environ.get("SONORUS_NEON") or Path(__file__).resolve().parent / "neon-minutes")
for _alt in (Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Neon Minutes",  # Windows, installer
             Path.home() / ".local" / "lib" / "neon-minutes"):                        # Linux, installer
    if not (NEON / "meeting_transcriber").is_dir():
        NEON = _alt
sys.path.insert(0, str(NEON))
os.chdir(NEON)
# Va importato per primo: imposta HF_HOME sulla cartella modelli di Neon, e
# huggingface_hub lo legge una volta sola. Se pyannote o faster-whisper arrivano
# prima, i modelli vengono cercati (e riscaricati) in ~/.cache.
import meeting_transcriber.paths  # noqa: E402,F401


def _local_models_first():
    """faster-whisper ricontrolla sempre online i modelli; il download via xet a
    volte resta appeso per sempre (GPU ferma, nessun errore). Si usano i file già
    presenti e si va in rete solo se mancano davvero."""
    import faster_whisper.transcribe as fwt
    import faster_whisper.utils as fwu
    original = fwu.download_model

    def download_model(name, *a, local_files_only=False, **kw):
        try:
            return original(name, *a, local_files_only=True, **kw)
        except Exception:  # noqa: BLE001
            return original(name, *a, local_files_only=local_files_only, **kw)

    fwu.download_model = download_model
    fwt.download_model = download_model


def emit(**kw):
    print(json.dumps(kw, ensure_ascii=False), flush=True)


def _small_gpu():
    import torch
    return torch.cuda.is_available() and torch.cuda.get_device_properties(0).total_memory <= 6.5 * 2**30


def _small_gpu_fix():
    """Con 4 GB di VRAM, dopo che ctranslate2 ha aperto il suo contesto CUDA,
    pyannote a batch 32 non trova memoria per cuDNN ("GET was unable to find an
    engine") e a batch 16 sfora nella RAM condivisa (90 s di audio -> 6 minuti).
    A batch 4 la separazione delle voci ci mette pochi secondi."""
    if not _small_gpu():
        return
    from pyannote.audio import Pipeline
    original = Pipeline.from_pretrained.__func__

    def from_pretrained(cls, *a, **kw):
        p = original(cls, *a, **kw)
        for attr in ("embedding_batch_size", "segmentation_batch_size"):
            if hasattr(p, attr):
                setattr(p, attr, 4)
        return p

    Pipeline.from_pretrained = classmethod(from_pretrained)


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    args = json.loads(sys.argv[1])
    _small_gpu_fix()
    _local_models_first()
    from meeting_transcriber.paths import DATA_DIR, setup_environment
    from meeting_transcriber.pipeline import Options, Reporter, expected_seconds, plan, process
    from meeting_transcriber.voices import VoiceLibrary

    class JsonReporter(Reporter):
        def log(self, message):
            emit(type="log", msg=str(message))

        def progress(self, fraction, stage):
            emit(type="progress", f=round(float(fraction), 4), stage=stage)

    video = Path(args["video"]).resolve()
    out_root = Path(args["out_root"]) if args.get("out_root") else None
    # con 4 GB large-v3 + il contesto di PyTorch sfora nella RAM condivisa e la
    # trascrizione diventa lentissima: sulle GPU piccole si usa large-v3-turbo
    model = args.get("model") or ("large-v3-turbo" if _small_gpu() else None)
    opts = Options(model=model, language=args.get("language", "it"), glossary=args.get("glossary", []),
                   use_video=args.get("use_video", True))
    token = setup_environment()
    try:
        expected = expected_seconds(plan(video, opts, out_root, token))
    except Exception:  # noqa: BLE001
        expected = None
    emit(type="start", expected=expected, t=time.time())
    library = VoiceLibrary(DATA_DIR / "voices.json")
    md = process(video, opts, token, library, out_root=out_root, report=JsonReporter())
    emit(type="done", md=str(md), dir=str(Path(md).parent))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # noqa: BLE001
        emit(type="error", msg=f"{type(e).__name__}: {e}", tb=traceback.format_exc()[-1500:])
        sys.exit(1)
