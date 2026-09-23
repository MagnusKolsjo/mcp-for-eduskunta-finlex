# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Magnus Kolsjö

"""
chunkning.py — Delar dokumenttext i stycken för embedding

Används av 03_chunka_och_embedda.py (Finlex i bulk) och av mcp_server.py
(riksdagsdokument som indexeras vid första semantiska sökningen). Samma
funktion på båda ställena ger samma chunkgränser och offsets.
"""

import os
import re

CHUNK_MAX_TECKEN     = int(os.getenv("CHUNK_MAX_TECKEN",     "800"))
CHUNK_MIN_TECKEN     = int(os.getenv("CHUNK_MIN_TECKEN",     "100"))
CHUNK_OVERLAP_TECKEN = int(os.getenv("CHUNK_OVERLAP_TECKEN", "200"))


def chunka_text(text: str) -> list[dict]:
    """
    Delar upp text i stycken på ~CHUNK_MAX_TECKEN tecken med CHUNK_OVERLAP_TECKEN
    tecken bakåtöverlapp mellan intilliggande chunks.

    Splittningsstrategi (i fallande prioritet):
      1. Paragrafrubriker (###) — naturliga gränser i AKN-extraherad text
      2. Dubbla radbrytningar (styckebrytning)
      3. Meningsgränser (. ? !) om stycket fortfarande är för långt
      4. Hårt snitt på CHUNK_MAX_TECKEN om inget bättre alternativ finns

    Överlapp: varje chunk inleds med de sista CHUNK_OVERLAP_TECKEN tecknen från
    föregående stycke så att kontexten bevaras vid chunkgränser.

    Returnerar lista med dicts:
      {chunk_index, text, tecken_start, tecken_slut}
    tecken_start/slut pekar på det primära styckets position i originaltexten
    (överlappstexten är inte medräknad i positionerna).
    """
    if not text:
        return []

    # Steg 1: dela på §-/kapitelrubriker
    delar = re.split(r"(?=\n###\s)", text)

    stycken: list[str] = []
    for del_ in delar:
        del_ = del_.strip()
        if not del_:
            continue
        if len(del_) <= CHUNK_MAX_TECKEN:
            stycken.append(del_)
        else:
            # Dela ytterligare på dubbla radbrytningar
            understycken = re.split(r"\n{2,}", del_)
            nuvarande = ""
            for us in understycken:
                us = us.strip()
                if not us:
                    continue
                if len(nuvarande) + len(us) + 2 <= CHUNK_MAX_TECKEN:
                    nuvarande = (nuvarande + "\n\n" + us).strip() if nuvarande else us
                else:
                    if nuvarande:
                        stycken.append(nuvarande)
                    if len(us) > CHUNK_MAX_TECKEN:
                        # Dela på meningsgränser
                        meningar = re.split(r"(?<=[.?!])\s+", us)
                        nuvarande = ""
                        for m in meningar:
                            if len(nuvarande) + len(m) + 1 <= CHUNK_MAX_TECKEN:
                                nuvarande = (nuvarande + " " + m).strip() if nuvarande else m
                            else:
                                if nuvarande:
                                    stycken.append(nuvarande)
                                # Hårt snitt om meningen är för lång
                                while len(m) > CHUNK_MAX_TECKEN:
                                    stycken.append(m[:CHUNK_MAX_TECKEN])
                                    m = m[CHUNK_MAX_TECKEN:]
                                nuvarande = m
                        if nuvarande:
                            stycken.append(nuvarande)
                            nuvarande = ""
                    else:
                        nuvarande = us
            if nuvarande:
                stycken.append(nuvarande)

    # Bygg chunks med positionsinfo och bakåtöverlapp
    chunks          = []
    pos             = 0
    index           = 0
    foregaende_text = ""
    for s in stycken:
        s = s.strip()
        if len(s) < CHUNK_MIN_TECKEN:
            continue

        # Bakåtöverlapp: inled med slutet av föregående stycke för bättre
        # kontexttäckning vid chunkgränser (t.ex. lagparagrafer som hänvisar bakåt)
        if foregaende_text and CHUNK_OVERLAP_TECKEN > 0:
            overlapp   = foregaende_text[-CHUNK_OVERLAP_TECKEN:]
            chunk_text = overlapp + "\n\n" + s
        else:
            chunk_text = s

        idx   = text.find(s[:40], pos)
        start = idx if idx >= 0 else pos
        slut  = start + len(s)
        chunks.append({
            "chunk_index":  index,
            "text":         chunk_text,
            "tecken_start": start,
            "tecken_slut":  slut,
        })
        pos             = max(pos, slut)
        index          += 1
        foregaende_text = s

    return chunks
