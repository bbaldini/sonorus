"""Unisce la diretta al verbale rifinito.

La diretta (Whisper a blocchi di <=20 s, due canali) e il verbale rifinito (motore di
Neon: pyannote + Whisper sull'audio intero) sbagliano in modi diversi: la prima taglia
le frasi ma non perde pezzi, il secondo è pulito ma a volte salta interi tratti e
inventa frasi di cortesia sul silenzio. Qui si usano l'uno per correggere l'altro:

1. merge_paragraphs: per tratti.
   - toglie dal verbale i paragrafi che sono solo una frase inventata ("Grazie a tutti.")
     e che la diretta non conferma;
   - reinserisce le frasi della diretta che cadono dove il verbale non ha niente.
2. fuse_paragraphs: parola per parola, dentro i paragrafi del verbale.
   Allinea le parole dei due sistemi (stesso intervallo di tempo, distanza di edit) e,
   dove non sono d'accordo, sceglie:
   - il termine del glossario, se una sola delle due versioni lo contiene;
   - altrimenti la versione con probabilità media nettamente più alta
     (solo se la diretta ha le probabilità per parola, salvate dal 24/11/2026);
   - parole che solo la diretta ha, in mezzo a un paragrafo, se molto sicure.

merge() lavora sempre a partire dalla copia intatta del verbale di Neon
(trascrizione.neon.json), quindi rifarlo dà lo stesso risultato.
Non dipende da numpy né dalla GPU: si prova anche da WSL."""
import json
import re
import shutil
import unicodedata
from pathlib import Path

HALL_FULL = re.compile(
    r"^\W*(grazie( a tutti| mille| a voi)?|thank you|sottotitoli.*|musica)\W*$", re.I)
MARK = "dalla diretta"
GAP_MIN = 0.5      # frazione minima della frase della diretta non coperta dal verbale
JOIN_GAP = 4.0     # frasi della stessa sorgente più vicine di così diventano un paragrafo
MIN_WORDS = 3      # "Ok", "Mm-hm", "Perfetto": non vale la pena recuperarli
P_MARGIN = 0.15    # di quanto la diretta deve essere più sicura per vincere
P_MIN = 0.6
INS_P_MIN = 0.8    # parole presenti solo nella diretta: soglia più alta
INS_MIN = 2
EDGE = 0.3         # tolleranza (s) per prendere le parole della diretta che cadono in un paragrafo


def _ov(a0, a1, b0, b1):
    return max(0.0, min(a1, b1) - max(a0, b0))


def hms(s):
    s = int(max(0, s))
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"


def norm_tok(w):
    w = unicodedata.normalize("NFC", w.lower())
    return re.sub(r"[^\w']+", "", w)


def norm_words(t):
    return [x for x in (norm_tok(w) for w in t.split()) if x]


# ------------------------------------------------------------------ parole della diretta
def live_words(line):
    """Parole di una frase della diretta con tempo e probabilità. Le sessioni prima del
    24/11/2026 non le hanno: tempi interpolati, probabilità sconosciuta (None)."""
    if line.get("words"):
        # testo grezzo di Whisper: lo spazio iniziale dice se è una parola nuova o un pezzo
        # attaccato alla precedente ("l" + "'eventuale")
        return [{"start": w["s"], "end": w["e"], "text": w["w"] if w["w"][:1] in " '-" else " " + w["w"],
                 "p": w.get("p")} for w in line["words"] if norm_tok(w["w"])]
    ws = line["text"].split()
    if not ws:
        return []
    step = max(line["dur"], 0.3) / len(ws)
    return [{"start": round(line["t"] + i * step, 2), "end": round(line["t"] + (i + 1) * step, 2), "text": " " + w, "p": None}
            for i, w in enumerate(ws) if norm_tok(w)]


# ------------------------------------------------------------------ 1. per tratti
def merge_paragraphs(live, paragraphs, me_name=None):
    """Ritorna (paragrafi, aggiunti, tolti)."""
    names = live.get("names", {})
    lines = [ln for ln in live.get("lines", []) if ln.get("text") and not HALL_FULL.match(ln["text"])]
    removed, keep = [], []
    for p in paragraphs:
        if HALL_FULL.match(p.get("text", "")):
            confirmed = any(_ov(p["start"] - 1, p["end"] + 1, ln["t"], ln["t"] + ln["dur"]) > 0
                            and "grazie" in ln["text"].lower() for ln in lines)
            if not confirmed:
                removed.append(p)
                continue
        keep.append(p)
    spans = [(p["start"], p["end"]) for p in keep]
    missing = []
    for ln in lines:
        a, b = ln["t"], ln["t"] + max(ln["dur"], 0.5)
        if sum(_ov(a, b, s, e) for s, e in spans) < (1 - GAP_MIN) * (b - a):
            missing.append(ln)
    groups = []
    for ln in missing:
        g = groups[-1] if groups else None
        if g and g["src"] == ln["src"] and ln["t"] - g["end"] < JOIN_GAP:
            g["end"] = ln["t"] + ln["dur"]
            g["lines"].append(ln)
        else:
            groups.append({"src": ln["src"], "start": ln["t"], "end": ln["t"] + ln["dur"], "lines": [ln]})
    added = []
    for g in groups:
        text = " ".join(ln["text"] for ln in g["lines"])
        if len(text.split()) < MIN_WORDS:
            continue
        who = (me_name if g["src"] == "me" and me_name and me_name != "Io" else None) \
            or names.get(g["src"]) or ("Io" if g["src"] == "me" else "Altri")
        added.append({"start": round(g["start"], 2), "end": round(g["end"], 2), "speaker": f"{who} ({MARK})",
                      "clusters": [], "text": text, "from_live": True,
                      "words": [w for ln in g["lines"] for w in live_words(ln)]})
    return sorted(keep + added, key=lambda p: p["start"]), added, removed


# ------------------------------------------------------------------ 2. parola per parola
def _align(a, b):
    """Allineamento di edit fra due liste di token. Ritorna blocchi
    (i0, i1, j0, j1) dove a[i0:i1] e b[j0:j1] non coincidono."""
    n, m = len(a), len(b)
    D = [[0] * (m + 1) for _ in range(n + 1)]
    for i in range(n + 1):
        D[i][0] = i
    for j in range(m + 1):
        D[0][j] = j
    for i in range(1, n + 1):
        ai, row, prev = a[i - 1], D[i], D[i - 1]
        for j in range(1, m + 1):
            row[j] = min(prev[j] + 1, row[j - 1] + 1, prev[j - 1] + (ai != b[j - 1]))
    i, j, ops = n, m, []
    while i > 0 or j > 0:
        if i > 0 and j > 0 and D[i][j] == D[i - 1][j - 1] + (a[i - 1] != b[j - 1]):
            ops.append(("=" if a[i - 1] == b[j - 1] else "~", i - 1, j - 1))
            i, j = i - 1, j - 1
        elif i > 0 and D[i][j] == D[i - 1][j] + 1:
            ops.append(("-", i - 1, j))
            i -= 1
        else:
            ops.append(("+", i, j - 1))
            j -= 1
    ops.reverse()
    blocks, grp = [], []
    for op in ops + [("=", 0, 0)]:
        if op[0] != "=":
            grp.append(op)
            continue
        if grp:
            ii = [i for o, i, j in grp if o in "~-"]
            jj = [j for o, i, j in grp if o in "~+"]
            i0 = min(ii) if ii else grp[0][1]
            j0 = min(jj) if jj else grp[0][2]
            blocks.append((i0, max(ii) + 1 if ii else i0, j0, max(jj) + 1 if jj else j0))
            grp = []
    return blocks


def join_words(ws):
    return re.sub(r"\s+", " ", "".join(w["text"] for w in ws)).strip()


def apply_names(paragraphs, speakers):
    """Nomi corretti a mano (speakers.json): per cluster, o per nome automatico."""
    auto = {v.get("auto"): v["name"] for v in speakers.values() if v.get("auto")}
    for p in paragraphs:
        cl = p.get("clusters", [])
        if len(cl) == 1 and cl[0] in speakers:
            p["speaker"] = speakers[cl[0]]["name"]
        elif p.get("speaker") in auto:
            p["speaker"] = auto[p["speaker"]]
    return paragraphs


def _glossary(glossary):
    terms = []
    for g in glossary or []:
        toks = tuple(norm_words(g))
        if toks:
            terms.append(toks)
    return terms


def _has_term(toks, terms):
    toks = tuple(toks)
    for t in terms:
        n = len(t)
        for k in range(len(toks) - n + 1):
            if toks[k:k + n] == t:
                return t
    return None


def _mean_p(ws):
    ps = [w["p"] for w in ws if w.get("p") is not None]
    return sum(ps) / len(ps) if ps else None


def fuse_paragraphs(paragraphs, live, glossary=None):
    """Ritorna (paragrafi, correzioni). I paragrafi corretti tengono il testo
    originale in 'text_neon'; ogni correzione dice dove, cosa e perché."""
    terms = _glossary(glossary)
    K = max([len(t) for t in terms] + [2]) - 1
    lw = sorted((w for ln in live.get("lines", []) if not HALL_FULL.match(ln.get("text", ""))
                 for w in live_words(ln)), key=lambda w: w["start"])
    changes, out = [], []
    for p in paragraphs:
        nw = [w for w in p.get("words", []) if norm_tok(w["text"])]
        if p.get("from_live") or not nw:
            out.append(p)
            continue
        cand = [w for w in lw if p["start"] - EDGE <= (w["start"] + w["end"]) / 2 <= p["end"] + EDGE]
        if not cand:
            out.append(p)
            continue
        a = [norm_tok(w["text"]) for w in nw]
        b = [norm_tok(w["text"]) for w in cand]
        new_words, last, local = [], 0, []
        for i0, i1, j0, j1 in _align(a, b):
            nb, lb = nw[i0:i1], cand[j0:j1]
            ta, tb = a[i0:i1], b[j0:j1]
            # contesto per i termini a cavallo del blocco ("biossi di litano" -> due blocchi)
            ctx_a = a[max(0, i0 - K):i1 + K]
            ctx_b = b[max(0, j0 - K):j1 + K]
            reason = None
            # ai bordi del paragrafo le parole della diretta possono essere della battuta
            # vicina: lì si accettano solo sostituzioni della stessa lunghezza
            edge = i0 == 0 or i1 >= len(a)
            if edge and len(lb) != len(nb):
                pass
            elif lb and not nb:
                # parole che ha solo la diretta: solo in mezzo al paragrafo e molto sicure
                inside = 0 < i0 < len(a)
                pm = _mean_p(lb)
                if inside and len(lb) >= INS_MIN and pm is not None and pm >= INS_P_MIN:
                    reason = f"solo nella diretta (p={pm:.2f})"
                elif inside and _has_term(ctx_b, terms) and not _has_term(ctx_a, terms):
                    reason = f"glossario: {' '.join(_has_term(ctx_b, terms))}"
            elif lb and nb:
                gt_b, gt_a = _has_term(ctx_b, terms), _has_term(ctx_a, terms)
                # il termine deve cadere (almeno in parte) dentro il blocco, non solo nel contesto
                if gt_b and not _has_term(tb + b[j1:j1 + K], terms) and not _has_term(b[max(0, j0 - K):j0] + tb, terms):
                    gt_b = None
                if gt_b and not gt_a:
                    reason = f"glossario: {' '.join(gt_b)}"
                elif not (gt_a and not gt_b):
                    pa, pb = _mean_p(nb), _mean_p(lb)
                    if pa is not None and pb is not None and pb >= P_MIN and pb > pa + P_MARGIN \
                            and abs(len(lb) - len(nb)) <= 2:
                        reason = f"più sicura nella diretta (p={pb:.2f} contro {pa:.2f})"
            new_words += nw[last:i0]
            if reason:
                new_words += [{**w, "from_live": True} for w in lb]
                local.append({"t": round((nb or lb)[0]["start"], 2), "old": " ".join(w["text"].strip() for w in nb),
                              "new": " ".join(w["text"].strip() for w in lb), "reason": reason,
                              "speaker": p.get("speaker")})
            else:
                new_words += nb
            last = i1
        new_words += nw[last:]
        if local:
            q = dict(p)
            q["text_neon"] = p["text"]
            q["words"] = new_words
            q["text"] = join_words(new_words)
            out.append(q)
            changes += local
        else:
            out.append(p)
    return out, changes


# ------------------------------------------------------------------ file
def pristine(verbale_dir):
    """Copia intatta del verbale di Neon. Se trascrizione.json è appena uscito da
    Neon (nessun segno di unione), la copia viene rinnovata."""
    verbale_dir = Path(verbale_dir)
    jpath, npath = verbale_dir / "trascrizione.json", verbale_dir / "trascrizione.neon.json"
    doc = json.loads(jpath.read_text(encoding="utf-8"))
    fresh = "merged_live" not in doc.get("meta", {}) and not any(
        p.get("from_live") or "text_neon" in p for p in doc.get("paragraphs", []))
    if fresh or not npath.exists():
        old = verbale_dir / "trascrizione.json.prima-unione"  # copia fatta a mano il 24/11/2026
        if not fresh and old.exists():
            doc = json.loads(old.read_text(encoding="utf-8"))
        elif not fresh:  # unito da una versione vecchia di merge, senza copia: meglio di niente
            doc["paragraphs"] = [p for p in doc["paragraphs"] if not p.get("from_live")]
        npath.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
    return json.loads(npath.read_text(encoding="utf-8"))


def merge(session_dir, verbale_dir, me_name=None, glossary=None, fine=True):
    """Riscrive trascrizione.json e la sezione '## Trascrizione' di trascrizione.md.
    Ritorna {'added': [...], 'removed': [...], 'changes': [...]}."""
    session_dir, verbale_dir = Path(session_dir), Path(verbale_dir)
    live = json.loads((session_dir / "diretta.json").read_text(encoding="utf-8"))
    doc = pristine(verbale_dir)
    # i nomi corretti a mano stanno in speakers.json, che Neon applica rigenerando:
    # la copia intatta può essere più vecchia, quindi si riprendono da lì
    sp = {}
    try:
        sp = json.loads((verbale_dir / "speakers.json").read_text(encoding="utf-8")).get("speakers", {})
    except Exception:  # noqa: BLE001
        pass
    apply_names(doc.get("paragraphs", []), sp)
    paras, added, removed = merge_paragraphs(live, doc.get("paragraphs", []), me_name)
    changes = []
    if fine:
        paras, changes = fuse_paragraphs(paras, live, glossary)
    doc["paragraphs"] = paras
    doc.setdefault("meta", {})["merged_live"] = {"added": len(added), "removed": len(removed),
                                                 "fused": len(changes), "fuse": changes}
    (verbale_dir / "trascrizione.json").write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")

    mpath = verbale_dir / "trascrizione.md"
    md = mpath.read_text(encoding="utf-8")
    body = "\n\n".join(f"**[{hms(p['start'])}] {p['speaker']}:** " +
                       (f"*{p['text']}*" if p.get("from_live") else p["text"]) for p in paras)
    note = []
    if added:
        note.append(f"{len(added)} tratti mancanti recuperati (in corsivo, «{MARK}»)")
    if removed:
        note.append(f"{len(removed)} frasi inventate tolte")
    if changes:
        note.append(f"{len(changes)} correzioni parola per parola (elenco in fondo)")
    head, sep, _ = md.partition("## Trascrizione")
    if not sep:
        head = md.rstrip() + "\n\n"
    out = head + "## Trascrizione\n\n" + (("> Unito alla diretta: " + "; ".join(note) + ".\n\n") if note else "") + body + "\n"
    if changes:
        out += "\n## Correzioni dalla diretta\n\n| Quando | Verbale | Diretta | Perché |\n|---|---|---|---|\n" + "".join(
            f"| {hms(c['t'])} | {c['old'] or '—'} | {c['new']} | {c['reason']} |\n" for c in changes)
    mpath.write_text(out, encoding="utf-8")
    return {"added": added, "removed": removed, "changes": changes}


if __name__ == "__main__":
    import sys
    gl = []
    if len(sys.argv) > 4:
        gl = [x.strip() for x in Path(sys.argv[4]).read_text(encoding="utf-8").splitlines() if x.strip()]
    r = merge(sys.argv[1], sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else None, gl)
    for p in r["added"]:
        print("+", hms(p["start"]), p["speaker"], p["text"][:100])
    for p in r["removed"]:
        print("-", hms(p["start"]), p["speaker"], p["text"])
    for c in r["changes"]:
        print("~", hms(c["t"]), repr(c["old"]), "->", repr(c["new"]), c["reason"])
