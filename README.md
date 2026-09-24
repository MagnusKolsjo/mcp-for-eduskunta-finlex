# MCP-server för finsk riksdags- och rättsdata

MCP-server (Model Context Protocol) som ger AI-verktyg tillgång till finska riksdags- och rättsdata via tio verktyg med prefixet `fi_`.

## Datakällor

| Källa | Innehåll | Täckning |
|---|---|---|
| [Eduskunta Public API](https://api.eduskunta.fi) | Riksdagsdokument (`asiakirja`), ärenden (`valtiopäiväasia`), voteringar, ledamöter | Dokument och ärenden fr.o.m. valperiod 2015; sök och metadata i realtid via live API |
| [Finlex öppna data](https://opendata.finlex.fi) | Originallagar (`statute`), konsoliderad lagtext (`statute-consolidated`), propositioner (`government-proposal`), fördrag (`treaty`) i AKN XML | Originallagar fr.o.m. 1929, konsoliderad lagtext fr.o.m. ca 2000, propositioner fr.o.m. 1992, fördrag fr.o.m. 1950; synkad lokalt med fulltext |
| [Eduskunta Avoin Data](https://avoindata.eduskunta.fi) | Voteringshistorik (historisk bulk, sekundär källa, avvecklas) | 1996–2014, synkad lokalt; se [Voteringshistorik 1996–2014](#voteringshistorik-19962014) |

Finlands tvåspråkiga lagstiftning finns på finska (`fin@`) och svenska (`swe@`) i Akoma Ntoso-format.

## MCP-verktyg

| Verktyg | Beskrivning |
|---|---|
| `fi_sok` | Aggregerad sökning över Eduskunta och Finlex |
| `fi_sok_eduskunta` | Strukturerad sökning i riksdagsdokument |
| `fi_sok_finlex` | FTS och semantisk sökning i lokal Finlex-databas |
| `fi_sok_i_dokument` | Semantisk sökning i ett riksdagsdokument; indexeras live vid första sökningen |
| `fi_hamta_dokument` | Hämtar fulltext för ett riksdagsdokument via edktunnus eller riksdagsbeteckning (`max_tecken`, `fran_tecken`) |
| `fi_hamta_arende` | Hämtar ett riksdagsärende (valtiopäiväasia) med tillhörande dokument via ärendenummer |
| `fi_hamta_lag` | Hämtar specifik lag eller proposition från Finlex via AKN URI eller år+nummer; `typ="statute-consolidated"` ger senaste konsoliderade lydelsen (`max_tecken`, `fran_tecken`) |
| `fi_hamta_aanestys` | Voteringsresultat live ur Eduskunta Public API (fr.o.m. 2008-10-17) |
| `fi_lista_vaalikaudet` | Valperioder och riksmöten (fr.o.m. 1907) |
| `fi_sok_voteringar_lokalt` | Lokalt lagrade voteringar 1996–2014 (synkad kopia, se nedan) |

## Krav

- Python 3.11+
- `mcp` 2.x (`mcp>=2.0,<3`)
- PostgreSQL med pgvector-tillägg eller SQLite (välj via `DATABASE_URL` — PostgreSQL krävs för semantisk sökning)
- Paket: se listan nedan

## Installation

**1. Installera beroenden**

```
pip install -r requirements.txt
```

**2. Konfigurera**

```
cp config.example.env .env
```

Redigera `.env` och ange korrekt `DATABASE_URL`.

**3. Initiera databas och kör synkskript**

Initial synk av Finlex-lagstiftning (kan ta flera dagar). Därefter räcker
`python3 01_synka_finlex.py` utan flaggor: den hämtar det som publicerats eller
ändrats sedan förra lyckade körningen (Finlex `publishedSince`).

```
python3 01_synka_finlex.py --alla
```

Bygg chunks och embeddings (kör efter avslutad synk):

```
python3 03_chunka_och_embedda.py --sprak bada --tvinga
python3 03_chunka_och_embedda.py --bygg-index
```

Voteringshistorik 1996–2014 (valfritt, se nedan):

```
python3 02_synka_voteringar_historik.py
```

**4. Konfigurera MCP-klienten**

Lägg till i klientens konfiguration:

```json
"finland": {
  "command": "/sökväg/till/python3",
  "args": ["/sökväg/till/mcp_server.py"],
  "cwd": "/sökväg/till/finland-mappen"
}
```

**5. http-läge (valfritt)**

Med `MCP_TRANSPORT=http` startar servern Streamable HTTP på
`MCP_HOST:MCP_PORT` (standard `127.0.0.1:8005`), med endpointen `/mcp`.
Varje anrop kräver `Authorization: Bearer <MCP_API_KEY>`: saknad header ger
401 och fel nyckel 403. Utan `MCP_API_KEY` startar servern inte i http-läge
(exitkod 2). SSE stöds inte.

## Voteringshistorik 1996–2014

`fi_hamta_aanestys` hämtar voteringar live ur Eduskunta Public API. Där finns
voteringar fr.o.m. plenum 94/2008 (2008-10-17); äldre voteringar saknas i det
nya API:t.

`02_synka_voteringar_historik.py` hämtar i stället hela perioden 1996–2014 ur
Eduskuntas gamla datatjänst `avoindata.eduskunta.fi` (tabellen `SaliDBAanestys`)
till tabellen `voteringar` i den lokala databasen. Verktyget
`fi_sok_voteringar_lokalt` söker i den och anger synkdatum och täckning.

- Eduskunta anger att materialet i den gamla tjänsten flyttas till den nya
  vid utgången av 2026. Något datum för nedstängning är inte angivet, och
  voteringarna före oktober 2008 finns ännu inte i det nya API:t.
- Redan synkade voteringar ligger kvar lokalt även om den gamla tjänsten
  stängs. Synken går däremot inte att köra om efter det. Den som vill ha
  materialet bör alltså köra synken medan tjänsten finns kvar.
- Svarar tjänsten inte längre som väntat (fel statuskod, HTML i stället för
  JSON, tom första sida) avbryts synken med ett felmeddelande och exitkod 1.
  Den rapporterar aldrig en lyckad körning med noll rader.
- `--max-sidor N` begränsar en provkörning; `--fran-sida N` återupptar en
  avbruten synk.

## Lokal lagring

Servern hämtar dokument live när de ska läsas: riksdagsdokument från Eduskunta
Public API och lagtext från Finlex (konsoliderad lydelse via versionen `latest`).
Lokalt lagras bara det som behövs för sökningen i databasen:

- metadata per dokument,
- chunks (textstycken) med embeddings och sin position i texten
  (`tecken_start`/`tecken_slut` per språk),
- fulltextindex på chunks (skapas av `03_chunka_och_embedda.py --bygg-index`).

Synken lagrar texten tillfälligt tills den chunkats; när fulltextindexen på chunks
finns tar `03_chunka_och_embedda.py` bort råtexten efter embedding
(`--behall-fulltext` stänger av det). Riksdagsdokument indexeras vid den första
semantiska sökningen i dem (`fi_sok_i_dokument`); taket `FI_MAX_CHUNKS_LIVE`
begränsar storleken.

En befintlig databas med lagrad råtext rensas med ett separat steg:

```
python3 05_rensa_fulltext.py --torrkorning   # visar vad som frigörs
python3 05_rensa_fulltext.py --vacuum-full   # rensar och lämnar tillbaka utrymmet
```

Fritextsökning i riksdagsdokument går alltid mot Eduskuntas eget sök-API. Dess
POST-anrop stryps under källans tak, 450 per 3000 sekunder och IP-adress.

## Tvåspråkig sökning

Sökning sker på finska med `TurkuNLP/sbert-cased-finnish-paraphrase` och på svenska med `KBLab/sentence-bert-swedish-cased`. Frågespråket detekteras automatiskt. Citat hämtas alltid från den svenska källtexten (`swe@`), aldrig via maskinöversättning.

## Licens

AGPLv3 — se LICENSE.
