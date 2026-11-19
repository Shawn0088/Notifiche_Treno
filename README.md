# 🚆 Notifiche Treno ViaggiaTreno (Telegram Bot)

Bot Python automatizzato per ricevere notifiche su Telegram riguardanti qualsiasi treno in Italia (**Regionali, Intercity, Frecce, linee suburbane**) monitorato tramite le API di ViaggiaTreno.

Progettato per essere leggero, resiliente e pronto per girare ogni 5 minuti su un'istanza cloud (come **Oracle Cloud Always Free**) via cron.

---

## ✨ Funzionalità

* **🎯 Monitoraggio flessibile (Tratta specifica o Corsa intera):**
  * Puoi impostare una `"tratta"` (es. `["MILANO CENTRALE", "ROMA TERMINI"]`) globalmente o per singolo treno — il bot riconosce automaticamente la direzione (andata e ritorno).
  * **Zero falsi allarmi:** se configuri una tratta, eventuali soppressioni parziali o ritardi fuori dalla tua tratta (es. dopo che sei già sceso) non generano falsi avvisi. Se invece viene cancellata la tua fermata di salita, discesa o una fermata intermedia, ricevi subito l'avviso 🔴.
  * Se non imposti nessuna tratta (o la lasci vuota), monitora l'intero percorso del treno dal capolinea di partenza a quello di arrivo.
* **⏱️ Soglia ritardo personalizzabile (default 5 minuti):**
  * Avvisa appena il treno supera la soglia configurata (es. `5` minuti).
  * Rinotifica intelligente solo se il ritardo cresce ulteriormente (di default ogni altri 5 minuti).
* **🟢 Notifica di rientro (recupero ritardo):**
  * Se avevi ricevuto un allarme di ritardo ma il treno recupera tornando puntuale (≤ 2 min), ricevi un avviso per non rischiare di perderlo.
* **🚉 Messaggi Telegram completi (HTML):**
  * Orario programmato vs orario stimato ricalcolato con il ritardo.
  * **Binario di partenza** (effettivo o programmato) alla stazione di salita.
  * Stazione e orario dell'**ultimo rilevamento** registrato.
* **⚡ Efficienza e risparmio chiamate:**
  * **Chiusura automatica a fine corsa:** appena il treno ha superato la stazione di discesa, viene marcato come completato e non viene più interrogato per il resto del giorno.
  * **Finestra temporale intelligente:** evita chiamate inutili ore prima della partenza (inizia a monitorare attivamente 60 minuti prima).
  * **Cache delle origini:** memorizza i codici delle stazioni di partenza in `stato.json`, dimezzando le richieste HTTP verso ViaggiaTreno.
  * **Sessione HTTP resiliente:** connection pooling e retry automatici con backoff esponenziale contro errori temporanei (429, 500, 502, 503, 504) o timeout.
* **📅 Calendario festività integrato:**
  * Riconosce automaticamente le festività nazionali italiane (incluse Pasqua e Pasquetta) ed eventuali festivi locali/patroni configurabili in `festivi_locali`, sospendendo i controlli nei giorni festivi.

---

## 📁 Struttura del Progetto

```text
├── Treni.py                 # Script principale con logica di monitoraggio e CLI
├── config.example.json      # Modello pubblico di configurazione treni e tratte
├── telegram.example.json    # Modello pubblico di configurazione credenziali Telegram
├── requirements.txt         # Dipendenze Python (requests, urllib3)
├── .gitignore               # Esclude config.json, telegram.json, log e file di stato
└── README.md                # Questa guida
```

---

## 🚀 Guida Rapida Locale

### 1. Prerequisiti
* Python 3.9 o superiore.
* Un bot Telegram (creabile tramite [@BotFather](https://t.me/BotFather)) e il tuo Chat ID (ricavabile tramite [@userinfobot](https://t.me/userinfobot)).

### 2. Installazione
```bash
git clone https://github.com/Shawn0088/Notifiche_Treno.git
cd notifiche-treno
python3 -m venv venv
source venv/bin/activate  # Su Windows: venv\Scripts\activate
pip install -r requirements.txt
```

### 3. Credenziali Telegram
Copia `telegram.example.json` in `telegram.json` (ignorato da Git):
```json
{
  "token": "TUO_TOKEN",
  "chat_id": "TUO_CHAT_ID"
}
```
*(In alternativa puoi usare le variabili d'ambiente `TELEGRAM_TOKEN` e `TELEGRAM_CHAT_ID`).*

### 4. Configurazione Treni e Tratte (`config.json`)
Copia `config.example.json` in `config.json` (anch'esso ignorato da Git per tutelare la tua privacy) e personalizzalo:

```json
{
  "tratta": ["STAZIONE A", "STAZIONE B"],
  "soglia_ritardo": 5,
  "passo_rinotifica": 5,
  "ignora_festivi": true,
  "festivi_locali": {
   
  }//inserisci qui le tue festività locali,
  "settimana": {
    "lun": ["10001", "10002"],
    "mar": [
      "10001",
      {"numero": "10003", "tratta": ["STAZIONE C", "STAZIONE D"]}
    ],
    "mer": ["10001", "10002"],
    "gio": ["10001", "10002"],
    "ven": ["10001", "10002"],
    "sab": [],
    "dom": []
  },
  "eccezioni": {
    "2026-12-24": ["10001"]
  }
}
```

* **`tratta`** *(opzionale)*: indica i due nomi (o parte del nome) delle stazioni che ti interessano in entrambe le direzioni. Se omessa o impostata a `[]`, il bot monitora il treno lungo tutta la corsa da origine a destinazione. Può anche essere personalizzata per singolo treno con un oggetto `{"numero": "...", "tratta": [...]}`.
* **`festivi_locali`** *(opzionale)*: dizionario `"MM-DD": "Nome Festa"` per aggiungere il patrono della tua città.
* **`eccezioni`** *(opzionale)*: permette di sovrascrivere i treni di una data specifica `"YYYY-MM-DD"` (anche in un giorno festivo).

Testa l'invio su Telegram:
```bash
python3 Treni.py test
```

---

## ☁️ Deploy su Oracle Cloud (Always Free VM)

### 1. Connessione all'istanza e clonazione
```bash
ssh ubuntu@IP_DELLA_TUA_MACCHINA_ORACLE
sudo apt update && sudo apt install -y python3-venv git
git clone https://github.com/Shawn0088/Notifiche_Treno.git
cd notifiche-treno
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
```

### 2. Configura credenziali e treni sul server
Poiché né `telegram.json` né `config.json` vengono caricati su GitHub, creali direttamente sul server a partire dai template:
```bash
cp telegram.example.json telegram.json && nano telegram.json
cp config.example.json config.json && nano config.json
```

### 3. Configura il Cron (`crontab -e`)
```bash
crontab -e
```
Aggiungi una riga per eseguire il controllo ogni 5 minuti negli orari di circolazione (esempio Lun-Ven dalle 06:00 alle 21:59):

```cron
# Controllo treni ogni 5 minuti (Lun-Ven 06:00 - 21:59) con battito giornaliero alle 07:00
*/5 6-21 * * 1-5 cd /home/ubuntu/Notifiche-treno && ./venv/bin/python Treni.py server 07:00 >> log.txt 2>&1
```

* `server 07:00`: invia una volta sola al giorno (dopo le 07:00) un messaggio di conferma che il bot e lo scheduler sono attivi. *(Se prendi treni anche sabato o domenica, usa `*` invece di `1-5`)*.

---

## 🛠️ Comandi CLI Utili

| Comando | Descrizione |
|---|---|
| `python Treni.py` | Esegue un controllo standard immediato. |
| `python Treni.py test` | Manda un messaggio di test su Telegram. |
| `python Treni.py prova` | Esegue il controllo sui treni di oggi e notifica anche i treni puntuali (senza salvare lo stato). |
| `python Treni.py stato <num>` | Mostra tutti i dettagli letti da ViaggiaTreno per un treno oggi (tratta, ritardo, binari, fermate). |
| `python Treni.py stato <num> "STAZ A" "STAZ B"` | Come sopra, simulando il filtro su una tratta specifica. |
| `python Treni.py stazione <nome>` | Cerca il codice interno ViaggiaTreno di una stazione. |
| `python Treni.py partenze <codice>` | Elenca le prossime partenze da una determinata stazione. |

