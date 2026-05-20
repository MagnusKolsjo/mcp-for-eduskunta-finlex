# Ändringslogg

Alla väsentliga ändringar dokumenteras här.
Formatet följer [Keep a Changelog](https://keepachangelog.com/sv/1.0.0/)
och versionshanteringen följer [Semantic Versioning](https://semver.org/).

## [Opublicerad] — 2026-05-17

### Lagt till
- `eduskunta_client.py` — `hamta_syskondokument_sv()`: ny funktion som söker efter det svenska
  syskondokumentet för en riksdagsbeteckning via POST /search med `kielikoodi=sv`-filter.
  api.eduskunta.fi lagrar fi- och sv-versioner som separata dokument med samma eduskuntatunnus.
- `mcp_server.py` — `fi_hamta_dokument`: hämtar nu alltid både finsk och svensk version.
  Nytt flöde: finskt primärdokument hämtas, sedan söks svenska syskondokumentet via
  `hamta_syskondokument_sv()`. Svaret innehåller `fulltext_fi` (för sökning/analys) och
  `fulltext_sv` (för citat) som explicita separata fält. `sprak`-parametern borttagen.
  Cache-kontroll uppdaterad: returnerar cachad post bara när båda språken finns.
- `mcp_server.py` — `fi_hamta_lag`: hämtar nu alltid både `fin@`- och `swe@`-URI från Finlex.
  Returnerar `fulltext_fi` och `fulltext_sv` som explicita separata fält. `sprak`-parametern
  borttagen. Fallback: om ett språk saknas loggas varning men svaret returneras ändå med
  det tillgängliga språket.
- `mcp_server.py` — `fi_sok` och `fi_sok_finlex`: söker nu alltid i **båda** språkens
  FTS-index och embeddingmodeller parallellt. `sprak`-parametern borttagen. Svaret
  innehåller separata `fi`- och `sv`-resultatlistor. Dokument som dyker upp i båda är
  säkra träffar; dokument som bara finns i ett av språken är komplementära och ger bredare
  täckning vid komplexa frågor eller vid svag finskt begreppsstöd.

- `02_synka_voteringar_historik.py` — API-svarsformat korrigerat: `rowData` är en lista
  av listor (inte lista av dicts). Kolumnnamnen hämtas nu från `columnNames` och zippas
  med radvärdena. Fältnamnen uppdaterade till faktiska kolumnnamn: `IstuntoVPVuosi`,
  `AanestysNumero`, `AanestysAlkuaika`, `AanestysOtsikko`, `AanestysTulosJaa/Ei/Tyhjia/Poissa`,
  `KieliId` (1=fi, 2=sv). `Tulos`-fältet finns inte — resultat beräknas nu från Jaa > Ei.
  Verifierat: 22 644 voteringar 1996–2014 synkade.

### Fixat
- `eduskunta_client.py` — `hamta_asiakirja_metadata()`: söksvarets metadata innehåller
  `fullText` (261k tecken) och `fullTextSnippet` — dessa strimlas nu bort. Metadata
  reducerades från 262k till ~2k tecken. Fulltext hämtas separat via `hamta_html_fulltext()`
  när `hamta_fulltext=True` anges.
- `mcp_server.py` — `fi_hamta_dokument`: cache-returen respekterade inte `hamta_fulltext=False`
  — returnerade fulltext_fi/sv/html/md från DB-cachen oavsett flaggan. Fixat: fulltext-fält
  strimlas från cachad dict när `hamta_fulltext=False`.
- `eduskunta_client.py` — `hamta_asiakirja_metadata()` och `hamta_asiakirja_via_eduskuntatunnus()`:
  GET `/asiakirjat/edktunnus/{id}` och `/asiakirjat/eduskuntatunnus/{tunnus}` är trasiga
  server-side — de kräver egenskapen `snippet` men accepterar den varken som query-param
  eller request body (verifierat 2026-05-17 mot live-API). Båda funktionerna byggs nu om
  till POST `/search` med expression-filter på `edktunnus` resp. `eduskuntatunnus`.
  Metadata är identisk. Dokumenterat i koden.

## [Opublicerad] — 2026-05-16

### Fixat
- `mcp_server.py` — `fi_hamta_aanestys`: `senaste=True`, `eduskuntatunnus` och
  `istuntotunnus` returnerade rådata med individuella ledamotsröster — API:et levererar
  en list-av-listor-struktur (~1,3 MB för 100 voteringar). Ny hjälpfunktion
  `_summarisera_aanestykset()` plattar ut strukturen och returnerar en rad per votering
  med `aanestystulos` (aggregerade ja/ei/poissa), partisplit och rubrik (fi+sv).
  Individuella ledamotsröster utelämnas. Enskild votering via `aanestystunnus`
  returneras oförändrad.
- `finlex_client.py` — `_hamta_titel()` ersatt med `_hamta_doc_titel()`: Finlex placerar
  titeln i `<docTitle>` i dokumentkroppen, inte i `<FRBRname>` i meta-sektionen. Tidigare
  returnerades alltid tom sträng. Nu söks `docTitle` med och utan AKN-namespace, och
  rätt språkfält (titel_fi/titel_sv) sätts via `FRBRlanguage/@language` i FRBRExpression.
- `finlex_client.py` — `parsad_akn_metadata()`: ersatte `a or b`-kedjor på lxml-element
  med en intern `_hitta()`-hjälpfunktion som testar `is not None`. lxml-element är aldrig
  falsy och `or`-mönstret gav FutureWarning i lxml 6.x samt potentiellt fel fallback-element.
- `db.py` — `upsert_dokument()`: `html_saatavilla` skickades som `1`/`0` (integer) till
  PostgreSQL:s `BOOLEAN`-kolumn. Fixat till `bool()`-konvertering för Postgres och
  `1`/`0` för SQLite (backends hanteras nu separat).
- `mcp_server.py` — saknad `import contextlib as _contextlib` och felplacerat
  `import os as _os` (inline före `@_contextlib.contextmanager`). Åtgärdat: import
  tillagd i modulhuvudet, felplacerad import borttagen.
- `01_synka_finlex.py` — saknad `import os` orsakade NameError i `installera_schema()`.
- `mcp_server.py` — `_eduskunta_treff_till_dict()`: api.eduskunta.fi:s sökresultat
  wrappar varje träff i ett typstyrt nästlat objekt (`r["asiakirja"]`, `r["aanestys"]`
  osv.) — inte platt som individuella dokumentendpoints. Alla fält var null eftersom
  koden läste från wrapper-roten. Fixat: dokumentobjektet extraheras via `r[type.lower()]`
  innan fältmappning.

### Lagt till
- `requirements.txt` — saknad fil; listar alla Python-beroenden inkl. `langdetect>=1.0.9`
  för automatisk finska/svenska-detektering i sökfrågor.
- `synk_daglig.sh` — shell-skript som kör finlex-synk + embedding dagligen.
- `01_synka_finlex.py --installera-schema` — installerar schemalagt jobb via cron eller
  launchd. Styrs av `SCHEMALAGGARE` och `CRON_SCHEMA` i `.env` (mönster från ström 9).

### Lagt till
- `eduskunta_client.py` — wrapper mot api.eduskunta.fi: POST /search, HTML-fulltext,
  XML-redirect, voteringsendpoints (enskild, per session, per ärende, senaste),
  ledamöter, referensdata och aggregationer
- `finlex_client.py` — wrapper mot opendata.finlex.fi: /list-paginering (JSON),
  AKN XML-hämtning, lxml-parsning av `<act>` och `<doc>`, språkbyte i URI
- `db.py` — PostgreSQL (schema: finland) + SQLite-fallback. Tabeller: dokument,
  chunks (embedding_fi + embedding_sv), voteringar, ledamoter, sync_status.
  FTS-index för finska (finnish) och svenska (swedish)
- `db/schema_postgres.sql` — fullständigt PostgreSQL-schema med pgvector
- `db/schema_sqlite.sql` — SQLite-fallback utan vektorkolumner
- `mcp_server.py` — 7 MCP-verktyg: fi_sok, fi_sok_eduskunta, fi_sok_finlex,
  fi_hamta_dokument, fi_hamta_lag, fi_hamta_aanestys, fi_lista_vaalikaudet.
  Tvåspråkig embedding (fi+sv), frågespråksdetektering, FD 1-skydd
- `01_synka_finlex.py` — bulk-synk via /list: statute (1929+), statute-consolidated,
  government-proposal (1992+), treaty. Inkrementell via checkpoint
- `02_synka_voteringar_historik.py` — voteringshistorik 1996–2014 från
  avoindata.eduskunta.fi (SaliDBAanestys)
- `prompts/expansion_prompt.txt` — query-expansion för finska + finlandssvenska
  juridiska termer
- `config.example.env` — konfigurationsmall
- `CHANGELOG.md`, `.gitignore`
