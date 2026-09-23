# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Magnus Kolsjö

"""
texthamtning.py — Hämtar ett dokuments text live från källan

Den lokala databasen behöver inte lagra dokumentens råtext: chunks,
embeddings och offsets räcker för sökning, och texten hämtas från källan
när den ska läsas eller chunkas om. Modulen samlar den hämtningen för
synkskripten och MCP-servern.
"""

import logging
from typing import Optional

import eduskunta_client as ed
import finlex_client as fx

log = logging.getLogger(__name__)


def finlex_text(akn_uri: str, strikt: bool = False) -> Optional[str]:
    """Lagtext ur en AKN-URI; None om dokumentet saknas (404)."""
    rot = fx.hamta_akn_dokument(akn_uri, strikt=strikt)
    if rot is None:
        return None
    text = fx.extrahera_fulltext(rot)
    return text if text and text.strip() else None


def eduskunta_text(edk_id: str) -> Optional[str]:
    """Riksdagsdokumentets text ur HTML-versionen; None om den saknas."""
    html = ed.hamta_html_fulltext(edk_id)
    return ed.html_till_text(html) if html else None


def dokumenttext(dok: dict, sprak: str) -> Optional[str]:
    """
    Text för en dokumentrad ur databasen, på angivet språk.

    Finlex: akn_uri_fi/akn_uri_sv. Eduskunta: edk_id är den finska
    versionen; den svenska slås upp som syskondokument via beteckningen.
    """
    if dok.get("kalla") == "finlex":
        uri = dok.get("akn_uri_fi") if sprak == "fi" else dok.get("akn_uri_sv")
        if not uri and dok.get("akn_uri_fi"):
            uri = fx.byt_sprak_i_uri(dok["akn_uri_fi"], fx.SPRAK_SV)
        return finlex_text(uri) if uri else None

    if sprak == "fi":
        return eduskunta_text(dok["edk_id"]) if dok.get("edk_id") else None
    tunnus = dok.get("eduskuntatunnus_fi") or dok.get("eduskuntatunnus_sv")
    if not tunnus:
        return None
    sv = ed.hamta_syskondokument_sv(tunnus)
    if sv and sv.get("edktunnus") and sv.get("htmlSaatavilla"):
        return eduskunta_text(sv["edktunnus"])
    return None
