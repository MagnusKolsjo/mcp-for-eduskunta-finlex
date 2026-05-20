# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Magnus Kolsjö

"""
eduskunta_client.py — Klient mot Eduskuntas öppna API (api.eduskunta.fi)

Täcker:
  - POST /api/v1/search        — sökning med filter och expressioner
  - GET  /api/v1/asiakirjat/edktunnus/{id}                    — metadata
  - GET  /api/v1/asiakirjat/eduskuntatunnus/{tunnus}          — metadata via beteckning
  - GET  /api/v1/asiakirjat/edktunnus/{id}/html               — HTML-fulltext
  - GET  /api/v1/asiakirjat/edktunnus/{id}/xml                — XML-fulltext (302)
  - GET  /api/v1/asiakirjat/edktunnus/{id}/pdf                — PDF (302)
  - GET  /api/v1/valtiopaivaasiat/{tunnus}                    — parlamentärt ärende
  - GET  /api/v1/taysistunnot/aanestykset/{tunnus}            — enskild votering
  - GET  /api/v1/taysistunnot/istunnon-aanestykset/{tunnus}   — voteringar per session
  - GET  /api/v1/taysistunnot/asian-aanestykset/{tunnus}      — voteringar per ärende
  - GET  /api/v1/taysistunnot/uusimmat-aanestykset            — senaste voteringar
  - GET  /api/v1/kansanedustajat + /{id}                      — ledamöter
  - GET  /api/v1/reference-data/...                           — referensdata
  - POST /api/v1/aggregations/unique-by                       — aggregationer

OBS: GET /api/v1/search är trasig server-side — använd alltid POST.

API-dokumentation: https://api.eduskunta.fi
"""

import logging
import os
import time
from pathlib import Path
from typing import Optional
from urllib.parse import quote

import httpx
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

log = logging.getLogger(__name__)

API_BASE   = os.getenv("EDUSKUNTA_API_BASE", "https://api.eduskunta.fi/api/v1")
RATE_LIMIT = int(os.getenv("EDUSKUNTA_RATE_LIMIT", "60"))   # anrop/minut
USER_AGENT = "mcp-for-eduskunta-finlex/1.0 (+https://github.com/MagnusKolsjo/mcp-for-eduskunta-finlex)"

# Token-bucket för rate-limiting
_bucket_tokens  = float(RATE_LIMIT)
_bucket_last_ts = time.monotonic()


# ---------------------------------------------------------------------------
# HTTP-primitiver
# ---------------------------------------------------------------------------

def _throttle():
    global _bucket_tokens, _bucket_last_ts
    now     = time.monotonic()
    elapsed = now - _bucket_last_ts
    _bucket_tokens  = min(RATE_LIMIT, _bucket_tokens + elapsed * (RATE_LIMIT / 60.0))
    _bucket_last_ts = now
    if _bucket_tokens < 1:
        sleep_s = (1 - _bucket_tokens) / (RATE_LIMIT / 60.0)
        log.debug("Rate-limit nådd, väntar %.1f s", sleep_s)
        time.sleep(sleep_s)
        _bucket_tokens = 0.0
    else:
        _bucket_tokens -= 1.0


def _headers() -> dict:
    return {
        "User-Agent": USER_AGENT,
        "Accept":     "application/json",
    }


def _get(path: str, params: Optional[dict] = None) -> dict:
    """GET mot API_BASE/{path} — returnerar JSON som dict."""
    _throttle()
    url = f"{API_BASE}/{path}"
    r = httpx.get(url, params=params or {}, headers=_headers(), timeout=30, follow_redirects=False)
    r.raise_for_status()
    return r.json()


def _post(path: str, data: dict) -> dict:
    """POST mot API_BASE/{path} med JSON-body — returnerar JSON som dict."""
    _throttle()
    url = f"{API_BASE}/{path}"
    r = httpx.post(
        url,
        json=data,
        headers={**_headers(), "Content-Type": "application/json"},
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


def _get_text(path: str) -> str:
    """GET mot API_BASE/{path} — returnerar svarskroppen som text (för HTML)."""
    _throttle()
    url = f"{API_BASE}/{path}"
    r = httpx.get(url, headers={**_headers(), "Accept": "text/html,*/*"}, timeout=60)
    r.raise_for_status()
    return r.text


def _get_redirect_url(path: str) -> Optional[str]:
    """GET mot API_BASE/{path} — returnerar Location-headern vid 302, annars None."""
    _throttle()
    url = f"{API_BASE}/{path}"
    r = httpx.get(url, headers=_headers(), timeout=30, follow_redirects=False)
    if r.status_code in (301, 302, 307, 308):
        return r.headers.get("Location")
    r.raise_for_status()
    return None


# ---------------------------------------------------------------------------
# Sökning — POST /api/v1/search
# ---------------------------------------------------------------------------

def sok(
    fraga: Optional[str] = None,
    kategori: str = "asiakirja",
    max_treff: int = 10,
    start_index: int = 0,
    langkod: Optional[str] = None,
    expression: Optional[dict] = None,
    sortering: Optional[list] = None,
    fulltext_highlight: bool = False,
) -> dict:
    """
    Söker i Eduskuntas index via POST /api/v1/search.

    Parametrar:
      fraga            — söktext (fritext)
      kategori         — asiakirja | valtiopaivaasia | aanestys | puheenvuoro |
                         kansanedustaja | tapahtuma | cmsSivu | sisaltosivu | tiedote
      max_treff        — max antal resultat (default 10)
      start_index      — paginering (0-baserat)
      langkod          — fi | sv (filterar på dokumentspråk, stöds av kansanedustaja m.fl.)
      expression       — Eduskuntas expression-objekt (se API-dok). Stöder:
                           {"and": [...]} | {"or": [...]} | {"not": {...}}
                           Leaf: {"property": "...", "match": "..."}
                                 {"property": "...", "stringValue": "..."}
                                 {"property": "...", "fromDate": "YYYY-MM-DD", "toDate": "YYYY-MM-DD"}
                                 {"property": "...", "booleanValue": true}
                                 {"exists": "..."}
      sortering        — [{"property": "laadintapvm", "ascending": False}]
      fulltext_highlight — om Eduskunta ska returnera textutdrag med träffmarkering

    Returnerar:
      {
        "results": [...],
        "aggs": {...},
        "searchMetadata": {"totalResultCount": N, "actualResultCount": N, "startFromIndex": N}
      }
    """
    kropp: dict = {
        "category":          kategori,
        "maxResults":        max_treff,
        "startFromIndex":    start_index,
        "fullTextHighlight": fulltext_highlight,
    }
    if fraga:
        kropp["query"] = fraga
    if langkod:
        kropp["langCode"] = langkod
    if expression:
        kropp["expression"] = expression
    if sortering:
        kropp["sort"] = sortering

    return _post("search", kropp)


def sok_asiakirjat(
    fraga: Optional[str] = None,
    typ: Optional[str] = None,
    fran_datum: Optional[str] = None,
    till_datum: Optional[str] = None,
    ar: Optional[int] = None,
    max_treff: int = 10,
    start_index: int = 0,
) -> dict:
    """
    Söker riksdagsdokument (asiakirja) med typ- och datumfilter.

    Parametrar:
      fraga      — söktext
      typ        — dokumenttyp, t.ex. "HE", "RP", "KK", "PTK"
      fran_datum — YYYY-MM-DD (inklusiv)
      till_datum — YYYY-MM-DD (exklusiv)
      ar         — riksdagsår (valtiopaivavuosi), t.ex. 2024
    """
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

    return sok(
        fraga=fraga,
        kategori="asiakirja",
        max_treff=max_treff,
        start_index=start_index,
        expression=expression,
    )


# ---------------------------------------------------------------------------
# Dokumenthämtning
# ---------------------------------------------------------------------------

def hamta_asiakirja_metadata(edktunnus: str) -> dict:
    """
    Hämtar metadata för ett dokument via edktunnus (t.ex. "EDK-2026-AK-8746").

    OBS: GET /api/v1/asiakirjat/edktunnus/{id} är trasig server-side —
    den kräver egenskapen 'snippet' men accepterar den varken som query-param
    eller request body (verifierat 2026-05-17). Workaround: POST /search med
    edktunnus-filter returnerar identisk metadata.
    """
    svar = sok(expression={"property": "edktunnus", "stringValue": edktunnus}, max_treff=1)
    results = svar.get("results", [])
    if not results:
        raise ValueError(f"Inget dokument hittades för edktunnus: {edktunnus}")
    r = results[0]
    meta = dict(r.get("asiakirja") or r)
    # Söksvaret inkluderar fulltext i 'fullText' och 'fullTextSnippet' — ta bort dem.
    # Fulltext hämtas separat via hamta_html_fulltext() om det behövs.
    for falt in ("snippet", "fullText", "fullTextSnippet"):
        meta.pop(falt, None)
    return meta


def hamta_asiakirja_via_eduskuntatunnus(tunnus: str) -> dict:
    """
    Hämtar metadata för ett dokument via riksdagsbeteckning (t.ex. "HE 15/2026 vp").

    OBS: GET /api/v1/asiakirjat/eduskuntatunnus/{tunnus} har samma 'snippet'-bugg
    som edktunnus-endpointen. Workaround: POST /search med eduskuntatunnus-filter.
    Returnerar sökresultat-dict med "results"-lista (kan innehålla flera dokument
    om beteckningen matchar t.ex. både fi- och sv-versionen).
    """
    return sok(
        expression={"property": "eduskuntatunnus", "stringValue": tunnus},
        max_treff=5,
    )


def hamta_syskondokument_sv(eduskuntatunnus: str) -> Optional[dict]:
    """
    Söker efter det svenska syskondokumentet för en riksdagsbeteckning.

    api.eduskunta.fi lagrar fi- och sv-versioner som separata dokument med
    samma eduskuntatunnus men olika kielikoodi. Returnerar metadata-dict för
    det svenska dokumentet, eller None om det saknas.

    eduskuntatunnus: t.ex. "HE 15/2026 vp" (finska) eller "RP 15/2026 rd" (svenska).
    Båda fungerar — API:et matchar mot samma ärende.
    """
    svar = sok(
        expression={"property": "eduskuntatunnus", "stringValue": eduskuntatunnus},
        max_treff=5,
    )
    for r in svar.get("results", []):
        doc = r.get("asiakirja") or r
        if isinstance(doc, dict) and doc.get("kielikoodi") == "sv":
            meta = dict(doc)
            for falt in ("snippet", "fullText", "fullTextSnippet"):
                meta.pop(falt, None)
            return meta
    return None


def hamta_html_fulltext(edktunnus: str) -> Optional[str]:
    """
    Hämtar HTML-fulltext för ett dokument.
    GET /api/v1/asiakirjat/edktunnus/{id}/html

    Returnerar HTML-strängen (ca 500k tecken för en typisk HE).
    Returnerar None om dokumentet saknar htmlSaatavilla eller om anropet misslyckas.
    """
    try:
        html = _get_text(f"asiakirjat/edktunnus/{quote(edktunnus, safe='')}/html")
        return html if html.strip() else None
    except Exception as exc:
        log.warning("Kunde inte hämta HTML för %s: %s", edktunnus, exc)
        return None


def hamta_xml_redirect_url(edktunnus: str) -> Optional[str]:
    """
    Hämtar URL till rådata-XML för ett dokument (302-redirect).
    GET /api/v1/asiakirjat/edktunnus/{id}/xml
    Returnerar Location-URL:en, eller None.
    """
    try:
        return _get_redirect_url(
            f"asiakirjat/edktunnus/{quote(edktunnus, safe='')}/xml"
        )
    except Exception as exc:
        log.warning("Kunde inte hämta XML-redirect för %s: %s", edktunnus, exc)
        return None


def hamta_pdf_redirect_url(edktunnus: str) -> Optional[str]:
    """
    Hämtar URL till PDF för ett dokument (302-redirect).
    GET /api/v1/asiakirjat/edktunnus/{id}/pdf
    Returnerar Location-URL:en, eller None.
    """
    try:
        return _get_redirect_url(
            f"asiakirjat/edktunnus/{quote(edktunnus, safe='')}/pdf"
        )
    except Exception as exc:
        log.warning("Kunde inte hämta PDF-redirect för %s: %s", edktunnus, exc)
        return None


def hamta_valtiopaivaasia(tunnus: str) -> dict:
    """
    Hämtar ett parlamentäriskt ärende (livscykel, bilagor).
    GET /api/v1/valtiopaivaasiat/{tunnus}
    tunnus: t.ex. "HE+15%2F2026+vp" (URL-encodat)
    """
    return _get(f"valtiopaivaasiat/{quote(tunnus, safe='')}")


# ---------------------------------------------------------------------------
# Voteringar
# ---------------------------------------------------------------------------

def hamta_aanestys(aanestystunnus: str) -> dict:
    """
    Hämtar en enskild votering.
    GET /api/v1/taysistunnot/aanestykset/{aanestystunnus}
    aanestystunnus: "{vpvuosi}-{istuntonr}-{aanestysnr}", t.ex. "2025-92-2"
    """
    return _get(f"taysistunnot/aanestykset/{aanestystunnus}")


def hamta_istunnon_aanestykset(istuntotunnus: str) -> dict:
    """
    Hämtar alla voteringar i en plenumsession.
    GET /api/v1/taysistunnot/istunnon-aanestykset/{istuntotunnus}
    istuntotunnus: "{vpvuosi}-{istuntonr}", t.ex. "2025-92"
    """
    return _get(f"taysistunnot/istunnon-aanestykset/{istuntotunnus}")


def hamta_asian_aanestykset(eduskuntatunnus: str) -> dict:
    """
    Hämtar alla voteringar kopplade till ett ärende.
    GET /api/v1/taysistunnot/asian-aanestykset/{eduskuntatunnus}
    eduskuntatunnus: t.ex. "HE 15/2026 vp" (URL-encodas automatiskt)
    """
    return _get(f"taysistunnot/asian-aanestykset/{quote(eduskuntatunnus, safe='')}")


def hamta_uusimmat_aanestykset() -> dict:
    """
    Hämtar senaste voteringar (max senaste 30 dagarna / 100 voteringar).
    GET /api/v1/taysistunnot/uusimmat-aanestykset
    """
    return _get("taysistunnot/uusimmat-aanestykset")


# ---------------------------------------------------------------------------
# Ledamöter
# ---------------------------------------------------------------------------

def lista_ledamoter(
    vaalikausitunnus: Optional[str] = None,
    max_treff: int = 200,
    start_index: int = 0,
) -> dict:
    """
    Listar riksdagsledamöter via sökning.
    Parametrar:
      vaalikausitunnus — t.ex. "2023-" för nuvarande valperiod
    """
    expression = None
    if vaalikausitunnus:
        expression = {
            "property": "vaalikausitunnus",
            "stringValue": vaalikausitunnus,
        }
    return sok(
        kategori="kansanedustaja",
        max_treff=max_treff,
        start_index=start_index,
        expression=expression,
    )


def hamta_ledamot(henkilonro: str) -> dict:
    """
    Hämtar metadata för en specifik ledamot.
    GET /api/v1/kansanedustajat/{henkilonro}
    """
    return _get(f"kansanedustajat/{henkilonro}")


# ---------------------------------------------------------------------------
# Referensdata
# ---------------------------------------------------------------------------

def hamta_vaalikaudet() -> dict:
    """
    Hämtar alla valperioder (fr.o.m. 1907).
    GET /api/v1/reference-data/vaalikaudet
    """
    return _get("reference-data/vaalikaudet")


def hamta_valtiopaivat() -> dict:
    """
    Hämtar alla riksmöten (fr.o.m. 1907, 128 st. som av 2026).
    GET /api/v1/reference-data/valtiopaivat
    """
    return _get("reference-data/valtiopaivat")


def hamta_valiokunnat() -> dict:
    """Hämtar referensdata för utskott."""
    return _get("reference-data/valiokunnat")


def hamta_eduskuntaryhmat() -> dict:
    """Hämtar referensdata för riksdagsgrupper."""
    return _get("reference-data/eduskuntaryhmat")


def hamta_asiakirjatyypit() -> dict:
    """Hämtar referensdata för dokumenttyper."""
    return _get("reference-data/asiakirjatyypit")


# ---------------------------------------------------------------------------
# Aggregationer
# ---------------------------------------------------------------------------

def aggregera_unika_varden(
    kategori: str,
    falt: str,
    max_resultat: int = 1000,
) -> list[dict]:
    """
    Hämtar unika värden för ett fält via POST /api/v1/aggregations/unique-by.

    Returnerar lista med [{"key": {"falt": "varde"}, "docCount": N}, ...]

    Exempel:
      aggregera_unika_varden("asiakirja", "asiakirjatyyppikoodi")
      → [{"key": {"asiakirjatyyppikoodi": "HE"}, "docCount": 2457}, ...]
    """
    svar = _post("aggregations/unique-by", {
        "category": kategori,
        "agg": {
            "unique": {
                "terms": [falt]
            }
        }
    })
    return svar.get("results", [])


# ---------------------------------------------------------------------------
# Hjälpfunktioner
# ---------------------------------------------------------------------------

def extrahera_edktunnus_fran_sok(svar: dict) -> list[str]:
    """Extraherar edktunnus-listan från ett sökresultat."""
    return [
        r.get("edktunnus", "")
        for r in svar.get("results", [])
        if r.get("edktunnus")
    ]


def html_till_text(html: str) -> str:
    """Konverterar HTML-fulltext till ren text (enkelt, utan externa beroenden)."""
    import re
    # Ta bort script- och style-block
    text = re.sub(r"<(script|style)[^>]*>.*?</(script|style)>", "", html, flags=re.DOTALL | re.IGNORECASE)
    # Ta bort alla HTML-taggar
    text = re.sub(r"<[^>]+>", " ", text)
    # Normalisera blanksteg
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    # Avkoda vanliga HTML-entiteter
    text = text.replace("&nbsp;", " ").replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"').replace("&#39;", "'")
    return text.strip()
