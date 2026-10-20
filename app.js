const get = id => document.getElementById(id);
const token = document.querySelector('meta[name="app-token"]').content;
let ready = false;
let busy = false;
let resultId = null;
let activeTool = 'search';
let activeMode = 'python';
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
  get('search').disabled = busy || !ready || (llm
    ? modelsLoading || !get('model').value || !get('query').value.trim()
    : !inputs[activeTool].optional && !get('query').value.trim());
  get('export').disabled = busy || !resultId;
  get('query').disabled = busy;
  get('tool').disabled = busy || llm;
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
function configureInput() {
  const llm = activeMode === 'llm';
  get('tool-name').textContent = activeTool;
  const input = llm ? {label: 'Richiesta per il modello', placeholder: '', max: 12000} : inputs[activeTool];
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
document.querySelectorAll('input[name="mode"]').forEach(input => input.addEventListener('change', () => {
  drafts.set(draftKey(), get('query').value);
  activeMode = input.value;
  configureInput();
  invalidate();
  if (activeMode === 'llm' && !modelsLoaded) loadModels();
}));
get('model').addEventListener('change', invalidate);
get('refresh-models').addEventListener('click', loadModels);
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
document.querySelectorAll('input[name="format"]').forEach(input => input.addEventListener('change', clearExport));
get('search-form').addEventListener('submit', async event => {
  event.preventDefault();
  if (get('search').disabled) return;
  const payload = activeMode === 'llm'
    ? {mode: 'llm', model: get('model').value, text: get('query').value}
    : {mode: 'python', tool: activeTool, text: get('query').value};
  if (activeMode === 'python' && activeTool === 'search') payload.count = Number(get('count').value);
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
    get('stats').textContent = `${data.model || data.tool} / ${date}`;
    get('export-status').textContent = 'Risposta pronta per il salvataggio';
    if (!data.raw_text) get('stats').textContent = 'Risposta senza content.text / esportabile in JSON o Markdown';
  } catch (error) {
    get('stats').textContent = 'Richiesta non completata';
    showError(error.message);
  } finally {
    busy = false;
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
  } catch (error) {
    get('health').textContent = 'Server non disponibile';
    showError(error.message);
  }
  controls();
}
initialize();