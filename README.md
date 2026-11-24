# Sonorus

Trascrizione delle riunioni **tutta in locale** su Windows: diretta mentre parli e, a fine riunione, un verbale rifinito con le voci separate e i nomi.

- **Diretta a due canali.** Cattura in loopback WASAPI ciò che esce dal PC ("Altri") e il microfono ("Io"), ognuno con il proprio segmentatore a energia e un filtro anti-eco. La trascrizione usa faster-whisper `large-v3-turbo` (int8_float16) sulla GPU.
- **Verbale rifinito.** Usa il motore di [Neon Minutes](https://github.com/battles5/neon-minutes) (pyannote + Whisper), installato a parte e guidato da `neon_worker.py` in un processo separato. La GPU (anche solo 4 GB) è di uno solo: la diretta ha la precedenza e la rifinitura si mette in pausa e riprende dalle cache.
- **La tua voce si riconosce da sola.** La traccia del microfono dice quando parlavi tu: lo speaker di pyannote che coincide con quei momenti prende il tuo nome ed entra nella libreria voci.
- **Diretta + verbale uniti** (`merge.py`). I tratti che la rifinitura salta vengono recuperati dalla diretta (in corsivo, «dalla diretta»). Le frasi di cortesia inventate sul silenzio ("Grazie a tutti.") vengono tolte, se la diretta non le conferma.
- **Pausa del microfono.** Pulsante o **Ctrl+Alt+M** globale, da usare insieme al mute di Teams: mentre è attiva la tua voce non viene né trascritta né salvata, e nel file al suo posto c'è silenzio. Gli intervalli di pausa sono scritti nella diretta.
- **Rifai la diretta.** Le due tracce restano salvate e si possono ritrascrivere più veloci del tempo reale, per esempio dopo aver aggiornato il glossario.
- Glossario dei termini di progetto, archivio, rinomina degli speaker, export in `.md`, `.json` e `.srt`.

## Installazione

Serve Windows 10/11, Python 3.12, una GPU NVIDIA (si ripiega sulla CPU con il modello `small`) e, per i verbali rifiniti, Neon Minutes installato in `%LOCALAPPDATA%\Programs\Neon Minutes`.

```bat
install.bat
```

Crea `.venv`, scarica il modello e mette l'icona sul Desktop.

## File

| File | Cosa fa |
|---|---|
| `app.py` | Finestra pywebview, coda delle rifiniture, GPU condivisa, pausa microfono, hotkey |
| `engine.py` | Cattura, segmentatore, trascrittore, sessione, ritrascrizione dalle tracce |
| `merge.py` | Unisce diretta e verbale rifinito (pure Python, si prova anche fuori da Windows) |
| `neon_worker.py` | Ponte verso Neon Minutes, con i fix per GPU da 4 GB |
| `static/index.html` | Interfaccia |

I dati restano sul PC: le riunioni in `Documents\Sonorus`, la configurazione e l'archivio in `%LOCALAPPDATA%\Sonorus`.
