"""Autotest multipiattaforma (CI su Windows, macOS, Linux): moduli, unione diretta/verbale, valutazione,
modello Whisper minuscolo sulla CPU. Dati sintetici, niente audio né riunioni vere."""
import json
import io
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
import numpy as np  # noqa: E402

import engine  # noqa: E402
import evalkit  # noqa: E402
import llm  # noqa: E402,F401
import merge  # noqa: E402
import passaporta  # noqa: E402,F401
import teams  # noqa: E402,F401


def check(cond, what):
    print(("ok  " if cond else "NO  ") + what, flush=True)
    if not cond:
        sys.exit(1)


d = Path(tempfile.mkdtemp())
(d / "registrazione").mkdir()
live = {"title": "Test", "started": "2026-01-01T10:00:00", "names": {"me": "Anna", "others": "Altri"}, "lines": [
    {"src": "others", "t": 0.0, "dur": 4.0, "text": "Oggi parliamo del sensore ToF e del biodigestore.", "words": []},
    {"src": "others", "t": 20.0, "dur": 6.0, "text": "Questo tratto il verbale rifinito lo ha saltato del tutto purtroppo.", "words": []},
    {"src": "me", "t": 30.0, "dur": 3.0, "text": "Perfetto, preparo io il dataset.", "words": []}]}
(d / "diretta.json").write_text(json.dumps(live), encoding="utf-8")
para = lambda s, e, sp, txt: {"start": s, "end": e, "speaker": sp, "clusters": ["S1"], "text": txt,  # noqa: E731
                              "words": [{"start": s + i * .5, "end": s + i * .5 + .4, "text": " " + w, "p": .9} for i, w in enumerate(txt.split())]}
doc = {"meta": {}, "paragraphs": [para(0, 4, "Speaker 1", "Oggi parliamo del sensore top e del biodigestore."),
                                  para(30, 33, "Speaker 2", "Perfetto, preparo io il dataset."),
                                  para(40, 41, "Speaker 1", "Grazie a tutti.")]}
(d / "registrazione" / "trascrizione.json").write_text(json.dumps(doc), encoding="utf-8")
(d / "registrazione" / "trascrizione.md").write_text("# t\n\n## Trascrizione\n\nx\n", encoding="utf-8")
r = merge.merge(d, d / "registrazione", "Anna", ["ToF"])
check(len(r["added"]) == 1, "merge: tratto mancante recuperato dalla diretta")
check(len(r["removed"]) == 1, "merge: frase inventata tolta")
check(any(c["new"].startswith("ToF") for c in r["changes"]), "merge: correzione dal glossario (top -> ToF)")
res = evalkit.evaluate(d, d / "registrazione", 0, 10, "Speaker 1: Oggi parliamo del sensore ToF e del biodigestore.", "Anna", ["ToF"])
check(res["unione fine"]["wer"] == 0 and res["rifinita"]["wer"] > 0, "evalkit: WER (unione fine 0, rifinita > 0)")

seg_out = []
seg = engine.Segmenter(lambda k, a, t: seg_out.append((k, len(a))), partial_every=1e9)
t = np.arange(engine.SR * 2) / engine.SR
x = np.concatenate([np.zeros(engine.SR), 0.3 * np.sin(2 * np.pi * 220 * t).astype(np.float32), np.zeros(engine.SR)])
for i in range(0, len(x), 800):
    seg.feed(x[i:i + 800].astype(np.float32), (i + 800) / engine.SR)
check(any(k == "final" for k, _ in seg_out), "segmentatore: riconosce un tratto di suono")

from faster_whisper import WhisperModel  # noqa: E402
m = WhisperModel("tiny", device="cpu", compute_type="int8", download_root=os.path.join(tempfile.gettempdir(), "sonorus-ci-models"))
segs, _ = m.transcribe(np.zeros(engine.SR, np.float32), language="it")
list(segs)
check(True, f"faster-whisper sulla CPU ({sys.platform})")
if sys.platform != "win32":
    import soundcard  # noqa: F401
    check(True, "soundcard importato (cattura PulseAudio/CoreAudio)")
print("Sonorus selftest: ok")
