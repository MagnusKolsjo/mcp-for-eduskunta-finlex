# Ändringslogg

Alla väsentliga ändringar dokumenteras här.
Formatet följer [Keep a Changelog](https://keepachangelog.com/sv/1.0.0/)
och versionshanteringen följer [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Fixat

- `synk_daglig.sh` väljer Python-tolken efter att `.env` laddats, så att `PYTHON_SOKVAG` i `.env`
  faktiskt gäller. Tidigare sattes tolken före inläsningen och inställningen hade ingen verkan.
- Samtidiga sökanrop kunde krascha servern med SIGSEGV när embeddingmodellen kördes på
  Apple-GPU:n (MPS). PyTorchs MPS-backend fyller sina kärncacher utan lås första gången de
  används, och verktygen körs på parallella arbetstrådar. Alla `encode()`-anrop i processen
  går nu genom ett gemensamt lås.

## [2.0.0] — 2026-09-26

### Ändrat

- Frågeexpansion på serversidan har inget förvalt modellnamn. `QUERY_EXPANSION_MODEL` anges alltid i `.env` (platshållare `<modellnamn>` i `config.example.env`).
- User-Agent-strängen följer huvudversionen: `mcp-for-eduskunta-finlex/2.0`.
- **Embeddings lagras som `halfvec(768)` med HNSW-index.** halfvec halverar
  vektorlagringen; på riktiga chunks gav exakt sökning samma topp-10 som `vector` i
  15 av 15 provfrågor (ordningen identisk i 14). HNSW (`m=16`, `ef_construction=64`)
  ersätter IVFFlat: det tål den dagliga synkens nya chunks och gav recall@10 0,997 mot
  0,88 för IVFFlat (probes 20) på 60 000 riktiga vektorer. Nya och nästan tomma
  databaser konverteras vid uppstart; större konverteras med `06_konvertera_vektorer.py`.
  Servern läser kolumntypen vid varje sökning och fungerar före och efter.
- Vektorsökningen sätter `hnsw.ef_search` (`FI_HNSW_EF_SEARCH`, standard 100) och
  iterativ indexsökning för filtrerade frågor. En databas som ännu har IVFFlat-index
  använder som förut `probes = 1` (`FI_IVFFLAT_PROBES`); i en provmätning gav det
  recall@10 0,64, och högre värden blir mycket långsamma på miljontals chunks. Sökning
  inom ett dokument sorterar exakt i stället för att gå via indexet.
- `03_chunka_och_embedda.py --bygg-index` bygger HNSW-index; `--lists` är borttagen och
  `--minne` sätter `maintenance_work_mem` för bygget.
- **`fi_hamta_dokument` hämtar alltid live och lagrar inget lokalt.** Riksdagsdokument
  sparades tidigare med hela råtexten vid varje hämtning. Svaret har inte längre
  formen `{"kalla": "cache", "dokument": …}`.
- **`fi_sok_i_dokument` indexerar riksdagsdokument live.** Saknas dokumentet lokalt på
  frågans språk hämtas det, delas i stycken och embeddas vid första sökningen; bara
  metadata, chunks och embeddings sparas. Träffarna bär `tecken_start`/`tecken_slut` i
  texten från `fi_hamta_dokument`. Taket `FI_MAX_CHUNKS_LIVE` (standard 1 500 stycken)
  begränsar hur stora dokument som indexeras i ett anrop. Annotationen är nu läsning mot
  källa (öppen värld). Kan indexet inte sparas (SQLite, skrivskyddad eller nere
  databas) görs sökningen i minnet och `dokument_id` är `null`.
- `fi_hamta_lag` sparar bara metadata; lagtexten hämtas live från Finlex.
- POST-anrop mot Eduskunta (sökning) stryps trådsäkert under källans tak, 450 per
  3000 s och IP (`EDUSKUNTA_POST_PER_3000S`, `EDUSKUNTA_POST_SKUR`).
- **Finlex-synken är inkrementell via `publishedSince`.** `01_synka_finlex.py` hämtar
  utan flaggor alla dokument som publicerats eller ändrats sedan senaste lyckade
  körning (med en dags marginal), oavsett dokumentets år. Tidigare synkades bara de två
  senaste åren, så nya konsoliderade lydelser av äldre lagar kom aldrig med. Tidpunkten
  sparas per dokumenttyp i `sync_status`; `--sedan` anger den uttryckligen. `--alla` och
  `--ar` synkar årsvis som förut.
- **Den inkrementella Finlex-synken går att återuppta.** Listan behandlas sida för sida
  och framsteget sparas efter varje sida (`sync_status.detaljer.pagaende`). En avbruten
  körning fortsätter på nästa sida, med samma tidpunkt, i stället för att lista om allt.
  Dokument som Finlex inte svarade för sparas i `detaljer.misslyckade` och prövas igen.
  `publicerad_sedan` flyttas fram först när listan är genomgången utan kvarstående fel,
  till starttiden för körningens första försök. `--max-sidor` begränsar en körning.
  Listanrop som möter nätverksfel prövas också igen.
- **Konsoliderad lagtext: bara senaste lydelsen, och bara verkliga ändringar hämtas.**
  Listan filtreras på `fin@latest` (Finlex `/list`), den svenska lydelsen hämtas med
  samma versionsbeteckning, och en version (AKN-URI med `@version`) som redan finns
  lokalt med text hämtas inte igen. Finlex publicerade om hela samlingen i maj 2026, så
  `publishedSince` ger nästan allt; med versionsjämförelsen blir en omkörning i huvudsak
  listanrop. Äldre lydelser hämtas live vid behov. Den inkrementella synken följer
  samma första år som den fullständiga (`--fran-ar` ändrar det), och `--i-kraft`
  begränsar till gällande författningar.
- Standardtakten mot Finlex är 40 anrop per minut (`FINLEX_RATE_LIMIT`, tidigare 20).
  Finlex anger ingen gräns och 429 hanteras med väntan.
- En redan lagrad text skrivs inte över med en väsentligt kortare (under hälften) för
  samma version; avvikelsen loggas.
- **Brytande: kräver `mcp>=2.0,<3`.** Servern bygger på `MCPServer`; `mcp.server.fastmcp`
  finns inte i mcp 2.x.
- **Brytande: http-läget kräver `MCP_API_KEY`.** Utan nyckel avbryts uppstarten med
  exitkod 2, i stället för att servern startar oautentiserad med en varning. Fel nyckel
  ger nu 403 (saknad header 401). Transporten sköts av `mcp_transport.py`.
- **Brytande: förväntade fel returneras som MCP-fel (`isError`)** i stället för svar med
  en `fel`-nyckel: okänd beteckning eller okänt ärende, okänd votering, dokument som
  saknas i cachen eller saknar embeddings, saknade argument och HTTP-fel från Eduskunta.
- Alla verktyg har titel och annotationer (läsning mot källa eller mot lokal databas)
  samt typade svar med `outputSchema`. Verktygsnamn och parametrar är oförändrade.
- `02_synka_voteringar_historik.py` avbryts med felmeddelande och exitkod 1 när
  `avoindata.eduskunta.fi` inte svarar som väntat (fel statuskod, icke-JSON, tom första
  sida). Nya flaggor: `--max-sidor` för provkörning och `--ja` för omkörning utan fråga.
- `fi_hamta_aanestys` och serverns instruktioner anger att Eduskunta Public API har
  voteringar fr.o.m. 2008-10-17.

### Tillagt

- `06_konvertera_vektorer.py` byter en befintlig databas till `halfvec(768)` och bygger
  HNSW-index. `--torrkorning` visar uppskattad tid, diskbehov under omskrivningen,
  rekommenderat `maintenance_work_mem` och slutstorlek. `--kolumn fi|sv` konverterar en
  kolumn i taget; standard är båda i samma omskrivning. `--bara-index` bygger om index.
- **Verktyget `fi_sok_voteringar_lokalt`** läser de lokalt lagrade voteringarna
  (tabellen `voteringar`, 1996–2014 ur `avoindata.eduskunta.fi`), bland dem de före
  2008-10-17 som saknas i Eduskuntas nya API. Filter på rubrik (finska och svenska),
  datumintervall, riksmöte, plenum och votering, med paginering. Svaret anger att det
  är lokal data, synkdatum och täckning.
- **Smal lokal cache för Finlex.** Chunks sparar nu sin position i texten
  (`tecken_start_fi/_sv`, `tecken_slut_fi/_sv`; nya kolumner läggs till vid uppstart).
  `03_chunka_och_embedda.py --bygg-index` skapar också fulltextindex på chunks, och när
  de finns tar skriptet bort dokumentets råtext ur `finland.dokument` så snart ett språk
  är chunkat och embeddat (`--behall-fulltext` stänger av det). FTS i `fi_sok` och
  `fi_sok_finlex` söker då i chunks; utan indexen fungerar sökningen som förut.
  Saknas råtexten hämtar skriptet texten live från källan, så dokument kan chunkas om.
  Chunkningen ligger i `chunkning.py` och live-hämtningen i `texthamtning.py`.
- `05_rensa_fulltext.py` tar bort redundant råtext ur en befintlig databas: för varje
  dokument och språk där alla chunks har embedding. Skapar först fulltextindexen på
  chunks. `--torrkorning` visar hur mycket som frigörs utan att ändra något;
  `--vacuum-full` lämnar tillbaka utrymmet till filsystemet (låser `finland.dokument`).

### Fixat

- **Finlex-synken loggade nätverksfel som 404** och behandlade dokumentet som saknat.
  DNS-fel, timeout och 5xx räknas nu som fel: de loggas som att Finlex inte svarade, och
  checkpointen (tidpunkt eller år) flyttas inte förbi dem, så att nästa körning prövar
  igen. Bara ett faktiskt 404 räknas som saknat dokument.
- **`03_chunka_och_embedda.py` embeddade aldrig nya dokument.** Urvalet krävde en
  befintlig chunk-rad utan embedding, så dokument utan några chunks alls valdes aldrig
  (utan `--tvinga`). Den dagliga synken lade alltså till dokument som aldrig blev
  sökbara semantiskt; i en driftdatabas saknade 612 dokument finska och 2 779 svenska
  chunks. Urvalet tar nu med både ochunkade och halvfärdiga dokument per språk.
- **`fi_hamta_lag` med `ar`+`nummer` och `typ="statute-consolidated"` gav 404 för
  ändrade lagar**, t.ex. grundlagen 731/1999. Konsoliderad lagtext finns i tidsversioner
  (`fin@20180817`); den oversionerade adressen `fin@` finns bara för lagar som aldrig
  ändrats. Adressen byggs nu med Finlex version `latest`, och svaret och cacheposten får
  den version som levererades. Saknas konsoliderad lydelse föreslår felet `typ="statute"`.
- `fi_hamta_lag` med en versionerad AKN-URI tolkade versionen som myndighetskod.
- `fi_hamta_lag` sade att dokumentet inte fanns när Finlex svarade med 5xx, timeout
  eller nätverksfel. Bara 404 tolkas nu som "finns inte"; övriga fel ger ett
  meddelande om att Finlex inte svarar. Synkskripten hoppar som förut över sådana poster.
- `fi_hamta_dokument` via `eduskuntatunnus` tog med söksvarets fulltext i `metadata`;
  ett svar med `max_tecken=1500` blev drygt 326 000 tecken.
- Historiksynken avbröt tidigare tyst vid fel och rapporterade körningen som lyckad.
- **Historiksynken tappade de finska rubrikerna.** Källan har en rad per språk och
  votering; den svenska raden skrev över `otsikko_fi` med NULL, och båda språkfälten fick
  den enbart finska `AanestysOtsikko`. Nu blir `otsikko_fi` ärendets rubrik plus
  voteringsrubriken ur den finska raden (`KohtaOtsikko – AanestysOtsikko`) och
  `otsikko_sv` ärendets rubrik ur den svenska raden. `upsert_votering` slår ihop raderna
  utan att nolla fält som den andra raden fyllt i. Redan synkade rader rättas vid omsynk.
- Historiksynkens `resultat` kunde motsäga rösttalen: saknade tal räknades som 0 och
  sammanslagningen behöll resultatet från den ena raden. Resultatet räknas nu om ur de
  sammanslagna `ja_roster`/`nej_roster` och lämnas tomt när något tal saknas.
- Historiksynken med `--fran-sida` förbi sista sidan gav exitkod 1 som om källan vore
  nedstängd. En tom startsida tolkas nu som "inget nytt" så länge sida 1 har rader.
- Trådsäkerhet: lås kring lat inläsning av de två embeddingmodellerna, kring
  tokenhinkarna i API-klienterna och kring skapandet av Postgres-poolen. SQLite får en
  anslutning per tråd. Omdirigeringen av fd 1/2, som samlar modellbibliotekens
  utskrifter i loggfilen, görs bara under modellinläsningen och under låset, så att två
  trådar inte kan återställa fildeskriptorerna i fel ordning.
- En cacheskrivning eller cacheläsning som misslyckas fäller inte längre
  `fi_hamta_dokument` och `fi_hamta_lag`; de hämtar då live från källan.
- Databasfel i `fi_sok`, `fi_sok_finlex` och `fi_sok_i_dokument` ger ett felmeddelande
  med orsak (t.ex. att Postgres inte svarar) i stället för ett fel utan förklaring.

### Borttaget

- SSE-transporten och den egna Starlette-/uvicorn-koden i `mcp_server.py`.

### Kända begränsningar

- Voteringar före 2008-10-17 finns inte i Eduskunta Public API. Historiksynken hämtar
  1996–2014 ur `avoindata.eduskunta.fi`, som ska ersättas vid utgången av 2026. Redan
  synkade rader ligger kvar lokalt; synken kan inte köras om när tjänsten har stängts.

## [1.2.0] — 2026-08-10

### Tillagt

- **`max_tecken` och `fran_tecken` i `fi_hamta_dokument` och `fi_hamta_lag`**, med
  standardtaket `FI_MAX_TECKEN` (60 000 tecken, konfigurerbart i `.env`). Taket gäller
  per språkversion eftersom både finsk och svensk text returneras. Kapade texter bär
  `trunkerad_fi`/`trunkerad_sv`, `tecken_totalt_fi`/`_sv` och `fortsatt_fran_tecken_*`.
  Kapningen sker på ordgräns; `max_tecken=0` ger hela texten som ett uttryckligt val.
- **`instructions`-sträng utökad** med storleksregeln, citatregeln och det skärpta
  sökkontraktet (komma = OR mellan termer, blanksteg = AND inom en term).

### Fixat

- **🔴 Omvänt skip-villkor i `03_chunka_och_embedda.py` gjorde partiellt embeddade
  dokument omöjliga att komplettera.** Urvalsfrågan i `_hamta_dokument_att_embeda`
  använde `NOT EXISTS (… AND c.<embedding-kolumn> IS NOT NULL)`, vilket hoppar över
  varje dokument som har *minst en* embedding — i stället för att hoppa över dokument
  där *alla* är klara. Rätt villkor är `EXISTS (… AND c.<embedding-kolumn> IS NULL)`.

  Konsekvensen syntes när pipelinen kraschade mitt i ett dokument: de chunks som
  hunnit få vektorer räckte för att hela dokumentet skulle betraktas som färdigt, och
  resterande chunks kunde aldrig fyllas på utan `--tvinga`. Felet upptäcktes
  2026-06-11 vid migreringen från VPS till lokal maskin, då 103 406 fi-embeddings
  saknades efter restore.

- **`fi_hamta_dokument` och `fi_hamta_lag` kunde inte begränsas och sprängde
  MCP-protokollets storleksgräns.** Materialet innehåller de största dokumenten i hela
  projektet: **75 dokument överskrider 1 000 000 tecken**, och det största har
  4 756 447 tecken finsk text. Eftersom båda språkversionerna returnerades i samma svar
  blev ett `fi_hamta_lag`-anrop mot det dokumentet **9 586 910 tecken** — drygt nio
  gånger gränsen. Anropet misslyckades alltid, oavsett hur det formulerades.
  Med standardtaket blir samma anrop 122 610 tecken.

  Databasen lagrar fortfarande hela texten — trunkeringen gäller bara svaret till
  anroparen, så `fi_sok_i_dokument` och den semantiska sökningen påverkas inte.

### Bakgrund

Genomför projektets svarskontrakt (`00-las-forst.md` → "Svarskontraktet — storlek,
trunkering, adressering och sökning"). Additiva parametrar; inga schemaändringar.

## [1.1.0] — 2026-05-22

### Lagt till
- `db.py` — `vektor_sok_i_dokument()`: semantisk pgvector-sökning scoped till ett enskilt
  cachat dokument. Tar `dokument_id`, `embedding`, `sprak` och `max_treff`. Returnerar
  chunk-träffar med `chunk_index`, `text` och `likhet` (cosine similarity).
- `mcp_server.py` — `fi_sok_i_dokument`: nytt MCP-verktyg. Tar `fraga` + `edk_id` eller
  `eduskuntatunnus` + valfritt `max_treff`. Detekterar frågespråk (fi/sv) automatiskt,
  embeddar med TurkuNLP eller KBLab och returnerar chunk-träffar från `finland.chunks`.
  Totalt 9 MCP-verktyg i Finland-strömmen.

## [1.0.1] — 2026-05-22

### Fixat
- `README.md` — installationstiden för `01_synka_finlex.py --alla` korrigerad
  från "timmar" till "dagar" (faktisk körtid 2–3 dagar)
- `README.md` — installationssteg för chunkning och embeddings lagt till direkt
  efter Finlex-synken (`03_chunka_och_embedda.py --sprak bada --tvinga` + `--bygg-index`)

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
