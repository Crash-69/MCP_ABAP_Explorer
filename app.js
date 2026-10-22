const get = id => document.getElementById(id);
const token = document.querySelector('meta[name="app-token"]').content;
let ready = false;
let busy = false;
let resultId = null;
let activeTool = 'search';
let activeMode = 'python';
let activeServer = 'sap-cloud';
let serverDefinitions = [];
let serverTools = [];
let toolsLoading = false;
let toolsReady = false;
let diagnosticReport = null;
let modelsLoading = false;
let modelsLoaded = false;
const drafts = new Map();
const inputs = {
  search: {label: 'Stringa di ricerca', placeholder: 'SELECT statements in ABAP Cloud'},
  fetch: {label: 'ID documento', placeholder: 'ID esatto del documento'},
  abap_lint: {label: 'Codice sorgente ABAP', placeholder: '', max: 51200},
  abap_feature_matrix: {label: 'Funzionalita ABAP (facoltativo)', placeholder: 'Parole chiave della funzionalita', optional: true},
  sap_community_search: {label: 'Ricerca SAP Community', placeholder: 'Termini di ricerca'},
  sap_search_objects: {label: 'Nome oggetto o argomento (facoltativo)', placeholder: 'Nome oggetto o parola chiave', optional: true},
  sap_get_object_details: {label: 'Tipo e nome oggetto', placeholder: 'CLAS, CL_ABAP_REGEX'},
  sap_discovery_center_search: {label: 'Ricerca servizi SAP BTP', placeholder: 'Nome servizio o argomento'},
  sap_discovery_center_service: {label: 'ID o nome servizio SAP BTP', placeholder: 'UUID o nome del servizio'},
};

function showError(message) {
  get('error').textContent = message;
  get('error').hidden = !message;
}
function clearExport() {
  get('success').hidden = true;
  get('download').removeAttribute('href');
}
function controls() {
  const llm = activeMode === 'llm';
  get('search').disabled = busy || !ready || toolsLoading || !toolsReady || (llm
    ? modelsLoading || !get('model').value || !get('query').value.trim()
    : !inputSpec(activeTool).optional && !get('query').value.trim());
  get('export').disabled = busy || !resultId;
  get('query').disabled = busy;
  get('tool').disabled = busy || llm || !toolsReady;
  get('server').disabled = busy || toolsLoading;
  ['adt-url', 'adt-sysnr', 'adt-system-id', 'adt-client', 'adt-user', 'adt-password']
    .forEach(id => { get(id).disabled = busy || toolsLoading; });
  get('refresh-adt-tools').disabled = busy || toolsLoading;
  get('run-diagnostics').disabled = busy || toolsLoading;
  get('system-type').disabled = busy || llm;
  get('model').disabled = busy || modelsLoading || !llm;
  get('refresh-models').disabled = busy || modelsLoading;
  get('count').disabled = busy || llm || activeTool !== 'search';
  document.querySelectorAll('input[name="mode"]').forEach(input => { input.disabled = busy; });
  document.querySelectorAll('input[name="format"]').forEach(input => { input.disabled = busy; });
  get('result').setAttribute('aria-busy', String(busy));
}
async function post(path, payload) {
  const response = await fetch(path, {
    method: 'POST', headers: {'Content-Type': 'application/json', 'X-App-Token': token},
    body: JSON.stringify(payload), signal: AbortSignal.timeout(payload.mode === 'llm' ? 330000 : 120000),
  });
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || 'Richiesta non riuscita.');
  return data;
}
function invalidate() {
  resultId = null;
  get('preview').value = '';
  get('mcp-raw').value = '';
  get('mcp-raw-panel').hidden = true;
  get('search-log').textContent = '';
  get('search-log').hidden = true;
  get('stats').textContent = 'Nessuna richiesta eseguita';
  get('export-status').textContent = 'Nessuna risposta selezionata';
  clearExport();
  showError('');
  controls();
}
get('query').addEventListener('input', invalidate);
get('count').addEventListener('input', invalidate);
function draftKey() { return activeMode === 'llm' ? 'llm-prompt' : activeTool; }
function inputSpec(tool) {
  const discovered = serverTools.find(item => item.name === tool);
  if (activeServer !== 'sap-cloud' && discovered) {
    const properties = discovered.inputSchema.properties || {};
    const required = discovered.inputSchema.required || [];
    const example = Object.fromEntries(required.map(name => [name, schemaExample(properties[name])]));
    return {
      label: `Argomenti JSON: ${tool}`,
      placeholder: JSON.stringify(example, null, 2),
      max: 320000,
      optional: required.length === 0,
    };
  }
  return inputs[tool] || {label: `Argomenti JSON: ${tool}`, placeholder: '{}'};
}
function schemaExample(schema = {}) {
  if (Array.isArray(schema.enum) && schema.enum.length) return schema.enum[0];
  if (schema.type === 'string') return '';
  if (schema.type === 'integer' || schema.type === 'number') return 0;
  if (schema.type === 'boolean') return false;
  if (schema.type === 'array') return [];
  if (schema.type === 'object') return {};
  return null;
}
function configureInput() {
  const llm = activeMode === 'llm';
  get('tool-name').textContent = activeTool;
  const isCloud = activeServer === 'sap-cloud';
  get('system-type-field').hidden = llm || !isCloud || !['sap_search_objects', 'sap_get_object_details'].includes(activeTool);
  const input = llm ? {label: 'Richiesta per il modello', placeholder: '', max: 12000} : inputSpec(activeTool);
  get('python-tool-field').hidden = llm;
  get('llm-settings').hidden = !llm;
  get('input-label').textContent = input.label;
  get('query').maxLength = input.max || 4000;
  get('query').required = !input.optional;
  get('query').placeholder = input.placeholder;
  get('query').rows = llm ? 5 : activeTool === 'abap_lint' ? 8 : 3;
  get('query').spellcheck = false;
  get('query').classList.toggle('code-input', !llm && activeTool === 'abap_lint');
  get('query').value = drafts.get(draftKey()) || '';
  get('count-field').hidden = llm || activeTool !== 'search';
  get('count').required = !llm && activeTool === 'search';
  get('preview-label').textContent = llm ? 'Risposta elaborata dal modello' : 'Anteprima content.text';
  get('preview').setAttribute('aria-label', get('preview-label').textContent);
}
get('tool').addEventListener('change', () => {
  drafts.set(draftKey(), get('query').value);
  activeTool = get('tool').value;
  configureInput();
  invalidate();
});
get('server').addEventListener('change', async () => {
  drafts.set(draftKey(), get('query').value);
  clearUiCredentials();
  activeServer = get('server').value;
  try { sessionStorage.setItem('mcp-server-id', activeServer); } catch {}
  invalidate();
  configureServerConnection();
  if (activeServer === 'sap-cloud') await loadServerTools();
  else prepareOnPremiseSelection();
});
document.querySelectorAll('input[name="mode"]').forEach(input => input.addEventListener('change', () => {
  drafts.set(draftKey(), get('query').value);
  activeMode = input.value;
  configureInput();
  invalidate();
  if (activeMode === 'llm' && !modelsLoaded) loadModels();
}));
get('model').addEventListener('change', invalidate);
get('system-type').addEventListener('change', invalidate);
get('refresh-models').addEventListener('click', loadModels);
get('refresh-adt-tools').addEventListener('click', () => loadServerTools(true));
get('run-diagnostics').addEventListener('click', runDiagnostics);
get('download-diagnostics').addEventListener('click', downloadDiagnostics);

function isOnPremise() {
  return serverDefinitions.find(server => server.id === activeServer)?.transport === 'stdio';
}

function connectionOptions(includeCredentials = true) {
  const options = {
    SAP_URL: get('adt-url').value.trim(),
    SAP_SYSNR: get('adt-sysnr').value.trim(),
    SAP_SYSTEM_ID: get('adt-system-id').value.trim().toUpperCase(),
    SAP_CLIENT: get('adt-client').value.trim(),
  };
  if (includeCredentials) {
    options.SAP_USERNAME = get('adt-user').value.trim();
    options.SAP_PASSWORD = get('adt-password').value;
  }
  return options;
}

function clearUiCredentials() {
  get('adt-user').value = '';
  get('adt-password').value = '';
}

function configureServerConnection() {
  const onPremise = isOnPremise();
  get('adt-connection').hidden = !onPremise;
  get('system-type-field').hidden = onPremise || !['sap_search_objects', 'sap_get_object_details'].includes(activeTool);
  if (!onPremise) return;
  const defaults = serverDefinitions.find(server => server.id === activeServer)?.connection_defaults || {};
  let saved = {};
  try { saved = JSON.parse(sessionStorage.getItem(`mcp-connection-${activeServer}`) || '{}'); } catch {}
  get('adt-url').value = saved.SAP_URL || defaults.SAP_URL || '';
  get('adt-sysnr').value = saved.SAP_SYSNR || defaults.SAP_SYSNR || '';
  get('adt-system-id').value = saved.SAP_SYSTEM_ID || defaults.SAP_SYSTEM_ID || '';
  get('adt-client').value = saved.SAP_CLIENT || defaults.SAP_CLIENT || '';
}

function prepareOnPremiseSelection() {
  toolsReady = false;
  toolsLoading = false;
  serverTools = [];
  activeTool = '';
  get('tool').replaceChildren(new Option('Premi Aggiorna tool ADT per collegarti', ''));
  get('tool-name').textContent = '';
  get('input-label').textContent = 'Tool MCP';
  get('query').value = '';
  get('query').placeholder = 'I parametri del tool saranno caricati da SAP ADT.';
  get('query').required = false;
  get('system-type-field').hidden = true;
  get('count-field').hidden = true;
  get('adt-status').textContent = 'Connessione non avviata';
  showError('');
  controls();
}

async function loadServerTools(connectOnPremise = false) {
  const onPremise = isOnPremise();
  if (onPremise && !connectOnPremise) return prepareOnPremiseSelection();
  toolsLoading = true;
  toolsReady = false;
  invalidate();
  try {
    let data;
    if (onPremise) {
      const options = connectionOptions();
      if (!options.SAP_USERNAME || !options.SAP_PASSWORD) {
        throw new Error('Inserire USER e password SAP prima di aggiornare i tool ADT.');
      }
      try { sessionStorage.setItem(`mcp-connection-${activeServer}`, JSON.stringify(connectionOptions(false))); } catch {}
      get('adt-status').textContent = 'Avvio server ADT e lettura tool SAP...';
      try {
        data = await post('/api/tools', {server_id: activeServer, connection_options: options});
      } finally {
        options.SAP_USERNAME = '';
        options.SAP_PASSWORD = '';
        clearUiCredentials();
      }
    } else {
      const response = await fetch(`/api/tools?server_id=${encodeURIComponent(activeServer)}`, {
        signal: AbortSignal.timeout(95000),
      });
      data = await response.json();
      if (!response.ok) throw new Error(data.error || 'Server MCP non disponibile.');
    }
    serverTools = data.tools;
    if (!serverTools.length) throw new Error('Il server MCP selezionato non espone tool utilizzabili.');
    const previousTool = activeTool;
    get('tool').replaceChildren(...serverTools.map(tool => new Option(tool.label || tool.name, tool.name)));
    activeTool = serverTools.some(tool => tool.name === previousTool) ? previousTool : serverTools[0]?.name;
    if (!activeTool) throw new Error('Il server MCP selezionato non espone tool utilizzabili.');
    get('tool').value = activeTool;
    toolsReady = true;
    configureInput();
    if (onPremise) get('adt-status').textContent = `${serverTools.length} tool ADT disponibili. Reinserire USER e password per ogni richiesta.`;
    showError('');
  } catch (error) {
    serverTools = [];
    activeTool = '';
    get('tool').replaceChildren(new Option('Server MCP non disponibile', ''));
    get('tool-name').textContent = '';
    get('system-type-field').hidden = true;
    get('count-field').hidden = true;
    get('input-label').textContent = 'Tool MCP non disponibile';
    get('query').placeholder = 'Verifica configurazione e disponibilita del server selezionato.';
    if (onPremise) get('adt-status').textContent = error.message;
    showError(`Impossibile caricare i tool del server ${activeServer}: ${error.message}`);
  } finally {
    toolsLoading = false;
    controls();
  }
}

async function loadServers() {
  const response = await fetch('/api/servers');
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || 'Configurazione server MCP non disponibile.');
  serverDefinitions = data.servers;
  get('server').replaceChildren(...serverDefinitions.map(server => new Option(server.label, server.id)));
  let savedServer = data.default_server;
  try {
    const stored = sessionStorage.getItem('mcp-server-id');
    if (data.servers.some(server => server.id === stored)) savedServer = stored;
  } catch {}
  activeServer = savedServer;
  get('server').value = savedServer;
  configureServerConnection();
  if (isOnPremise()) prepareOnPremiseSelection();
  else await loadServerTools();
}
async function loadModels() {
  modelsLoading = true;
  invalidate();
  get('model-status').textContent = 'Verifica modelli Ollama...';
  const selected = get('model').value;
  try {
    const response = await fetch('/api/models', {signal: AbortSignal.timeout(30000)});
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || 'Catalogo Ollama non disponibile.');
    get('model').replaceChildren(new Option('Seleziona modello Ollama...', ''));
    for (const model of data.models) {
      get('model').add(new Option(`${model.name} / ${(model.size / 1e9).toFixed(1)} GB`, model.name));
    }
    if (!data.models.length) get('model').replaceChildren(new Option('Nessun modello locale compatibile', ''));
    if (data.models.some(model => model.name === selected)) get('model').value = selected;
    get('model-status').textContent = `${data.models.length} modelli locali con capability tools`;
    modelsLoaded = true;
  } catch (error) {
    get('model').replaceChildren(new Option('Ollama non disponibile', ''));
    get('model-status').textContent = error.message;
    modelsLoaded = false;
  } finally {
    modelsLoading = false;
    controls();
  }
}

function diagnosticStatus(status) {
  const labels = {
    OK: 'OK', PARTIAL: 'Parziale', ERROR: 'Errore', UNKNOWN: 'Non verificato', SKIPPED: 'Saltato',
    NOT_FOUND: 'ADT non attivo (404)', FORBIDDEN: 'Accesso negato (403)',
    SSL_ERROR: 'Errore SSL', UNREACHABLE: 'Non raggiungibile', INSECURE_URL: 'URL non sicuro',
  };
  return labels[status] || status || 'Non verificato';
}

function renderDiagnostics(report) {
  diagnosticReport = report;
  const rows = (report.environment.checks || []).map(item => [
    `Environment / ${item.name}`, item.status, '',
  ]);
  rows.push(
    ['DNS', report.network.dns.status, report.network.dns.error || `${report.network.dns.addressesFound || 0} indirizzi`],
    ['Host reachability', report.network.hostReachable.status, report.network.hostReachable.error || ''],
    ['ADT endpoint', report.adt.status, `${report.adt.httpStatus ? `HTTP ${report.adt.httpStatus} / ` : ''}${report.adt.endpoint || ''}${report.adt.error ? ` / ${report.adt.error}` : ''}${report.adt.message ? ` / ${report.adt.message}` : ''}`],
    ['Autenticazione SAP', report.authentication.status, report.authentication.message || ''],
    ['Permessi ADT', report.adtPermissions.status, report.adtPermissions.message || ''],
    ['Avvio MCP', report.mcpStartup.status, report.mcpStartup.error || ''],
    [`Tool discovery (${report.toolDiscovery.toolsFound})`, report.toolDiscovery.status,
      [...(report.toolDiscovery.tools || []), report.toolDiscovery.error].filter(Boolean).join(', ')],
  );
  const container = get('diagnostics-results');
  container.replaceChildren();
  for (const [label, status, detail] of rows) {
    const row = document.createElement('div');
    row.className = 'diagnostic-row';
    const heading = document.createElement('strong');
    heading.textContent = `${diagnosticStatus(status)} / ${label}`;
    const description = document.createElement('span');
    description.className = 'diagnostic-detail';
    description.textContent = detail;
    row.append(heading, description);
    container.append(row);
  }
  if (report.suggestions.length) {
    const list = document.createElement('ul');
    list.className = 'diagnostic-suggestions';
    for (const suggestion of report.suggestions) {
      const item = document.createElement('li');
      item.textContent = suggestion;
      list.append(item);
    }
    container.append(list);
  }
  get('diagnostics-summary').textContent = `Esito generale: ${diagnosticStatus(report.summary)} / ${report.summaryMessage || ''} / ${new Date(report.timestamp).toLocaleString('it-IT')}`;
  get('download-diagnostics').disabled = false;
}

async function runDiagnostics() {
  if (!isOnPremise() || busy || toolsLoading) return;
  const options = connectionOptions();
  const missing = ['SAP_URL', 'SAP_USERNAME', 'SAP_PASSWORD', 'SAP_CLIENT'].filter(key => !options[key]);
  if (missing.length) {
    get('diagnostics-summary').textContent = `Inserire i parametri richiesti: ${missing.join(', ')}`;
    return;
  }
  get('run-diagnostics').disabled = true;
  get('diagnostics-summary').textContent = 'Diagnostica in corso: rete, ADT, autenticazione e MCP...';
  try {
    renderDiagnostics(await post('/api/diagnostics', {connection_options: options}));
  } catch (error) {
    get('diagnostics-summary').textContent = `Diagnostica non completata: ${error.message}`;
  } finally {
    options.SAP_USERNAME = '';
    options.SAP_PASSWORD = '';
    clearUiCredentials();
    get('run-diagnostics').disabled = false;
  }
}

function downloadDiagnostics() {
  if (!diagnosticReport) return;
  const reportFile = new Blob([JSON.stringify(diagnosticReport, null, 2)], {type: 'application/json'});
  const reportUrl = URL.createObjectURL(reportFile);
  const link = document.createElement('a');
  link.href = reportUrl;
  link.download = 'sap-on-premise-diagnostics.json';
  link.click();
  URL.revokeObjectURL(reportUrl);
}

document.querySelectorAll('input[name="format"]').forEach(input => input.addEventListener('change', clearExport));
get('search-form').addEventListener('submit', async event => {
  event.preventDefault();
  if (get('search').disabled) return;
  const payload = activeMode === 'llm'
    ? {mode: 'llm', model: get('model').value, text: get('query').value, server_id: activeServer}
    : {mode: 'python', tool: activeTool, text: get('query').value, server_id: activeServer};
  if (isOnPremise()) {
    const options = connectionOptions();
    if (!options.SAP_USERNAME || !options.SAP_PASSWORD) {
      showError('Inserire USER SAP e password per questa richiesta ADT.');
      return;
    }
    payload.connection_options = options;
  }
  if (activeMode === 'python' && activeTool === 'search') payload.count = Number(get('count').value);
  if (activeMode === 'python' && ['sap_search_objects', 'sap_get_object_details'].includes(activeTool)) {
    payload.system_type = get('system-type').value;
  }
  invalidate();
  busy = true;
  controls();
  get('search').querySelector('span').textContent = 'Invocazione in corso...';
  get('stats').textContent = activeMode === 'llm' ? 'Elaborazione Ollama e chiamate MCP...' : 'Connessione al server MCP...';
  try {
    const data = await post('/api/invoke', payload);
    resultId = data.result_id;
    get('preview').value = data.raw_text;
    get('mcp-raw').value = data.mcp_raw_text || '';
    get('mcp-raw-panel').hidden = !data.mcp_raw_text;
    get('search-log').textContent = (data.search_log || []).join('\n');
    get('search-log').hidden = !get('search-log').textContent;
    const date = new Date(data.created_at).toLocaleString('it-IT');
    get('stats').textContent = `${data.model || data.tool} / ${data.server_id || activeServer} / ${date}`;
    get('export-status').textContent = 'Risposta pronta per il salvataggio';
    if (!data.raw_text) get('stats').textContent = 'Risposta senza content.text / esportabile in JSON o Markdown';
  } catch (error) {
    get('stats').textContent = 'Richiesta non completata';
    showError(error.message);
  } finally {
    busy = false;
    if (isOnPremise()) {
      clearUiCredentials();
      if (payload.connection_options) {
        payload.connection_options.SAP_USERNAME = '';
        payload.connection_options.SAP_PASSWORD = '';
      }
    }
    get('search').querySelector('span').textContent = 'Interroga Server MCP';
    controls();
  }
});
get('export').addEventListener('click', async () => {
  if (get('export').disabled) return;
  const format = document.querySelector('input[name="format"]:checked').value;
  busy = true;
  controls();
  clearExport();
  showError('');
  get('export').querySelector('span').textContent = 'Verifica Magika...';
  try {
    const data = await post('/api/export', {result_id: resultId, format});
    get('validation').textContent = `Contenuto validato da Magika come: ${data.mime_type}`;
    get('saved-path').textContent = data.path;
    get('detected-note').textContent = `File .${format} / tipo rilevato: ${data.label} / ${data.bytes.toLocaleString('it-IT')} byte`;
    get('download').href = data.download_url;
    get('download').download = data.filename;
    get('success').hidden = false;
    get('export-status').textContent = 'File salvato nella cartella locale';
  } catch (error) {
    showError(error.message);
    get('export-status').textContent = 'Salvataggio non completato';
  } finally {
    busy = false;
    get('export').querySelector('span').textContent = 'Esporta Risposta';
    controls();
  }
});
async function initialize() {
  if (window.lucide) lucide.createIcons();
  try {
    const response = await fetch('/api/health');
    if (!response.ok) throw new Error('Server locale non disponibile.');
    const data = await response.json();
    ready = data.ready;
    get('health').textContent = ready ? 'Client MCP pronto' : 'Client non disponibile';
    get('port-info').textContent = `127.0.0.1:${data.port}`;
    await loadServers();
  } catch (error) {
    get('health').textContent = 'Server non disponibile';
    showError(error.message);
  }
  controls();
}
initialize();