# MCP_ABAP_Explorer

## Installazione

Da PowerShell, senza attivare l'ambiente virtuale:

```powershell
py -m venv "C:\Progetto_AI\MCP_ABAP_Explorer\.venv"
& "C:\Progetto_AI\MCP_ABAP_Explorer\.venv\Scripts\python.exe" -m pip install -r "C:\Progetto_AI\MCP_ABAP_Explorer\requirements.txt"
```

Dipendenze: `mcp`, `anyio`, `httpx`, `magika`, `jsonschema`. Magika installa anche ONNX Runtime. Ambiente verificato con Python 3.14; su sistemi senza wheel ONNX compatibili usare Python 3.12.

## Avvio

```powershell
& "C:\Progetto_AI\MCP_ABAP_Explorer\.venv\Scripts\python.exe" "C:\Progetto_AI\MCP_ABAP_Explorer\app.py"
```

URL: **http://127.0.0.1:8036**. Il browser viene aperto automaticamente; `--no-browser` lo disabilita. `Ctrl+C` termina il server. Per cambiare porta usare `--port 8037` dopo averne verificato la disponibilita. In caso di conflitto la porta non cambia automaticamente: il bind esclusivo impedisce istanze ambigue.

## Client MCP

La configurazione server e in `mcp_servers.json`. Il default retrocompatibile e `sap-cloud`, con endpoint `https://mcp-abap.marianzeis.de/mcp` su Streamable HTTP. `sap-on-premise` avvia `c:\Users\IT05020\.vscode\extensions\harieprasad.abap-mcp-2.0.0\bin\abap-mcp-windows.exe` con argomenti `["-y", "abap-mcp"]` su stdio. Il file deve essere presente sulla macchina; non vengono usati npx o download automatici. La selezione SAP nell'interfaccia viene mantenuta nel browser. Trasporto, discovery ed esecuzione sono isolati dietro `IMcpProvider` e `create_provider()` in `mcp_providers.py`.

Per la connessione On-Premise, i default non segreti sono URL `http://172.20.253.162:8000`, istanza `00`, sistema `S4B`, client `800`; restano modificabili nella UI. Le vecchie preferenze di connessione del browser vengono ignorate usando una nuova chiave di sessione (`mcp-connection-v2-...`); le successive modifiche ai soli parametri non segreti vengono ricordate nella scheda. `SAP_USERNAME` e password vengono inseriti una sola volta e mantenuti nei campi in memoria per discovery, interrogazioni e diagnostica, anche dopo errori o cambi di server. Non vengono inviati al server Cloud. La password e mascherata e non usa autocomplete. Le credenziali non vengono salvate in session/local storage e vengono svuotate all'uscita dalla pagina, inclusi chiusura, navigazione e ricaricamento; il ripristino dalla cache di navigazione le svuota nuovamente. I valori viaggiano solo nel POST locale protetto da token e sono passati al processo ADT tramite le variabili richieste dal package (`SAP_URL`, `SAP_USERNAME`, `SAP_PASSWORD`, `SAP_CLIENT`). Il backend li redige da stderr e risposte MCP prima di conservarli. Per automazioni senza UI si possono impostare le stesse variabili nell'ambiente del processo. In PowerShell si puo ottenere l'utente/password senza inserirli nella cronologia dei comandi:

```powershell
$sapCredential = Get-Credential
$env:SAP_URL = 'https://host-sap'
$env:SAP_CLIENT = '100'
$env:SAP_USERNAME = $sapCredential.UserName
$env:SAP_PASSWORD = $sapCredential.GetNetworkCredential().Password
& '.\.venv\Scripts\python.exe' '.\app.py'
Remove-Variable sapCredential
```

Le credenziali UI sono effimere e non vengono scritte in file, browser storage o log. Non inserire segreti nel JSON di configurazione o in file versionati. Il template `.env.example` documenta i nomi, ma non viene caricato automaticamente. Per un MCP ADT diverso, modificare `command`, `args` e `env` in `mcp_servers.json`; il protocollo deve essere stdio.

I sei campi di connessione sono visibili solo selezionando **SAP On-Premise**. **Aggiorna tool ADT** invia `server_id` e `connection_options` via POST a `/api/tools`; anche `/api/invoke` inoltra i valori correnti. La modifica di URL, istanza, ID sistema o client invalida il catalogo precedente e richiede un nuovo aggiornamento. Ogni operazione apre e inizializza una sessione MCP dedicata e chiude il processo stdio al termine, anche in caso di errore: non esiste una connessione condivisa con credenziali obsolete. Per aggiungere server dello stesso trasporto basta la configurazione; nuovi trasporti richiedono un provider nella factory. Nuove UI specifiche richiedono un ramo dedicato alla selezione.

Il processo MCP riceve `SAP_LANGUAGE=IT` e `TLS_REJECT_UNAUTHORIZED=0` dalla configurazione, oltre ai dati SAP della UI. Sono accettati URL HTTP e HTTPS senza credenziali incorporate, query o frammenti. **HTTP invia credenziali e dati senza cifratura; TLS_REJECT_UNAUTHORIZED=0 disabilita la verifica dei certificati nel server MCP.** Usare questa configurazione solo su rete fidata. La diagnostica Python HTTPS mantiene invece la verifica dei certificati attiva e puo quindi segnalare errori TLS anche se il processo MCP si collega.

La sezione **SAP On-Premise Diagnostics** esegue, su richiesta, check delle quattro env, DNS/TCP, `GET /sap/bc/adt/discovery` con il parametro `sap-client`, classificazione HTTP (200/401/403/404/SSL), avvio MCP stdio e `list_tools`. Distingue errori di startup da errori di discovery e mostra nomi/count tool, stato autenticazione, permessi e suggerimenti. **Scarica report JSON** esporta solo esiti e dettagli sanitizzati, mai username/password. Il check 200 conferma l'accesso all'endpoint; non certifica i permessi per ogni operazione ADT.

I campi UI **Numero istanza** e **ID sistema** vengono mantenuti e inoltrati al processo MCP come `SAP_SYSNR` e `SAP_SYSTEM_ID`. Sono parametri aggiuntivi: la configurazione funzionante di abap-mcp non li richiede e il server potrebbe ignorarli. L'URL SAP e passato come inserito nella UI; la porta non viene derivata dal numero istanza.

Il Cloud mantiene i nove tool compatibili esistenti. Per On-Premise, nomi e schemi sono letti dal server SAP selezionato a ogni caricamento della lista e prima delle chiamate LLM; l'input manuale e validato con lo schema runtime originale. In modalita On-Premise l'LLM usa il server selezionato come fonte runtime e non la whitelist Cloud. Se un server e offline, l'errore resta visibile e si puo cambiare server senza riavviare la sessione.

Per compatibilita, `/api/invoke` e `/api/search` senza `server_id` continuano a selezionare `sap-cloud`; `MCP_DEFAULT_SERVER` puo cambiare il default. `SAP_CLOUD_MCP_ENDPOINT` e `MCP_SERVERS_CONFIG` consentono override tramite ambiente.

In modalita Python non vengono usati LLM, Ollama o API key. La modalita LLM usa esclusivamente Ollama locale. Gli argomenti dei tool, incluso l'eventuale codice da analizzare, vengono inviati al server MCP selezionato: non inserire dati aziendali riservati se la configurazione non e approvata.

Timeout: 15 secondi per connettersi, 60 per la risposta MCP, 90 complessivi. Una richiesta alla volta. L'anteprima si aggiorna al completamento del tool, non token per token.

## Python Oppure Ollama

Il controllo Modalita di esecuzione permette di scegliere:

- **Python diretto** (predefinito): selezione manuale dei tool; in Cloud conserva i nove tool e il formato precedente, in On-Premise carica i tool ADT disponibili e i relativi schemi. Funziona anche con Ollama spento.
- **Ollama locale**: richiesta discorsiva, scelta del modello e orchestrazione automatica dei tool MCP. Non usa LM Studio, provider cloud o endpoint LLM esterni.

Ollama deve essere gia in esecuzione su **http://127.0.0.1:11434**. Il menu viene ricavato da `/api/tags` e `/api/show`: mostra solo modelli installati con capability `completion` e `tools`. Esclude modelli di embedding, modelli senza tool-calling e quelli con metadati remoti o suffisso `:cloud`. Il catalogo viene ricontrollato prima di ogni inferenza; nessun modello viene scaricato automaticamente. Il pulsante con icona di aggiornamento rilegge il catalogo. Il supporto dichiarato dal runtime non garantisce precisione o prestazioni del singolo modello; modelli grandi o eseguiti su CPU possono andare in timeout.

La scelta del modello e sempre esplicita: nessun modello viene preselezionato automaticamente. Nella verifica locale del 19 ottobre 2026, `qwen3.5:latest` ha completato il ciclo richiesta -> tool MCP -> sintesi, con risposte originali consultabili ed export JSON/TXT/Markdown validati da Magika. I tentativi con altri modelli hanno mostrato timeout o assenza di chiamate riconoscibili: la capability dichiarata non equivale a una certificazione di funzionamento di tutti i modelli elencati.

Il modello usa function/tool-calling, non il protocollo MCP direttamente. Il bridge Python legge gli schemi live del server e offre soltanto i nove tool autorizzati. Se la richiesta nomina esplicitamente un tool, vengono offerti soltanto i tool nominati, riducendo il costo del contesto. In assenza di nomi espliciti il modello puo scegliere tra tutti e nove. Gli schemi inviati al modello sono compattati; gli argomenti vengono comunque validati con JSON Schema originale prima di chiamare MCP. Non sono consentiti parametri sconosciuti o nomi di tool esterni al catalogo.

Sono supportati sia `tool_calls` nativi di Ollama sia messaggi interamente JSON, anche racchiusi in un blocco JSON, nella forma `{"name":"tool","arguments":{...}}`. Non vengono estratti comandi da prosa libera. Risposte senza alcuna chiamata MCP vengono rifiutate dopo un solo richiamo al modello: la UI non presenta una risposta senza fonti come se fosse stata verificata.

Limiti della modalita LLM: 12000 caratteri per la richiesta, quattro chiamate MCP, rilevamento di chiamate duplicate, 180 secondi per ciascuna risposta Ollama e 300 secondi complessivi. I risultati molto lunghi vengono abbreviati soltanto nel contesto del modello (con avviso nel log); anteprima grezza e JSON esportato conservano le risposte originali nei limiti complessivi di 5 MB. I testi provenienti dai tool sono trattati come dati non fidati nel prompt di sistema; la sintesi rimane generativa e va verificata contro le fonti.

L'anteprima principale mostra la sintesi del modello. La sezione espandibile **Risposte MCP originali** mostra i dati restituiti dai tool, mentre il log riporta modello e chiamate. JSON conserva modello, richiesta, argomenti, sintesi e trace completo; TXT e Markdown esportano la sintesi. Magika viene applicata anche agli export della modalita LLM. Cambiare modalita invalida il vecchio risultato e mantiene bozze separate degli input.

Le API aggiuntive sono `GET /api/models` e `POST /api/invoke` con `mode: "llm"`, `model` e `text`. Il valore predefinito di `mode` rimane `python` per compatibilita con i client precedenti.

## Strumenti Disponibili

I nove nomi tecnici e i parametri sono stati verificati tramite `list_tools` sul server remoto:

| Tool | Input | Argomenti inviati |
| --- | --- | --- |
| `search` | Ricerca documentazione | `query`, `k` |
| `fetch` | ID esatto restituito da search | `id` |
| `abap_lint` | Codice ABAP | `code` |
| `abap_feature_matrix` | Funzionalita, anche vuoto per tutte | `query` |
| `sap_community_search` | Ricerca nella community | `query` |
| `sap_search_objects` | Nome oggetto o argomento, facoltativo | `query`, `system_type` |
| `sap_get_object_details` | Tipo e nome separati da virgola | `object_type`, `object_name`, `system_type` |
| `sap_discovery_center_search` | Nome servizio BTP o argomento | `query` |
| `sap_discovery_center_service` | UUID o nome di servizio | `serviceId` |

Per i dettagli oggetto, una stringa come `CLAS, CL_ABAP_REGEX` viene suddivisa sui due parametri; le virgolette esterne sul nome vengono rimosse. Valori mancanti o virgole aggiuntive sono rifiutati. Per ricerca e dettaglio oggetti si puo selezionare il sistema SAP; il default resta `public_cloud`. Un oggetto classico puo risultare `found: false` nel Public Cloud e risultare presente come `classicAPI` in On-Premise.

Limiti: 4000 caratteri per gli input testuali, **50 KB UTF-8** per il codice di `abap_lint`. Il codice mantiene spazi, a capo, virgolette e tag letterali, senza sanificazione. Anche ID e nomi oggetto non vengono riscritti. Gli input di ricerca dovrebbero essere in inglese, come indicato dal server; non viene effettuata una traduzione automatica.

Le opzioni non esposte usano i default del server (per esempio versione Cloud del lint, filtri Clean Core della ricerca oggetti, valuta USD e pricing/roadmap per i servizi). Il sistema di destinazione per ricerca e dettaglio oggetti e selezionabile nell'interfaccia; il default e `public_cloud`. Il controllo del numero di risultati e visibile solo per `search`, dove viene inviato come `k`. Per `abap_feature_matrix` e `sap_search_objects` e possibile inviare input vuoto.

Cambiare strumento invalida il risultato precedente; ogni tool conserva una bozza del proprio input finche la pagina resta aperta. L'API UI e `POST /api/invoke` con `tool`, `text` ed eventualmente `count`. `POST /api/search` resta compatibile con i precedenti client (`query`, `count`).

Gli export JSON conservano `tool`, `arguments` e risposta MCP completa. TXT mantiene `content.text`; Markdown aggiunge il nome del tool ai metadati. Magika valida gli stessi byte destinati al file per tutti gli strumenti.

## Raffinamento Della Ricerca

La sanificazione e il fallback si applicano soltanto al tool **search**. La sanificazione e generica, non dipende da un particolare nome SAP. Rimuove tag come `@search`/`@fetch` e formule italiane ricorrenti come "Cerca documentazione per" o "Trova informazioni su". Il testo tra virgolette ha priorita; sono supportate virgolette doppie, singole, tipografiche e caporali. Una query senza parole chiave viene rifiutata prima della connessione.

La cascata esegue al massimo due tentativi nella stessa sessione MCP e nel limite complessivo di 90 secondi, entrambi derivati esclusivamente dall'input:

1. Query sanificata.
2. Per nomi con underscore, parole separate da spazi, eliminando soltanto connettivi inglesi (`OF`, `AN`, `AND`, `THE`, `TO`, `FOR`, `A`, `IN`). I prefissi degli identificatori non vengono rimossi.

Non esistono associazioni di contesto a nomi predefiniti di tabelle o function module. Un nome SAP viene cercato soltanto se presente nell'input dell'utente; il fallback non aggiunge termini o nomi di oggetti. Se non esiste una variante distinta, viene eseguita una sola query.

Lo score di un tentativo e il massimo score numerico finito dei suoi risultati. Il parser legge `structuredContent.results`, oppure il JSON di `content.text`. Risultati vuoti o score mancanti/non validi non soddisfano la soglia **0.06**. La cascata si ferma al primo tentativo con risultati e score almeno 0.06. Se nessuno supera la soglia, viene mostrata la risposta migliore: a parita di score si preferisce una risposta non vuota, poi il primo tentativo. Gli errori di rete/protocollo/tool restano errori, non vengono trattati come punteggi bassi.

Il log sotto l'anteprima riporta query ottimizzata, eventuale fallback, score dei tentativi e query selezionata. L'export JSON conserva anche query originale, tentativi e log. L'anteprima e i file esportati appartengono sempre alla risposta selezionata. Un punteggio basso rimane segnalato, senza deviare la ricerca verso una tabella generica.

## Stile

Riprende APP_COMPILA_WORD: `ThreadingHTTPServer`, HTML/CSS/JS separati, Segoe UI, sfondo `#f5f5f5`, bianco, testo `#1a1a1a`, verde `#059669`/`#047857`, bordi `#e2e8f0`, larghezza 1080 px, sezioni numerate, breakpoint 700 px. Stesso asset locale Lucide, senza CDN o Node.js.

## Esportazione

- **JSON:** query, endpoint, timestamp UTC, testo grezzo e intera risposta MCP, inclusi `structuredContent` e contenuti non testuali.
- **TXT:** i blocchi `content.text` concatenati senza riscritture.
- **Markdown:** metadati e testo grezzo in un blocco delimitato; se manca testo, risposta MCP serializzata.

I file UTF-8 vengono salvati nella cartella dell'app con nome univoco `mcp_abap_DATA_ORA_ID.estensione`. La UI mostra percorso, dimensione, MIME rilevato e download. Le risposte restano in memoria un'ora, massimo 16; scadenza o riavvio richiedono una nuova ricerca. I file salvati restano su disco. Limite risposta/file: 5 MB.

**Prima di scrivere**, Magika analizza gli stessi byte formattati destinati al file. Contenuti non riconosciuti come testuali vengono rifiutati. Il JSON passa anche dal parser standard. La UI riporta il MIME effettivo: per esempio `application/json`, `text/plain` o `text/markdown`. Un TXT contenente JSON puo essere rilevato come JSON; un Markdown semplice come testo. Non viene forzato il risultato in base all'estensione. Magika identifica il contenuto, non ne certifica veridicita, sicurezza o sintassi Markdown.

Il server ascolta solo su loopback; le POST richiedono token di sessione e Host locale. Il testo remoto non viene interpretato come HTML. I download sono limitati ai file esportati nella sessione corrente.

## Porte Del Launcher

Estratte il 19 ottobre 2026 da `LAUNCHER_DASHBOARD/launcher_dashboard.py`:

| Applicazione | Porta |
| --- | --- |
| Inlay Studio | 3002 |
| Jarvis2 | 8008 |
| SecureVault | 8011 |
| Estrattore PDF AI | 8020 |
| Diagnostics Uniemens | 8021 |
| Monitoraggio Estrazione CF | 8022 |
| Word in Markdown | 8027 |
| Compila Word | 8030 |
| TxtToObsidian | 8035 |
| Trascrizioni YouTube | 8501 |
| Word in Mermaid | 8502 |
| Launcher Dashboard | 8040-8049 riservate |

Verifica Cedolino e desktop senza porta HTTP. Inlay puo ridefinire porta/URL tramite metadata locale. L'API runtime del launcher su 8040 non ha risposto entro 5 secondi: la tabella descrive le porte configurate, non garantisce quelle runtime di Inlay. Il controllo TCP tra 8000 e 8999 ha rilevato listener su 8040 e 8053. **8036** era libera e non registrata ed e la porta assegnata a questa app.

L'app e ora registrata nel Launcher Dashboard con ID `mcp-abap-explorer`, categoria Sviluppo e automazione e porta **8036**. La scheda usa lo stile standard delle altre applicazioni e l'interprete della `.venv` locale. Il launcher passa `--port 8036 --no-browser`, apre la pagina appena pronta e riutilizza l'istanza esistente soltanto se riconosce il titolo HTML dell'app. I processi avviati esternamente non vengono terminati dal launcher.

Per ripetere il controllo:

```powershell
Get-NetTCPConnection -State Listen | Sort-Object LocalPort -Unique | Select-Object LocalAddress, LocalPort, OwningProcess
```

## Test

```powershell
& "C:\Progetto_AI\MCP_ABAP_Explorer\.venv\Scripts\python.exe" -m unittest discover -s "C:\Progetto_AI\MCP_ABAP_Explorer" -p "test_mcp_engine.py" -v
```

Test senza rete remota: formati, Unicode, Magika reale, rifiuto prima della scrittura, validazione parametri, Host/token, API locali, download ed esclusivita della porta. La prova del flusso remoto richiede Internet.