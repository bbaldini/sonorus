# Prima valutazione su una riunione reale

**Riunione:** call di progetto su Teams del 24/11/2026.
- 23 minuti, 6 partecipanti, in italiano, con un lessico tecnico (computer vision, sensori).
- L'utente era in cuffia e spesso in muto su Teams. La versione usata non aveva ancora la pausa del microfono, quindi il canale "Io" registrava comunque.

**Hardware:** laptop con NVIDIA RTX A2000 Laptop (4 GB).
- Diretta: faster-whisper `large-v3-turbo` int8_float16.
- Verbale rifinito: motore di Neon Minutes, cioè pyannote `speaker-diarization-community-1` più `large-v3-turbo`.
- La rifinitura ha impiegato 211 s per 1.410 s di audio, cioè 0,15× il tempo reale. Di questi, 124 s per separare le voci e 86 s per trascrivere.

> **Non c'è una trascrizione di riferimento corretta a mano**, quindi nessuno di questi numeri è un WER. Si misurano invece:
> - la copertura, contro i turni di parlato di pyannote;
> - il disaccordo fra i due sistemi;
> - le frasi inventate;
> - quanto il canale del microfono individua la voce dell'utente.
>
> Il testo della riunione non è pubblicato. Lo script è `tools/valuta.py` (la lista dei termini resta locale) e si può rifare su qualsiasi sessione.

| Misura | Valore |
|---|---|
| Durata | 23:30 min |
| Parlato secondo pyannote | 21:08 min (89.9%) |
| Voci distinte (pyannote) | 6; quota di parlato: 58.4%, 14.8%, 13.5%, 5.0%, 4.7%, 3.6% |
| Parole: diretta / rifinita | 2918 / 2778 (di cui dal microfono: 180) |
| Disaccordo parola per parola diretta ↔ rifinita | 16.1% (448 modifiche) |
| Parlato **senza testo** nella rifinita | 1:08 min (5.4% del parlato) |
| Tratti che solo la diretta ha trascritto | 1:22 min |
| Recuperati dall'unione | 3 tratti, 171 parole (+6.2% sul verbale) |
| Frasi inventate: diretta / rifinita | 4 / 2 (tolte dall'unione: 1) |
| Frasi dal microfono | 26; fuori dal turno dell'utente secondo pyannote: 11 |
| Tempo del microfono che coincide con la voce dell'utente | 72.3% |
| Voce dell'utente (pyannote) coperta dal canale microfono | 99.2% |

| Termine di dominio | Errori nella diretta | Errori nella rifinita |
|---|---|---|
| sigla di sensore | 3 | 3 |
| termine ML (inglese) | 2 | 1 |
| termine di processo | 1 | 1 |
| termine ML | 1 | 1 |
| termine ML | 1 | 1 |
| nome di persona | 1 | 0 |
| nome di persona | 1 | 1 |
| termine ML | 1 | 0 |
| termine chimico | 0 | 1 |
| nome di azienda | 1 | 1 |
| **Totale** | 12 | 10 |

## Cosa dicono i numeri

- **La rifinita è più pulita ma perde parlato.** Su circa 21 minuti di voce, 1:08 min non hanno testo nel verbale rifinito, il 5,4%. Due di questi tratti contenevano contenuto importante: il cambio di piano del progetto e una proposta di metodo. La diretta li aveva, e l'unione (`merge.py`) li ha riportati nel verbale: +6,2% di parole.
- **I due sistemi sono in disaccordo sul 16% delle parole.** Pesano soprattutto i tagli della diretta ogni 20 s, la punteggiatura e i termini tecnici. Senza un riferimento non si sa quale dei due abbia ragione caso per caso.
- **Il canale del microfono trova l'utente.** Copre il 99,2% della sua voce secondo pyannote, e questo permette di dare il nome all'utente senza registrare prima un campione della voce. Il 28% del tempo del microfono però non è voce dell'utente in call: frasi dette col muto attivo su Teams, assensi ("mm-hm") e 4 frasi inventate sul rumore. Da qui la pausa del microfono (Ctrl+Alt+M) e il filtro più severo.
- **I termini di dominio restano il punto debole di entrambi**: 12 errori nella diretta e 10 nella rifinita su 10 termini ricorrenti (sigle di sensori, termini di ML in inglese, nomi di persone e aziende). Allora non c'era glossario; questa misura va ripetuta con il glossario attivo.

## Prossimi passi per una valutazione vera

1. Correggere a mano 2-3 riunioni, almeno su campioni di 5 minuti, e calcolare WER e cpWER (WER con attribuzione degli speaker) per diretta, rifinita e unione.
2. Confrontare la copertura con e senza unione su più riunioni.
3. Rifare la diretta dalle tracce (pulsante «Rifai la diretta») con e senza glossario, per misurarne l'effetto.
