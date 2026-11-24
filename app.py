"""Sonorus — trascrizione in diretta e verbali con i nomi, in un'unica app.

La diretta usa il motore di Sonorus (engine.py). I verbali rifiniti (voci separate,
nomi dal video di Teams e dalla libreria voci) usano il motore di Neon Minutes,
installato a parte e guidato da neon_worker.py in un processo separato.
La GPU ha 4 GB: le due cose non girano insieme, la diretta ha la precedenza."""
import json
import os
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

import webview

import engine
import merge

HERE = Path(getattr(sys, "_MEIPASS", Path(__file__).parent))
CFG_PATH = engine.APPDATA / "config.json"
JOBS_PATH = engine.APPDATA / "archivio.json"
NEON_PY = engine.NEON_APP / "runtime" / "python.exe"
NO_WINDOW = 0x08000000
MEDIA = ("mp4", "mkv", "mov", "avi", "webm", "m4a", "mp3", "wav", "ogg")
DEFAULTS = {"language": "it", "me_name": "Io", "others_name": "Altri", "pause": 0.7,
            "save_audio": True, "echo_guard": True, "glossary": "", "out_dir": "",
            "output_name": None, "mic_name": None, "on_top": False, "auto_refine": True}


def load_json(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return default


def save_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def overlap(a0, a1, b0, b1):
    return max(0.0, min(a1, b1) - max(a0, b0))


class Api:
    def __init__(self):
        self.cfg = {**DEFAULTS, **load_json(CFG_PATH, {})}
        self.events = []
        self.status = "Avvio…"
        self.session = None
        self.lock = threading.RLock()
        self.gpu = threading.Lock()  # chi tiene la GPU: il modello della diretta o Neon
        self.last_dir = None
        self.window = None
        self.jobs = load_json(JOBS_PATH, [])
        for j in self.jobs:
            if j.get("status") == "running":
                j["status"] = "queued"
        self.proc = None
        self.pausing = False
        self.live_wanted = False  # una diretta sta partendo: la coda non deve riprendersi la GPU
        self.job_ev = threading.Event()
        self.transcriber = engine.Transcriber(lambda *a: None, language=self.cfg["language"],
                                              on_status=self._status)
        self.transcriber.start()
        self.neon_ok = NEON_PY.exists()
        threading.Thread(target=self._job_loop, daemon=True).start()
        self.hotkey = None
        threading.Thread(target=self._hotkey_loop, daemon=True).start()
        self.relive = None  # id della riunione di cui si sta rifacendo la diretta
        if not any(j["status"] == "queued" for j in self.jobs) or not self.neon_ok:
            threading.Thread(target=self._ensure_model, daemon=True).start()
        self.job_ev.set()

    # ------------------------------------------------------------ modello diretta
    @property
    def ready(self):
        return self.transcriber.model is not None

    def _ensure_model(self):
        with self.gpu:
            if self.transcriber.model is None:
                try:
                    self.transcriber.load()
                except Exception as e:  # noqa: BLE001
                    self._status(f"Impossibile caricare il modello: {e}")

    def _status(self, s):
        self.status = s

    def _event(self, ev):
        with self.lock:
            ev["id"] = len(self.events)
            self.events.append(ev)

    # ------------------------------------------------------------ archivio / coda
    def _save_jobs(self):
        with self.lock:
            save_json(JOBS_PATH, self.jobs)

    def _job(self, jid):
        return next((j for j in self.jobs if j["id"] == jid), None)

    def _add_job(self, **kw):
        job = {"id": uuid.uuid4().hex[:10], "added": time.time(), "status": "queued", "progress": 0.0,
               "expected": None, "started": None, "log": [], "error": None, "md": None, "dir": None,
               "language": self.cfg["language"], **kw}
        with self.lock:
            self.jobs.insert(0, job)
        self._save_jobs()
        self.job_ev.set()
        return job

    def _job_loop(self):
        while True:
            self.job_ev.wait(2)
            self.job_ev.clear()
            if self.session or self.live_wanted or not self.neon_ok:
                continue
            with self.lock:
                job = next((j for j in reversed(self.jobs) if j["status"] == "queued"), None)
            if job is None:
                if not self.ready:
                    self._ensure_model()
                continue
            with self.gpu:
                if self.session or self.live_wanted:
                    continue
                if self.transcriber.model is not None:
                    self.transcriber.unload()
                self._status("La GPU sta rifinendo un verbale")
                self._run_job(job)
            self.job_ev.set()

    def _run_job(self, job):
        args = {"video": job["path"], "language": job.get("language", "it"),
                "glossary": [g.strip() for g in self.cfg.get("glossary", "").splitlines() if g.strip()]}
        if job.get("out_root"):
            args["out_root"] = job["out_root"]
        job.update(status="running", progress=0.0, error=None, started=time.time(), log=[])
        self._save_jobs()
        logf = open(engine.APPDATA / "neon.log", "a", encoding="utf-8")  # noqa: SIM115
        logf.write(f"\n=== {time.ctime()} {job['path']}\n")
        logf.flush()
        try:
            self.proc = subprocess.Popen(
                [str(NEON_PY), "-u", str(HERE / "neon_worker.py"), json.dumps(args)],
                stdout=subprocess.PIPE, stderr=logf, cwd=str(engine.NEON_APP),
                creationflags=NO_WINDOW, encoding="utf-8", errors="replace")
        except Exception as e:  # noqa: BLE001
            job.update(status="error", error=str(e))
            self._save_jobs()
            return
        done = None
        for line in self.proc.stdout:
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            t = ev.get("type")
            if t == "start":
                job["expected"] = ev.get("expected")
                job["started"] = time.time()
            elif t == "progress":
                job["progress"] = ev["f"]
                job["stage"] = ev.get("stage")
            elif t == "log":
                job["log"] = (job["log"] + [ev["msg"]])[-40:]
            elif t == "done":
                done = ev
            elif t == "error":
                job["error"] = ev.get("msg")
                logf.write(ev.get("tb", "") + "\n")
        self.proc.wait()
        paused = self.pausing
        logf.close()
        if paused and job.pop("cancel_req", False):
            job.update(status="done" if job.get("md") else ("live" if job.get("kind") == "live" else "cancelled"),
                       progress=1.0 if job.get("md") else 0.0)
        elif paused:
            # interrotto dalla diretta: i passaggi già fatti sono in _lavoro, riprende da lì
            job.update(status="queued", progress=0.0, paused=True)
        elif done:
            job.update(status="done", progress=1.0, md=done["md"], dir=done["dir"], paused=False,
                       finished=time.time())
            if job.get("kind") == "live" and not job.get("me_named"):
                self._name_me(job)
            if job.get("kind") == "live" and job["status"] == "done":
                self._merge_live(job)
        else:
            job.update(status="error", error=job.get("error") or "Elaborazione interrotta (vedi neon.log)")
        self._save_jobs()
        self.proc = None

    def _name_me(self, job):
        """Nella diretta sappiamo quando parlava il microfono: la voce che coincide
        di più con quei momenti è l'utente. Le si dà il suo nome (e così entra nella
        libreria voci di Neon per le riunioni successive)."""
        job["me_named"] = True
        try:
            live = load_json(Path(job["session"]) / "diretta.json", {})
            mine = [(ln["t"], ln["t"] + ln["dur"]) for ln in live.get("lines", []) if ln["src"] == "me"]
            my_total = sum(b - a for a, b in mine)
            if my_total < 5:
                return
            doc = load_json(Path(job["dir"]) / "trascrizione.json", {})
            per = {}
            for p in doc.get("paragraphs", []):
                if len(p.get("clusters", [])) != 1:
                    continue
                c = p["clusters"][0]
                tot, ov = per.get(c, (0.0, 0.0))
                per[c] = (tot + p["end"] - p["start"], ov + sum(overlap(p["start"], p["end"], a, b) for a, b in mine))
            if not per:
                return
            c, (tot, ov) = max(per.items(), key=lambda kv: kv[1][1])
            if tot <= 0 or ov / tot < 0.5 or ov < 0.3 * my_total:
                return
            sp_path = Path(job["dir"]) / "speakers.json"
            sp = load_json(sp_path, {})
            info = sp.get("speakers", {}).get(c)
            me = self.cfg.get("me_name") or "Io"
            if not info or info.get("source") in ("manual", "video") or info["name"] == me or me == "Io":
                return
            info["name"] = me
            save_json(sp_path, sp)
            job["status"] = "queued"  # rigenera solo i testi: è questione di secondi
        except Exception as e:  # noqa: BLE001
            job["log"].append(f"Riconoscimento della tua voce non riuscito: {e}")

    def _merge_live(self, job):
        """Recupera nel verbale i tratti che il motore di Neon ha saltato (vedi merge.py)."""
        try:
            added, removed = merge.merge(job["session"], job["dir"], self.cfg.get("me_name"))
            if added or removed:
                job["log"] = (job["log"] + [f"Unito alla diretta: {len(added)} tratti recuperati, "
                                            f"{len(removed)} frasi inventate tolte"])[-40:]
        except Exception as e:  # noqa: BLE001
            job["log"] = (job["log"] + [f"Unione con la diretta non riuscita: {e}"])[-40:]

    def _pause_job(self):
        if self.proc is None:
            return
        self.pausing = True
        subprocess.run(["taskkill", "/PID", str(self.proc.pid), "/T", "/F"], capture_output=True,
                       creationflags=NO_WINDOW)
        for _ in range(100):
            if self.proc is None:
                break
            time.sleep(0.1)
        self.pausing = False

    # ------------------------------------------------------------ API: diretta
    def init(self):
        try:
            devs = engine.list_devices()
        except Exception as e:  # noqa: BLE001
            devs = {"outputs": [], "mics": [], "error": str(e)}
        return {"cfg": self.cfg, "devices": devs, "neon": self.neon_ok, "hotkey": self.hotkey,
                "default_out": str(Path.home() / "Documents" / "Sonorus")}

    def start(self, cfg):
        if self.session:
            return {"ok": False, "error": "Già in ascolto."}
        self.cfg.update({k: v for k, v in cfg.items() if k in DEFAULTS})
        save_json(CFG_PATH, self.cfg)
        self.live_wanted = True
        try:
            return self._start(cfg)
        finally:
            self.live_wanted = False
            self.job_ev.set()

    def _start(self, cfg):
        if self.proc is not None:
            self._status("Metto in pausa la rifinitura…")
            self._pause_job()
        self._ensure_model()
        if not self.ready:
            return {"ok": False, "error": self.status}
        tr = self.transcriber
        tr.language = None if self.cfg["language"] == "auto" else self.cfg["language"]
        tr.prompt = self.cfg.get("glossary", "").replace("\n", ", ").strip(", ")
        tr.context.clear()
        with self.lock:
            self.events = []
        sess = engine.Session({**self.cfg, **cfg}, self._event, self._status)
        try:
            sess.start(tr)
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": str(e)}
        self.session = sess
        self._status(f"In ascolto ({tr.device})")
        self.last_dir = str(sess.out_dir)
        return {"ok": True, "dir": self.last_dir}

    def stop(self):
        if not self.session:
            return {"ok": True}
        s = self.session
        self._status("Chiudo e salvo…")
        d = s.stop()
        self.session = None
        self._status(f"Pronto ({self.transcriber.device})")
        title = s.cfg.get("title") or "Riunione"
        wav = Path(d) / "registrazione.wav"
        job = self._add_job(kind="live", title=title, session=d, path=str(wav), out_root=d,
                            language=self.cfg["language"],
                            status="queued" if (self.cfg.get("auto_refine") and wav.exists() and self.neon_ok)
                            else "live")
        self.job_ev.set()
        return {"ok": True, "dir": d, "job": job["id"]}

    def toggle_mic(self, paused=None):
        """Mette in pausa (o riattiva) il tuo microfono nella diretta: niente audio
        salvato, niente trascrizione. Da usare insieme al mute di Teams."""
        s = self.session
        if not s:
            return None
        want = (not s.mic_paused) if paused is None else bool(paused)
        s.set_mic_paused(want)
        self._event({"src": "sys", "kind": "mute", "paused": want,
                     "ts": engine.fmt_ts(time.monotonic() - s.t0), "t": time.monotonic() - s.t0})
        return want

    def _hotkey_loop(self):
        """Ctrl+Alt+M da qualsiasi finestra (anche da Teams) mette in pausa il microfono."""
        if sys.platform != "win32":
            return
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        MOD_ALT, MOD_CONTROL, MOD_NOREPEAT, WM_HOTKEY = 0x1, 0x2, 0x4000, 0x0312
        if not user32.RegisterHotKey(None, 1, MOD_ALT | MOD_CONTROL | MOD_NOREPEAT, ord("M")):
            self.hotkey = False
            return
        self.hotkey = True
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
            if msg.message == WM_HOTKEY and self.session:
                self.toggle_mic()

    def poll(self, since):
        with self.lock:
            evs = self.events[since:]
            running = next((j for j in self.jobs if j["status"] == "running"), None)
            queued = sum(1 for j in self.jobs if j["status"] == "queued")
        out = {"events": evs, "status": self.status, "ready": self.ready, "running": self.session is not None,
               "jobs_sig": self._jobs_sig(), "queued": queued,
               "job": running and {k: running.get(k) for k in ("id", "title", "progress", "expected", "started")}}
        if self.session:
            out["mic_paused"] = self.session.mic_paused
            out["levels"] = self.session.levels()
            out["errors"] = self.session.errors()
            out["lag"] = len(self.transcriber.finals)
        return out

    def _jobs_sig(self):
        return "|".join(f"{j['id']}:{j['status']}:{round(j.get('progress') or 0, 2)}" for j in self.jobs)

    def rename(self, which, name):
        self.cfg[which] = name
        save_json(CFG_PATH, self.cfg)
        if self.session:
            self.session.names["me" if which == "me_name" else "others"] = name

    def save_settings(self, cfg):
        self.cfg.update({k: v for k, v in cfg.items() if k in DEFAULTS})
        save_json(CFG_PATH, self.cfg)
        if self.window is not None and "on_top" in cfg:
            self.window.on_top = bool(cfg["on_top"])
        return self.cfg

    def open_folder(self, path=None):
        p = path or self.last_dir or self.cfg.get("out_dir") or str(Path.home() / "Documents" / "Sonorus")
        Path(p).mkdir(parents=True, exist_ok=True)
        os.startfile(p)  # noqa: S606

    def pick_folder(self):
        r = self.window.create_file_dialog(webview.FileDialog.FOLDER)
        return r[0] if r else None

    # ------------------------------------------------------------ API: archivio
    def list_jobs(self):
        with self.lock:
            return [{k: j.get(k) for k in ("id", "title", "kind", "status", "progress", "expected", "started",
                                          "added", "error", "paused", "path", "language")} for j in self.jobs]

    def relive_state(self):
        return self.relive

    def add_files(self, language=None):
        r = self.window.create_file_dialog(
            webview.FileDialog.OPEN, allow_multiple=True,
            file_types=("Registrazioni (" + ";".join(f"*.{e}" for e in MEDIA) + ")", "Tutti i file (*.*)"))
        added = []
        for p in r or []:
            added.append(self._add_job(kind="file", title=Path(p).stem, path=p,
                                       language=language or self.cfg["language"])["id"])
        return added

    def refine(self, jid):
        j = self._job(jid)
        if j and Path(j["path"]).exists():
            j.update(status="queued", error=None)
            self._save_jobs()
            self.job_ev.set()
            return True
        return False

    def cancel(self, jid):
        j = self._job(jid)
        if not j:
            return
        if j["status"] == "running":
            j["cancel_req"] = True
            self._pause_job()
            return
        j["status"] = "done" if j.get("md") else ("live" if j.get("kind") == "live" else "cancelled")
        self._save_jobs()

    def remove(self, jid):
        """Toglie dall'elenco (i file restano dove sono)."""
        j = self._job(jid)
        if not j:
            return
        if j["status"] == "running":
            self._pause_job()
        with self.lock:
            self.jobs = [x for x in self.jobs if x["id"] != jid]
        self._save_jobs()

    def job_detail(self, jid):
        j = self._job(jid)
        if not j:
            return None
        out = {"job": j, "live": None, "verbale": None, "speakers": None,
               "tracks": bool(j.get("session")) and any((Path(j["session"]) / t).exists() for t in engine.TRACKS),
               "relive": self.relive == jid}
        if j.get("session"):
            out["live"] = load_json(Path(j["session"]) / "diretta.json", None)
        if j.get("dir"):
            doc = load_json(Path(j["dir"]) / "trascrizione.json", None)
            if doc:
                out["verbale"] = [{"start": p["start"], "speaker": p["speaker"], "text": p["text"]}
                                  for p in doc.get("paragraphs", [])]
            sp = load_json(Path(j["dir"]) / "speakers.json", {}).get("speakers")
            if sp:
                out["speakers"] = [{"id": k, "name": v["name"], "speech_s": v.get("speech_s", 0),
                                    "source": v.get("source")} for k, v in
                                   sorted(sp.items(), key=lambda kv: -kv[1].get("speech_s", 0))]
        return out

    def retranscribe(self, jid):
        """Rifà la diretta dalle tracce separate (dopo aver cambiato glossario, filtri…).
        Se c'è già un verbale rifinito, lo riunisce alla nuova diretta."""
        j = self._job(jid)
        if not j or not j.get("session"):
            return {"ok": False, "error": "Non è una diretta."}
        if self.session or self.proc is not None or self.relive:
            return {"ok": False, "error": "La GPU è occupata (diretta o rifinitura in corso): riprova dopo."}
        if not any((Path(j["session"]) / t).exists() for t in engine.TRACKS):
            return {"ok": False, "error": "Per questa riunione le tracce separate non ci sono: "
                                          "le sessioni prima del 24/11/2026 le cancellavano."}
        self.relive = jid
        threading.Thread(target=self._retranscribe, args=(j,), daemon=True).start()
        return {"ok": True}

    def _retranscribe(self, j):
        try:
            with self.gpu:
                if self.transcriber.model is None:  # _ensure_model() riprenderebbe self.gpu: stallo
                    self.transcriber.load()
                tr = self.transcriber
                tr.language = None if j.get("language") in (None, "auto") else j["language"]
                tr.prompt = self.cfg.get("glossary", "").replace("\n", ", ").strip(", ")
                names = {"me": self.cfg.get("me_name") or "Io", "others": self.cfg.get("others_name") or "Altri"}
                self._status("Rifaccio la diretta…")
                n = engine.retranscribe(j["session"], tr, names, pause=float(self.cfg.get("pause", 0.7)),
                                        echo_guard=self.cfg.get("echo_guard", True),
                                        on_progress=lambda f: self._status(f"Rifaccio la diretta… {round(f * 100)}%"))
            j["log"] = (j.get("log", []) + [f"Diretta rifatta: {n} frasi"])[-40:]
            if j.get("dir") and Path(j["dir"], "trascrizione.json").exists():
                self._merge_live(j)
            self._status(f"Diretta rifatta: {n} frasi")
        except Exception as e:  # noqa: BLE001
            self._status(f"Ritrascrizione non riuscita: {e}")
        finally:
            self.relive = None
            self._save_jobs()
            self.job_ev.set()

    def rename_speakers(self, jid, names):
        """names: {cluster: nuovo nome}. Neon rigenera i testi e aggiorna la libreria voci."""
        j = self._job(jid)
        if not j or not j.get("dir"):
            return False
        sp_path = Path(j["dir"]) / "speakers.json"
        sp = load_json(sp_path, {})
        for c, n in names.items():
            if c in sp.get("speakers", {}) and n.strip():
                sp["speakers"][c]["name"] = n.strip()
        save_json(sp_path, sp)
        return self.refine(jid)

    def open_job_folder(self, jid):
        j = self._job(jid)
        if j:
            self.open_folder(j.get("dir") or j.get("session") or str(Path(j["path"]).parent))

    def open_file(self, jid, which="md"):
        j = self._job(jid)
        if j:
            f = Path(j["md"]) if which == "md" and j.get("md") else Path(j.get("session") or "") / "diretta.md"
            if f.exists():
                os.startfile(str(f))  # noqa: S606


def main():
    api = Api()
    win = webview.create_window("Sonorus", str(HERE / "static" / "index.html"), js_api=api,
                                width=1060, height=780, min_size=(600, 480), on_top=api.cfg.get("on_top", False),
                                background_color="#0f0d16")
    api.window = win

    def on_closing():
        if api.session:
            api.stop()
        if api.proc is not None:
            api._pause_job()
            api._save_jobs()

    win.events.closing += on_closing
    webview.start(private_mode=False, storage_path=str(engine.APPDATA / "webview"))


if __name__ == "__main__":
    main()
