"""Unisce la diretta al verbale rifinito.

Il motore di Neon a volte salta interi tratti (filtro voce troppo severo) e ogni
tanto inventa una frase di cortesia sul silenzio. La diretta invece ha tutto, ma
senza voci separate. Qui:
  1. si tolgono dal verbale i paragrafi che sono solo una frase inventata
     ("Grazie a tutti.") e che la diretta non conferma;
  2. si reinseriscono le frasi della diretta che cadono in un tratto dove il
     verbale non ha niente, segnate come "dalla diretta".
È idempotente: i paragrafi aggiunti in un giro precedente vengono rifatti.
Non dipende da numpy né dalla GPU: si può provare anche da WSL."""
import json
import re
from pathlib import Path

HALL_FULL = re.compile(
    r"^\W*(grazie( a tutti| mille| a voi)?|thank you|sottotitoli.*|musica)\W*$", re.I)
MARK = "dalla diretta"
GAP_MIN = 0.5      # frazione minima della frase della diretta non coperta dal verbale
JOIN_GAP = 4.0     # frasi della stessa sorgente più vicine di così diventano un paragrafo
MIN_WORDS = 3      # "Ok", "Mm-hm", "Perfetto": non vale la pena recuperarli


def _ov(a0, a1, b0, b1):
    return max(0.0, min(a1, b1) - max(a0, b0))


def hms(s):
    s = int(max(0, s))
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"


def merge(session_dir, verbale_dir, me_name=None):
    """Ritorna (aggiunti, tolti). Riscrive trascrizione.json e la sezione
    '## Trascrizione' di trascrizione.md."""
    session_dir, verbale_dir = Path(session_dir), Path(verbale_dir)
    live = json.loads((session_dir / "diretta.json").read_text(encoding="utf-8"))
    jpath, mpath = verbale_dir / "trascrizione.json", verbale_dir / "trascrizione.md"
    doc = json.loads(jpath.read_text(encoding="utf-8"))
    paras = [p for p in doc.get("paragraphs", []) if not p.get("from_live")]
    names = live.get("names", {})
    lines = [ln for ln in live.get("lines", []) if ln.get("text") and not HALL_FULL.match(ln["text"])]

    # 1. frasi inventate nel verbale, non confermate dalla diretta
    removed = []
    keep = []
    for p in paras:
        if HALL_FULL.match(p.get("text", "")):
            confirmed = any(_ov(p["start"] - 1, p["end"] + 1, ln["t"], ln["t"] + ln["dur"]) > 0
                            and "grazie" in ln["text"].lower() for ln in lines)
            if not confirmed:
                removed.append(p)
                continue
        keep.append(p)
    paras = keep

    # 2. frasi della diretta in tratti scoperti
    spans = [(p["start"], p["end"]) for p in paras]
    missing = []
    for ln in lines:
        a, b = ln["t"], ln["t"] + max(ln["dur"], 0.5)
        covered = sum(_ov(a, b, s, e) for s, e in spans)
        if covered < (1 - GAP_MIN) * (b - a):
            missing.append(ln)
    groups = []
    for ln in missing:
        g = groups[-1] if groups else None
        if g and g["src"] == ln["src"] and ln["t"] - g["end"] < JOIN_GAP:
            g["end"] = ln["t"] + ln["dur"]
            g["text"].append(ln["text"])
        else:
            groups.append({"src": ln["src"], "start": ln["t"], "end": ln["t"] + ln["dur"], "text": [ln["text"]]})
    added = []
    for g in groups:
        if len(" ".join(g["text"]).split()) < MIN_WORDS:
            continue
        who = (me_name if g["src"] == "me" and me_name and me_name != "Io" else None) \
            or names.get(g["src"]) or ("Io" if g["src"] == "me" else "Altri")
        added.append({"start": round(g["start"], 2), "end": round(g["end"], 2), "speaker": f"{who} ({MARK})",
                      "clusters": [], "text": " ".join(g["text"]), "words": [], "from_live": True})
    paras = sorted(paras + added, key=lambda p: p["start"])
    doc["paragraphs"] = paras
    doc.setdefault("meta", {})["merged_live"] = {"added": len(added), "removed": len(removed)}
    jpath.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")

    # md: si rifà solo la sezione Trascrizione, il resto (partecipanti ecc.) resta di Neon
    md = mpath.read_text(encoding="utf-8")
    body = "\n\n".join(f"**[{hms(p['start'])}] {p['speaker']}:** " +
                       (f"*{p['text']}*" if p.get("from_live") else p["text"]) for p in paras)
    note = ""
    if added or removed:
        note = (f"> Unito alla diretta: {len(added)} tratti mancanti recuperati (in corsivo, «{MARK}»)"
                + (f", {len(removed)} frasi inventate tolte" if removed else "") + ".\n\n")
    head, sep, _ = md.partition("## Trascrizione")
    if not sep:
        head = md.rstrip() + "\n\n"
    mpath.write_text(head + "## Trascrizione\n\n" + note + body + "\n", encoding="utf-8")
    return added, removed


if __name__ == "__main__":
    import sys
    a, r = merge(sys.argv[1], sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else None)
    for p in a:
        print("+", hms(p["start"]), p["speaker"], p["text"][:100])
    for p in r:
        print("-", hms(p["start"]), p["speaker"], p["text"])
