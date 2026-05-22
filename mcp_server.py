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

Transport-lägen (styrs via MCP_TRANSPORT i .env):
  stdio (standard): MCP-klienten startar processen direkt.
  http: Servern lyssnar på MCP_HOST:MCP_PORT med Bearer-token-autentisering.
"""

import contextlib as _contextlib
import logging
import os
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

import eduskunta_client as ed
import finlex_client as fx
import db

load_dotenv(Path(__file__).parent / ".env")

# ── Konfiguration ──────────────────────────────────────────────────────────────

_SCRIPT_DIR = Path(__file__).parent.resolve()

MCP_TRANSPORT = os.getenv("MCP_TRANSPORT", "stdio").lower()
MCP_HOST      = os.getenv("MCP_HOST",      "127.0.0.1")
MCP_PORT      = int(os.getenv("MCP_PORT",  "8005"))
MCP_API_KEY   = os.getenv("MCP_API_KEY",   "")

# Embeddingmodeller (laddas lazily vid första semantiska sökning)
EMBEDDING_MODEL_FI = os.getenv("EMBEDDING_MODEL_FI", "TurkuNLP/sbert-cased-finnish-paraphrase")
EMBEDDING_MODEL_SV = os.getenv("EMBEDDING_MODEL_SV", "KBLab/sentence-bert-swedish-cased")
_modell_fi = None
_modell_sv = None

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

mcp = FastMCP(
    "finland",
    instructions=(
        "MCP-server för finsk riksdags- och rättsdata. "
        "Täcker Eduskunta (Finlands riksdag) och Finlex (finsk lagstiftning). "
        "Verktygen har prefixet fi_. "
        "Söktermen kan innehålla kommaseparerade ord — de tolkas som OR-logik. "
        "Dokumenten finns på både finska och svenska. "
        "För citat hämtas alltid den svenska källtexten, inte en maskinöversättning."
    ),
)


# ---------------------------------------------------------------------------
# Hjälpfunktioner — embedding och expansion
# ---------------------------------------------------------------------------

@_contextlib.contextmanager
def _tysta_stdout():
    """
    OS-nivå FD-omdirigering skyddar MCP stdio-protokollet från oavsiktliga
    utskrifter under sentence-transformers-modell-laddning.
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
    """Laddar TurkuNLP-modellen lazily (skyddad mot FD 1-läckage)."""
    global _modell_fi
    if _modell_fi is None:
        from sentence_transformers import SentenceTransformer
        log.info("Laddar embeddingmodell (fi): %s", EMBEDDING_MODEL_FI)
        with _tysta_stdout():
            _modell_fi = SentenceTransformer(EMBEDDING_MODEL_FI)
    return _modell_fi


def _hamta_modell_sv():
    """Laddar KBLab-modellen lazily (skyddad mot FD 1-läckage)."""
    global _modell_sv
    if _modell_sv is None:
        from sentence_transformers import SentenceTransformer
        log.info("Laddar embeddingmodell (sv): %s", EMBEDDING_MODEL_SV)
        with _tysta_stdout():
            _modell_sv = SentenceTransformer(EMBEDDING_MODEL_SV)
    return _modell_sv


def _embedda(text: str, sprak: str = "fi") -> list[float]:
    """Skapar en embedding för texten med rätt modell."""
    with _tysta_stdout():
        if sprak == "sv":
            vec = _hamta_modell_sv().encode(text, normalize_embeddings=True)
        else:
            vec = _hamta_modell_fi().encode(text, normalize_embeddings=True)
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

@mcp.tool()
def fi_sok(
    fraga: str,
    max_treff: int = 10,
) -> dict:
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
    ed_svar    = ed.sok(fraga=expanderad, kategori="asiakirja", max_treff=max_treff)
    ed_treffar = [_eduskunta_treff_till_dict(r) for r in ed_svar.get("results", [])]

    # Finlex FTS — finska och svenska parallellt
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

    svar = {
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


@mcp.tool()
def fi_sok_eduskunta(
    fraga: Optional[str] = None,
    kategori: str = "asiakirja",
    typ: Optional[str] = None,
    fran_datum: Optional[str] = None,
    till_datum: Optional[str] = None,
    ar: Optional[int] = None,
    max_treff: int = 10,
    start_index: int = 0,
) -> dict:
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

    ed_svar = ed.sok(
        fraga=expanderad if fraga else None,
        kategori=kategori,
        max_treff=max_treff,
        start_index=start_index,
        expression=expression,
    )

    treffar = [_eduskunta_treff_till_dict(r) for r in ed_svar.get("results", [])]
    svar = {
        "treffar":          treffar,
        "totalt":           ed_svar.get("searchMetadata", {}).get("totalResultCount", 0),
        "start_index":      start_index,
        "nasta_start_index": start_index + len(treffar) if len(treffar) == max_treff else None,
    }
    if expansion_logg:
        svar["expansion"] = expansion_logg
    return svar


@mcp.tool()
def fi_sok_finlex(
    fraga: str,
    finlex_typ: Optional[str] = None,
    fran_ar: Optional[int] = None,
    till_ar: Optional[int] = None,
    max_treff: int = 10,
) -> dict:
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

    svar = {
        "fi":          {"fts": fts_fi, "semantisk": sem_fi},
        "sv":          {"fts": fts_sv, "semantisk": sem_sv},
        "fraga":       fraga,
        "fraga_sprak": fraga_sprak,
    }
    if expansion_logg:
        svar["expansion"] = expansion_logg
    return svar


@mcp.tool()
def fi_hamta_dokument(
    edk_id: Optional[str] = None,
    eduskuntatunnus: Optional[str] = None,
    hamta_fulltext: bool = True,
) -> dict:
    """
    Hämtar metadata och fulltext för ett riksdagsdokument från api.eduskunta.fi.

    Hämtar HTML-fulltext om htmlSaatavilla=true, annars raw XML via redirect.
    Hämtar alltid både finsk och svensk version när båda finns.
    Resultatet cachas lokalt i databasen (TTL 24h).

    Svaret innehåller alltid:
      fulltext_fi — finsk fulltext (för sökning och analys)
      fulltext_sv — svensk fulltext (för citat till användaren, None om saknas)

    Minst ett av edk_id eller eduskuntatunnus måste anges.
    """
    if not edk_id and not eduskuntatunnus:
        return {"fel": "Ange edk_id eller eduskuntatunnus"}

    # Kontrollera cache — returnera om båda fulltexterna finns (eller om fulltext inte önskas)
    cachad = None
    if edk_id:
        cachad = db.hamta_dokument_via_edk_id(edk_id)
    elif eduskuntatunnus:
        cachad = db.hamta_dokument_via_eduskuntatunnus(eduskuntatunnus)
    if cachad and (not hamta_fulltext or
                   (cachad.get("fulltext_fi") and cachad.get("fulltext_sv"))):
        if not hamta_fulltext:
            cachad = {k: v for k, v in cachad.items()
                      if k not in ("fulltext_fi", "fulltext_sv", "fulltext_html", "fulltext_md")}
        return {"kalla": "cache", "dokument": cachad}

    # Hämta finskt primärdokument
    try:
        if edk_id:
            meta = ed.hamta_asiakirja_metadata(edk_id)
        else:
            sok_svar = ed.hamta_asiakirja_via_eduskuntatunnus(eduskuntatunnus)
            treffar = sok_svar.get("results", [])
            if not treffar:
                return {"fel": f"Inget dokument hittades för beteckning: {eduskuntatunnus}"}
            # Välj finskt dokument som primär, annars första träffen
            fi_treff = next(
                (r.get("asiakirja") or r for r in treffar
                 if (r.get("asiakirja") or r).get("kielikoodi") == "fi"),
                treffar[0].get("asiakirja") or treffar[0],
            )
            meta = fi_treff
            edk_id = meta.get("edktunnus")
    except Exception as exc:
        return {"fel": f"Kunde inte hämta metadata: {exc}"}

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

    # Spara i cache
    db.upsert_dokument(
        kalla="eduskunta",
        edk_id=edk_id,
        eduskuntatunnus_fi=ed_tunnus_str,
        typ=meta.get("asiakirjatyyppikoodi"),
        titel_fi=meta.get("nimeketeksti") if meta.get("kielikoodi") == "fi" else None,
        titel_sv=sv_meta.get("nimeketeksti") if sv_meta else None,
        ar=int(meta["valtiopaivavuosi"]) if meta.get("valtiopaivavuosi") else None,
        datum=meta.get("laadintapvm"),
        html_saatavilla=html_saatavilla,
        fulltext_fi=fulltext_fi,
        fulltext_sv=fulltext_sv,
    )

    return {
        "kalla":           "eduskunta",
        "edk_id":          edk_id,
        "edk_id_sv":       edk_id_sv,
        "html_saatavilla": html_saatavilla,
        "metadata":        meta,
        "fulltext_fi":     fulltext_fi,   # för sökning och analys
        "fulltext_sv":     fulltext_sv,   # för citat till användaren
    }


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


@mcp.tool()
def fi_hamta_arende(
    tunnus: str,
) -> dict:
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

    Returnerar {} med 'fel'-nyckel om ärendet inte hittas.

    Exempel: fi_hamta_arende(tunnus="HE 15/2026 vp")
    """
    try:
        svar = ed.hamta_valtiopaivaasia(tunnus)
        if not svar or not svar.get("eduskuntatunnus"):
            return {
                "fel":    f"Ärendet '{tunnus}' hittades inte.",
                "tunnus": tunnus,
                "tips":   "Kontrollera formatet. Exempel: 'HE 15/2026 vp', 'KAA 5/2024 vp'.",
            }

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

    except Exception as exc:
        log.error("fi_hamta_arende misslyckades (%s): %s", tunnus, exc)
        return {"fel": str(exc), "tunnus": tunnus}


@mcp.tool()
def fi_hamta_lag(
    ar: Optional[int] = None,
    nummer: Optional[str] = None,
    hierarki: str = "act",
    typ: str = "statute",
    myndighetskod: Optional[str] = None,
    akn_uri_fi: Optional[str] = None,
) -> dict:
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
                      "treaty" | "authority-regulation"
      myndighetskod — krävs för authority-regulation, t.ex. "national-audit-office-of-finland"

    Svaret innehåller:
      fulltext_fi — finsk lagtext (för sökning och analys)
      fulltext_sv — svensk lagtext (för citat till användaren, None om saknas)
    """
    if akn_uri_fi:
        # Parsa ar, nummer, hierarki, typ ur AKN URI
        stig = akn_uri_fi.replace(fx.API_BASE, "")
        delar = [d for d in stig.split("/") if d and d not in ("akn", "fi") and not d.endswith("@")]
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
        def _bygg_uri(sprak_suffix: str) -> str:
            if myndighetskod:
                return f"{fx.API_BASE}/akn/fi/{hierarki}/{typ}/{myndighetskod}/{ar}/{nummer}/{sprak_suffix}"
            return f"{fx.API_BASE}/akn/fi/{hierarki}/{typ}/{ar}/{nummer}/{sprak_suffix}"
        akn_uri_fi = _bygg_uri("fin@")
        akn_uri_sv = _bygg_uri("swe@")
    else:
        return {"fel": "Ange antingen akn_uri_fi eller både ar och nummer"}

    # Kolla cache — returnera om båda finns
    cachad = db.hamta_dokument_via_akn_uri(akn_uri_fi)
    if cachad and cachad.get("fulltext_fi") and cachad.get("fulltext_sv"):
        return {
            "kalla":      "cache",
            "dokument":   cachad,
            "fulltext_fi": cachad.get("fulltext_fi"),   # för sökning och analys
            "fulltext_sv": cachad.get("fulltext_sv"),   # för citat till användaren
        }

    # Hämta finska versionen
    rot_fi = fx.hamta_akn_dokument(akn_uri_fi)
    meta      = None
    fulltext_fi = None
    fulltext_sv = None

    if rot_fi is not None:
        meta        = fx.parsad_akn_metadata(rot_fi)
        fulltext_fi = fx.extrahera_fulltext(rot_fi)
    else:
        log.warning("Finsk version saknas för %s/%s (%s)", ar, nummer, typ)

    # Hämta svenska versionen
    rot_sv = fx.hamta_akn_dokument(akn_uri_sv)
    if rot_sv is not None:
        if meta is None:
            meta = fx.parsad_akn_metadata(rot_sv)
        fulltext_sv = fx.extrahera_fulltext(rot_sv)
    else:
        log.debug("Svensk version saknas för %s/%s (%s) — kan saknas för enspråkiga lagar", ar, nummer, typ)

    if meta is None:
        return {"fel": f"Dokument hittades inte: {ar}/{nummer} ({typ})"}

    # Spara i cache
    db.upsert_dokument(
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
        fulltext_fi=fulltext_fi,
        fulltext_sv=fulltext_sv,
    )

    return {
        "kalla":      "finlex",
        "metadata":   meta,
        "akn_uri_fi": akn_uri_fi,
        "akn_uri_sv": akn_uri_sv,
        "fulltext_fi": fulltext_fi,   # för sökning och analys
        "fulltext_sv": fulltext_sv,   # för citat till användaren
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


@mcp.tool()
def fi_hamta_aanestys(
    aanestystunnus: Optional[str] = None,
    eduskuntatunnus: Optional[str] = None,
    istuntotunnus: Optional[str] = None,
    senaste: bool = False,
) -> dict:
    """
    Hämtar voteringsresultat från Eduskunta.

    Parametrar:
      aanestystunnus  — enskild votering: "{vpvuosi}-{istuntonr}-{aanestysnr}", t.ex. "2025-92-2"
      eduskuntatunnus — alla voteringar för ett ärende, t.ex. "HE 15/2026 vp"
      istuntotunnus   — alla voteringar i en session: "{vpvuosi}-{istuntonr}", t.ex. "2025-92"
      senaste         — om True returneras de 100 senaste voteringsresultaten (ignorerar övriga)

    Minst ett argument måste anges.
    """
    if senaste:
        radata = ed.hamta_uusimmat_aanestykset()
        return _summarisera_aanestykset(radata)
    if aanestystunnus:
        return ed.hamta_aanestys(aanestystunnus)
    if eduskuntatunnus:
        radata = ed.hamta_asian_aanestykset(eduskuntatunnus)
        return _summarisera_aanestykset(radata)
    if istuntotunnus:
        radata = ed.hamta_istunnon_aanestykset(istuntotunnus)
        return _summarisera_aanestykset(radata)
    return {"fel": "Ange aanestystunnus, eduskuntatunnus, istuntotunnus eller senaste=True"}


@mcp.tool()
def fi_sok_i_dokument(
    fraga: str,
    edk_id: Optional[str] = None,
    eduskuntatunnus: Optional[str] = None,
    max_treff: int = 5,
) -> dict:
    """
    Semantisk sökning via pgvector inom ett enskilt cachat dokument.

    Används för att hitta specifika stycken i ett riksdagsdokument eller en
    lag utan att läsa hela fulltexten. Kräver PostgreSQL med pgvector samt
    att dokumentet är chunkat och indexerat (03_chunka_och_embedda.py).

    Parametrar:
      fraga           — vad du söker efter, på finska eller svenska
      edk_id          — dokumentets edktunnus, t.ex. "EDK-2026-AK-8746"
                        (returneras av fi_sok_eduskunta och fi_hamta_dokument)
      eduskuntatunnus — riksdagsbeteckning, t.ex. "HE 15/2026 vp" eller "RP 15/2026 rd"
                        (antingen fi- eller sv-form fungerar)
      max_treff       — max antal chunk-träffar att returnera (standard 5)

    Minst ett av edk_id eller eduskuntatunnus måste anges.

    Returnerar dokumentmetadata + lista med matchande chunk-träffar sorterade
    efter semantisk likhet, med chunk_index, text och likhetspoäng.

    Typiskt arbetsflöde:
      1. fi_sok_eduskunta(fraga=...) → identifiera dokument, notera edk_id
      2. fi_sok_i_dokument(fraga=..., edk_id=...) → hitta relevanta stycken
      3. fi_hamta_dokument(edk_id=...) → hämta fulltext vid behov
    """
    if not edk_id and not eduskuntatunnus:
        return {"fel": "Ange edk_id eller eduskuntatunnus."}

    if not db.ar_postgres():
        return {"fel": "Semantisk sökning kräver PostgreSQL med pgvector — SQLite-läge stöds inte."}

    # Slå upp dokumentets interna id
    if edk_id:
        cachad = db.hamta_dokument_via_edk_id(edk_id)
    else:
        cachad = db.hamta_dokument_via_eduskuntatunnus(eduskuntatunnus)

    if not cachad:
        identifierare = edk_id or eduskuntatunnus
        return {
            "fel": (
                f"Dokumentet '{identifierare}' finns inte i lokal cache. "
                "Hämta det först med fi_hamta_dokument så att det indexeras."
            ),
            "edk_id":          edk_id,
            "eduskuntatunnus": eduskuntatunnus,
        }

    dokument_id = cachad["id"]

    # Detektera frågespråk och generera embedding med rätt modell
    sprak = _detektera_sprak(fraga)
    try:
        embedding = _embedda(fraga, sprak)
    except Exception as exc:
        log.error("fi_sok_i_dokument: embedding misslyckades: %s", exc)
        return {"fel": f"Embedding misslyckades: {exc}"}

    return db.vektor_sok_i_dokument(
        dokument_id=dokument_id,
        embedding=embedding,
        sprak=sprak,
        max_treff=max_treff,
    )


@mcp.tool()
def fi_lista_vaalikaudet(inkludera_riksmoten: bool = False) -> dict:
    """
    Listar finska valperioder (fr.o.m. 1907) och optionellt riksmöten.

    Parametrar:
      inkludera_riksmoten — om True inkluderas alla 128+ riksmöten (fr.o.m. 1907)
    """
    vaalikaudet = ed.hamta_vaalikaudet()
    svar: dict = {"vaalikaudet": vaalikaudet}
    if inkludera_riksmoten:
        svar["valtiopaivat"] = ed.hamta_valtiopaivat()
    return svar


# ---------------------------------------------------------------------------
# Uppstart
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# HTTP-autentisering
# ---------------------------------------------------------------------------

def _make_auth_app(asgi_app, api_key: str):
    """
    Wrappa en ASGI-app med enkel Bearer-token-autentisering.
    Alla anrop utan korrekt Authorization-header avvisas med HTTP 401.
    """
    from starlette.applications import Starlette
    from starlette.middleware import Middleware
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.responses import PlainTextResponse
    from starlette.routing import Mount

    class ApiNyckelMellanvara(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            token = (
                request.headers.get("Authorization", "")
                .removeprefix("Bearer ")
                .strip()
            )
            if token != api_key:
                return PlainTextResponse(
                    "Obehörig: ogiltig eller saknad API-nyckel.", status_code=401
                )
            return await call_next(request)

    return Starlette(
        routes=[Mount("/", app=asgi_app)],
        middleware=[Middleware(ApiNyckelMellanvara)],
    )


# ---------------------------------------------------------------------------
# Startpunkt
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    (_SCRIPT_DIR / "logs").mkdir(parents=True, exist_ok=True)

    try:
        db.init_db()
    except Exception as exc:
        log.warning("Databasinitiering misslyckades: %s — fortsätter utan DB", exc)

    if MCP_TRANSPORT == "http":
        import uvicorn

        try:
            asgi_app = mcp.streamable_http_app()
        except AttributeError:
            log.warning(
                "mcp.streamable_http_app() saknas — försöker med sse_app(). "
                "Uppgradera mcp-paketet om problem uppstår."
            )
            asgi_app = mcp.sse_app()

        if MCP_API_KEY:
            log.info("API-nyckelautentisering aktiverad")
            app = _make_auth_app(asgi_app, MCP_API_KEY)
        else:
            log.warning(
                "MCP_API_KEY är inte satt — servern körs utan autentisering. "
                "Bind enbart till loopback (MCP_HOST=127.0.0.1) eller "
                "skydda via reverse proxy."
            )
            app = asgi_app

        log.info("Startar HTTP-transport på %s:%s", MCP_HOST, MCP_PORT)
        uvicorn.run(app, host=MCP_HOST, port=MCP_PORT, log_level="info")
    else:
        log.info("Startar stdio-transport (lokal användning)")
        mcp.run()
