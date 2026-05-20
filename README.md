# MCP-server för finsk riksdags- och rättsdata

MCP-server (Model Context Protocol) som ger AI-verktyg tillgång till finska riksdags- och rättsdata via sju verktyg med prefixet `fi_`.

## Datakällor

| Källa | Innehåll | Täckning |
|---|---|---|
| [Eduskunta Public API](https://api.eduskunta.fi) | Riksdagsdokument (`asiakirja`), ärenden (`valtiopäiväasia`), voteringar, ledamöter | Dokument och ärenden fr.o.m. valperiod 2015; sök och metadata i realtid via live API |
| [Finlex öppna data](https://opendata.finlex.fi) | Originallagar (`statute`), konsoliderad lagtext (`statute-consolidated`), propositioner (`government-proposal`), fördrag (`treaty`) i AKN XML | Originallagar fr.o.m. 1929, konsoliderad lagtext fr.o.m. ca 2000, propositioner fr.o.m. 1992, fördrag fr.o.m. 1950; synkad lokalt med fulltext |
| [Eduskunta Avoin Data](https://avoindata.eduskunta.fi) | Voteringshistorik (historisk bulk, sekundär källa) | 1996–2014; voteringar fr.o.m. 2015 hämtas via Eduskunta Public API |

Finlands tvåspråkiga lagstiftning finns på finska (`fin@`) och svenska (`swe@`) i Akoma Ntoso-format.

## MCP-verktyg

| Verktyg | Beskrivning |
|---|---|
| `fi_sok` | Aggregerad sökning över Eduskunta och Finlex |
| `fi_sok_eduskunta` | Strukturerad sökning i riksdagsdokument |
| `fi_sok_finlex` | FTS och semantisk sökning i lokal Finlex-databas |
| `fi_hamta_dokument` | Hämtar fulltext för ett riksdagsdokument via edktunnus eller riksdagsbeteckning |
| `fi_hamta_arende` | Hämtar ett riksdagsärende (valtiopäiväasia) med tillhörande dokument via ärendenummer |
| `fi_hamta_lag` | Hämtar specifik lag eller proposition från Finlex via AKN URI, år+nummer eller ELI |
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

Initial synk av Finlex-lagstiftning (kan ta flera timmar):

```
python3 01_synka_finlex.py --alla
```

Voteringshistorik 1996–2014:

```
python3 01_synka_voteringar_historik.py
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

## Tvåspråkig sökning

Sökning sker på finska med `TurkuNLP/sbert-cased-finnish-paraphrase` och på svenska med `KBLab/sentence-bert-swedish-cased`. Frågespråket detekteras automatiskt. Citat hämtas alltid från den svenska källtexten (`swe@`), aldrig via maskinöversättning.

## Licens

AGPLv3 — se LICENSE.
