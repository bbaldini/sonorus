"""Sonorus engine: cattura audio di sistema (WASAPI loopback) + microfono,
segmenta il parlato e lo trascrive in diretta con faster-whisper."""
import glob
import json
import os
import queue
import re
import sys
import threading
import time
import wave
from collections import deque
from datetime import datetime
from pathlib import Path

import numpy as np

SR = 16000
FRAME = 480  # 30 ms a 16 kHz
FRAME_S = FRAME / SR

APPDATA = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "Sonorus"
NEON_DATA = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "Neon Minutes"
NEON_APP = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "Programs" / "Neon Minutes"
# un'unica cartella di modelli, condivisa con il motore di Neon Minutes
MODEL_DIR = NEON_DATA / "modelli" / "hub" if NEON_APP.exists() else APPDATA / "modelli"


def _add_cuda_dlls():
    """Rende visibili a ctranslate2 le DLL di cuBLAS/cuDNN installate via pip."""
    if sys.platform != "win32":
        return
    roots = [Path(p) for p in sys.path if p.endswith("site-packages")]
    for root in roots:
        for d in glob.glob(str(root / "nvidia" / "*" / "bin")):
            try:
                os.add_dll_directory(d)
            except OSError:
                pass
            os.environ["PATH"] = d + os.pathsep + os.environ.get("PATH", "")


_add_cuda_dlls()
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")

# Frasi che Whisper "inventa" sul silenzio o sul rumore.
HALLUCINATIONS = [
    r"sottotitoli (creati|a cura|e revisione)", r"amara\.org", r"qtss",
    r"grazie (a tutti )?per (la visione|l'attenzione|aver guardato)",
    r"iscriviti al canale", r"thanks? (you )?for watching", r"^\W*(grazie|thank you)\W*$",
    r"^\W*\.+\W*$", r"^\W*musica\W*$", r"^\W*\[.*\]\W*$",
    # frase intera inventata sul rumore del microfono chiuso (call ADAPT AI, 24/11)
    r"^\W*grazie (a tutti|mille|a voi)\W*$",
]
_HALL_RE = re.compile("|".join(HALLUCINATIONS), re.I)


def fmt_ts(sec, srt=False):
    sec = max(0.0, sec)
    h, rem = divmod(int(sec), 3600)
    m, s = divmod(rem, 60)
    if srt:
        return f"{h:02d}:{m:02d}:{s:02d},{int((sec % 1) * 1000):03d}"
    return f"{h:02d}:{m:02d}:{s:02d}"


# ---------------------------------------------------------------- dispositivi
_PA = None
_PA_LOCK = threading.Lock()


def get_pa(refresh=False):
    """Un'unica istanza PortAudio per processo: due istanze aperte insieme fanno
    fallire WASAPI con 'Unanticipated host error'."""
    global _PA
    import pyaudiowpatch as pa
    with _PA_LOCK:
        if refresh and _PA is not None:
            _PA.terminate()
            _PA = None
        if _PA is None:
            _PA = pa.PyAudio()
        return _PA


def list_devices(refresh=False):
    import pyaudiowpatch as pa
    p = get_pa(refresh)
    wasapi = p.get_host_api_info_by_type(pa.paWASAPI)
    default_out = p.get_device_info_by_index(wasapi["defaultOutputDevice"])["name"]
    default_in = wasapi["defaultInputDevice"]
    outs, mics = [], []
    for i in range(p.get_device_count()):
        d = p.get_device_info_by_index(i)
        if d["hostApi"] != wasapi["index"]:
            continue
        if d.get("isLoopbackDevice"):
            name = d["name"].replace(" [Loopback]", "")
            outs.append({"index": i, "name": name, "default": name == default_out})
        elif d["maxInputChannels"] > 0:
            mics.append({"index": i, "name": d["name"], "default": i == default_in})
    return {"outputs": outs, "mics": mics}


# ---------------------------------------------------------------- segmentatore
class Segmenter:
    """Rilevatore di parlato a energia con soglia adattiva.
    Emette ('partial', audio) durante il parlato e ('final', audio) a fine frase."""

    def __init__(self, emit, silence_end=0.7, max_len=20.0, partial_every=1.0, gate=None):
        self.emit = emit
        self.silence_end = silence_end
        self.max_len = max_len
        self.partial_every = partial_every
        self.gate = gate  # funzione(db, floor) -> bool aggiuntiva (anti-eco)
        self.floor = -60.0
        self.pre = deque(maxlen=int(0.3 / FRAME_S))
        self.buf = []
        self.speaking = False
        self.run = 0
        self.silence = 0
        self.since_partial = 0
        self.start_t = 0.0
        self.level = -90.0
        self.active_until = 0.0
        self._rest = np.zeros(0, np.float32)

    def feed(self, audio, t_now):
        audio = np.concatenate([self._rest, audio])
        n = len(audio) // FRAME
        self._rest = audio[n * FRAME:]
        for k in range(n):
            fr = audio[k * FRAME:(k + 1) * FRAME]
            # tempo di fine di questo frame
            t = t_now - (len(audio) - (k + 1) * FRAME) / SR
            self._frame(fr, t)

    def _frame(self, fr, t):
        rms = float(np.sqrt(np.mean(fr * fr)) + 1e-10)
        db = 20 * np.log10(rms)
        self.level = max(db, self.level - 1.5)
        is_speech = db > self.floor + 9 and db > -52
        if is_speech and self.gate is not None:
            is_speech = self.gate(db, self.floor)
        if not self.speaking and not is_speech:
            # aggiorna lentamente il rumore di fondo
            self.floor = min(-30.0, max(-80.0, 0.97 * self.floor + 0.03 * db))
        if is_speech:
            self.active_until = t + 0.3

        if not self.speaking:
            self.pre.append(fr)
            self.run = self.run + 1 if is_speech else 0
            if self.run >= 3:
                self.speaking = True
                self.buf = list(self.pre)
                self.pre.clear()
                self.start_t = t - len(self.buf) * FRAME_S
                self.silence = 0
                self.since_partial = 0
            return

        self.buf.append(fr)
        self.since_partial += 1
        self.silence = 0 if is_speech else self.silence + 1
        dur = len(self.buf) * FRAME_S
        if self.silence * FRAME_S >= self.silence_end or dur >= self.max_len:
            keep = len(self.buf) - max(0, self.silence - int(0.25 / FRAME_S))
            seg = np.concatenate(self.buf[:keep])
            if len(seg) / SR >= 0.4:
                self.emit("final", seg, self.start_t)
            self.speaking = False
            self.buf = []
            self.run = 0
            self.pre.clear()
        elif self.since_partial * FRAME_S >= self.partial_every and dur >= 0.8:
            self.since_partial = 0
            self.emit("partial", np.concatenate(self.buf), self.start_t)

    def flush(self):
        if self.speaking and self.buf:
            seg = np.concatenate(self.buf)
            if len(seg) / SR >= 0.4:
                self.emit("final", seg, self.start_t)
        self.speaking = False
        self.buf = []


# ---------------------------------------------------------------- cattura
class Capture(threading.Thread):
    def __init__(self, key, device_index, segmenter, t0, wav_path=None, fill_gaps=False):
        super().__init__(daemon=True)
        self.key = key
        self.device_index = device_index
        self.seg = segmenter
        self.t0 = t0
        self.wav_path = wav_path
        self.fill_gaps = fill_gaps
        self.q = queue.Queue()
        self.stop_ev = threading.Event()
        self.error = None
        self.muted = False  # in pausa: nel wav va silenzio e niente arriva al trascrittore

    def run(self):
        import pyaudiowpatch as pa
        import soxr
        if sys.platform == "win32":
            # WASAPI apre i flussi solo da thread con COM inizializzato,
            # altrimenti: "Unanticipated host error"
            import ctypes
            ctypes.windll.ole32.CoInitializeEx(None, 0)
        p = get_pa()
        wav = None
        try:
            d = p.get_device_info_by_index(self.device_index)
            ch = max(1, int(d["maxInputChannels"]))
            rate = int(d["defaultSampleRate"])
            rs = soxr.ResampleStream(rate, SR, 1, dtype="float32")

            def cb(data, frames, info, status):
                self.q.put(np.frombuffer(data, np.int16))
                return (None, pa.paContinue)

            stream = p.open(format=pa.paInt16, channels=ch, rate=rate, input=True,
                            input_device_index=self.device_index,
                            frames_per_buffer=int(rate * 0.05), stream_callback=cb)
            if self.wav_path:
                wav = wave.open(str(self.wav_path), "wb")
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(SR)
            written = 0
            while not self.stop_ev.is_set():
                try:
                    x = self.q.get(timeout=0.1)
                    x = x.reshape(-1, ch).astype(np.float32).mean(axis=1) / 32768.0
                    y = rs.resample_chunk(x)
                except queue.Empty:
                    y = np.zeros(0, np.float32)
                now = time.monotonic() - self.t0
                # il loopback WASAPI non manda nulla quando il PC è muto:
                # riempiamo i buchi di silenzio per tenere allineato il tempo.
                if self.fill_gaps:
                    missing = int(now * SR) - (written + len(y))
                    if missing > SR * 0.2:
                        y = np.concatenate([np.zeros(missing, np.float32), y])
                if len(y):
                    written += len(y)
                    if self.muted:
                        y = np.zeros_like(y)
                        if self.seg.speaking:
                            self.seg.flush()
                        self.seg.level = -90.0
                    if wav:
                        wav.writeframes((np.clip(y, -1, 1) * 32767).astype(np.int16).tobytes())
                    if not self.muted:
                        self.seg.feed(y, now)
            stream.stop_stream()
            stream.close()
        except Exception as e:  # noqa: BLE001
            self.error = f"{type(e).__name__}: {e}"
        finally:
            if wav:
                wav.close()
            self.seg.flush()


# ---------------------------------------------------------------- trascrittore
class Transcriber(threading.Thread):
    def __init__(self, on_text, language="it", prompt="", on_status=None):
        super().__init__(daemon=True)
        self.on_text = on_text
        self.on_status = on_status or (lambda s: None)
        self.language = None if language in ("", "auto") else language
        self.prompt = prompt
        self.finals = deque()
        self.partials = {}
        self.cv = threading.Condition()
        self.stop_ev = threading.Event()
        self.model = None
        self.device = None
        self.context = deque(maxlen=3)
        self.busy = 0.0
        self.inflight = False

    def unload(self):
        """Libera la GPU (serve al motore di Neon per le rifiniture)."""
        import gc
        self.model = None
        gc.collect()

    def load(self):
        from faster_whisper import WhisperModel
        if self.model is not None:
            return
        MODEL_DIR.mkdir(parents=True, exist_ok=True)
        try:
            self.on_status("Carico il modello sulla GPU…")
            try:
                self.model = WhisperModel("large-v3-turbo", device="cuda", compute_type="int8_float16",
                                          download_root=str(MODEL_DIR), local_files_only=True)
            except Exception:  # noqa: BLE001  (modello non ancora scaricato)
                self.model = WhisperModel("large-v3-turbo", device="cuda", compute_type="int8_float16",
                                          download_root=str(MODEL_DIR))
            self.device = "GPU"
            # riscaldamento: la prima inferenza CUDA è lenta
            list(self.model.transcribe(np.zeros(SR, np.float32), language=self.language or "it")[0])
        except Exception as e:  # noqa: BLE001
            self.on_status(f"GPU non disponibile ({e}); passo alla CPU…")
            self.model = WhisperModel("small", device="cpu", compute_type="int8",
                                      download_root=str(MODEL_DIR))
            self.device = "CPU"
        self.on_status(f"Pronto ({self.device})")

    def submit(self, src, kind, audio, t):
        with self.cv:
            if kind == "final":
                self.finals.append((src, audio, t))
                self.partials.pop(src, None)
            else:
                self.partials[src] = (audio, t)
            self.cv.notify()

    def _prompt(self, final):
        # il contesto delle frasi precedenti aiuta le finali, ma nelle provvisorie
        # (beam 1, audio troncato) il modello tende a ricopiarlo: lì solo glossario
        ctx = " ".join(self.context)[-220:] if final else ""
        return (self.prompt + " " + ctx).strip() or None

    def _run(self, audio, final):
        segs, _info = self.model.transcribe(
            audio, language=self.language, beam_size=5 if final else 1,
            initial_prompt=self._prompt(final), condition_on_previous_text=False,
            vad_filter=final, vad_parameters={"min_silence_duration_ms": 500},
            no_speech_threshold=0.6, temperature=0.0 if not final else (0.0, 0.2, 0.4),
        )
        out = []
        for s in segs:
            # prima serviva che valessero entrambe: troppo permissivo sul rumore
            if s.no_speech_prob > 0.6 or s.avg_logprob < -1.0 or s.compression_ratio > 2.4:
                continue
            txt = s.text.strip()
            if txt and not _HALL_RE.search(txt):
                out.append(txt)
        return " ".join(out).strip()

    def run(self):
        while not self.stop_ev.is_set():
            with self.cv:
                while not self.finals and not self.partials and not self.stop_ev.is_set():
                    self.cv.wait(0.2)
                if self.stop_ev.is_set() and not self.finals:
                    break
                if self.finals:
                    src, audio, t = self.finals.popleft()
                    final = True
                elif self.partials:
                    src = next(iter(self.partials))
                    audio, t = self.partials.pop(src)
                    final = False
            t1 = time.monotonic()
            self.inflight = True
            try:
                text = self._run(audio, final)
            except Exception as e:  # noqa: BLE001
                self.on_status(f"Errore di trascrizione: {e}")
                continue
            finally:
                self.inflight = False
            self.busy = time.monotonic() - t1
            if final and text:
                self.context.append(text)
            self.on_text(src, "final" if final else "partial", text, t, len(audio) / SR)

    def drain(self, timeout=60):
        """Aspetta che le frasi finali in coda siano trascritte."""
        end = time.monotonic() + timeout
        while (self.finals or self.inflight) and time.monotonic() < end:
            time.sleep(0.1)
        with self.cv:
            self.partials.clear()


# ---------------------------------------------------------------- sessione
class Session:
    def __init__(self, cfg, on_event, on_status):
        self.cfg = cfg
        self.on_event = on_event
        self.on_status = on_status
        self.caps = []
        self.segs = {}
        self.names = {"me": cfg.get("me_name") or "Io", "others": cfg.get("others_name") or "Altri"}
        self.lines = []
        self.lock = threading.Lock()
        self.transcriber = None
        self.out_dir = None
        self.started = None
        self.t0 = None
        self.mute_spans = []  # [inizio, fine] in secondi dei tratti col microfono in pausa

    def start(self, transcriber):
        self.transcriber = transcriber
        transcriber.on_text = self._on_text
        base = Path(self.cfg.get("out_dir") or (Path.home() / "Documents" / "Sonorus"))
        self.started = datetime.now()
        title = re.sub(r'[<>:"/\\|?*]', "", self.cfg.get("title") or "").strip()
        name = self.started.strftime("%Y-%m-%d_%H%M") + (f" {title}" if title else "")
        self.out_dir = base / name
        self.out_dir.mkdir(parents=True, exist_ok=True)
        with open(self.out_dir / "diretta.md", "w", encoding="utf-8") as f:
            f.write(f"# {title or 'Riunione'} — {self.started.strftime('%d/%m/%Y %H:%M')}\n\n")

        t0 = self.t0 = time.monotonic()
        save_audio = self.cfg.get("save_audio", True)
        others = self.segs["others"] = Segmenter(lambda k, a, t: transcriber.submit("others", k, a, t),
                                                 silence_end=float(self.cfg.get("pause", 0.7)))
        echo = self.cfg.get("echo_guard", True)

        def gate(db, floor):
            # se in quel momento parla qualcuno dagli altoparlanti, il microfono
            # deve essere molto più forte per contare (evita di trascrivere l'eco)
            if not echo:
                return True
            now = time.monotonic() - t0
            return now > others.active_until or db > floor + 22

        me = self.segs["me"] = Segmenter(lambda k, a, t: transcriber.submit("me", k, a, t),
                                         silence_end=float(self.cfg.get("pause", 0.7)), gate=gate)
        if self.cfg.get("output_index") is not None:
            self.caps.append(Capture("others", int(self.cfg["output_index"]), others, t0,
                                     self.out_dir / "_altri.wav" if save_audio else None, fill_gaps=True))
        if self.cfg.get("mic_index") is not None:
            self.caps.append(Capture("me", int(self.cfg["mic_index"]), me, t0,
                                     self.out_dir / "_io.wav" if save_audio else None, fill_gaps=True))
        for c in self.caps:
            c.start()

    def set_mic_paused(self, paused):
        now = round(time.monotonic() - self.t0, 2)
        for c in self.caps:
            if c.key == "me":
                c.muted = bool(paused)
        if paused and not (self.mute_spans and self.mute_spans[-1][1] is None):
            self.mute_spans.append([now, None])
        elif not paused and self.mute_spans and self.mute_spans[-1][1] is None:
            self.mute_spans[-1][1] = now
        return self.mic_paused

    @property
    def mic_paused(self):
        return any(c.muted for c in self.caps if c.key == "me")

    def levels(self):
        return {k: round(float(max(s.level, -90.0)), 1) for k, s in self.segs.items()}

    def errors(self):
        return [f"{c.key}: {c.error}" for c in self.caps if c.error]

    def _on_text(self, src, kind, text, t, dur):
        ev = {"src": src, "name": self.names[src], "kind": kind, "text": text,
              "t": round(t, 2), "ts": fmt_ts(t), "dur": round(dur, 2)}
        if kind == "final" and text:
            with self.lock:
                self.lines.append(ev)
                with open(self.out_dir / "diretta.md", "a", encoding="utf-8") as f:
                    f.write(f"**[{ev['ts']}] {ev['name']}:** {text}\n\n")
        self.on_event(ev)

    def stop(self):
        for c in self.caps:
            c.stop_ev.set()
        for c in self.caps:
            c.join(timeout=5)
        self.transcriber.drain()
        if self.mute_spans and self.mute_spans[-1][1] is None:
            self.mute_spans[-1][1] = round(time.monotonic() - self.t0, 2)
        self._write_extras()
        return str(self.out_dir)

    def _write_extras(self):
        write_live(self.out_dir, self.cfg.get("title") or "", self.started, self.names, self.lines, self.mute_spans)
        mix_tracks(self.out_dir)


def write_live(out_dir, title, started, names, lines, mute_spans=()):
    """diretta.md / .json / .srt in ordine cronologico (le due sorgenti arrivano sfasate)."""
    out_dir = Path(out_dir)
    lines = sorted(lines, key=lambda e: e["t"])
    with open(out_dir / "diretta.md", "w", encoding="utf-8") as f:
        f.write(f"# {title or 'Riunione'} — {started.strftime('%d/%m/%Y %H:%M')}\n\n")
        if mute_spans:
            f.write("> Microfono in pausa: " + ", ".join(f"{fmt_ts(a)}–{fmt_ts(b or a)}" for a, b in mute_spans)
                    + " (in quei tratti la tua voce non è stata né registrata né trascritta).\n\n")
        for e in lines:
            f.write(f"**[{e['ts']}] {e['name']}:** {e['text']}\n\n")
    with open(out_dir / "diretta.json", "w", encoding="utf-8") as f:
        json.dump({"title": title, "started": started.isoformat(timespec="seconds"), "names": names,
                   "mute_spans": list(mute_spans), "lines": lines}, f, ensure_ascii=False, indent=1)
    with open(out_dir / "diretta.srt", "w", encoding="utf-8") as f:
        for i, e in enumerate(lines, 1):
            f.write(f"{i}\n{fmt_ts(e['t'], True)} --> {fmt_ts(e['t'] + e['dur'], True)}\n"
                    f"{e['name']}: {e['text']}\n\n")


TRACKS = ("_altri.wav", "_io.wav")


def mix_tracks(out_dir):
    """Unisce le due tracce in registrazione.wav (per il verbale rifinito).
    Le tracce restano: servono per ritrascrivere la diretta."""
    out_dir = Path(out_dir)
    arrays = []
    for name in TRACKS:
        pth = out_dir / name
        if pth.exists():
            with wave.open(str(pth)) as w:
                arrays.append(np.frombuffer(w.readframes(w.getnframes()), np.int16).astype(np.float32))
    if not arrays:
        return
    n = max(len(a) for a in arrays)
    mix = np.zeros(n, np.float32)
    for a in arrays:
        mix[:len(a)] += a
    peak = np.abs(mix).max() if n else 0
    if peak > 32767:
        mix *= 32767 / peak
    with wave.open(str(out_dir / "registrazione.wav"), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(mix.astype(np.int16).tobytes())


def _read_track(path):
    with wave.open(str(path)) as w:
        rate, ch = w.getframerate(), w.getnchannels()
        x = np.frombuffer(w.readframes(w.getnframes()), np.int16).reshape(-1, ch).mean(axis=1) / 32768.0
    if rate != SR:
        import soxr
        x = soxr.resample(x, rate, SR)
    return x.astype(np.float32)


def retranscribe(out_dir, transcriber, names=None, pause=0.7, echo_guard=True, on_progress=None):
    """Rifà la diretta dalle tracce salvate (_altri.wav, _io.wav), più veloce del
    tempo reale. Utile dopo aver cambiato glossario o filtri. Ritorna il numero di frasi."""
    out_dir = Path(out_dir)
    old = {}
    try:
        old = json.loads((out_dir / "diretta.json").read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        pass
    names = names or old.get("names") or {"me": "Io", "others": "Altri"}
    srcs = {k: out_dir / f for k, f in (("others", "_altri.wav"), ("me", "_io.wav")) if (out_dir / f).exists()}
    if not srcs:
        raise FileNotFoundError("Le tracce separate di questa riunione non ci sono (sessioni più vecchie le cancellavano).")
    tracks = {k: _read_track(p) for k, p in srcs.items()}
    lines, lock = [], threading.Lock()
    clock = {"t": 0.0}

    def on_text(src, kind, text, t, dur):
        if kind == "final" and text:
            with lock:
                lines.append({"src": src, "name": names.get(src, src), "kind": "final", "text": text,
                              "t": round(t, 2), "ts": fmt_ts(t), "dur": round(dur, 2)})

    saved_cb, saved_ctx = transcriber.on_text, list(transcriber.context)
    transcriber.on_text = on_text
    transcriber.context.clear()
    try:
        segs = {}
        segs["others"] = Segmenter(lambda k, a, t: transcriber.submit("others", k, a, t),
                                   silence_end=pause, partial_every=1e9)
        others = segs["others"]

        def gate(db, floor):
            return not echo_guard or clock["t"] > others.active_until or db > floor + 22

        segs["me"] = Segmenter(lambda k, a, t: transcriber.submit("me", k, a, t),
                               silence_end=pause, partial_every=1e9, gate=gate)
        n = max(len(x) for x in tracks.values())
        step = SR // 20
        for i in range(0, n, step):
            clock["t"] = (i + step) / SR
            for k, x in tracks.items():
                if i < len(x):
                    segs[k].feed(x[i:i + step], clock["t"])
            # non accumulare troppo audio in memoria: aspetta se il trascrittore è indietro
            while len(transcriber.finals) > 8:
                time.sleep(0.05)
            if on_progress and i % (step * 200) == 0:
                on_progress(i / n)
        for sg in segs.values():
            sg.flush()
        transcriber.drain(timeout=600)
    finally:
        transcriber.on_text = saved_cb
        transcriber.context.clear()
        transcriber.context.extend(saved_ctx)
    started = datetime.fromisoformat(old["started"]) if old.get("started") else datetime.now()
    write_live(out_dir, old.get("title", ""), started, names, lines, old.get("mute_spans", []))
    return len(lines)


# ---------------------------------------------------------------- test da file
def transcribe_file(path, language="it"):
    """Simula la diretta su un file wav (per i test): lo passa al segmentatore
    a blocchi di 50 ms e stampa partial/final."""
    import soxr
    with wave.open(path) as w:
        rate, ch = w.getframerate(), w.getnchannels()
        x = np.frombuffer(w.readframes(w.getnframes()), np.int16).reshape(-1, ch).mean(axis=1) / 32768.0
    x = soxr.resample(x.astype(np.float32), rate, SR)
    res = []
    tr = Transcriber(lambda s, k, txt, t, d: (res.append((k, t, txt)),
                                              print(f"[{k:7s}] {fmt_ts(t)} {txt}", flush=True)),
                     language=language, on_status=print)
    tr.load()
    tr.start()
    seg = Segmenter(lambda k, a, t: tr.submit("others", k, a, t))
    step = SR // 20
    t0 = time.monotonic()
    for i in range(0, len(x), step):
        seg.feed(x[i:i + step].astype(np.float32), (i + step) / SR)
        # tempo reale: aspetta come farebbe la cattura vera
        lag = (i + step) / SR - (time.monotonic() - t0)
        if lag > 0:
            time.sleep(lag)
    seg.feed(np.zeros(SR, np.float32), len(x) / SR + 1)
    tr.drain()
    tr.stop_ev.set()
    return res


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--devices":
        import json
        print(json.dumps(list_devices(), indent=1, ensure_ascii=False))
    elif len(sys.argv) > 1:
        transcribe_file(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else "it")
