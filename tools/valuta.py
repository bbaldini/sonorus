"""Statistiche di una sessione Sonorus, senza trascrizione di riferimento.

Confronta la diretta (diretta.json) con il verbale rifinito *prima* dell'unione
e con quello unito, usando anche i turni di pyannote (_lavoro/diarization.json).
Nessun numero qui è un WER vero: senza una trascrizione corretta a mano si misurano
copertura, disaccordo fra i due sistemi, frasi inventate e attribuzione della voce.

    python tools/valuta.py <cartella sessione> [--me SPEAKER_xx] [--prima trascrizione.json.prima-unione]
Stampa Markdown con soli numeri aggregati (niente testo della riunione)."""
import argparse
import json
import re
import sys
import unicodedata
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from merge import HALL_FULL  # noqa: E402

# errori di dominio: file JSON locale [[regex di come viene trascritto, termine giusto, categoria], ...]
# (--termini; non è nella repo perché contiene nomi di persone e partner)
DOMAIN = []


def norm(t):
    t = unicodedata.normalize("NFC", t.lower())
    return re.sub(r"[^\w' ]+", " ", t).split()


def lev(a, b):
    """Distanza di edit fra liste di parole (sostituzioni, cancellazioni, inserimenti)."""
    prev = list(range(len(b) + 1))
    for i, x in enumerate(a, 1):
        cur = [i] + [0] * len(b)
        for j, y in enumerate(b, 1):
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (x != y))
        prev = cur
    return prev[-1]


def union(iv):
    out = []
    for a, b in sorted(iv):
        if out and a <= out[-1][1]:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return out


def inter(x, y):
    i = j = 0
    tot = 0.0
    while i < len(x) and j < len(y):
        tot += max(0.0, min(x[i][1], y[j][1]) - max(x[i][0], y[j][0]))
        if x[i][1] < y[j][1]:
            i += 1
        else:
            j += 1
    return tot


def length(x):
    return sum(b - a for a, b in x)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("sessione")
    ap.add_argument("--me", help="cluster pyannote dell'utente (default: dal nome in speakers.json)")
    ap.add_argument("--prima", default="trascrizione.json.prima-unione")
    ap.add_argument("--termini", help="JSON con [[regex, termine, categoria], ...]")
    ap.add_argument("--anonimo", action="store_true", help="nella tabella dei termini mostra solo la categoria")
    a = ap.parse_args()
    if a.termini:
        DOMAIN.extend(json.loads(Path(a.termini).read_text(encoding="utf-8")))
    S = Path(a.sessione)
    R = S / "registrazione"
    live = json.loads((S / "diretta.json").read_text(encoding="utf-8"))
    pre_p = R / a.prima
    pre = json.loads((pre_p if pre_p.exists() else R / "trascrizione.json").read_text(encoding="utf-8"))
    post = json.loads((R / "trascrizione.json").read_text(encoding="utf-8"))
    diar = json.loads((R / "_lavoro" / "diarization.json").read_text(encoding="utf-8"))["turns"]
    spk = json.loads((R / "speakers.json").read_text(encoding="utf-8"))["speakers"]
    me_name = live.get("names", {}).get("me")
    me = a.me or next((c for c, v in spk.items() if v["name"] == me_name), None)
    dur = pre["meta"]["duration_s"]

    L = live["lines"]
    P = [p for p in pre["paragraphs"]]
    words_live = sum(len(norm(x["text"])) for x in L)
    words_live_me = sum(len(norm(x["text"])) for x in L if x["src"] == "me")
    words_pre = sum(len(norm(p["text"])) for p in P)
    added = [p for p in post["paragraphs"] if p.get("from_live")]
    words_added = sum(len(norm(p["text"])) for p in added)

    # parlato secondo pyannote e copertura dei due sistemi
    speech = union([[t["start"], t["end"]] for t in diar])
    cov_live = union([[x["t"], x["t"] + x["dur"]] for x in L])
    cov_pre = union([[w["start"], w["end"]] for p in P for w in p.get("words", [])] or
                    [[p["start"], p["end"]] for p in P])
    cov_pre_pad = union([[s - 0.5, e + 0.5] for s, e in cov_pre])
    speech_s = length(speech)
    gap_s = speech_s - inter(speech, cov_pre_pad)
    live_only = length(cov_live) - inter(cov_live, cov_pre_pad)

    # disaccordo fra i due sistemi (testo intero in ordine di tempo)
    tl = [w for x in sorted(L, key=lambda e: e["t"]) for w in norm(x["text"])]
    tp = [w for p in P for w in norm(p["text"])]
    d = lev(tl, tp)

    # frasi inventate
    hall_live = [x for x in L if HALL_FULL.match(x["text"])]
    hall_pre = [p for p in P if HALL_FULL.match(p["text"])]
    removed = post.get("meta", {}).get("merged_live", {}).get("removed", 0)

    # la tua voce: canale microfono contro il cluster pyannote dell'utente
    io = union([[x["t"], x["t"] + x["dur"]] for x in L if x["src"] == "me" and not HALL_FULL.match(x["text"])])
    mine = union([[t["start"], t["end"]] for t in diar if t["speaker"] == me]) if me else []
    io_in_mine = inter(io, union([[s - 1, e + 1] for s, e in mine])) if mine else 0.0
    io_lines = [x for x in L if x["src"] == "me"]
    stray = [x for x in io_lines if not HALL_FULL.match(x["text"]) and
             inter([[x["t"], x["t"] + x["dur"]]], union([[s - 1, e + 1] for s, e in mine])) < 0.3 * x["dur"]]

    # parlato per persona (anonimo, in ordine di tempo)
    per = {}
    for t in diar:
        per[t["speaker"]] = per.get(t["speaker"], 0) + t["end"] - t["start"]
    shares = sorted(per.values(), reverse=True)

    # errori di dominio
    txt_live = " ".join(x["text"] for x in L).lower()
    txt_pre = " ".join(p["text"] for p in P).lower()
    dom = [(cat if a.anonimo else right, len(re.findall(rx, txt_live)), len(re.findall(rx, txt_pre)))
           for rx, right, cat in DOMAIN]

    f = lambda s: f"{int(s // 60)}:{int(s % 60):02d}"  # noqa: E731
    pct = lambda x, y: f"{100 * x / y:.1f}%" if y else "—"  # noqa: E731
    print(f"""| Misura | Valore |
|---|---|
| Durata | {f(dur)} min |
| Parlato secondo pyannote | {f(speech_s)} min ({pct(speech_s, dur)}) |
| Voci distinte (pyannote) | {len(per)}; quota di parlato: {", ".join(pct(s, speech_s) for s in shares)} |
| Parole: diretta / rifinita | {words_live} / {words_pre} (di cui dal microfono: {words_live_me}) |
| Disaccordo parola per parola diretta ↔ rifinita | {pct(d, len(tp))} ({d} modifiche) |
| Parlato **senza testo** nella rifinita | {f(gap_s)} min ({pct(gap_s, speech_s)} del parlato) |
| Tratti che solo la diretta ha trascritto | {f(live_only)} min |
| Recuperati dall'unione | {len(added)} tratti, {words_added} parole (+{pct(words_added, words_pre)} sul verbale) |
| Frasi inventate: diretta / rifinita | {len(hall_live)} / {len(hall_pre)} (tolte dall'unione: {removed}) |
| Frasi dal microfono | {len(io_lines)}; fuori dal turno dell'utente secondo pyannote: {len(stray)} |
| Tempo del microfono che coincide con la voce dell'utente | {pct(io_in_mine, length(io))} |
| Voce dell'utente (pyannote) coperta dal canale microfono | {pct(inter(mine, union([[s - 1, e + 1] for s, e in io])), length(mine))} |
""")
    if not DOMAIN:
        return
    print("| Termine di dominio | Errori nella diretta | Errori nella rifinita |\n|---|---|---|")
    for right, nl, npre in dom:
        if nl or npre:
            print(f"| {right} | {nl} | {npre} |")
    print(f"| **Totale** | {sum(x[1] for x in dom)} | {sum(x[2] for x in dom)} |")


def riferimenti(cartelle):
    """WER e cpWER sui tratti corretti a mano (pulsante «Valuta» in Archivio), sommati
    su una o più riunioni: python tools/valuta.py --riferimenti <sessione> [<sessione> ...]"""
    import evalkit
    res = []
    for c in cartelle:
        res += [r.get("risultati") for r in evalkit.references(Path(c) / "registrazione")]
    tot = evalkit.aggregate(res)
    if not tot:
        print("Nessun tratto corretto a mano in queste riunioni.")
        return
    any_ = next(iter(tot.values()))
    print(f"{any_['finestre']} tratti, {any_['parole_rif']} parole di riferimento\n")
    print("| Versione | WER | cpWER |\n|---|---|---|")
    for k, v in tot.items():
        f = lambda x: f"{100 * x:.1f}%" if x is not None else "—"  # noqa: E731
        print(f"| {k} | {f(v['wer'])} | {f(v['cpwer'])}{' (tu / altri)' if k == 'diretta' else ''} |")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--riferimenti":
        riferimenti(sys.argv[2:])
    else:
        main()
