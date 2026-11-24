"""Segue il muto di Teams tramite la "API per app di terze parti" del nuovo Teams.

È la stessa interfaccia locale che usano Stream Deck e simili: un WebSocket su
127.0.0.1:8124, solo dal proprio PC. Va attivata una volta in Teams:
  Impostazioni → Privacy → «Gestisci API» → «Abilita API».
La prima volta, durante una riunione, Teams chiede se consentire a «Sonorus» di
collegarsi ("abbinamento"); il token che rilascia viene salvato e riusato.
Teams manda un messaggio meetingUpdate a ogni cambiamento: qui si guarda isMuted
(e isInMeeting). Sonorus non manda comandi, legge soltanto.

Protocollo (v2, ricavato dai client open source, non documentato ufficialmente):
  ws://127.0.0.1:8124?token=…&protocol-version=2.0.0&manufacturer=…&device=…&app=…&app-version=…
  ← {"meetingUpdate": {"meetingState": {"isMuted": bool, "isInMeeting": bool, ...},
                       "meetingPermissions": {"canPair": bool, ...}}}
  → {"action": "pair", "parameters": {}, "requestId": n}      (solo se canPair)
  ← {"tokenRefresh": "…"}"""
import json
import threading
import time
import urllib.parse

PORT = 8124


class TeamsWatcher(threading.Thread):
    def __init__(self, get_token, save_token, on_state, app_version="1.1"):
        super().__init__(daemon=True)
        self.get_token, self.save_token, self.on_state = get_token, save_token, on_state
        self.app_version = app_version
        self.enabled = threading.Event()
        self.stop_ev = threading.Event()
        self.status = "spento"
        self.muted = None
        self.in_meeting = None
        self.ws = None
        self._rid = 0
        self._pair_sent = 0.0

    # stati leggibili per l'interfaccia
    def describe(self):
        return {"spento": "Non segue Teams",
                "cerca": "Teams non risponde: attiva l'API in Teams (Impostazioni → Privacy → Gestisci API)",
                "collegato": "Collegato a Teams: entra in una riunione per l'abbinamento",
                "abbinamento": "In Teams consenti a «Sonorus» di collegarsi (richiesta in riunione)",
                "abbinato": "Segue il muto di Teams"}.get(self.status, self.status)

    def set_enabled(self, on):
        if on:
            self.enabled.set()
        else:
            self.enabled.clear()
            self.status = "spento"
            try:
                if self.ws:
                    self.ws.close()
            except Exception:  # noqa: BLE001
                pass

    def _url(self):
        q = {"protocol-version": "2.0.0", "manufacturer": "Sonorus", "device": "PC", "app": "Sonorus",
             "app-version": self.app_version}
        tok = self.get_token()
        if tok:
            q = {"token": tok, **q}
        return f"ws://127.0.0.1:{PORT}?" + urllib.parse.urlencode(q)

    def run(self):
        import websocket  # websocket-client
        while not self.stop_ev.is_set():
            if not self.enabled.wait(1):
                continue
            try:
                self.ws = websocket.create_connection(self._url(), timeout=5)
                self.ws.settimeout(1)
                self.status = "abbinato" if self.get_token() else "collegato"
                while self.enabled.is_set() and not self.stop_ev.is_set():
                    try:
                        msg = self.ws.recv()
                    except websocket.WebSocketTimeoutException:
                        continue
                    if not msg:
                        break
                    self._handle(json.loads(msg))
            except Exception:  # noqa: BLE001  Teams chiuso, API spenta, connessione persa
                if self.enabled.is_set():
                    self.status = "cerca"
                self.muted = self.in_meeting = None
            finally:
                try:
                    if self.ws:
                        self.ws.close()
                except Exception:  # noqa: BLE001
                    pass
                self.ws = None
            self.stop_ev.wait(5)

    def _send(self, action):
        self._rid += 1
        self.ws.send(json.dumps({"action": action, "parameters": {}, "requestId": self._rid}))

    def _handle(self, m):
        if m.get("tokenRefresh"):
            self.save_token(m["tokenRefresh"])
            self.status = "abbinato"
            return
        mu = m.get("meetingUpdate")
        if not mu:
            return
        st, perms = mu.get("meetingState") or {}, mu.get("meetingPermissions") or {}
        if not self.get_token() and perms.get("canPair") and time.time() - self._pair_sent > 30:
            self._send("pair")
            self._pair_sent = time.time()
            self.status = "abbinamento"
        in_meeting = st.get("isInMeeting")
        muted = st.get("isMuted")
        changed = (muted != self.muted) or (in_meeting != self.in_meeting)
        self.muted, self.in_meeting = muted, in_meeting
        if changed and muted is not None:
            self.on_state(bool(muted), bool(in_meeting))
