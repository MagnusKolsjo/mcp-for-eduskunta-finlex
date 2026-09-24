# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Magnus Kolsjö

"""
mcp_server.py — MCP-server för finsk riksdags- och rättsdata

Exponerar följande verktyg:

  fi_sok              — Aggregerad sökning över alla finska källor (fanout)
  fi_sok_eduskunta    — Strukturerad sökning i riksdagsdokument (api.eduskunta.fi)
  fi_sok_finlex       — FTS + semantisk sökning i lokal Finlex-databas
  fi_sok_i_dokument   — Semantisk sökning via pgvector inom ett enskilt cachat dokument
  fi_hamta_dokument   — Hämtar fulltext via edktunnus eller eduskuntatunnus
  fi_hamta_arende     — Ärendelivscykel, kärnedokument och expertutlåtanden
  fi_hamta_lag        — Hämtar specifik lag/proposition från Finlex (AKN XML)
  fi_hamta_aanestys   — Voteringsresultat
  fi_lista_vaalikaudet — Valperioder och riksmöten (fr.o.m. 1907)

Datakällor:
  api.eduskunta.fi       — Eduskuntas öppna API (sökning, fulltext, voteringar)
  opendata.finlex.fi     — Finlex öppna data (AKN XML, bulk-synkad lokal DB)
  avoindata.eduskunta.fi — Sekundär (voteringshistorik 1996–2014 via lokal DB)

Tvåspråkighet:
  Sökning sker primärt på finska med TurkuNLP/sbert-cased-finnish-paraphrase.
  Sökning på svenska använder KBLab/sentence-bert-swedish-cased.
  Frågespråket detekteras automatiskt. Citat hämtas alltid från swe@-AKN-URI,
  inte maskinöversätts.

Transport (MCP_TRANSPORT i .env, se mcp_transport.py):
  stdio: MCP-klienten startar processen direkt.
  http:  Streamable HTTP på MCP_HOST:MCP_PORT bakom Bearer-autentisering;
         kräver MCP_API_KEY.
"""

import contextlib as _contextlib
import logging
import os
import sqlite3
import threading
from pathlib import Path
from typing import Any, NotRequired, Optional, TypedDict

import httpx
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

import db
import eduskunta_client as ed
import finlex_client as fx
from mcp_annotationer import CACHE_HINTAR, LASNING_DB, LASNING_EXTERN
from mcp_transport import starta

# ── Konfiguration ──────────────────────────────────────────────────────────────

_SCRIPT_DIR = Path(__file__).parent.resolve()

# Standardtak för fulltext i hämtverktygen. Materialet innehåller dokument på
# flera miljoner tecken — utan ett tak som gäller by default kan ett anrop
# överskrida MCP-protokollets storleksgräns och misslyckas helt. Anroparen kan
# alltid höja taket eller sätta 0 för hela texten.
FI_MAX_TECKEN = int(os.getenv("FI_MAX_TECKEN", "60000"))

# Embeddingmodeller (laddas lazily vid första semantiska sökning)
EMBEDDING_MODEL_FI = os.getenv("EMBEDDING_MODEL_FI", "TurkuNLP/sbert-cased-finnish-paraphrase")
EMBEDDING_MODEL_SV = os.getenv("EMBEDDING_MODEL_SV", "KBLab/sentence-bert-swedish-cased")
_modell_fi = None
_modell_sv = None
# Synkrona verktyg körs på arbetstrådar; låset hindrar att två samtidiga
# sökningar laddar samma modell var för sig.
_modell_lock = threading.Lock()

# Query-expansion
QUERY_EXPANSION_ENABLED     = os.getenv("QUERY_EXPANSION_ENABLED", "false").lower() == "true"
QUERY_EXPANSION_BASE_URL    = os.getenv("QUERY_EXPANSION_BASE_URL", "")
QUERY_EXPANSION_API_KEY     = os.getenv("QUERY_EXPANSION_API_KEY", "")
QUERY_EXPANSION_MODEL       = os.getenv("QUERY_EXPANSION_MODEL", "")
QUERY_EXPANSION_PROMPT_FILE = os.getenv(
    "QUERY_EXPANSION_PROMPT_FILE",
    str(_SCRIPT_DIR / "prompts" / "expansion_prompt.txt"),
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# ── MCP-server ─────────────────────────────────────────────────────────────────

mcp = MCPServer(
    "finland",
    version="1.2.0",
    cache_hints=CACHE_HINTAR,
    instructions=(
        "MCP-server för finsk riksdags- och rättsdata. "
        "Täcker Eduskunta (Finlands riksdag) och Finlex (finsk lagstiftning). "
        "Verktygen har prefixet fi_. "
        "SÖKTERMER: komma separerar termer och ger OR mellan dem; flera ord inom "
        "en term ger AND — alla orden måste förekomma. "
        "Dokumenten finns på både finska och svenska. "
        "För citat hämtas alltid den svenska källtexten, inte en maskinöversättning. "
        "SVARSSTORLEK: dokumenten här är bland de största i materialet — det största "
        "är över 4,7 miljoner tecken och ett sjuttiotal överskrider svarsgränsen. "
        "Eftersom både finsk och svensk version returneras fördubblas volymen. "
        "fi_hamta_dokument och fi_hamta_lag tar därför max_tecken och fran_tecken; "
        "använd hamta_fulltext=False när bara metadata behövs, och sök riktat med "
        "fi_sok_i_dokument i stället för att läsa hela texter. "
        "CITAT: citera aldrig ur en text där trunkerad_fi eller trunkerad_sv är true. "
        "VOTERINGAR: fi_hamta_aanestys läser live ur api.eduskunta.fi, som har "
        "voteringar fr.o.m. 2008-10-17; äldre voteringar finns inte där."
    ),
)


# ---------------------------------------------------------------------------
# Svarstyper
# ---------------------------------------------------------------------------
# Nästlade poster från källorna är heterogena och historiska poster saknar
# ofta fält, så de typas som dict[str, Any]. Skalen nedan är stabila.

class _Tvasprakig(TypedDict, total=False):
    """Fälten som _begransa_tvasprakig lägger till per språkversion."""
    fulltext_fi: str | None
    fulltext_sv: str | None
    tecken_totalt_fi: int
    tecken_totalt_sv: int
    trunkerad_fi: bool
    trunkerad_sv: bool
    fortsatt_fran_tecken_fi: int | None
    fortsatt_fran_tecken_sv: int | None
    las_vidare: str


class FiSokSvar(TypedDict):
    eduskunta: list[dict[str, Any]]
    finlex: dict[str, Any]
    fraga: str
    fraga_sprak: str
    expansion: NotRequired[str]


class EduskuntaSokSvar(TypedDict):
    treffar: list[dict[str, Any]]
    totalt: int
    start_index: int
    nasta_start_index: int | None
    expansion: NotRequired[str]


class FinlexSokSvar(TypedDict):
    fi: dict[str, Any]
    sv: dict[str, Any]
    fraga: str
    fraga_sprak: str
    expansion: NotRequired[str]


class DokumentSvar(_Tvasprakig):
    """Svar från fi_hamta_dokument: cachepost under dokument, annars live-fält."""
    kalla: str
    dokument: NotRequired[dict[str, Any]]
    edk_id: NotRequired[str | None]
    edk_id_sv: NotRequired[str | None]
    html_saatavilla: NotRequired[bool | None]
    metadata: NotRequired[dict[str, Any]]


class ArendeSvar(TypedDict):
    eduskuntatunnus: dict[str, Any]
    nimeke: dict[str, Any]
    tila: dict[str, Any]
    laadintapvm: Any
    paattymispvm: Any
    asiakirjatyyppi: dict[str, Any]
    asiakirjatyyppinimi: dict[str, Any]
    viimeisinKasittelyvaihe: dict[str, Any]
    vaalikausi: Any
    valtiopaivavuosi: Any
    keskeisetAsiakirjat: list[dict[str, Any]]
    kasittelyt: list[dict[str, Any]]
    kasittelynAsiakirjat_antal: int
    asiantuntijalausunnot: list[dict[str, Any]]


class LagSvar(_Tvasprakig):
    """Svar från fi_hamta_lag: cachepost under dokument, annars live-fält."""
    kalla: str
    dokument: NotRequired[dict[str, Any]]
    metadata: NotRequired[dict[str, Any]]
    akn_uri_fi: NotRequired[str]
    akn_uri_sv: NotRequired[str]


class ChunkTraff(TypedDict):
    chunk_index: int
    text: str | None
    likhet: float
    tecken_start: NotRequired[int | None]
    tecken_slut: NotRequired[int | None]


class SokIDokumentSvar(TypedDict):
    dokument_id: int
    edk_id: str | None
    eduskuntatunnus_fi: str | None
    eduskuntatunnus_sv: str | None
    kalla: str | None
    typ: str | None
    titel: str | None
    ar: int | None
    datum: str | None
    fraga_sprak: str
    antal_chunks: int
    antal_traffar: int
    traffar: list[ChunkTraff]


class VaalikaudetSvar(TypedDict):
    vaalikaudet: Any
    valtiopaivat: NotRequired[Any]


# ---------------------------------------------------------------------------
# Fel från källorna
# ---------------------------------------------------------------------------

@_contextlib.contextmanager
def _kallfel(kalla: str, ej_hittad: str | None = None):
    """
    Översätter HTTP-fel från källan till ToolError med ett begripligt meddelande.

    Utan översättning får klienten bara "Error executing tool" utan orsak.
    ej_hittad används som meddelande vid 404, då felet är ett okänt id och
    inte ett driftfel hos källan.
    """
    try:
        yield
    except ToolError:
        raise
    except httpx.HTTPStatusError as exc:
        kod = exc.response.status_code
        if kod == 404 and ej_hittad:
            raise ToolError(ej_hittad) from exc
        raise ToolError(f"{kalla} svarade med HTTP {kod}. Försök igen senare.") from exc
    except httpx.HTTPError as exc:
        raise ToolError(f"{kalla} svarar inte ({exc}). Försök igen senare.") from exc


try:
    import psycopg2 as _psycopg2
    _DB_FEL: tuple[type[Exception], ...] = (sqlite3.Error, _psycopg2.Error)
except ImportError:
    _DB_FEL = (sqlite3.Error,)


@_contextlib.contextmanager
def _dbfel():
    """
    Översätter databasfel till ToolError med orsak.

    Ett okänt undantag når klienten som "Error executing tool" utan orsak;
    ett nedstängt Postgres ska i stället synas som just det.
    """
    try:
        yield
    except _DB_FEL as exc:
        orsak = str(exc).strip().splitlines()[0] if str(exc).strip() else type(exc).__name__
        raise ToolError(
            f"Den lokala databasen svarar inte ({orsak}). Kontrollera att databasen "
            "i DATABASE_URL är igång och att schemat är initierat."
        ) from exc


def _las_cache(hamta, nyckel: str) -> dict | None:
    """
    Läser en cachepost; ett databasfel ger None så att anroparen hämtar live.

    Cachen är en genväg, inte en förutsättning för verktyg som kan hämta
    direkt från källan.
    """
    try:
        return hamta(nyckel)
    except _DB_FEL as exc:
        log.warning("Cacheläsning misslyckades, hämtar live: %s", exc)
        return None


def _cacha(**falt) -> None:
    """
    Sparar ett hämtat dokument i den lokala cachen.

    Cachen är en bekvämlighet: ett skrivfel (databasen nere eller
    skrivskyddad) loggas men hindrar inte att svaret levereras.
    """
    try:
        db.upsert_dokument(**falt)
    except Exception as exc:
        log.warning("Kunde inte cacha dokument: %s", exc)


# ---------------------------------------------------------------------------
# Textutdrag och trunkering
# ---------------------------------------------------------------------------

def _skar_ut(text, max_tecken: int, fran_tecken: int = 0) -> dict:
    """
    Skär ut ett textutdrag och redovisa alltid vad som kapats.

    Trunkering utan markering är ett tyst datafel — svaret ser ut att vara hela
    innehållet. max_tecken <= 0 betyder ingen trunkering. Klipper på ordgräns.
    """
    text   = text or ""
    totalt = len(text)
    start  = max(0, min(fran_tecken, totalt))
    rest   = text[start:]

    if max_tecken and max_tecken > 0 and len(rest) > max_tecken:
        utdrag    = rest[:max_tecken]
        brytpunkt = max(utdrag.rfind(" "), utdrag.rfind("\n"))
        if brytpunkt > max_tecken * 0.6:
            utdrag = utdrag[:brytpunkt]
        utdrag    = utdrag.rstrip()
        trunkerad = True
    else:
        utdrag    = rest
        trunkerad = False

    slut = start + len(utdrag)
    return {
        "text":                 utdrag,
        "tecken_totalt":        totalt,
        "tecken_visade":        len(utdrag),
        "trunkerad":            trunkerad,
        "fortsatt_fran_tecken": slut if slut < totalt else None,
    }


# ---------------------------------------------------------------------------
# Hjälpfunktioner — embedding och expansion
# ---------------------------------------------------------------------------

@_contextlib.contextmanager
def _tysta_stdout():
    """
    Leder fd 1 och 2 till logs/subprocess.log medan en modell laddas.

    sentence-transformers och dess beroenden skriver förlopp och varningar
    direkt till fd 1/2; omdirigeringen samlar dem i loggfilen. Den ändrar
    processglobala fildeskriptorer, så den körs bara under _modell_lock:
    två trådar som omdirigerar samtidigt kan annars återställa i fel ordning
    och lämna stdout pekande på loggfilen.
    """
    import os as _os2
    logs_mapp = _SCRIPT_DIR / "logs"
    logs_mapp.mkdir(parents=True, exist_ok=True)
    log_sokvag = str(logs_mapp / "subprocess.log")

    spara_out = _os2.dup(1)
    spara_err = _os2.dup(2)
    log_fd    = _os2.open(log_sokvag, _os2.O_WRONLY | _os2.O_APPEND | _os2.O_CREAT)
    try:
        _os2.dup2(log_fd, 1)
        _os2.dup2(log_fd, 2)
        yield
    finally:
        _os2.dup2(spara_out, 1)
        _os2.dup2(spara_err, 2)
        _os2.close(spara_out)
        _os2.close(spara_err)
        _os2.close(log_fd)


def _hamta_modell_fi():
    """Laddar TurkuNLP-modellen lat; utskrifter under inläsningen hamnar i loggfilen."""
    global _modell_fi
    if _modell_fi is None:
        with _modell_lock:
            if _modell_fi is None:
                from sentence_transformers import SentenceTransformer
                log.info("Laddar embeddingmodell (fi): %s", EMBEDDING_MODEL_FI)
                with _tysta_stdout():
                    _modell_fi = SentenceTransformer(EMBEDDING_MODEL_FI)
    return _modell_fi


def _hamta_modell_sv():
    """Laddar KBLab-modellen lat; utskrifter under inläsningen hamnar i loggfilen."""
    global _modell_sv
    if _modell_sv is None:
        with _modell_lock:
            if _modell_sv is None:
                from sentence_transformers import SentenceTransformer
                log.info("Laddar embeddingmodell (sv): %s", EMBEDDING_MODEL_SV)
                with _tysta_stdout():
                    _modell_sv = SentenceTransformer(EMBEDDING_MODEL_SV)
    return _modell_sv


def _embedda(text: str, sprak: str = "fi") -> list[float]:
    """Skapar en embedding för texten med rätt modell.

    Bara modellinläsningen omdirigeras (se _tysta_stdout); encode() körs
    utan förloppsindikator och skriver då inget till fd 1/2.
    """
    modell = _hamta_modell_sv() if sprak == "sv" else _hamta_modell_fi()
    vec = modell.encode(text, normalize_embeddings=True, show_progress_bar=False)
    return vec.tolist()


def _detektera_sprak(text: str) -> str:
    """
    Detekterar textens språk. Returnerar "fi" eller "sv".
    Använder langdetect om tillgängligt, annars enkel heuristik.
    """
    try:
        from langdetect import detect
        lang = detect(text)
        if lang in ("fi", "fi-FI"):
            return "fi"
        if lang in ("sv", "sv-SE", "sv-FI"):
            return "sv"
    except Exception:
        pass
    # Heuristik: vanliga svenska ord
    sv_ord = {"och", "att", "det", "är", "en", "ett", "på", "av", "för", "med"}
    ord_i_fragan = set(text.lower().split())
    if len(ord_i_fragan & sv_ord) >= 2:
        return "sv"
    return "fi"


def _expandera_fraga(fraga: str, sprak: str = "fi") -> tuple[str, str]:
    """
    Expanderar sökfrågan till juridiska termer via LLM.
    Returnerar (expanderad_fraga, expansion_logg).
    Expansion är valfri — aktiveras via QUERY_EXPANSION_ENABLED=true i .env.
    """
    if not QUERY_EXPANSION_ENABLED:
        return fraga, ""
    if not QUERY_EXPANSION_BASE_URL or not QUERY_EXPANSION_MODEL:
        return fraga, ""

    prompt_fil = Path(QUERY_EXPANSION_PROMPT_FILE)
    if not prompt_fil.exists():
        log.warning("Expansion-prompt saknas: %s", prompt_fil)
        return fraga, ""

    try:
        import httpx
        systemprompt = prompt_fil.read_text(encoding="utf-8")
        svar = httpx.post(
            f"{QUERY_EXPANSION_BASE_URL}/chat/completions",
            headers={"Authorization": f"Bearer {QUERY_EXPANSION_API_KEY}"},
            json={
                "model": QUERY_EXPANSION_MODEL,
                "messages": [
                    {"role": "system", "content": systemprompt},
                    {"role": "user", "content": f"Sökterm: {fraga}\nSpråk: {sprak}"},
                ],
                "max_tokens": 200,
            },
            timeout=10,
        )
        if svar.status_code == 200:
            expansion = svar.json()["choices"][0]["message"]["content"].strip()
            kombinerad = f"{fraga},{expansion}"
            return kombinerad, expansion
    except Exception as exc:
        log.warning("Query-expansion misslyckades: %s", exc)

    return fraga, ""


def _asiakirja_till_dict(doc: dict) -> dict:
    """Normaliserar ett asiakirja-träffobjekt från api.eduskunta.fi."""
    tunnus = doc.get("eduskuntatunnus")
    return {
        "kalla":               "eduskunta",
        "edk_id":              doc.get("edktunnus"),
        "eduskuntatunnus_fi":  tunnus.get("fi") if isinstance(tunnus, dict) else tunnus,
        "eduskuntatunnus_sv":  tunnus.get("sv") if isinstance(tunnus, dict) else None,
        "typ":                 doc.get("asiakirjatyyppikoodi"),
        "titel_fi":            doc.get("nimeketeksti") if doc.get("kielikoodi") == "fi" else None,
        "titel_sv":            doc.get("nimeketeksti") if doc.get("kielikoodi") == "sv" else None,
        "datum":               doc.get("laadintapvm"),
        "ar":                  doc.get("valtiopaivavuosi"),
        "html_saatavilla":     doc.get("htmlSaatavilla", False),
    }


def _valtiopaivaasia_till_dict(doc: dict) -> dict:
    """Normaliserar ett valtiopaivaasia-träffobjekt från api.eduskunta.fi."""
    tunnus  = doc.get("eduskuntatunnus", {})
    nimeke  = doc.get("nimeke", {})
    ar_val  = doc.get("valtiopaivavuosi")
    dat_val = doc.get("laadintapvm")
    return {
        "kalla":               "eduskunta",
        "edk_id":              None,
        "eduskuntatunnus_fi":  tunnus.get("fi") if isinstance(tunnus, dict) else tunnus,
        "eduskuntatunnus_sv":  tunnus.get("sv") if isinstance(tunnus, dict) else None,
        "typ":                 doc.get("asiatyyppikoodi"),
        "titel_fi":            nimeke.get("fi") if isinstance(nimeke, dict) else None,
        "titel_sv":            nimeke.get("sv") if isinstance(nimeke, dict) else None,
        "datum":               dat_val.get("fi") if isinstance(dat_val, dict) else dat_val,
        "ar":                  ar_val.get("fi") if isinstance(ar_val, dict) else ar_val,
        "html_saatavilla":     False,
    }


def _eduskunta_treff_till_dict(r: dict) -> dict:
    """Normaliserar ett sökresultat från api.eduskunta.fi till ett enhetligt format.

    API:et returnerar en wrapper per resultattyp där dokumentdatan ligger nästlad
    under en typstyrd nyckel. Separata normaliserare hanterar kategorispecifika
    fältnamn och typer — undviker att dicts hamnar i str-fält.
    """
    # Kända kategori-nycklar (whitelistade för att undvika _highlightResult etc.)
    _KATEGORI_NYCKLAR = {
        "asiakirja", "valtiopaivaasia", "aanestys", "kansanedustaja",
        "puheenvuoro", "tapahtuma", "cmsSivu", "sisaltosivu",
        "tiedote", "tiedosto", "yhteystieto",
    }
    doc = None
    hittad_kategori = ""
    for nyckel in _KATEGORI_NYCKLAR:
        if nyckel in r and isinstance(r[nyckel], dict):
            doc = r[nyckel]
            hittad_kategori = nyckel
            break
    if doc is None:
        log.warning(
            "_eduskunta_treff_till_dict: okänd resultatstruktur — inga kända kategori-nycklar "
            "hittades i: %s", list(r.keys())
        )
        return {"kalla": "eduskunta", "fel": "okänd_kategori", "rådata_nycklar": list(r.keys())}

    if hittad_kategori == "valtiopaivaasia":
        return _valtiopaivaasia_till_dict(doc)
    return _asiakirja_till_dict(doc)


# ---------------------------------------------------------------------------
# Verktyg
# ---------------------------------------------------------------------------

@mcp.tool(title="Sök i Eduskunta och Finlex", annotations=LASNING_EXTERN)
def fi_sok(
    fraga: str,
    max_treff: int = 10,
) -> FiSokSvar:
    """
    Aggregerad sökning över alla finska källor: Eduskunta och Finlex.

    Söker alltid i BÅDE finsk och svensk text och returnerar separata resultatlistor
    per språk. Eduskunta-sökning sker mot live-API (språkoberoende).

    Svaret innehåller:
      eduskunta         — riksdagsdokument från api.eduskunta.fi
      finlex.fi         — finska Finlex-träffar (FTS + semantisk; primärkälla för analys)
      finlex.sv         — svenska Finlex-träffar (FTS + semantisk; komplement för citat)
      fraga_sprak       — detekterat frågespråk

    Dokument som dyker upp i både fi och sv är säkra träffar. Övriga är komplementära
    och kan ge bredare täckning vid komplexa frågor.
    """
    fraga_sprak = _detektera_sprak(fraga)
    expanderad, expansion_logg = _expandera_fraga(fraga, fraga_sprak)

    # Eduskunta — live-API, språkoberoende
    with _kallfel("Eduskuntas API"):
        ed_svar = ed.sok(fraga=expanderad, kategori="asiakirja", max_treff=max_treff)
    ed_treffar = [_eduskunta_treff_till_dict(r) for r in ed_svar.get("results", [])]

    # Finlex FTS — finska och svenska parallellt
    with _dbfel():
        fts_fi = db.fts_sok(fraga=expanderad, sprak="fi", kalla_filter="finlex", max_treff=max_treff)
        fts_sv = db.fts_sok(fraga=expanderad, sprak="sv", kalla_filter="finlex", max_treff=max_treff)

    # Semantisk sökning — båda embeddingmodellerna
    sem_fi: list = []
    sem_sv: list = []
    if db.ar_postgres():
        try:
            sem_fi = db.vektor_sok(_embedda(fraga, "fi"), sprak="fi", max_treff=5)
        except Exception as exc:
            log.warning("Semantisk sökning (fi) misslyckades: %s", exc)
        try:
            sem_sv = db.vektor_sok(_embedda(fraga, "sv"), sprak="sv", max_treff=5)
        except Exception as exc:
            log.warning("Semantisk sökning (sv) misslyckades: %s", exc)

    svar: FiSokSvar = {
        "eduskunta":  ed_treffar,
        "finlex": {
            "fi": {"fts": fts_fi, "semantisk": sem_fi},
            "sv": {"fts": fts_sv, "semantisk": sem_sv},
        },
        "fraga":       fraga,
        "fraga_sprak": fraga_sprak,
    }
    if expansion_logg:
        svar["expansion"] = expansion_logg
    return svar


@mcp.tool(title="Sök i Eduskuntas riksdagsdokument", annotations=LASNING_EXTERN)
def fi_sok_eduskunta(
    fraga: Optional[str] = None,
    kategori: str = "asiakirja",
    typ: Optional[str] = None,
    fran_datum: Optional[str] = None,
    till_datum: Optional[str] = None,
    ar: Optional[int] = None,
    max_treff: int = 10,
    start_index: int = 0,
) -> EduskuntaSokSvar:
    """
    Strukturerad sökning i Eduskuntas riksdagsdokument (api.eduskunta.fi).

    Parametrar:
      fraga      — söktext (fritext)
      kategori   — asiakirja | valtiopaivaasia | aanestys | puheenvuoro | kansanedustaja
      typ        — dokumenttyp: HE, RP, KK, SSS, EV, RSv, PTK, LS, ...
                   (HE = hallituksen esitys/prop på fi, RP = regeringsproposition/prop på sv)
      fran_datum — YYYY-MM-DD
      till_datum — YYYY-MM-DD
      ar         — riksdagsår (valtiopaivavuosi), t.ex. 2024
      max_treff  — max antal resultat (default 10)
      start_index — paginering (0-baserat)

    Returnerar sökresultat med metadata. Fulltext hämtas via fi_hamta_dokument.
    """
    expanderad, expansion_logg = _expandera_fraga(fraga or "", "fi")

    villkor = []
    if typ:
        villkor.append({"property": "asiakirjatyyppikoodi", "stringValue": typ})
    if fran_datum or till_datum:
        datum_expr: dict = {"property": "laadintapvm"}
        if fran_datum:
            datum_expr["fromDate"] = fran_datum
        if till_datum:
            datum_expr["toDate"] = till_datum
        villkor.append(datum_expr)
    if ar:
        villkor.append({"property": "valtiopaivavuosi", "stringValue": str(ar)})

    expression = None
    if len(villkor) == 1:
        expression = villkor[0]
    elif len(villkor) > 1:
        expression = {"and": villkor}

    with _kallfel("Eduskuntas API"):
        ed_svar = ed.sok(
            fraga=expanderad if fraga else None,
            kategori=kategori,
            max_treff=max_treff,
            start_index=start_index,
            expression=expression,
        )

    treffar = [_eduskunta_treff_till_dict(r) for r in ed_svar.get("results", [])]
    svar: EduskuntaSokSvar = {
        "treffar":          treffar,
        "totalt":           ed_svar.get("searchMetadata", {}).get("totalResultCount", 0),
        "start_index":      start_index,
        "nasta_start_index": start_index + len(treffar) if len(treffar) == max_treff else None,
    }
    if expansion_logg:
        svar["expansion"] = expansion_logg
    return svar


@mcp.tool(title="Sök i lokala Finlex-databasen", annotations=LASNING_DB)
def fi_sok_finlex(
    fraga: str,
    finlex_typ: Optional[str] = None,
    fran_ar: Optional[int] = None,
    till_ar: Optional[int] = None,
    max_treff: int = 10,
) -> FinlexSokSvar:
    """
    FTS och semantisk sökning i den lokala Finlex-databasen.

    Söker alltid i BÅDE finsk och svensk text och returnerar separata resultatlistor.
    Finska är primärkällan för sökning och analys; svenska ger komplement vid
    komplexa frågor och används för citat.

    Parametrar:
      fraga      — söktext på finska eller svenska
      finlex_typ — "statute" | "statute-consolidated" | "government-proposal" | "treaty"
      fran_ar    — årsfilter från
      till_ar    — årsfilter till
      max_treff  — max antal resultat per språk (default 10)

    Svaret innehåller:
      fi — finska träffar (FTS + semantisk; primärkälla)
      sv — svenska träffar (FTS + semantisk; komplement)

    Dokument som dyker upp i båda är säkra träffar. Övriga är komplementära.

    OBS: Söker bara i bulk-synkade Finlex-dokument. Eduskunta-dokument som
    cachats via fi_hamta_dokument ingår inte i FTS-indexet.
    """
    fraga_sprak = _detektera_sprak(fraga)
    expanderad, expansion_logg = _expandera_fraga(fraga, fraga_sprak)

    # FTS — båda språken
    with _dbfel():
        fts_fi = db.fts_sok(
            fraga=expanderad, sprak="fi",
            kalla_filter="finlex", typ_filter=finlex_typ, ar_filter=fran_ar,
            max_treff=max_treff,
        )
        fts_sv = db.fts_sok(
            fraga=expanderad, sprak="sv",
            kalla_filter="finlex", typ_filter=finlex_typ, ar_filter=fran_ar,
            max_treff=max_treff,
        )

    # Semantisk sökning — båda embeddingmodellerna
    sem_fi: list = []
    sem_sv: list = []
    if db.ar_postgres():
        try:
            sem_fi = db.vektor_sok(
                _embedda(fraga, "fi"), sprak="fi", kalla_filter="finlex", max_treff=5
            )
        except Exception as exc:
            log.warning("Semantisk sökning (fi) misslyckades: %s", exc)
        try:
            sem_sv = db.vektor_sok(
                _embedda(fraga, "sv"), sprak="sv", kalla_filter="finlex", max_treff=5
            )
        except Exception as exc:
            log.warning("Semantisk sökning (sv) misslyckades: %s", exc)

    svar: FinlexSokSvar = {
        "fi":          {"fts": fts_fi, "semantisk": sem_fi},
        "sv":          {"fts": fts_sv, "semantisk": sem_sv},
        "fraga":       fraga,
        "fraga_sprak": fraga_sprak,
    }
    if expansion_logg:
        svar["expansion"] = expansion_logg
    return svar


@mcp.tool(title="Hämta riksdagsdokument med fulltext", annotations=LASNING_EXTERN)
def fi_hamta_dokument(
    edk_id: Optional[str] = None,
    eduskuntatunnus: Optional[str] = None,
    hamta_fulltext: bool = True,
    max_tecken: int = FI_MAX_TECKEN,
    fran_tecken: int = 0,
) -> DokumentSvar:
    """
    Hämtar metadata och fulltext för ett riksdagsdokument från api.eduskunta.fi.

    Hämtar HTML-fulltext om htmlSaatavilla=true, annars raw XML via redirect.
    Hämtar alltid både finsk och svensk version när båda finns. Dokumentet
    hämtas live varje gång; det lagras inte lokalt.

    Svaret innehåller alltid:
      fulltext_fi — finsk fulltext (för sökning och analys)
      fulltext_sv — svensk fulltext (för citat till användaren, None om saknas)

    Parametrar:
      max_tecken  — teckentak PER språkversion (0 = hela texten).
      fran_tecken — börja texten vid denna teckenposition, för att läsa vidare.

    STORLEK: dokumenten här är bland de största i hela materialet — det största
    är över 4,7 miljoner tecken, och ett sjuttiotal överskrider MCP-protokollets
    storleksgräns per svar. Eftersom både finsk och svensk version returneras
    fördubblas volymen. Sätt därför max_tecken för långa dokument, eller
    hamta_fulltext=False när bara metadata behövs, och sök riktat med
    fi_sok_i_dokument i stället för att läsa hela texten.

    Kapade texter bär fälten trunkerad_fi/trunkerad_sv, tecken_totalt_fi/_sv och
    fortsatt_fran_tecken. Citera aldrig ur en kapad text.

    Minst ett av edk_id eller eduskuntatunnus måste anges.
    """
    if not edk_id and not eduskuntatunnus:
        raise ToolError("Ange edk_id eller eduskuntatunnus.")

    # Hämta finskt primärdokument
    with _kallfel("Eduskuntas API"):
        if edk_id:
            try:
                meta = ed.hamta_asiakirja_metadata(edk_id)
            except ValueError as exc:
                raise ToolError(
                    f"Inget dokument hittades för edk_id {edk_id}. "
                    "Hämta id:t ur fi_sok_eduskunta eller ange eduskuntatunnus."
                ) from exc
        else:
            sok_svar = ed.hamta_asiakirja_via_eduskuntatunnus(eduskuntatunnus)
            treffar = sok_svar.get("results", [])
            if not treffar:
                raise ToolError(
                    f"Inget dokument hittades för beteckning {eduskuntatunnus}. "
                    "Kontrollera formatet, t.ex. 'HE 15/2026 vp' eller 'RP 15/2026 rd'."
                )
            # Välj finskt dokument som primär, annars första träffen
            fi_treff = next(
                (r.get("asiakirja") or r for r in treffar
                 if (r.get("asiakirja") or r).get("kielikoodi") == "fi"),
                treffar[0].get("asiakirja") or treffar[0],
            )
            # Söksvaret bär hela fulltexten i fullText; den hämtas separat nedan
            # och ska inte följa med i metadata (ett par hundra tusen tecken).
            meta = {k: v for k, v in fi_treff.items()
                    if k not in ("snippet", "fullText", "fullTextSnippet")}
            edk_id = meta.get("edktunnus")

    html_saatavilla = meta.get("htmlSaatavilla", False)
    fulltext_fi = None
    fulltext_sv = None

    # Hämta finsk fulltext
    if hamta_fulltext and edk_id:
        if html_saatavilla:
            html = ed.hamta_html_fulltext(edk_id)
            if html:
                fulltext_fi = ed.html_till_text(html)
        else:
            xml_url = ed.hamta_xml_redirect_url(edk_id)
            if xml_url:
                fulltext_fi = f"[XML tillgänglig via redirect: {xml_url}]"

    # Hämta svenskt syskondokument
    ed_tunnus = meta.get("eduskuntatunnus")
    ed_tunnus_str = (
        ed_tunnus.get("fi") or ed_tunnus.get("sv")
        if isinstance(ed_tunnus, dict)
        else ed_tunnus
    )

    sv_meta = None
    edk_id_sv = None
    if hamta_fulltext and ed_tunnus_str:
        try:
            sv_meta = ed.hamta_syskondokument_sv(ed_tunnus_str)
            if sv_meta:
                edk_id_sv = sv_meta.get("edktunnus")
                if edk_id_sv and sv_meta.get("htmlSaatavilla"):
                    html_sv = ed.hamta_html_fulltext(edk_id_sv)
                    if html_sv:
                        fulltext_sv = ed.html_till_text(html_sv)
                elif edk_id_sv:
                    xml_url_sv = ed.hamta_xml_redirect_url(edk_id_sv)
                    if xml_url_sv:
                        fulltext_sv = f"[XML tillgänglig via redirect: {xml_url_sv}]"
        except Exception as exc:
            log.warning("Kunde inte hämta sv syskondokument för %s: %s", ed_tunnus_str, exc)

    return {
        "kalla":           "eduskunta",
        "edk_id":          edk_id,
        "edk_id_sv":       edk_id_sv,
        "html_saatavilla": html_saatavilla,
        "metadata":        meta,
        **_begransa_tvasprakig(
            {"fulltext_fi": fulltext_fi, "fulltext_sv": fulltext_sv},
            max_tecken, fran_tecken,
        ),
    }


def _begransa_tvasprakig(d: dict, max_tecken: int, fran_tecken: int = 0) -> dict:
    """
    Tillämpar teckentaket på båda språkversionerna i ett dokumentsvar.

    Taket gäller per språk, eftersom fi och sv är två självständiga texter som
    båda kan citeras. Varje kapad text får egna redovisningsfält så att det
    framgår vilken version som är avkortad.
    """
    for nyckel, suffix in (("fulltext_fi", "fi"), ("fulltext_sv", "sv")):
        text = d.get(nyckel)
        if not text:
            continue
        utdrag = _skar_ut(text, max_tecken, fran_tecken)
        d[nyckel] = utdrag["text"]
        d[f"tecken_totalt_{suffix}"] = utdrag["tecken_totalt"]
        d[f"trunkerad_{suffix}"]     = utdrag["trunkerad"]
        if utdrag["trunkerad"]:
            d[f"fortsatt_fran_tecken_{suffix}"] = utdrag["fortsatt_fran_tecken"]
            d["las_vidare"] = (
                "Texten är kapad. Läs vidare med fran_tecken, eller sök riktat "
                "med fi_sok_i_dokument i stället för att läsa hela dokumentet."
            )
    return d


def _tvasprakigt(field) -> dict:
    """Returnerar {'fi': ..., 'sv': ...} om field är tvåspråkig dict, annars {'fi': field, 'sv': None}."""
    if isinstance(field, dict) and ("fi" in field or "sv" in field):
        return {"fi": field.get("fi"), "sv": field.get("sv")}
    return {"fi": field, "sv": None}


def _sammanfatta_keskeisetAsiakirjat(field) -> list[dict]:
    """Reducerar keskeisetAsiakirjat till en kompakt lista av dokumentreferenser."""
    if not isinstance(field, dict):
        return []
    fi_list = field.get("fi") or []
    if not isinstance(fi_list, list):
        return []
    return [
        {
            "edktunnus":        a.get("edktunnus"),
            "eduskuntatunnus":  a.get("eduskuntatunnus"),
            "asiakirjatyyppi":  a.get("asiakirjatyyppikoodi"),
            "typnamn":          a.get("asiakirjatyyppinimi"),
            "nimeke":           a.get("nimeketeksti"),
            "laadintapvm":      a.get("laadintapvm"),
            "valiokunta":       a.get("valiokuntanimi"),
            "htmlSaatavilla":   a.get("htmlSaatavilla", False),
        }
        for a in fi_list
    ]


def _sammanfatta_kasittelyt(field) -> list[dict]:
    """Reducerar kasittelyt-livscykeln till kompakta behandlingssteg."""
    if not isinstance(field, dict):
        return []
    fi_list = field.get("fi") or []
    if not isinstance(fi_list, list):
        return []
    return [
        {
            "tapahtumapvm":   k.get("tapahtumapvm"),
            "kasittelyvaihe": k.get("kasittelyvaihe"),
            "valiokunta":     (k.get("valiokunta") or {}).get("nimi"),
            "fraasi":         ((k.get("fraasi") or {}).get("fraasisisalto") or "").strip(),
        }
        for k in fi_list
    ]


def _sammanfatta_asiantuntijalausunnot(field) -> list[dict]:
    """Reducerar expertutlåtanden till kompakta referenser."""
    if not isinstance(field, dict):
        return []
    fi_list = field.get("fi") or []
    if not isinstance(fi_list, list):
        return []
    return [
        {
            "edktunnus":     a.get("edktunnus"),
            "nimeke":        a.get("nimeketeksti"),
            "laadintapvm":   a.get("laadintapvm"),
            "htmlSaatavilla": a.get("htmlSaatavilla", False),
        }
        for a in fi_list
    ]


@mcp.tool(title="Hämta riksdagsärende med historik", annotations=LASNING_EXTERN)
def fi_hamta_arende(
    tunnus: str,
) -> ArendeSvar:
    """
    Hämtar fullständig ärendehistorik (valtiopaivaasia / statsdagsärende) från
    Eduskuntas API — ärende-metadata plus livscykel, kärnedokument,
    behandlingsstegens dokument och expertutlåtanden från hearings.

    Avsett som första steg i utredningsarbete: hitta ärendet via
    fi_sok_eduskunta(kategori="valtiopaivaasia", ...), läs hela tråden via
    fi_hamta_arende, hämta sedan fulltext för enskilda dokument via
    fi_hamta_dokument(eduskuntatunnus=...) eller fi_hamta_dokument(edk_id=...).

    Parametrar:
      tunnus — Ärendets beteckning, t.ex. "HE 15/2026 vp" (finsk form,
               RP-formen funkar också på svensk sida). Hämta från
               eduskuntatunnus-fältet i sökresultat.

    Returnerar:
      eduskuntatunnus  — {fi, sv}-beteckning (HE/RP, EV/RSv, ...)
      nimeke           — {fi, sv}-titel
      tila             — {fi, sv}-status ("Käsittelyssä", "Hyväksytty", ...)
      laadintapvm      — start-/inlämningsdatum
      paattymispvm     — slutdatum (null om pågående)
      asiakirjatyyppi  — {fi, sv}-typkod (HE, KAA, ...)
      viimeisinKasittelyvaihe — {fi, sv}-senaste behandlingssteg
      vaalikausi       — valperiod (t.ex. "2023-2026")
      valtiopaivavuosi — riksdagsår

      keskeisetAsiakirjat   — lista med kärnedokument (lagförslag, utskotts-
                              utlåtanden, slutlig lagtext). Använd `edktunnus`
                              eller `eduskuntatunnus` som indata till
                              fi_hamta_dokument för fulltext.
      kasittelyt            — komplett livscykel (kan vara 20–50 steg för
                              stora ärenden — varje plenardebatt, varje
                              utskottsmöte, varje votering)
      kasittelynAsiakirjat_antal — antal dokument i behandlingsstegen
      asiantuntijalausunnot — expertutlåtanden från utskottens hearings
                              (för djupare proceduriell analys)

    Ett okänt ärende ger ett fel med exempel på giltiga format.

    Exempel: fi_hamta_arende(tunnus="HE 15/2026 vp")
    """
    ej_hittad = (
        f"Ärendet '{tunnus}' hittades inte. Kontrollera formatet, "
        "t.ex. 'HE 15/2026 vp' eller 'KAA 5/2024 vp'."
    )
    with _kallfel("Eduskuntas API", ej_hittad=ej_hittad):
        svar = ed.hamta_valtiopaivaasia(tunnus)
    if not svar or not svar.get("eduskuntatunnus"):
        raise ToolError(ej_hittad)

    # Räkna kasittelynAsiakirjat utan att kopiera hela strukturen — den kan
    # vara djupt nästlad och innehålla många bilagor per behandlingssteg.
    kas_dok = svar.get("kasittelynAsiakirjat", {})
    kas_dok_antal = 0
    if isinstance(kas_dok, dict):
        fi_list = kas_dok.get("fi") or []
        if isinstance(fi_list, list):
            kas_dok_antal = sum(len(grupp) if isinstance(grupp, list) else 1
                                for grupp in fi_list)

    return {
        "eduskuntatunnus":         _tvasprakigt(svar.get("eduskuntatunnus")),
        "nimeke":                  _tvasprakigt(svar.get("nimeke")),
        "tila":                    _tvasprakigt(svar.get("tila")),
        "laadintapvm":             _tvasprakigt(svar.get("laadintapvm")).get("fi"),
        "paattymispvm":            _tvasprakigt(svar.get("paattymispvm")).get("fi"),
        "asiakirjatyyppi":         _tvasprakigt(svar.get("asiakirjatyyppikoodi")),
        "asiakirjatyyppinimi":     _tvasprakigt(svar.get("asiakirjatyyppinimi")),
        "viimeisinKasittelyvaihe": _tvasprakigt(svar.get("viimeisinKasittelyvaihe")),
        "vaalikausi":              _tvasprakigt(svar.get("vaalikausitunnus")).get("fi"),
        "valtiopaivavuosi":        _tvasprakigt(svar.get("valtiopaivavuosi")).get("fi"),

        "keskeisetAsiakirjat":         _sammanfatta_keskeisetAsiakirjat(svar.get("keskeisetAsiakirjat")),
        "kasittelyt":                  _sammanfatta_kasittelyt(svar.get("kasittelyt")),
        "kasittelynAsiakirjat_antal":  kas_dok_antal,
        "asiantuntijalausunnot":       _sammanfatta_asiantuntijalausunnot(svar.get("asiantuntijalausunnot")),
    }


@mcp.tool(title="Hämta lag eller proposition ur Finlex", annotations=LASNING_EXTERN)
def fi_hamta_lag(
    ar: Optional[int] = None,
    nummer: Optional[str] = None,
    hierarki: str = "act",
    typ: str = "statute",
    myndighetskod: Optional[str] = None,
    akn_uri_fi: Optional[str] = None,
    max_tecken: int = FI_MAX_TECKEN,
    fran_tecken: int = 0,
) -> LagSvar:
    """
    Hämtar en specifik lag, proposition eller förordning från Finlex (AKN XML).

    Hämtar alltid både finsk och svensk version när båda finns.

    Parametrar (antingen akn_uri_fi ELLER ar+nummer):
      akn_uri_fi    — AKN URI från fi_sok_finlex-resultat, t.ex.
                      "https://opendata.finlex.fi/.../act/statute/2024/1/fin@"
      ar            — utgivningsår, t.ex. 2024
      nummer        — lagns nummer, t.ex. "123"
      hierarki      — "act" (lagar) | "doc" (propositioner, fördrag)
      typ           — "statute" | "statute-consolidated" | "government-proposal" |
                      "treaty" | "authority-regulation". Med ar+nummer ger
                      "statute-consolidated" den senaste konsoliderade lydelsen;
                      "statute" ger lagen i ursprunglig lydelse.
      myndighetskod — krävs för authority-regulation, t.ex. "national-audit-office-of-finland"

    Svaret innehåller:
      fulltext_fi — finsk lagtext (för sökning och analys)
      fulltext_sv — svensk lagtext (för citat till användaren, None om saknas)
    """
    if akn_uri_fi:
        # Parsa ar, nummer, hierarki, typ ur AKN URI. Språkdelen kan bära en
        # version efter @ (fin@20180817), så den sorteras bort på @-tecknet.
        stig = akn_uri_fi.replace(fx.API_BASE, "")
        delar = [d for d in stig.split("/") if d and d not in ("akn", "fi") and "@" not in d]
        # delar = [hierarki, typ, (myndighetskod,) ar, nummer]
        if len(delar) >= 4:
            hierarki = delar[0]
            typ      = delar[1]
            if len(delar) == 5:
                myndighetskod = delar[2]
                ar     = int(delar[3])
                nummer = delar[4]
            else:
                myndighetskod = None
                ar     = int(delar[2])
                nummer = delar[3]
        akn_uri_sv = fx.byt_sprak_i_uri(akn_uri_fi, fx.SPRAK_SV)
    elif ar is not None and nummer is not None:
        # Konsoliderad lagtext finns i tidsversioner (fin@20180817 = lydelsen
        # efter ändringslagen 2018/817). Den oversionerade adressen fin@ finns
        # bara för lagar som aldrig ändrats; för övriga ger den 404. Finlex
        # löser upp versionen "latest" till den senaste lydelsen.
        version = "latest" if typ == "statute-consolidated" else ""

        def _bygg_uri(sprak: str) -> str:
            if myndighetskod:
                return f"{fx.API_BASE}/akn/fi/{hierarki}/{typ}/{myndighetskod}/{ar}/{nummer}/{sprak}{version}"
            return f"{fx.API_BASE}/akn/fi/{hierarki}/{typ}/{ar}/{nummer}/{sprak}{version}"
        akn_uri_fi = _bygg_uri("fin@")
        akn_uri_sv = _bygg_uri("swe@")
    else:
        raise ToolError("Ange antingen akn_uri_fi eller både ar och nummer.")

    # Kolla cache — returnera om båda finns
    cachad = _las_cache(db.hamta_dokument_via_akn_uri, akn_uri_fi)
    if cachad and cachad.get("fulltext_fi") and cachad.get("fulltext_sv"):
        # Trunkera i cachad-dicten själv. Att lägga kapade kopior bredvid en
        # orörd dokument-post hjälper inte — hela texten följer ändå med i svaret.
        cachad = _begransa_tvasprakig(dict(cachad), max_tecken, fran_tecken)
        return {
            "kalla":       "cache",
            "dokument":    cachad,
            "fulltext_fi": cachad.get("fulltext_fi"),
            "fulltext_sv": cachad.get("fulltext_sv"),
        }

    # Hämta finska versionen
    def _hamta(uri: str):
        try:
            return fx.hamta_akn_dokument(uri, strikt=True)
        except fx.FinlexOtillganglig as exc:
            raise ToolError(
                f"Finlex svarar inte ({exc}). Dokumentet kan finnas; försök igen senare."
            ) from exc

    rot_fi = _hamta(akn_uri_fi)
    meta      = None
    fulltext_fi = None
    fulltext_sv = None

    if rot_fi is not None:
        meta        = fx.parsad_akn_metadata(rot_fi)
        fulltext_fi = fx.extrahera_fulltext(rot_fi)
    else:
        log.warning("Finsk version saknas för %s/%s (%s)", ar, nummer, typ)

    # Hämta svenska versionen
    rot_sv = _hamta(akn_uri_sv)
    if rot_sv is not None:
        if meta is None:
            meta = fx.parsad_akn_metadata(rot_sv)
        fulltext_sv = fx.extrahera_fulltext(rot_sv)
    else:
        log.debug("Svensk version saknas för %s/%s (%s) — kan saknas för enspråkiga lagar", ar, nummer, typ)

    if meta is None:
        fel = f"Finlex har inget dokument {ar}/{nummer} av typen {typ}."
        if typ == "statute-consolidated":
            fel += (
                " Alla lagar har inte konsoliderad lydelse i Finlex öppna data. "
                'Prova typ="statute" för den ursprungliga lagtexten.'
            )
        raise ToolError(fel)

    # Ersätt "latest" med den version Finlex faktiskt levererade, så att
    # cacheposten och svaret pekar på en bestämd lydelse.
    eli = meta.get("eli") or ""
    if akn_uri_fi.endswith("@latest") and eli.startswith("/akn/"):
        faktisk = fx.API_BASE + eli
        akn_uri_fi = fx.byt_sprak_i_uri(faktisk, fx.SPRAK_FI)
        akn_uri_sv = fx.byt_sprak_i_uri(faktisk, fx.SPRAK_SV)

    # Bara metadata sparas. Lagtexten hämtas live nästa gång också; lokalt
    # behövs bara chunks och embeddings för sökningen.
    _cacha(
        kalla="finlex",
        akn_uri_fi=akn_uri_fi,
        akn_uri_sv=akn_uri_sv,
        eli=meta.get("eli"),
        typ=typ,
        finlex_hierarki=hierarki,
        finlex_typ=typ,
        titel_fi=meta.get("titel_fi"),
        titel_sv=meta.get("titel_sv"),
        ar=meta.get("ar") or ar,
        nummer=meta.get("nummer") or nummer,
    )

    return {
        "kalla":      "finlex",
        "metadata":   meta,
        "akn_uri_fi": akn_uri_fi,
        "akn_uri_sv": akn_uri_sv,
        **_begransa_tvasprakig(
            {"fulltext_fi": fulltext_fi, "fulltext_sv": fulltext_sv},
            max_tecken, fran_tecken,
        ),
    }


def _summarisera_aanestykset(radata: dict | list) -> dict:
    """
    Omvandlar rådata från Eduskuntas voteringsendpoint till en kompakt sammanfattning.

    API:et returnerar list-av-listor: yttre = sessioner, inre = voteringar per session.
    Varje votering innehåller bl.a. 'aanestystapahtumat' (individuella röster, ~200 poster)
    vilket ger enorma svar. Funktionen plattar ut strukturen och returnerar en rad per
    votering med bara metadata + 'aanestystulos' (aggregerade resultat).

    Enskild votering via aanestystunnus returneras oförändrad (liten och specifik).
    """
    # Normalisera till platt lista av votering-dict:ar
    platt: list[dict] = []

    def _platta(obj):
        if isinstance(obj, list):
            for item in obj:
                _platta(item)
        elif isinstance(obj, dict):
            platt.append(obj)

    _platta(radata)

    if not platt:
        return {"voteringar": [], "antal": 0}

    sammanfattning = []
    for v in platt:
        # Extrahera rubrik (tvåspråkig)
        otsikko = v.get("aanestysotsikko") or {}
        titel_sv = otsikko.get("sv") if isinstance(otsikko, dict) else str(otsikko)
        titel_fi = otsikko.get("fi") if isinstance(otsikko, dict) else None

        dagordning = v.get("paivajarjestyksenotsikko") or {}
        session_sv = dagordning.get("sv") if isinstance(dagordning, dict) else str(dagordning)

        sammanfattning.append({
            "aanestystunnus": v.get("id"),
            "istuntopvm":     v.get("istuntopvm"),
            "titel_sv":       titel_sv,
            "titel_fi":       titel_fi,
            "session":        session_sv,
            "tulos":          v.get("aanestystulos"),       # aggregerade ja/nej/frånvaro
            "hallitus_vs_opposition": v.get("hallitusoppositioJakaumat"),
            "eduskuntaryhmat": v.get("eduskuntaryhmaJakaumat"),
            # aanestystapahtumat (individuella röster) utelämnas avsiktligt
        })

    sammanfattning.sort(key=lambda x: x.get("istuntopvm") or "", reverse=True)
    return {
        "voteringar": sammanfattning,
        "antal":      len(sammanfattning),
        "not":        "Individuella ledamotsröster utelämnade. Använd aanestystunnus för full rosterdata.",
    }


@mcp.tool(title="Hämta voteringsresultat", annotations=LASNING_EXTERN)
def fi_hamta_aanestys(
    aanestystunnus: Optional[str] = None,
    eduskuntatunnus: Optional[str] = None,
    istuntotunnus: Optional[str] = None,
    senaste: bool = False,
) -> dict[str, Any]:
    """
    Hämtar voteringsresultat från Eduskunta.

    Parametrar:
      aanestystunnus  — enskild votering: "{vpvuosi}-{istuntonr}-{aanestysnr}", t.ex. "2025-92-2"
      eduskuntatunnus — alla voteringar för ett ärende, t.ex. "HE 15/2026 vp"
      istuntotunnus   — alla voteringar i en session: "{vpvuosi}-{istuntonr}", t.ex. "2025-92"
      senaste         — om True returneras de 100 senaste voteringsresultaten (ignorerar övriga)

    Täckning: API:t har voteringar fr.o.m. plenum 94/2008 (2008-10-17).
    Äldre voteringar finns inte här.

    Minst ett argument måste anges.
    """
    ej_hittad = (
        "Ingen votering hittades. Kontrollera formatet (t.ex. '2025-92-2', "
        "'2025-92' eller 'HE 15/2026 vp'). Eduskuntas API har voteringar "
        "fr.o.m. 2008-10-17; äldre voteringar finns inte där."
    )
    with _kallfel("Eduskuntas API", ej_hittad=ej_hittad):
        if senaste:
            return _summarisera_aanestykset(ed.hamta_uusimmat_aanestykset())
        if aanestystunnus:
            return ed.hamta_aanestys(aanestystunnus)
        if eduskuntatunnus:
            return _summarisera_aanestykset(ed.hamta_asian_aanestykset(eduskuntatunnus))
        if istuntotunnus:
            return _summarisera_aanestykset(ed.hamta_istunnon_aanestykset(istuntotunnus))
    raise ToolError("Ange aanestystunnus, eduskuntatunnus, istuntotunnus eller senaste=True.")


# Tak för live-indexering. Ett dokument på 1,2 miljoner tecken ger ungefär
# 1 500 stycken; fler än så tar för lång tid att embedda i ett verktygsanrop.
FI_MAX_CHUNKS_LIVE = int(os.getenv("FI_MAX_CHUNKS_LIVE", "1500"))


def _indexera_eduskunta(edk_id: str | None, eduskuntatunnus: str | None, sprak: str) -> int:
    """
    Hämtar ett riksdagsdokument live, chunkar och embeddar det på ett språk.

    Sparar dokumentets metadata och chunks (text, embedding, offsets), inte
    råtexten. Returnerar dokumentets id i databasen.
    """
    from chunkning import chunka_text

    with _kallfel("Eduskuntas API", ej_hittad="Dokumentet hittades inte i Eduskuntas API."):
        if edk_id:
            try:
                meta = ed.hamta_asiakirja_metadata(edk_id)
            except ValueError as exc:
                raise ToolError(f"Inget dokument hittades för edk_id {edk_id}.") from exc
        else:
            treffar = ed.hamta_asiakirja_via_eduskuntatunnus(eduskuntatunnus).get("results", [])
            if not treffar:
                raise ToolError(
                    f"Inget dokument hittades för beteckning {eduskuntatunnus}. "
                    "Kontrollera formatet, t.ex. 'HE 15/2026 vp' eller 'RP 15/2026 rd'."
                )
            docs = [r.get("asiakirja") or r for r in treffar]
            meta = next((d for d in docs if d.get("kielikoodi") == "fi"), docs[0])
        edk_fi = meta.get("edktunnus")
        tunnus = meta.get("eduskuntatunnus")
        tunnus = (tunnus.get("fi") or tunnus.get("sv")) if isinstance(tunnus, dict) else tunnus

        if sprak == "fi":
            text = ed.html_till_text(ed.hamta_html_fulltext(edk_fi) or "") if meta.get("htmlSaatavilla") else ""
            titel = meta.get("nimeketeksti")
        else:
            sv = ed.hamta_syskondokument_sv(tunnus) if tunnus else None
            text = ""
            titel = sv.get("nimeketeksti") if sv else None
            if sv and sv.get("htmlSaatavilla") and sv.get("edktunnus"):
                text = ed.html_till_text(ed.hamta_html_fulltext(sv["edktunnus"]) or "")

    if not text:
        spraknamn = "finska" if sprak == "fi" else "svenska"
        raise ToolError(
            f"Dokumentet saknar text på {spraknamn} i HTML-form hos Eduskunta och kan "
            "inte sökas semantiskt. Ställ frågan på det andra språket, eller läs "
            "dokumentet med fi_hamta_dokument."
        )
    chunks = chunka_text(text)
    if not chunks:
        raise ToolError("Dokumentets text är för kort för semantisk sökning; läs det med fi_hamta_dokument.")
    if len(chunks) > FI_MAX_CHUNKS_LIVE:
        raise ToolError(
            f"Dokumentet ger {len(chunks)} stycken, fler än taket {FI_MAX_CHUNKS_LIVE} för "
            "indexering i ett anrop. Läs det med fi_hamta_dokument och fran_tecken, "
            "eller höj FI_MAX_CHUNKS_LIVE."
        )

    modell = _hamta_modell_sv() if sprak == "sv" else _hamta_modell_fi()
    texter = [f"{titel}\n\n{c['text']}" if titel else c["text"] for c in chunks]
    embeddings = modell.encode(texter, batch_size=32, normalize_embeddings=True,
                               show_progress_bar=False)

    with _dbfel():
        dok_id = db.upsert_dokument(
            kalla="eduskunta",
            edk_id=edk_fi,
            eduskuntatunnus_fi=tunnus,
            typ=meta.get("asiakirjatyyppikoodi"),
            titel_fi=meta.get("nimeketeksti") if meta.get("kielikoodi") == "fi" else None,
            titel_sv=titel if sprak == "sv" else None,
            ar=int(meta["valtiopaivavuosi"]) if meta.get("valtiopaivavuosi") else None,
            datum=meta.get("laadintapvm"),
            html_saatavilla=bool(meta.get("htmlSaatavilla")),
        )
        if not dok_id or dok_id < 0:
            dok_id = db.hamta_dokument_via_edk_id(edk_fi)["id"]
        db.spara_chunks(dok_id, chunks, embeddings, sprak)
    log.info("Indexerade %s (%s): %d stycken", edk_fi, sprak, len(chunks))
    return dok_id


@mcp.tool(title="Sök semantiskt i ett dokument", annotations=LASNING_EXTERN)
def fi_sok_i_dokument(
    fraga: str,
    edk_id: Optional[str] = None,
    eduskuntatunnus: Optional[str] = None,
    max_treff: int = 5,
) -> SokIDokumentSvar:
    """
    Semantisk sökning via pgvector inom ett enskilt riksdagsdokument.

    Används för att hitta specifika stycken utan att läsa hela fulltexten.
    Kräver PostgreSQL med pgvector. Är dokumentet inte indexerat på frågans
    språk hämtas det live, delas i stycken och embeddas vid första sökningen
    (tar från några sekunder upp till någon minut för mycket långa dokument);
    därefter finns styckena lokalt. Dokumentets råtext sparas inte.

    Parametrar:
      fraga           — vad du söker efter, på finska eller svenska
      edk_id          — dokumentets edktunnus, t.ex. "EDK-2026-AK-8746"
                        (returneras av fi_sok_eduskunta och fi_hamta_dokument)
      eduskuntatunnus — riksdagsbeteckning, t.ex. "HE 15/2026 vp" eller "RP 15/2026 rd"
                        (antingen fi- eller sv-form fungerar)
      max_treff       — max antal chunk-träffar att returnera (standard 5)

    Minst ett av edk_id eller eduskuntatunnus måste anges.

    Returnerar dokumentmetadata + lista med matchande chunk-träffar sorterade
    efter semantisk likhet, med chunk_index, text, likhetspoäng och styckets
    position (tecken_start/tecken_slut) i texten från fi_hamta_dokument.

    Typiskt arbetsflöde:
      1. fi_sok_eduskunta(fraga=...) → identifiera dokument, notera edk_id
      2. fi_sok_i_dokument(fraga=..., edk_id=...) → hitta relevanta stycken
      3. fi_hamta_dokument(edk_id=...) → hämta fulltext vid behov
    """
    if not edk_id and not eduskuntatunnus:
        raise ToolError("Ange edk_id eller eduskuntatunnus.")

    if not db.ar_postgres():
        raise ToolError("Semantisk sökning kräver PostgreSQL med pgvector; SQLite-läget stöds inte.")

    sprak = _detektera_sprak(fraga)

    # Slå upp dokumentet; indexera det live om det saknas på frågans språk.
    with _dbfel():
        if edk_id:
            cachad = db.hamta_dokument_via_edk_id(edk_id)
        else:
            cachad = db.hamta_dokument_via_eduskuntatunnus(eduskuntatunnus)
        klart = bool(cachad) and db.antal_chunks_med_embedding(cachad["id"], sprak) > 0
    dokument_id = cachad["id"] if klart else _indexera_eduskunta(edk_id, eduskuntatunnus, sprak)

    try:
        embedding = _embedda(fraga, sprak)
    except Exception as exc:
        log.error("fi_sok_i_dokument: embedding misslyckades: %s", exc)
        raise ToolError(f"Embeddingmodellen kunde inte köras: {exc}") from exc

    with _dbfel():
        svar = db.vektor_sok_i_dokument(
            dokument_id=dokument_id,
            embedding=embedding,
            sprak=sprak,
            max_treff=max_treff,
        )
    if "fel" in svar:
        fel = svar["fel"]
        if svar.get("antal_chunks") == 0:
            fel += " Läs dokumentet med fi_hamta_dokument i stället."
        raise ToolError(fel)
    return svar


@mcp.tool(title="Lista valperioder och riksmöten", annotations=LASNING_EXTERN)
def fi_lista_vaalikaudet(inkludera_riksmoten: bool = False) -> VaalikaudetSvar:
    """
    Listar finska valperioder (fr.o.m. 1907) och optionellt riksmöten.

    Parametrar:
      inkludera_riksmoten — om True inkluderas alla 128+ riksmöten (fr.o.m. 1907)
    """
    with _kallfel("Eduskuntas API"):
        svar: VaalikaudetSvar = {"vaalikaudet": ed.hamta_vaalikaudet()}
        if inkludera_riksmoten:
            svar["valtiopaivat"] = ed.hamta_valtiopaivat()
    return svar


# ---------------------------------------------------------------------------
# Startpunkt
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    (_SCRIPT_DIR / "logs").mkdir(parents=True, exist_ok=True)
    starta(mcp, standardport=8005, initiera=db.init_db)
