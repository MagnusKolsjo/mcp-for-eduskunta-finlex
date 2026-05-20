# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Magnus Kolsjö

"""
finlex_client.py — Klient mot Finlex öppna data-API (opendata.finlex.fi)

Täcker tre resurshierarkier:
  /akn/fi/act/{typ}/...     — lagstiftning (statute, statute-consolidated)
  /akn/fi/doc/{typ}/...     — dokument (government-proposal, treaty, authority-regulation)
  /akn/fi/judgment/{typ}/   — rättspraxis — 404, ej tillgänglig, utelämnas

Akoma Ntoso XML returneras. Rotelementet skiljer sig:
  act/statute       → <akomaNtoso><act ...>
  doc/government-proposal → <akomaNtoso><doc ...>

Viktigt:
  - User-Agent-header är OBLIGATORISK — servern returnerar 403 utan den.
  - publishedSince på /list returnerar 400 — använd startYear/endYear istället.
  - /list returnerar JSON (inte XML), max 10 poster per sida.
  - Rate-limiting: vänta vid 429.

API-dokumentation / Swagger: https://opendata.finlex.fi/swagger-ui/index.html
OpenAPI-spec: https://opendata.finlex.fi/v3/api-docs
"""

import logging
import os
import time
from pathlib import Path
from typing import Optional

import httpx
from dotenv import load_dotenv
from lxml import etree

load_dotenv(Path(__file__).parent / ".env")

log = logging.getLogger(__name__)

API_BASE   = os.getenv("FINLEX_API_BASE", "https://opendata.finlex.fi/finlex/avoindata/v1")
RATE_LIMIT = int(os.getenv("FINLEX_RATE_LIMIT", "20"))   # anrop/minut (konservativt)
USER_AGENT = "mcp-for-eduskunta-finlex/1.0 (+https://github.com/MagnusKolsjo/mcp-for-eduskunta-finlex)"

# Token-bucket för rate-limiting
_bucket_tokens  = float(RATE_LIMIT)
_bucket_last_ts = time.monotonic()

# AKN-namespace
AKN_NS = "http://docs.oasis-open.org/legaldocml/ns/akn/3.0"
AKN    = f"{{{AKN_NS}}}"

# Verifierade dokumenttyper (2026-05-16)
AKT_TYPER = ["statute", "statute-consolidated"]
DOK_TYPER = ["government-proposal", "treaty", "authority-regulation"]

# Språkkoder
SPRAK_FI = "fin@"
SPRAK_SV = "swe@"


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
    return {"User-Agent": USER_AGENT}


def _get_json(url: str, params: Optional[dict] = None, max_forsok: int = 3) -> dict | list:
    """GET med JSON-svar, hanterar 429 med exponentiell backoff."""
    for forsok in range(max_forsok):
        _throttle()
        try:
            r = httpx.get(
                url,
                params=params or {},
                headers={**_headers(), "Accept": "application/json"},
                timeout=30,
            )
            if r.status_code == 429:
                vantetid = 60 * (forsok + 1)
                log.warning("429 från Finlex — väntar %d s (försök %d/%d)", vantetid, forsok + 1, max_forsok)
                time.sleep(vantetid)
                continue
            r.raise_for_status()
            return r.json()
        except httpx.HTTPStatusError as exc:
            if forsok < max_forsok - 1:
                time.sleep(5 * (forsok + 1))
                continue
            raise
    raise RuntimeError(f"Max försök nådd för {url}")


def _get_xml(url: str, params: Optional[dict] = None, max_forsok: int = 3) -> Optional[etree._Element]:
    """GET med XML-svar, returnerar lxml-rot eller None vid 404."""
    for forsok in range(max_forsok):
        _throttle()
        try:
            r = httpx.get(
                url,
                params=params or {},
                headers={**_headers(), "Accept": "application/xml,text/xml"},
                timeout=60,
            )
            if r.status_code == 404:
                log.debug("404 för URL: %s", url)
                return None
            if r.status_code == 429:
                vantetid = 60 * (forsok + 1)
                log.warning("429 från Finlex — väntar %d s", vantetid)
                time.sleep(vantetid)
                continue
            r.raise_for_status()
            return etree.fromstring(r.content)
        except Exception as exc:
            if forsok < max_forsok - 1:
                time.sleep(5 * (forsok + 1))
                continue
            log.error("XML-hämtning misslyckades för %s: %s", url, exc)
            return None
    return None


# ---------------------------------------------------------------------------
# /list-endpoint — primär synkkälla
# ---------------------------------------------------------------------------

def hamta_lista(
    hierarki: str,
    typ: str,
    sida: int = 1,
    limit: int = 10,
    start_ar: Optional[int] = None,
    slut_ar: Optional[int] = None,
    sprak_version: Optional[str] = None,
    titel_innehaller: Optional[str] = None,
) -> list[dict]:
    """
    Hämtar en sida från /list-endpointen för en dokumenttyp.

    Returnerar JSON-array: [{"akn_uri": "...", "status": "NEW"|"MODIFIED"}, ...]

    Parametrar:
      hierarki        — "act" eller "doc"
      typ             — t.ex. "statute", "government-proposal"
      sida            — sida (börjar på 1)
      limit           — max 10 per sida
      start_ar        — årsfilter från (t.ex. 1929)
      slut_ar         — årsfilter till
      sprak_version   — "fin@" eller "swe@"
      titel_innehaller — titelfilter
    """
    url = f"{API_BASE}/akn/fi/{hierarki}/{typ}/list"
    params: dict = {"page": sida, "limit": min(limit, 10)}
    if start_ar:
        params["startYear"] = start_ar
    if slut_ar:
        params["endYear"] = slut_ar
    if sprak_version:
        params["langAndVersion"] = sprak_version
    if titel_innehaller:
        params["titleContains"] = titel_innehaller

    svar = _get_json(url, params)
    if isinstance(svar, list):
        return svar
    # Ibland är svaret inlindat i ett objekt
    if isinstance(svar, dict):
        return svar.get("content", svar.get("items", svar.get("results", [])))
    return []


def hamta_alla_i_lista(
    hierarki: str,
    typ: str,
    start_ar: Optional[int] = None,
    slut_ar: Optional[int] = None,
    sprak_version: Optional[str] = None,
) -> list[dict]:
    """
    Paginerar igenom hela /list-endpointen och returnerar alla poster.

    Används av synkskript för att bygga lokal index.
    OBS: kan ta lång tid för statute (fr.o.m. 1929 — tusentals lagar).
    """
    alla = []
    sida = 1
    while True:
        poster = hamta_lista(
            hierarki=hierarki,
            typ=typ,
            sida=sida,
            limit=10,
            start_ar=start_ar,
            slut_ar=slut_ar,
            sprak_version=sprak_version,
        )
        if not poster:
            break
        alla.extend(poster)
        log.info("%s/%s — hämtade sida %d, totalt %d poster", hierarki, typ, sida, len(alla))
        if len(poster) < 10:
            break  # Sista sidan
        sida += 1
    return alla


# ---------------------------------------------------------------------------
# AKN-dokumenthämtning
# ---------------------------------------------------------------------------

def bygg_akn_url(
    hierarki: str,
    typ: str,
    ar: int,
    nummer: str,
    sprak: str = SPRAK_FI,
    myndighetskod: Optional[str] = None,
) -> str:
    """
    Bygger AKN-URL för ett dokument.

    För authority-regulation inkluderas myndighetskoden:
      .../authority-regulation/{myndighetskod}/{ar}/{nummer}/{sprak}
    För övriga:
      .../act/{typ}/{ar}/{nummer}/{sprak}
      .../doc/{typ}/{ar}/{nummer}/{sprak}
    """
    if typ == "authority-regulation" and myndighetskod:
        return f"{API_BASE}/akn/fi/{hierarki}/{typ}/{myndighetskod}/{ar}/{nummer}/{sprak}"
    return f"{API_BASE}/akn/fi/{hierarki}/{typ}/{ar}/{nummer}/{sprak}"


def hamta_akn_dokument(akn_uri: str) -> Optional[etree._Element]:
    """
    Hämtar ett enskilt AKN-dokument direkt via dess URI.
    Returnerar lxml-rotelementet, eller None om 404.
    """
    return _get_xml(akn_uri)


def hamta_ar(
    hierarki: str,
    typ: str,
    ar: int,
) -> Optional[etree._Element]:
    """
    Hämtar XML med alla dokument för ett år.
    GET /akn/fi/{hierarki}/{typ}/{ar}
    """
    url = f"{API_BASE}/akn/fi/{hierarki}/{typ}/{ar}"
    return _get_xml(url)


# ---------------------------------------------------------------------------
# AKN XML-parsning
# ---------------------------------------------------------------------------

def parsad_akn_metadata(rot: etree._Element) -> dict:
    """
    Extraherar metadata från ett AKN-rots元素.

    Fungerar för både <act> och <doc> (rotelementet under <akomaNtoso>).
    Returnerar dict med: typ, frbrwork, eli, titel_fi, titel_sv, ar, nummer, sprak
    """
    if rot is None:
        return {}

    def _hitta(el, *taggar):
        """Letar upp första matchande tagg bland alternativ. Säker för lxml-element."""
        for t in taggar:
            r = el.find(t)
            if r is not None:
                return r
        return None

    # Hitta innehållsrot — <act> eller <doc>
    innehall = _hitta(rot, f"{AKN}act", f"{AKN}doc", "act", "doc")
    if innehall is None:
        innehall = rot  # Fallback

    meta = _hitta(innehall, f"{AKN}meta", "meta")
    if meta is None:
        return {}

    # FRBRWork
    frbrwork_el = _hitta(meta, f".//{AKN}FRBRWork", ".//FRBRWork")
    frbrwork = ""
    eli_fi   = ""
    ar       = None
    nummer   = ""
    if frbrwork_el is not None:
        frbruri = _hitta(frbrwork_el, f"{AKN}FRBRuri", "FRBRuri")
        if frbruri is not None:
            frbrwork = frbruri.get("value", "")
        frbrdate = _hitta(frbrwork_el, f"{AKN}FRBRdate", "FRBRdate")
        if frbrdate is not None:
            datum = frbrdate.get("date", "")
            if datum and len(datum) >= 4:
                try:
                    ar = int(datum[:4])
                except ValueError:
                    pass
        frbrnumber = _hitta(frbrwork_el, f"{AKN}FRBRnumber", "FRBRnumber")
        if frbrnumber is not None:
            nummer = frbrnumber.get("value", "")

    # ELI (finns i FRBRExpression)
    frbrexpr = _hitta(meta, f".//{AKN}FRBRExpression", ".//FRBRExpression")
    if frbrexpr is not None:
        frbruri_expr = _hitta(frbrexpr, f"{AKN}FRBRuri", "FRBRuri")
        if frbruri_expr is not None:
            eli_fi = frbruri_expr.get("value", "")

    # Titlar — docTitle sitter i dokumentkroppen, inte i meta.
    # FRBRlanguage i FRBRExpression talar om vilket språk just detta dokument är på.
    sprak_el = None
    if frbrexpr is not None:
        sprak_el = _hitta(frbrexpr, f"{AKN}FRBRlanguage", "FRBRlanguage")
    sprak_kod = sprak_el.get("language", "") if sprak_el is not None else ""

    doc_titel = _hamta_doc_titel(innehall)

    if sprak_kod == "fin":
        titel_fi = doc_titel
        titel_sv = ""
    elif sprak_kod == "swe":
        titel_fi = ""
        titel_sv = doc_titel
    else:
        titel_fi = doc_titel
        titel_sv = ""

    # Dokumenttyp (rot-elementnamn utan namespace)
    rot_tag = innehall.tag
    if "}" in rot_tag:
        rot_tag = rot_tag.split("}")[1]

    return {
        "rot_element": rot_tag,       # "act" eller "doc"
        "frbrwork":    frbrwork,
        "eli":         eli_fi,
        "titel_fi":    titel_fi,
        "titel_sv":    titel_sv,
        "ar":          ar,
        "nummer":      nummer,
    }


def _hamta_doc_titel(innehall: etree._Element) -> str:
    """
    Extraherar titel ur AKN-dokumentkroppen.

    Finlex placerar titeln i <docTitle> i body-sektionen, inte i <meta>.
    Letar med och utan AKN-namespace.
    """
    for ns in [AKN_NS, ""]:
        pref = f"{{{ns}}}" if ns else ""
        el = innehall.find(f".//{pref}docTitle")
        if el is not None:
            text = (el.text or "").strip()
            if text:
                return text
    return ""


def extrahera_fulltext(rot: etree._Element) -> str:
    """
    Extraherar all löptext ur ett AKN-dokument som ren text.

    Hanterar både <act> och <doc> som rotvärde under <akomaNtoso>.
    Returnerar sammanslagen text med radbrytningar mellan stycken.
    """
    if rot is None:
        return ""

    stycken = []
    for el in rot.iter():
        tag = el.tag
        if "}" in tag:
            tag = tag.split("}")[1]
        if tag in ("p", "num", "heading"):
            text = "".join(el.itertext()).strip()
            if text:
                stycken.append(text)

    return "\n\n".join(stycken)


def sprak_av_uri(akn_uri: str) -> str:
    """
    Extraherar språkkod från AKN-URI.
    "...statute/2024/123/fin@" → "fi"
    "...statute/2024/123/swe@" → "sv"
    """
    if "fin@" in akn_uri:
        return "fi"
    if "swe@" in akn_uri:
        return "sv"
    return "fi"


def byt_sprak_i_uri(akn_uri: str, ny_sprak: str) -> str:
    """
    Byter språkdelen i en AKN-URI.
    byt_sprak_i_uri("...statute/2024/123/fin@", "swe@") → "...statute/2024/123/swe@"
    """
    for gammal in [SPRAK_FI, SPRAK_SV, "fin@/", "swe@/"]:
        if gammal in akn_uri:
            return akn_uri.replace(gammal, ny_sprak)
    return akn_uri
