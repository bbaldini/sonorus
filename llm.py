"""Verbale e riassunti con un modello locale (Ollama).

- minutes(): a fine riunione, dalla trascrizione unita scrive il verbale nel modello
  di Passaporta (presenti, punti discussi, decisioni, azioni con chi e quando).
  Gira dopo il motore di Neon, quando la GPU è libera; poi il modello viene scaricato.
- live_summary(): durante la diretta, ogni pochi minuti, "finora si è detto…".
  Gira sulla CPU (num_gpu=0) per non togliere memoria a Whisper sulla GPU da 4 GB.
Tutto via HTTP verso l'Ollama di Windows (127.0.0.1:11434), solo libreria standard."""
import json
import re
import threading
import urllib.error
import urllib.request
from datetime import date

DEFAULT_URL = "http://127.0.0.1:11434"


class Cancelled(Exception):
    pass


def models(url=DEFAULT_URL):
    try:
        with urllib.request.urlopen(url + "/api/tags", timeout=3) as r:
            return [m["name"] for m in json.load(r).get("models", [])]
    except Exception:  # noqa: BLE001
        return None


def chat(messages, model, url=DEFAULT_URL, options=None, fmt=None, stop_ev=None, on_token=None, keep_alive=None):
    """Chiamata in streaming, interrompibile con stop_ev (la diretta ha la precedenza).
    La lettura gira in un thread a parte: un modello sulla CPU può metterci minuti a
    produrre il primo token, e l'interruzione deve essere immediata anche in quel caso."""
    body = {"model": model, "messages": messages, "stream": True, "think": False, "options": options or {}}
    if fmt is not None:
        body["format"] = fmt
    if keep_alive is not None:
        body["keep_alive"] = keep_alive
    req = urllib.request.Request(url + "/api/chat", json.dumps(body).encode(), {"Content-Type": "application/json"})
    out, box, done = [], {}, threading.Event()

    def worker():
        try:
            with urllib.request.urlopen(req, timeout=900) as r:
                box["resp"] = r
                for line in r:
                    if not line.strip():
                        continue
                    ev = json.loads(line)
                    if ev.get("error"):
                        raise RuntimeError(ev["error"])
                    tok = ev.get("message", {}).get("content", "")
                    if tok:
                        out.append(tok)
                        if on_token:
                            on_token(len(out))
                    if ev.get("done") or box.get("cancel"):
                        break
        except urllib.error.HTTPError as e:
            box["err"] = RuntimeError(f"Ollama: {e.read().decode(errors='replace')[:300]}")
        except Exception as e:  # noqa: BLE001
            box["err"] = e
        finally:
            done.set()

    threading.Thread(target=worker, daemon=True).start()
    while not done.wait(0.25):
        if stop_ev is not None and stop_ev.is_set():
            box["cancel"] = True
            try:  # chiudere la connessione fa smettere Ollama di generare
                if box.get("resp"):
                    box["resp"].close()
            except Exception:  # noqa: BLE001
                pass
            raise Cancelled()
    if box.get("err") and not box.get("cancel"):
        raise box["err"]
    return "".join(out)


def unload(model, url=DEFAULT_URL):
    try:
        req = urllib.request.Request(url + "/api/generate", json.dumps({"model": model, "keep_alive": 0}).encode(),
                                     {"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=20).read()
    except Exception:  # noqa: BLE001
        pass


def _ctx_for(text):
    # stima grossolana: ~1,6 token per parola in italiano, più spazio per la risposta
    need = int(len(text.split()) * 1.6) + 2500
    return min(32768, max(4096, 1 << (need - 1).bit_length()))


# ------------------------------------------------------------------ verbale
SCHEMA = {
    "type": "object",
    "properties": {
        "sintesi": {"type": "string"},
        "presenti": {"type": "array", "items": {"type": "string"}},
        "punti": {"type": "array", "items": {"type": "object", "properties": {
            "titolo": {"type": "string"}, "dettagli": {"type": "array", "items": {"type": "string"}}},
            "required": ["titolo", "dettagli"]}},
        "decisioni": {"type": "array", "items": {"type": "string"}},
        "azioni": {"type": "array", "items": {"type": "object", "properties": {
            "chi": {"type": "string"}, "cosa": {"type": "string"}, "entro": {"type": "string"}},
            "required": ["chi", "cosa", "entro"]}},
    },
    "required": ["sintesi", "presenti", "punti", "decisioni", "azioni"],
}

SYSTEM = """Sei un segretario di riunione preciso. Ricevi la trascrizione automatica di una riunione di lavoro in italiano (può contenere errori di riconoscimento: correggili quando il senso è chiaro, non inventare).
Scrivi il verbale in JSON con questi campi:
- sintesi: 2-3 frasi su di cosa si è parlato e con quale esito;
- presenti: le persone che parlano o vengono nominate come presenti (usa i nomi; se un parlante è solo "Speaker N" non includerlo);
- punti: gli argomenti discussi, nell'ordine, ognuno con un titolo breve e 2-5 dettagli concreti (numeri, scelte, motivazioni);
- decisioni: solo cose effettivamente decise o concordate;
- azioni: chi deve fare cosa. "chi" è il nome della persona o del gruppo, "cosa" è un'azione concreta che inizia con un verbo, "entro" è una data AAAA-MM-GG solo se detta o ricavabile in modo certo, altrimenti stringa vuota.
Frasi brevi e concrete, niente commenti sul tono. Non riportare frasi di cortesia."""


def transcript_text(paragraphs, max_words=None):
    lines = []
    for p in paragraphs:
        s = int(p["start"])
        lines.append(f"[{s // 60:02d}:{s % 60:02d}] {p['speaker']}: {p['text']}")
    text = "\n".join(lines)
    if max_words:
        w = text.split()
        if len(w) > max_words:
            text = " ".join(w[:max_words]) + "\n[... trascrizione troncata]"
    return text


def minutes(paragraphs, title="", when=None, project=None, glossary=None, model="qwen3.5:9b", url=DEFAULT_URL,
            stop_ev=None, on_progress=None):
    """Ritorna (dati, markdown)."""
    text = transcript_text(paragraphs, max_words=14000)
    ctx = [f"Titolo: {title}" if title else "", f"Data: {when}" if when else "",
           f"Progetto: {project}" if project else "",
           ("Termini e nomi corretti: " + ", ".join(glossary[:80])) if glossary else ""]
    user = "\n".join(c for c in ctx if c) + "\n\nTRASCRIZIONE:\n" + text
    raw = chat([{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}], model, url,
               options={"temperature": 0.2, "num_ctx": _ctx_for(user)}, fmt=SCHEMA, stop_ev=stop_ev,
               on_token=on_progress, keep_alive=0)
    data = _parse(raw)
    return data, render(data, title=title, when=when)


def _parse(raw):
    try:
        return json.loads(raw)
    except ValueError:
        m = re.search(r"\{.*\}", raw, re.S)
        if m:
            return json.loads(m.group(0))
        raise


def render(d, title="", when=None):
    """Markdown nel modello «Verbale di riunione» di Passaporta."""
    out = [f"**Data:** {when or date.today().strftime('%d/%m/%Y')}",
           f"**Presenti:** {', '.join(d.get('presenti') or []) or '—'}", ""]
    if d.get("sintesi"):
        out += [f"> {d['sintesi']}", ""]
    out += ["## Punti discussi"]
    for pt in d.get("punti") or []:
        out.append(f"### {pt.get('titolo', '').strip()}")
        out += [f"- {x}" for x in pt.get("dettagli") or []]
        out.append("")
    out += ["## Decisioni"] + ([f"- {x}" for x in d.get("decisioni") or []] or ["- —"]) + [""]
    out += ["## Azioni"]
    for a in d.get("azioni") or []:
        entro = a.get("entro") or ""
        out.append(f"- [ ] **{a.get('chi', '?')}** — {a.get('cosa', '')}" + (f" — entro {entro}" if entro else ""))
    if not d.get("azioni"):
        out.append("- —")
    out += ["", "<p class=\"hatnote\">Verbale scritto da un modello locale a partire dalla trascrizione automatica "
            "(Sonorus): da rileggere.</p>"]
    return "\n".join(out) + "\n"


# ------------------------------------------------------------------ riassunto in diretta
LIVE_SYSTEM = """Aggiorni il riassunto di una riunione in corso, per chi si è distratto o è appena entrato.
Ricevi il riassunto finora e le frasi nuove (trascrizione automatica, con errori).
Rispondi solo con il riassunto aggiornato: al massimo 6 punti elenco, ognuno di al massimo 20 parole, in italiano, i più recenti in fondo.
Sintetizza con parole tue (argomenti, numeri, decisioni, chi fa cosa): non copiare le frasi e non scrivere chi le ha dette. Niente introduzioni."""


def live_summary(prev, new_text, model="qwen2.5:3b", url=DEFAULT_URL, stop_ev=None):
    user = f"RIASSUNTO FINORA:\n{prev or '(vuoto)'}\n\nFRASI NUOVE:\n{new_text}"
    out = chat([{"role": "system", "content": LIVE_SYSTEM}, {"role": "user", "content": user}], model, url,
               options={"temperature": 0.2, "num_ctx": 4096, "num_gpu": 0, "num_predict": 300}, stop_ev=stop_ev)
    return out.strip()


class LiveSummarizer(threading.Thread):
    """Ogni `every` secondi, se ci sono abbastanza frasi nuove, aggiorna il riassunto."""

    def __init__(self, get_lines, on_summary, model, url=DEFAULT_URL, every=300, min_words=120):
        super().__init__(daemon=True)
        self.get_lines, self.on_summary = get_lines, on_summary
        self.model, self.url, self.every, self.min_words = model, url, every, min_words
        self.stop_ev = threading.Event()
        self.summary, self.done_upto, self.error = "", 0, None

    def run(self):
        while not self.stop_ev.wait(self.every):
            self.step()

    def step(self, force=False):
        lines = self.get_lines()
        new = lines[self.done_upto:]
        text = "\n".join(f"{ln['name']}: {ln['text']}" for ln in new)
        if not new or (len(text.split()) < self.min_words and not force):
            return
        try:
            self.summary = live_summary(self.summary, text, self.model, self.url, self.stop_ev)
            self.done_upto = len(lines)
            self.error = None
            self.on_summary(self.summary, new[-1]["t"])
        except Cancelled:
            pass
        except Exception as e:  # noqa: BLE001
            self.error = str(e)
