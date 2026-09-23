# MCP-server för finsk riksdags- och rättsdata

MCP-server (Model Context Protocol) som ger AI-verktyg tillgång till finska riksdags- och rättsdata via sju verktyg med prefixet `fi_`.

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
| `fi_hamta_dokument` | Hämtar fulltext för ett riksdagsdokument via edktunnus eller riksdagsbeteckning (`max_tecken`, `fran_tecken`) |
| `fi_hamta_arende` | Hämtar ett riksdagsärende (valtiopäiväasia) med tillhörande dokument via ärendenummer |
| `fi_hamta_lag` | Hämtar specifik lag eller proposition från Finlex via AKN URI, år+nummer eller ELI (`max_tecken`, `fran_tecken`) |
| `fi_hamta_aanestys` | Voteringsresultat för en specifik votering |
| `fi_lista_vaalikaudet` | Valperioder och riksmöten (fr.o.m. 1907) |

## Krav

- Python 3.11+
- PostgreSQL med pgvector-tillägg eller SQLite (välj via `DATABASE_URL` — PostgreSQL krävs för semantisk sökning)
- Paket: se listan nedan

## Installation

**1. Installera beroenden**

```
pip install mcp psycopg2-binary python-dotenv requests httpx lxml sentence-transformers pgvector langdetect
```

**2. Konfigurera**

```
cp config.example.env .env
```

Redigera `.env` och ange korrekt `DATABASE_URL`.

**3. Initiera databas och kör synkskript**

Initial synk av Finlex-lagstiftning (kan ta flera dagar):

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

## Voteringshistorik 1996–2014

`fi_hamta_aanestys` hämtar voteringar live ur Eduskunta Public API. Där finns
voteringar fr.o.m. plenum 94/2008 (2008-10-17); äldre voteringar saknas i det
nya API:t.

`02_synka_voteringar_historik.py` hämtar i stället hela perioden 1996–2014 ur
Eduskuntas gamla datatjänst `avoindata.eduskunta.fi` (tabellen `SaliDBAanestys`)
till tabellen `voteringar` i den lokala databasen. Inget MCP-verktyg läser
tabellen; den är ett lokalt arkiv för egna frågor mot databasen.

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

## Tvåspråkig sökning

Sökning sker på finska med `TurkuNLP/sbert-cased-finnish-paraphrase` och på svenska med `KBLab/sentence-bert-swedish-cased`. Frågespråket detekteras automatiskt. Citat hämtas alltid från den svenska källtexten (`swe@`), aldrig via maskinöversättning.

## Licens

AGPLv3 — se LICENSE.
