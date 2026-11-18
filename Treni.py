#!/usr/bin/env python3
"""
Notifica Telegram per treni monitorati tramite API non ufficiali di ViaggiaTreno.
Funziona con qualsiasi treno (Regionali, Intercity, Frecce, ecc.) e qualsiasi tratta,
permettendo di monitorare l'intero percorso oppure solo una tratta specifica.

Avvisa in caso di:
  - RITARDO >= 5 minuti (configurabile) sul percorso/tratta di interesse
  - RIENTRO del ritardo (quando un treno in ritardo torna puntuale)
  - SOPPRESSIONE totale o parziale che impatta il percorso/tratta monitorato
  - CANCELLAZIONE di fermate lungo il percorso/tratta monitorato

Uso:
    python Treni.py                                # controllo standard (da pianificare ogni 5 minuti)
    python Treni.py server 07:00                   # come sopra + messaggio giornaliero 'servizio attivo' dopo le 07:00
    python Treni.py test                           # manda un messaggio di prova su Telegram
    python Treni.py prova                          # controllo di prova: avvisa su TUTTI i treni di oggi
                                                   # (anche puntuali), senza salvare lo stato
    python Treni.py stato 12345                    # mostra i dettagli e cosa legge lo script adesso
    python Treni.py stato 12345 "STAZ A" "STAZ B"  # come sopra filtrando una tratta specifica
    python Treni.py stazione MILANO                # trova il codice di una stazione
    python Treni.py partenze S01700                # prossime partenze da una stazione
    python Treni.py partenze S01700 07:30
"""
from __future__ import annotations

import html
import json
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo

import requests
from requests.adapters import HTTPAdapter
from urllib3.util import Retry

BASE = "http://www.viaggiatreno.it/infomobilita/resteasy/viaggiatreno"
TZ = ZoneInfo("Europe/Rome")
DIR = Path(__file__).resolve().parent
CONFIG = DIR / "config.json"
STATE = DIR / "stato.json"
TELEGRAM_FILE = DIR / "telegram.json"  # {"token": "...", "chat_id": "..."}
LOG = DIR / "log.txt"
LOG_MAX_BYTES = 500_000

GIORNI = ["lun", "mar", "mer", "gio", "ven", "sab", "dom"]
SOGLIA_RITARDO_DEFAULT = 5     # minuti: da qui in su avvisa
PASSO_RINOTIFICA_DEFAULT = 5   # se il ritardo cresce di altri 5 minuti, avvisa di nuovo
SOGLIA_RECUPERO = 2            # minuti: se scende a <= 2 min dopo una notifica di ritardo, avvisa del recupero
ANTICIPO_MONITORAGGIO_MIN = 60 # inizia a monitorare attivamente un treno 60 min prima della partenza

MESI_EN = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
GIORNI_EN = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

# Sessione HTTP globale con retry automatico e connection pooling
_SESSION: requests.Session | None = None


def sessione_http() -> requests.Session:
    global _SESSION
    if _SESSION is None:
        s = requests.Session()
        retry = Retry(
            total=3,
            connect=3,
            read=3,
            backoff_factor=1.0,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["GET", "POST"],
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry, pool_connections=4, pool_maxsize=8)
        s.mount("http://", adapter)
        s.mount("https://", adapter)
        s.headers.update({"User-Agent": "Mozilla/5.0 (NotificheTreno)"})
        _SESSION = s
    return _SESSION


# ---------------------------------------------------------------- Festivita'

def pasqua_e_pasquetta(anno: int) -> tuple[date, date]:
    """Calcola la data di Pasqua e Pasquetta (algoritmo di Meeus/Jones/Butcher)."""
    a = anno % 19
    b = anno // 100
    c = anno % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    mese = (h + l - 7 * m + 114) // 31
    giorno = ((h + l - 7 * m + 114) % 31) + 1
    pasqua = date(anno, mese, giorno)
    return pasqua, pasqua + timedelta(days=1)


def festivi_italiani(anno: int, festivi_locali: dict[str, str] | None = None) -> dict[date, str]:
    """Restituisce i giorni festivi nazionali ed eventuali festivi locali ('MM-DD': 'Nome')."""
    feste = {
        date(anno, 1, 1): "Capodanno",
        date(anno, 1, 6): "Epifania",
        date(anno, 4, 25): "Festa della Liberazione",
        date(anno, 5, 1): "Festa dei Lavoratori",
        date(anno, 6, 2): "Festa della Repubblica",
        date(anno, 8, 15): "Ferragosto",
        date(anno, 11, 1): "Tutti i Santi",
        date(anno, 12, 8): "Immacolata Concezione",
        date(anno, 12, 25): "Natale",
        date(anno, 12, 26): "Santo Stefano",
    }
    pasqua, pasquetta = pasqua_e_pasquetta(anno)
    feste[pasqua] = "Pasqua"
    feste[pasquetta] = "Pasquetta"

    if isinstance(festivi_locali, dict):
        for mm_dd, nome in festivi_locali.items():
            try:
                mese, giorno = map(int, str(mm_dd).strip().split("-"))
                feste[date(anno, mese, giorno)] = str(nome)
            except (ValueError, TypeError):
                print(f"[attenzione] festivo locale non valido '{mm_dd}' (atteso formato 'MM-DD')",
                      file=sys.stderr)
    return feste


# ---------------------------------------------------------------- API ViaggiaTreno

def get(path: str) -> str:
    r = sessione_http().get(f"{BASE}/{path}", timeout=15)
    r.raise_for_status()
    return r.text


def mezzanotte_ms(d: date) -> int:
    """Timestamp (ms) della mezzanotte italiana del giorno d, come richiesto da andamentoTreno."""
    return int(datetime(d.year, d.month, d.day, tzinfo=TZ).timestamp() * 1000)


def origini_treno(numero: str) -> list[tuple[str, str]]:
    """[(nome_stazione, codice_stazione)] possibili per quel numero di treno."""
    out = []
    for riga in get(f"cercaNumeroTrenoTrenoAutocomplete/{numero}").splitlines():
        if "|" not in riga:
            continue
        sinistra, destra = riga.split("|", 1)
        parti = destra.strip().split("-")
        if len(parti) >= 2:
            nome = sinistra.split("-", 1)[-1].strip()
            out.append((nome, parti[1]))
    return out


def scarica_andamento(cod: str, numero: str, ts: int) -> dict | None:
    testo = get(f"andamentoTreno/{cod}/{numero}/{ts}")
    if not testo or not testo.strip():
        return None
    try:
        dati = json.loads(testo)
    except json.JSONDecodeError:
        return None
    if isinstance(dati, dict) and dati.get("numeroTreno") is not None:
        return dati
    return None


def andamento(numero: str, giorno: date, cache_origini: dict[str, str] | None = None) -> dict | None:
    """Stato del treno in quel giorno, usando la cache dei codici stazione se disponibile."""
    ts = mezzanotte_ms(giorno)
    if cache_origini and numero in cache_origini:
        dati = scarica_andamento(cache_origini[numero], numero, ts)
        if dati is not None:
            return dati

    for _, cod in origini_treno(numero):
        dati = scarica_andamento(cod, numero, ts)
        if dati is not None:
            if cache_origini is not None:
                cache_origini[numero] = cod
            return dati
    return None


# ---------------------------------------------------------------- Analisi Percorso / Tratta

def formatta_ms(ts) -> str | None:
    if isinstance(ts, (int, float)) and ts > 0:
        return datetime.fromtimestamp(ts / 1000, tz=TZ).strftime("%H:%M")
    return None


def datetime_da_ms(ts) -> datetime | None:
    if isinstance(ts, (int, float)) and ts > 0:
        return datetime.fromtimestamp(ts / 1000, tz=TZ)
    return None


def orario_fermata(f: dict | None, partenza: bool = True) -> str:
    """Restituisce l'orario programmato della fermata in formato HH:MM."""
    if not f:
        return "--:--"
    chiavi_ms = ["partenzaTeorica", "programmata", "partenzaReale"] if partenza else [
        "arrivoTeorico", "programmata", "arrivoReale"
    ]
    for k in chiavi_ms:
        s = formatta_ms(f.get(k))
        if s:
            return s
    return "--:--"


def orario_stimato(ora_prog: str, ritardo: int | None) -> str | None:
    """Calcola l'orario stimato sommando il ritardo all'orario programmato HH:MM."""
    if not ritardo or ritardo <= 0 or ":" not in ora_prog or ora_prog == "--:--":
        return None
    try:
        h, m = map(int, ora_prog.split(":"))
        tot = (h * 60 + m + ritardo) % (24 * 60)
        return f"{tot // 60:02d}:{tot % 60:02d}"
    except ValueError:
        return None


def estrai_binario(f: dict | None, partenza: bool = True) -> str | None:
    if not f:
        return None
    chiavi = [
        ("binarioEffettivoPartenzaDescrizione", True),
        ("binarioProgrammatoPartenzaDescrizione", False),
    ] if partenza else [
        ("binarioEffettivoArrivoDescrizione", True),
        ("binarioProgrammatoArrivoDescrizione", False),
    ]
    for k, effettivo in chiavi:
        val = f.get(k)
        if isinstance(val, str) and val.strip():
            v = val.strip()
            return f"<b>{html.escape(v)}</b>" if effettivo else f"{html.escape(v)} <i>(prog.)</i>"
    return None


def estrai_ultimo_rilevamento(dati: dict) -> str | None:
    staz = dati.get("stazioneUltimoRilevamento")
    if not staz or staz.strip() in ("", "--"):
        return None
    ora_s = formatta_ms(dati.get("oraUltimoRilevamento"))
    if ora_s:
        return f"{html.escape(staz.title())} ({ora_s})"
    return html.escape(staz.title())


def ritardo_globale(dati: dict) -> int | None:
    """Ritardo generale del treno in minuti."""
    r = dati.get("ritardo")
    if isinstance(r, (int, float)):
        return int(r)
    for f in reversed(dati.get("fermate") or []):
        if f.get("actualFermataType") == 1 and isinstance(f.get("ritardo"), (int, float)):
            return int(f["ritardo"])
    return None


def trova_fermata(fermate: list[dict], nome_target: str) -> tuple[int | None, dict | None]:
    target = nome_target.upper()
    for i, f in enumerate(fermate):
        st = (f.get("stazione") or "").upper()
        if target in st:
            return i, f
    return None, None


def normalizza_tratta(val) -> tuple[str, str] | None:
    """Converte una tratta [stazione_a, stazione_b] in tupla normalizzata, o None se assente/vuota."""
    if isinstance(val, (list, tuple)) and len(val) == 2:
        a, b = str(val[0]).strip(), str(val[1]).strip()
        if a and b and a.upper() != b.upper():
            return a, b
    return None


def analizza_tratta(dati: dict, soglia: int, tratta: tuple[str, str] | None = None) -> dict:
    """
    Valuta lo stato del treno sulla tratta specificata (se presente sul percorso),
    altrimenti sull'intera corsa dall'origine alla destinazione.
    Restituisce un dizionario con:
      - esito: 'soppresso' | 'ritardo' | 'ok'
      - ritardo: int | None (minuti di ritardo rilevanti)
      - motivo: str | None (dettaglio in caso di soppressione/cancellazione)
      - completata: bool (True se il treno ha gia' superato la fermata di discesa)
      - fuori_finestra: bool (True se manca ancora troppo alla partenza e il treno e' regolare)
      - salita_nome, discesa_nome, salita_ora, discesa_ora
      - f_salita, f_discesa
    """
    fermate = dati.get("fermate") or []
    idx_a = idx_b = None
    f_a = f_b = None
    if tratta:
        idx_a, f_a = trova_fermata(fermate, tratta[0])
        idx_b, f_b = trova_fermata(fermate, tratta[1])

    if idx_a is not None and idx_b is not None and idx_a != idx_b:
        if idx_a < idx_b:
            idx_salita, f_salita = idx_a, f_a
            idx_discesa, f_discesa = idx_b, f_b
        else:
            idx_salita, f_salita = idx_b, f_b
            idx_discesa, f_discesa = idx_a, f_a
        salita_nome = (f_salita.get("stazione") or "").strip().title() or "?"
        discesa_nome = (f_discesa.get("stazione") or "").strip().title() or "?"
        tratta_fermate = fermate[idx_salita : idx_discesa + 1]
    elif fermate:
        # Nessuna tratta configurata oppure stazioni non presenti sul treno: controlla l'intero percorso
        idx_salita, f_salita = 0, fermate[0]
        idx_discesa, f_discesa = len(fermate) - 1, fermate[-1]
        salita_nome = (f_salita.get("stazione") or dati.get("origine") or "?").title()
        discesa_nome = (f_discesa.get("stazione") or dati.get("destinazione") or "?").title()
        tratta_fermate = fermate
    else:
        idx_salita = idx_discesa = 0
        f_salita = f_discesa = None
        salita_nome = (dati.get("origine") or "?").title()
        discesa_nome = (dati.get("destinazione") or "?").title()
        tratta_fermate = []

    salita_ora = orario_fermata(f_salita, partenza=True) if f_salita else dati.get("compOrarioPartenza", "--:--")
    discesa_ora = orario_fermata(f_discesa, partenza=False) if f_discesa else dati.get("compOrarioArrivo", "--:--")

    base = {
        "salita_nome": salita_nome,
        "discesa_nome": discesa_nome,
        "salita_ora": salita_ora,
        "discesa_ora": discesa_ora,
        "f_salita": f_salita,
        "f_discesa": f_discesa,
    }

    # 1. Soppressione totale del treno
    if dati.get("provvedimento") == 1 or dati.get("tipoTreno") == "ST":
        return {**base, "esito": "soppresso", "ritardo": None,
                "motivo": "Treno interamente soppresso", "completata": False, "fuori_finestra": False}

    # 2. Controllo soppressioni parziali / fermate cancellate sul percorso o tratta monitorata
    # In ViaggiaTreno actualFermataType: 0 = non ancora effettuata, 1 = effettuata, 2 o 3 = soppressa/saltata
    if f_salita and f_salita.get("actualFermataType") in (2, 3):
        return {**base, "esito": "soppresso", "ritardo": None,
                "motivo": f"Fermata di partenza ({salita_nome}) soppressa",
                "completata": False, "fuori_finestra": False}

    if f_discesa and f_discesa.get("actualFermataType") in (2, 3):
        return {**base, "esito": "soppresso", "ritardo": None,
                "motivo": f"Fermata di arrivo ({discesa_nome}) soppressa / corsa limitata",
                "completata": False, "fuori_finestra": False}

    fermate_saltate = [
        (f.get("stazione") or "").title()
        for f in tratta_fermate
        if f.get("actualFermataType") in (2, 3)
    ]
    if fermate_saltate:
        return {**base, "esito": "soppresso", "ritardo": None,
                "motivo": f"Soppressione sulla tratta (fermate cancellate: {', '.join(fermate_saltate)})",
                "completata": False, "fuori_finestra": False}

    # 3. Verifica se il treno ha GIA' completato la tratta di interesse
    completata = False
    if f_discesa and f_discesa.get("actualFermataType") == 1:
        completata = True
    elif fermate and idx_discesa == len(fermate) - 1 and dati.get("arrivato") is True:
        completata = True
    elif fermate and idx_discesa < len(fermate) - 1:
        completata = any(f_succ.get("actualFermataType") == 1 for f_succ in fermate[idx_discesa + 1 :])

    # 4. Calcolo del ritardo rilevante
    if f_discesa and f_discesa.get("actualFermataType") == 1 and isinstance(f_discesa.get("ritardo"), (int, float)):
        ritardo = int(f_discesa["ritardo"])
    else:
        ritardo = ritardo_globale(dati)
        if ritardo is None and f_salita and isinstance(f_salita.get("ritardo"), (int, float)):
            ritardo = int(f_salita["ritardo"])

    # 5. Verifica finestra temporale (evita di monitorare a vuoto ore prima della partenza)
    fuori_finestra = False
    if f_salita:
        ts_salita = f_salita.get("partenzaTeorica") or f_salita.get("programmata")
        dt_salita = datetime_da_ms(ts_salita)
        if dt_salita and datetime.now(TZ) < dt_salita - timedelta(minutes=ANTICIPO_MONITORAGGIO_MIN):
            fuori_finestra = True

    esito = "ritardo" if (ritardo is not None and ritardo >= soglia) else "ok"
    return {**base, "esito": esito, "ritardo": ritardo, "motivo": None,
            "completata": completata, "fuori_finestra": fuori_finestra}


def azione_notifica(info: dict, precedente: dict | str | None, passo_rinotifica: int) -> str | None:
    """
    Decide se inviare una notifica e di quale tipo:
      - 'soppresso': prima volta che risulta soppresso sulla tratta
      - 'ritardo': prima volta sopra soglia, oppure aumento di almeno `passo_rinotifica` minuti
      - 'recupero': era stato notificato in ritardo, ma ora il ritardo e' rientrato (<= SOGLIA_RECUPERO)
      - None: nessuna notifica da inviare
    """
    if isinstance(precedente, str):
        precedente = {"tipo": precedente}

    esito = info["esito"]
    valore = info["ritardo"]
    tipo_prec = precedente.get("tipo") if precedente else None
    val_prec = precedente.get("valore", 0) if precedente else 0

    if esito == "soppresso":
        return "soppresso" if tipo_prec != "soppresso" else None

    if esito == "ritardo":
        if tipo_prec != "ritardo":
            return "ritardo"
        if valore is not None and valore >= (val_prec or 0) + passo_rinotifica:
            return "ritardo"
        return None

    if esito == "ok" and tipo_prec == "ritardo":
        if valore is not None and valore <= SOGLIA_RECUPERO:
            return "recupero"

    return None


# ---------------------------------------------------------------- Config e Stato

def treni_del_giorno(cfg: dict, d: date) -> list[dict]:
    """Prima le eccezioni sul singolo giorno, altrimenti il programma settimanale."""
    eccezioni = cfg.get("eccezioni", {})
    if d.isoformat() in eccezioni:
        elenco = eccezioni[d.isoformat()]
    else:
        if cfg.get("ignora_festivi", True):
            feste = festivi_italiani(d.year, cfg.get("festivi_locali"))
            if d in feste:
                return []
        elenco = cfg.get("settimana", {}).get(GIORNI[d.weekday()], [])

    tratta_globale = normalizza_tratta(cfg.get("tratta"))
    out = []
    for t in elenco:
        if isinstance(t, str):
            out.append({"numero": t.strip(), "tratta": tratta_globale})
        elif isinstance(t, dict) and t.get("numero") is not None:
            tratta_treno = normalizza_tratta(t["tratta"]) if "tratta" in t else tratta_globale
            out.append({**t, "numero": str(t["numero"]).strip(), "tratta": tratta_treno})
    return out


def carica_json(path: Path, default):
    if not path.exists():
        return default
    try:
        testo = path.read_text(encoding="utf-8").strip()
        return json.loads(testo) if testo else default
    except (json.JSONDecodeError, OSError) as e:
        print(f"[attenzione] impossibile leggere {path.name} ({e}), uso stato vuoto.", file=sys.stderr)
        return default


def salva_json_atomico(path: Path, dati: dict) -> None:
    """Scrittura atomica tramite file temporaneo per evitare corruzioni in caso di crash."""
    tmp = path.with_suffix(f".tmp.{os.getpid()}")
    try:
        tmp.write_text(json.dumps(dati, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)
    except Exception:
        if tmp.exists():
            tmp.unlink(missing_ok=True)
        raise


def carica_config() -> dict:
    """Legge config.json e segnala errori comuni."""
    def senza_duplicati(coppie):
        d = {}
        for k, v in coppie:
            if k in d:
                raise ValueError(f"nel config.json la chiave '{k}' compare due volte: unisci le due sezioni")
            d[k] = v
        return d

    if not CONFIG.exists():
        raise SystemExit(
            f"File non trovato: {CONFIG} (copia config.example.json in config.json e personalizzalo)"
        )
    try:
        cfg = json.loads(CONFIG.read_text(encoding="utf-8"), object_pairs_hook=senza_duplicati)
    except json.JSONDecodeError as e:
        raise SystemExit(f"config.json non e' un JSON valido: {e}")
    except ValueError as e:
        raise SystemExit(f"Errore: {e}")

    chiavi_note = {
        "settimana",
        "eccezioni",
        "tratta",
        "soglia_ritardo",
        "passo_rinotifica",
        "ignora_festivi",
        "festivi_locali",
        "_nota",
    }
    ignote = set(cfg) - chiavi_note
    if ignote:
        print(f"[attenzione] chiavi ignorate in config.json: {sorted(ignote)}")
    for k in cfg.get("settimana", {}):
        if k not in GIORNI:
            print(f"[attenzione] '{k}' dentro 'settimana' non e' un giorno valido {GIORNI}")
    return cfg


# ---------------------------------------------------------------- Notifiche Telegram

def credenziali() -> tuple[str | None, str | None]:
    """Prima le variabili d'ambiente, poi il file telegram.json accanto allo script."""
    token = os.environ.get("TELEGRAM_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if (not token or not chat) and TELEGRAM_FILE.exists():
        f = carica_json(TELEGRAM_FILE, {})
        token = token or f.get("token")
        chat = chat or f.get("chat_id")
    return token, chat


def notifica(testo: str, parse_mode: str | None = "HTML") -> None:
    token, chat = credenziali()
    if not token or not chat:
        print("[NOTIFICA - Telegram non configurato]\n" + testo)
        return
    payload = {"chat_id": chat, "text": testo}
    if parse_mode:
        payload["parse_mode"] = parse_mode
    r = sessione_http().post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=payload,
        timeout=15,
    )
    r.raise_for_status()


def formatta_messaggio(prefisso: str, numero: str, d: date, dati: dict, info: dict, tipo_avviso: str) -> str:
    salita = html.escape(info["salita_nome"])
    discesa = html.escape(info["discesa_nome"])
    ora_s = info["salita_ora"]
    ora_d = info["discesa_ora"]
    rit = info["ritardo"]
    pref = html.escape(prefisso) if prefisso else ""

    stimata_s = orario_stimato(ora_s, rit)
    stimata_d = orario_stimato(ora_d, rit)
    str_ora_s = f"{ora_s} ➔ <b>{stimata_s}</b>" if stimata_s else ora_s
    str_ora_d = f"{ora_d} ➔ <b>{stimata_d}</b>" if stimata_d else ora_d

    righe = []
    if tipo_avviso == "soppresso":
        righe.append(f"{pref}🔴 <b>Treno {html.escape(numero)} SOPPRESSO</b>")
        righe.append(f"📍 <b>{salita}</b> ({ora_s}) ➔ <b>{discesa}</b> ({ora_d})")
        if info.get("motivo"):
            righe.append(f"ℹ️ {html.escape(info['motivo'])}")
        return "\n".join(righe)

    if tipo_avviso == "recupero":
        rit_str = f"+{rit} min" if rit and rit > 0 else "in orario"
        righe.append(f"{pref}🟢 <b>Treno {html.escape(numero)}: ritardo rientrato ({rit_str})</b>")
        righe.append(f"📍 <b>{salita}</b> ({ora_s}) ➔ <b>{discesa}</b> ({ora_d})")
    elif tipo_avviso == "ritardo":
        righe.append(f"{pref}⚠️ <b>Treno {html.escape(numero)} in ritardo di {rit} min</b>")
        righe.append(f"📍 Partenza <b>{salita}</b>: {str_ora_s}")
        righe.append(f"🏁 Arrivo <b>{discesa}</b>: {str_ora_d}")
    else:
        # Modalita' prova su treno puntuale
        rit_str = f"{rit:+} min" if isinstance(rit, int) else "n.d."
        righe.append(f"{pref}🚆 <b>Treno {html.escape(numero)} regolare ({rit_str})</b>")
        righe.append(f"📍 <b>{salita}</b> ({ora_s}) ➔ <b>{discesa}</b> ({ora_d})")

    bin_part = estrai_binario(info.get("f_salita"), partenza=True)
    if bin_part:
        righe.append(f"🚉 Binario a {salita}: {bin_part}")

    rilev = estrai_ultimo_rilevamento(dati)
    if rilev:
        righe.append(f"📡 Ultimo rilevamento: <i>{rilev}</i>")

    return "\n".join(righe)


def invia_battito(stato: dict, orario: str, n_treni: int, festa: str | None = None,
                  tratta_globale: tuple[str, str] | None = None) -> None:
    """Una volta al giorno (dopo 'orario') avvisa che il servizio e' attivo."""
    adesso = datetime.now(TZ)
    chiave = f"{adesso.date().isoformat()}:battito"
    h, m = map(int, orario.split(":"))
    if chiave in stato or (adesso.hour, adesso.minute) < (h, m):
        return

    if festa:
        testo = f"ℹ️ <b>Servizio attivo</b>\nOggi è <b>{html.escape(festa)}</b>: nessun controllo in programma."
    elif n_treni:
        tratta_str = (
            f" sulla tratta <b>{html.escape(tratta_globale[0].title())} ⇄ {html.escape(tratta_globale[1].title())}</b>"
            if tratta_globale else ""
        )
        testo = f"ℹ️ <b>Servizio attivo</b>\nOggi monitoro <b>{n_treni} treni</b>{tratta_str}."
    else:
        testo = "ℹ️ <b>Servizio attivo</b>\nOggi non ci sono treni in programma."

    try:
        notifica(testo)
    except requests.RequestException as e:
        print(f"[errore Telegram] {e}", file=sys.stderr)
        return
    stato[chiave] = True


# ---------------------------------------------------------------- Controllo Principale

def controlla(soglia_override: int | None = None, salva_stato: bool = True, prefisso: str = "",
              battito_ora: str | None = None, modalita_prova: bool = False) -> None:
    """Controlla i treni di OGGI sul percorso/tratta configurato in config.json."""
    cfg = carica_config()
    soglia = soglia_override if soglia_override is not None else int(cfg.get("soglia_ritardo", SOGLIA_RITARDO_DEFAULT))
    passo = int(cfg.get("passo_rinotifica", PASSO_RINOTIFICA_DEFAULT))
    tratta_globale = normalizza_tratta(cfg.get("tratta"))

    stato = carica_json(STATE, {}) if salva_stato else {}
    cache_origini = stato.setdefault("cache_origini", {})

    adesso = datetime.now(TZ)
    d = adesso.date()
    feste = festivi_italiani(d.year, cfg.get("festivi_locali"))
    festa_oggi = feste.get(d) if (cfg.get("ignora_festivi", True) and d.isoformat() not in cfg.get("eccezioni", {})) else None

    elenco = treni_del_giorno(cfg, d)
    print(f"--- {adesso:%Y-%m-%d %H:%M:%S}")
    if festa_oggi:
        print(f"[info] oggi {d} e' festivo ({festa_oggi}): controllo treni disattivato.")
    else:
        print(f"[info] oggi {d} ({GIORNI[d.weekday()]}): {len(elenco)} treni | soglia ritardo: {soglia} min")

    if battito_ora and salva_stato:
        invia_battito(stato, battito_ora, len(elenco), festa=festa_oggi, tratta_globale=tratta_globale)

    for t in elenco:
        numero = str(t["numero"]).strip()
        chiave = f"{d.isoformat()}:{numero}"
        prec = stato.get(chiave)

        # Se il treno ha gia' completato il percorso/tratta oggi, non interrogarlo piu'
        if not modalita_prova and isinstance(prec, dict) and prec.get("finito"):
            print(f"[skip] treno {numero}: percorso gia' completato oggi.")
            continue

        try:
            dati = andamento(numero, d, cache_origini)
        except Exception as e:
            print(f"[errore] treno {numero}: {e}", file=sys.stderr)
            continue

        if dati is None:
            print(f"[info] treno {numero}: nessun dato (non ancora disponibile o non circola oggi)")
            continue

        info = analizza_tratta(dati, soglia, tratta=t.get("tratta"))

        # Se fuori finestra temporale (manca > 60 min) e il treno non e' soppresso/in ritardo, attendi
        if not modalita_prova and info["fuori_finestra"] and info["esito"] == "ok":
            print(f"[attesa] treno {numero} ({info['salita_nome']} {info['salita_ora']} -> "
                  f"{info['discesa_nome']} {info['discesa_ora']}): partenza lontana.")
            continue

        print(f"[ok] treno {numero} ({info['salita_nome']} {info['salita_ora']} -> "
              f"{info['discesa_nome']} {info['discesa_ora']}): {info['esito']} "
              f"(ritardo {info['ritardo']} min, completata={info['completata']})")

        if modalita_prova:
            tipo_avviso = info["esito"] if info["esito"] in ("soppresso", "ritardo") else "prova"
            try:
                notifica(formatta_messaggio(prefisso, numero, d, dati, info, tipo_avviso))
            except requests.RequestException as e:
                print(f"[errore Telegram] {e}", file=sys.stderr)
            continue

        # Se non ha ancora finito la tratta, verifica se inviare avviso
        if not info["completata"]:
            tipo_avviso = azione_notifica(info, prec, passo)
            if tipo_avviso:
                try:
                    notifica(formatta_messaggio(prefisso, numero, d, dati, info, tipo_avviso))
                except requests.RequestException as e:
                    print(f"[errore Telegram] {e}", file=sys.stderr)
                    continue
                stato[chiave] = {
                    "tipo": "ok" if tipo_avviso == "recupero" else tipo_avviso,
                    "valore": info["ritardo"],
                    "finito": False,
                }
        else:
            # Segna la tratta come completata per non fare piu' chiamate inutili oggi
            nuovo_stato = dict(prec) if isinstance(prec, dict) else {"tipo": info["esito"], "valore": info["ritardo"]}
            nuovo_stato["finito"] = True
            stato[chiave] = nuovo_stato

    if salva_stato:
        # Mantieni la cache_origini e solo gli stati a partire da oggi
        stato_pulito = {
            k: v for k, v in stato.items()
            if k == "cache_origini" or k.split(":")[0] >= d.isoformat()
        }
        salva_json_atomico(STATE, stato_pulito)


# ---------------------------------------------------------------- Comandi CLI di aiuto

def cmd_stazione(nome: str) -> None:
    for riga in get(f"autocompletaStazione/{quote(nome)}").splitlines():
        if "|" in riga:
            print(riga.strip())


def cmd_partenze(codice: str, ora: str | None = None) -> None:
    n = datetime.now(TZ)
    if ora:
        h, m = map(int, ora.split(":"))
        n = n.replace(hour=h, minute=m, second=0, microsecond=0)
    base = (f"{GIORNI_EN[n.weekday()]} {MESI_EN[n.month - 1]} {n.day:02d} {n.year} "
            f"{n:%H:%M:%S} GMT{n:%z}")
    nome_tz = "CEST" if n.dst() else "CET"
    for quando in [base, f"{base} ({nome_tz})"]:
        testo = get(f"partenze/{codice}/{quote(quando)}")
        try:
            dati = json.loads(testo)
        except json.JSONDecodeError:
            continue
        if dati:
            for p in dati:
                print(p.get("numeroTreno"), p.get("compOrarioPartenza"),
                      "->", p.get("destinazione"), f"(ritardo {p.get('ritardo')} min)")
            return
    print("Nessuna partenza trovata.")


def cmd_stato(numero: str, tratta_cli: tuple[str, str] | None = None) -> None:
    oggi = datetime.now(TZ).date()
    cfg = carica_config() if CONFIG.exists() else {}
    soglia = int(cfg.get("soglia_ritardo", SOGLIA_RITARDO_DEFAULT))

    tratta = tratta_cli
    if tratta is None:
        # Cerca se per oggi il treno ha una tratta specifica in config.json, altrimenti usa la tratta globale
        tratta = normalizza_tratta(cfg.get("tratta"))
        for t in treni_del_giorno(cfg, oggi):
            if t["numero"] == str(numero).strip():
                tratta = t.get("tratta")
                break

    dati = andamento(numero, oggi)
    if dati is None:
        print("Nessun dato per questo treno oggi (numero sbagliato o treno non in programma oggi).")
        return

    info = analizza_tratta(dati, soglia, tratta=tratta)
    print(f"=== Treno {numero} ({dati.get('origine')} {dati.get('compOrarioPartenza')} -> "
          f"{dati.get('destinazione')} {dati.get('compOrarioArrivo')}) ===")
    print(f"Percorso monitorato: {info['salita_nome']} ({info['salita_ora']}) -> "
          f"{info['discesa_nome']} ({info['discesa_ora']})")
    print(f"Esito              : {info['esito'].upper()} | Ritardo: {info['ritardo']} min | "
          f"Completata: {info['completata']}")
    if info.get("motivo"):
        print(f"Dettaglio          : {info['motivo']}")
    print(f"Ultimo rilevamento : {dati.get('stazioneUltimoRilevamento')} | "
          f"tipoTreno: {dati.get('tipoTreno')} | provvedimento: {dati.get('provvedimento')}")
    print("\nFermate:")
    for f in dati.get("fermate") or []:
        st = f.get("stazione", "")
        if f is info.get("f_salita"):
            evidenzia = " <-- SALITA"
        elif f is info.get("f_discesa"):
            evidenzia = " <-- DISCESA"
        else:
            evidenzia = ""
        ora_p = orario_fermata(f, partenza=True)
        ora_a = orario_fermata(f, partenza=False)
        print(f"  - {st:26s} | prog {ora_a}/{ora_p} | tipoFermata={f.get('actualFermataType')} "
              f"| ritardo={f.get('ritardo')} min{evidenzia}")


def avvia_log() -> None:
    """Senza console (es. pythonw.exe) scrive su log.txt con rotazione automatica."""
    if sys.stdout is not None and sys.stderr is not None:
        return
    if LOG.exists() and LOG.stat().st_size > LOG_MAX_BYTES:
        LOG.replace(DIR / "log_vecchio.txt")
    f = open(LOG, "a", encoding="utf-8", buffering=1)
    if sys.stdout is None:
        sys.stdout = f
    if sys.stderr is None:
        sys.stderr = f


def main() -> None:
    avvia_log()
    args = sys.argv[1:]
    if not args:
        controlla()
    elif args[0] == "server":
        controlla(battito_ora=args[1] if len(args) > 1 else "07:00")
    elif args[0] == "prova":
        controlla(soglia_override=0, salva_stato=False, prefisso="[PROVA] ", modalita_prova=True)
    elif args[0] == "test":
        if not all(credenziali()):
            print("Credenziali Telegram mancanti: imposta le variabili d'ambiente o crea telegram.json")
        tratta_str = ""
        if CONFIG.exists():
            try:
                tratta = normalizza_tratta(carica_config().get("tratta"))
                if tratta:
                    tratta_str = (f"\nTratta configurata: <b>{html.escape(tratta[0].title())} ⇄ "
                                  f"{html.escape(tratta[1].title())}</b>.")
            except SystemExit:
                pass
        notifica(f"✅ <b>Test notifiche treno riuscito!</b>{tratta_str}")
    elif args[0] == "stato" and len(args) > 1:
        tratta_cli = (args[2], args[3]) if len(args) >= 4 else None
        cmd_stato(args[1], tratta_cli=tratta_cli)
    elif args[0] == "stazione" and len(args) > 1:
        cmd_stazione(" ".join(args[1:]))
    elif args[0] == "partenze" and len(args) > 1:
        cmd_partenze(args[1], args[2] if len(args) > 2 else None)
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
