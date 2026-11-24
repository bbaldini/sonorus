"""Collegamento con Passaporta (Gantt + wiki dell'organizzazione, server locale su :8790).

- projects(): i progetti in corso o programmati, per scegliere a quale appartiene la riunione;
- project_terms(): glossario automatico di un progetto (nome, sigla, persone, partner,
  temi dalla scheda, pagina «Glossario» se c'è);
- send(): il verbale diventa una sottopagina di «Verbali» del progetto e le azioni
  diventano task nel Gantt. Rimandarlo aggiorna le stesse pagine e task (niente doppioni).
Solo libreria standard; Passaporta accetta solo richieste locali con l'header X-Passaporta."""
import json
import re
import urllib.request

DEFAULT_URL = "http://127.0.0.1:8790"
H = {"X-Passaporta": "1", "Content-Type": "application/json"}


class Unavailable(Exception):
    pass


def _req(url, path, body=None, timeout=8):
    req = urllib.request.Request(url + path, json.dumps(body).encode() if body is not None else None, H)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)
    except Exception as e:  # noqa: BLE001
        raise Unavailable(f"Passaporta non raggiungibile ({e}). È aperta?") from e


def state(url=DEFAULT_URL):
    s = _req(url, "/api/state")
    if "projects" not in s:
        raise Unavailable("Passaporta è chiusa a chiave: aprila e riprova.")
    return s


def projects(url=DEFAULT_URL, st=None):
    st = st or state(url)
    order = {"attivo": 0, "programmato": 1, "pausa": 2}
    ps = [p for p in st["projects"] if p.get("status", "attivo") in order]
    ps.sort(key=lambda p: (order.get(p.get("status", "attivo"), 9), p.get("name", "").lower()))
    return [{"id": p["id"], "name": p.get("name", ""), "emoji": p.get("emoji", ""), "members": p.get("members", []),
             "status": p.get("status")} for p in ps]


def _split(s):
    return [x.strip(" .") for x in re.split(r"[;\n,]", s or "") if 2 < len(x.strip(" .")) < 60]


def project_terms(pid, url=DEFAULT_URL, st=None):
    """Nomi e termini che Whisper deve conoscere per le riunioni di quel progetto."""
    st = st or state(url)
    p = next((x for x in st["projects"] if x["id"] == pid), None)
    if not p:
        return []
    db = p.get("db") or {}
    terms = [p.get("name"), db.get("acronym"), db.get("full_name"), p.get("pm"), db.get("pi")]
    terms += p.get("members", [])
    terms += _split(db.get("orgs_full")) + _split(db.get("orgs")) + _split(db.get("subarea"))
    terms += re.findall(r"\(([A-Z][\w&-]{1,12})\)", db.get("orgs_full") or "")  # sigle tra parentesi, es. «(CNR)»
    for g in st.get("pages", []):
        if g.get("project") == pid and g.get("title", "").lower() in ("glossario", "glossary"):
            terms += [re.sub(r"^[-*•\s]+|\*\*", "", ln).split(":")[0].strip()
                      for ln in g.get("body", "").splitlines() if ln.strip().startswith(("-", "*", "•"))]
    seen, out = set(), []
    for t in terms:
        t = (t or "").strip()
        if t and t.lower() not in seen:
            seen.add(t.lower())
            out.append(t)
    return out


def _save(url, kind, fields, oid=None):
    body = {"kind": kind, "fields": fields}
    if oid:
        body["id"] = oid
    return _req(url, "/api/save", body)["id"]


def send(pid, title, body_md, actions, url=DEFAULT_URL, ids=None, when_iso=None, source=""):
    """actions: [{chi, cosa, entro, start}]; ids: quelli di un invio precedente (si aggiornano).
    Ritorna i nuovi ids {'page':…, 'verbali':…, 'tasks':[…]}."""
    ids = dict(ids or {})
    st = state(url)
    pages = [g for g in st.get("pages", []) if g.get("project") == pid]
    alive = {g["id"] for g in pages}
    verbali = ids.get("verbali") if ids.get("verbali") in alive else None
    if not verbali:
        verbali = next((g["id"] for g in pages if g.get("title") == "Verbali" and not g.get("parent")), None)
    if not verbali:
        verbali = _save(url, "page", {"project": pid, "parent": "", "title": "Verbali", "emoji": "🗒️",
                                      "order": 3, "feeds": [],
                                      "body": "Le riunioni del progetto: una sottopagina per incontro (modello *Verbale di riunione*).\n"})
    page = ids.get("page") if ids.get("page") in alive else None
    ids["page"] = _save(url, "page", {"project": pid, "parent": verbali, "title": title, "emoji": "🗒️", "order": 0,
                                      "feeds": [], "body": body_md}, page)
    ids["verbali"] = verbali
    tasks_alive = {t["id"] for t in st.get("tasks", [])}
    old = [t for t in ids.get("tasks", []) if t in tasks_alive]
    new_tasks = []
    for k, a in enumerate(actions):
        start = a.get("start") or when_iso or ""
        end = a.get("entro") or start
        f = {"project": pid, "title": a["cosa"][:120], "assignee": a.get("chi", ""), "start": start, "end": end,
             "status": "todo", "progress": 0, "milestone": False, "link": "", "depends": [],
             "notes": (a["cosa"] + "\n\n" if len(a["cosa"]) > 120 else "") +
                      f"Dal verbale «{title}»" + (f" ({source})" if source else "") +
                      ("." if a.get("entro") else ". Data di fine non detta in riunione: da fissare.")}
        new_tasks.append(_save(url, "task", f, old[k] if k < len(old) else None))
    for t in old[len(new_tasks):]:  # azioni tolte rispetto all'invio precedente
        _req(url, "/api/delete", {"kind": "task", "id": t})
    ids["tasks"] = new_tasks
    return ids
