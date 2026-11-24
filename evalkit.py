"""Valutazione vera: si corregge a mano qualche minuto di riunione e si misurano
WER e cpWER delle varie versioni della trascrizione.

Versioni confrontate:
  diretta      le frasi trascritte durante la riunione (due canali: tu / gli altri)
  rifinita     il verbale del motore di Neon così com'è
  unione       rifinita + tratti mancanti presi dalla diretta (merge_paragraphs)
  unione fine  unione + correzioni parola per parola (fuse_paragraphs)

WER = (sostituzioni + cancellazioni + inserimenti) / parole del riferimento, a testo
intero in ordine di tempo. cpWER ("concatenated minimum-permutation WER", CHiME-6):
si concatenano le parole di ogni parlante, si abbinano i parlanti del riferimento a
quelli della trascrizione nel modo più favorevole e si conta come sopra: misura
insieme parole e attribuzione delle voci. Per la diretta i "parlanti" sono solo tu
e gli altri, quindi il suo cpWER non è confrontabile con gli altri: si riporta a parte.

Il riferimento è testo semplice, una battuta per riga: «Nome: testo».
Si salva in <verbale>/riferimenti/<inizio>-<fine>.json."""
import base64
import io
import itertools
import json
import time
import wave
from pathlib import Path

import merge

SYSTEMS = ("diretta", "rifinita", "unione", "unione fine")


def _words_of(paragraphs):
    out = []
    for p in paragraphs:
        ws = p.get("words") or []
        if not ws:  # paragrafo senza tempi delle parole: interpolati
            toks = p["text"].split()
            step = max(p["end"] - p["start"], 0.3) / max(1, len(toks))
            ws = [{"start": p["start"] + i * step, "end": p["start"] + (i + 1) * step, "text": " " + t}
                  for i, t in enumerate(toks)]
        out += [{"start": w["start"], "end": w["end"], "text": w["text"], "speaker": p["speaker"]}
                for w in ws if merge.norm_tok(w["text"])]
    return out


def systems(session_dir, verbale_dir, me_name=None, glossary=None):
    session_dir, verbale_dir = Path(session_dir), Path(verbale_dir)
    live = json.loads((session_dir / "diretta.json").read_text(encoding="utf-8"))
    names = live.get("names", {})
    dw = []
    for ln in live.get("lines", []):
        who = me_name if ln["src"] == "me" and me_name and me_name != "Io" else names.get(ln["src"], ln.get("name"))
        dw += [{"start": w["start"], "end": w["end"], "text": w["text"], "speaker": who}
               for w in merge.live_words(ln)]
    neon = merge.pristine(verbale_dir)
    try:
        sp = json.loads((verbale_dir / "speakers.json").read_text(encoding="utf-8")).get("speakers", {})
    except Exception:  # noqa: BLE001
        sp = {}
    merge.apply_names(neon.get("paragraphs", []), sp)
    paras = neon.get("paragraphs", [])
    un, _, _ = merge.merge_paragraphs(live, paras, me_name)
    fine, _ = merge.fuse_paragraphs(un, live, glossary)
    return {"diretta": sorted(dw, key=lambda w: w["start"]), "rifinita": _words_of(paras),
            "unione": _words_of(un), "unione fine": _words_of(fine)}


def window(words, a, b):
    return [w for w in words if a <= (w["start"] + w["end"]) / 2 < b]


def prefill(words):
    """Testo da correggere: battute per parlante, dall'unione fine."""
    lines, cur, buf = [], None, []
    for w in words:
        if w["speaker"] != cur and buf:
            lines.append(f"{cur}: {merge.join_words(buf)}")
            buf = []
        cur = w["speaker"]
        buf.append(w)
    if buf:
        lines.append(f"{cur}: {merge.join_words(buf)}")
    return "\n".join(lines)


def parse_reference(text):
    turns = []
    for raw in text.splitlines():
        ln = raw.strip()
        if not ln:
            continue
        head, sep, rest = ln.partition(":")
        if sep and 0 < len(head) <= 40 and not head.strip().isdigit():
            turns.append([head.strip(), rest.strip()])
        elif turns:
            turns[-1][1] += " " + ln
        else:
            turns.append(["?", ln])
    return [{"speaker": s, "text": t} for s, t in turns]


def _lev(a, b):
    prev = list(range(len(b) + 1))
    for i, x in enumerate(a, 1):
        cur = [i] + [0] * len(b)
        for j, y in enumerate(b, 1):
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (x != y))
        prev = cur
    return prev[-1]


def wer(ref_tokens, hyp_tokens):
    return _lev(ref_tokens, hyp_tokens), len(ref_tokens)


def cpwer(ref_by, hyp_by):
    """ref_by / hyp_by: {parlante: [token]}. Ritorna (errori, parole del riferimento, abbinamento)."""
    R, Hn = list(ref_by), list(hyp_by)
    n_ref = sum(len(v) for v in ref_by.values())
    cost = {(r, h): _lev(ref_by[r], hyp_by[h]) for r in R for h in Hn}
    best, best_map = None, {}
    # si abbinano i parlanti; quelli in più dall'una o dall'altra parte contano per intero
    k = min(len(R), len(Hn))
    if len(R) <= len(Hn):
        combos = ((dict(zip(R, perm))) for perm in itertools.permutations(Hn, k)) if len(Hn) <= 8 else None
    else:
        combos = ((dict(zip(perm, Hn))) for perm in itertools.permutations(R, k)) if len(R) <= 8 else None
    if combos is None:  # troppi parlanti: abbinamento avido
        m, used = {}, set()
        for (r, h), c in sorted(cost.items(), key=lambda kv: kv[1] - len(ref_by[kv[0][0]])):
            if r not in m and h not in used:
                m[r] = h
                used.add(h)
        combos = [m]
    for m in combos:
        used = set(m.values())
        e = sum(cost[(r, h)] for r, h in m.items())
        e += sum(len(ref_by[r]) for r in R if r not in m)
        e += sum(len(hyp_by[h]) for h in Hn if h not in used)
        if best is None or e < best:
            best, best_map = e, m
    return best or 0, n_ref, best_map


def _by_speaker(items, key_text):
    texts = {}
    for it in items:
        texts.setdefault(it["speaker"], []).append(it[key_text] if key_text == "text" and it[key_text][:1] in " '-"
                                                   else " " + it[key_text])
    return {k: merge.norm_words("".join(v)) for k, v in texts.items()}


def evaluate(session_dir, verbale_dir, a, b, ref_text, me_name=None, glossary=None):
    ref = parse_reference(ref_text)
    rtok = [t for r in ref for t in merge.norm_words(r["text"])]
    rby = _by_speaker(ref, "text")
    res = {}
    for name, words in systems(session_dir, verbale_dir, me_name, glossary).items():
        w = window(words, a, b)
        htok = merge.norm_words(merge.join_words(w))
        e, n = wer(rtok, htok)
        ce, cn, m = cpwer(rby, _by_speaker(w, "text"))
        res[name] = {"wer": e / n if n else None, "errori": e, "parole_rif": n, "parole_ipotesi": len(htok),
                     "cpwer": ce / cn if cn else None, "abbinamento": m}
    return res


def save_reference(verbale_dir, a, b, ref_text, results=None):
    d = Path(verbale_dir) / "riferimenti"
    d.mkdir(exist_ok=True)
    p = d / f"{int(a):05d}-{int(b):05d}.json"
    p.write_text(json.dumps({"a": a, "b": b, "testo": ref_text, "salvato": time.strftime("%Y-%m-%d %H:%M"),
                             "risultati": results}, ensure_ascii=False, indent=1), encoding="utf-8")
    return str(p)


def references(verbale_dir):
    d = Path(verbale_dir) / "riferimenti"
    return [json.loads(p.read_text(encoding="utf-8")) for p in sorted(d.glob("*.json"))] if d.exists() else []


def aggregate(results_list):
    """Somma errori e parole su più finestre (anche di riunioni diverse)."""
    tot = {}
    for res in results_list:
        for name, r in (res or {}).items():
            t = tot.setdefault(name, {"errori": 0, "parole_rif": 0, "cp_err": 0.0, "finestre": 0})
            t["errori"] += r["errori"]
            t["parole_rif"] += r["parole_rif"]
            t["cp_err"] += (r["cpwer"] or 0) * r["parole_rif"]
            t["finestre"] += 1
    return {k: {"wer": v["errori"] / v["parole_rif"] if v["parole_rif"] else None,
                "cpwer": v["cp_err"] / v["parole_rif"] if v["parole_rif"] else None,
                "parole_rif": v["parole_rif"], "finestre": v["finestre"]} for k, v in tot.items()}


def audio_clip(wav_path, a, b):
    """Il tratto [a, b) della registrazione come data URI, per ascoltarlo mentre si corregge."""
    with wave.open(str(wav_path)) as w:
        sr = w.getframerate()
        w.setpos(min(w.getnframes(), int(a * sr)))
        frames = w.readframes(int((b - a) * sr))
        ch, sw = w.getnchannels(), w.getsampwidth()
    buf = io.BytesIO()
    with wave.open(buf, "wb") as o:
        o.setnchannels(ch)
        o.setsampwidth(sw)
        o.setframerate(sr)
        o.writeframes(frames)
    return "data:audio/wav;base64," + base64.b64encode(buf.getvalue()).decode()
