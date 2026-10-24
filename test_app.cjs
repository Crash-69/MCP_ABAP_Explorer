const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {test} = require('node:test');

function createPage() {
  const elements = new Map();
  function get(id) {
    if (!elements.has(id)) elements.set(id, {
      value: '', content: 'test-token', listeners: {}, classList: {toggle() {}},
      addEventListener(type, handler) { this.listeners[type] = handler; },
      setAttribute() {}, removeAttribute() {},
      replaceChildren(...children) { this.children = children; },
      querySelector() { return get(`${id}-span`); },
    });
    return elements.get(id);
  }
  const lifecycle = {};
  const storage = new Map();
  const requests = [];
  let failure = false;
  const context = vm.createContext({
    document: {
      getElementById: get, querySelector: () => get('token'), querySelectorAll: () => [],
    },
    window: {addEventListener: (type, handler) => { lifecycle[type] = handler; }},
    sessionStorage: {
      getItem: key => storage.get(key),
      setItem: (key, value) => storage.set(key, value),
    },
    Option: function(label, value) { this.label = label; this.value = value; },
    AbortSignal,
    fetch: async (url, options) => {
      requests.push({url, payload: options?.body && JSON.parse(options.body)});
      if (failure) throw new Error('Simulated failure');
      return {ok: true, json: async () => ({
        tools: [{name: 'read_table', inputSchema: {properties: {}, required: []}}],
        result_id: 'result', created_at: '2026-10-23T12:00:00Z', raw_text: 'OK',
      })};
    },
  });
  const source = fs.readFileSync(path.join(__dirname, 'app.js'), 'utf8');
  vm.runInContext(source.replace(/initialize\(\);\s*$/, ''), context);
  const run = code => vm.runInContext(code, context);
  const config = JSON.parse(fs.readFileSync(path.join(__dirname, 'mcp_servers.json'), 'utf8'));
  context.definitions = Object.entries(config.servers).map(([id, server]) => ({id, ...server}));
  run("serverDefinitions = definitions; activeServer = 'sap-on-premise'; ready = true;");
  get('adt-user').value = 'test-user';
  get('adt-password').value = 'test-password';
  return {get, lifecycle, storage, requests, run, fail: () => { failure = true; }};
}

test('S4B defaults replace legacy saved values without storing credentials', () => {
  const page = createPage();
  page.storage.set('mcp-connection-sap-on-premise', JSON.stringify({SAP_CLIENT: '100'}));
  page.run('configureServerConnection()');
  assert.equal(page.get('adt-url').value, 'http://172.20.253.162:8000');
  assert.equal(page.get('adt-sysnr').value, '00');
  assert.equal(page.get('adt-system-id').value, 'S4B');
  assert.equal(page.get('adt-client').value, '800');
});

test('credentials survive repeated discovery, invocation, diagnostics and failures', async () => {
  const page = createPage();
  page.run('configureServerConnection(); renderDiagnostics = () => {};');
  await page.run('loadServerTools(true)');
  await page.run('loadServerTools(true)');
  await page.get('search-form').listeners.submit({preventDefault() {}});
  await page.run('runDiagnostics()');
  assert.deepEqual(page.requests.map(item => item.url),
    ['/api/tools', '/api/tools', '/api/invoke', '/api/diagnostics']);
  page.fail();
  await page.run('loadServerTools(true)');
  await page.run('runDiagnostics()');
  page.get('search').disabled = false;
  await page.get('search-form').listeners.submit({preventDefault() {}});
  for (const request of page.requests) {
    assert.equal(request.payload.connection_options.SAP_USERNAME, 'test-user');
    assert.equal(request.payload.connection_options.SAP_PASSWORD, 'test-password');
  }
  assert.equal(page.get('adt-user').value, 'test-user');
  assert.equal(page.get('adt-password').value, 'test-password');
  assert.equal([...page.storage.values()].some(value =>
    value.includes('test-user') || value.includes('test-password')), false);
});

test('server switches retain credentials but Cloud requests never include them', async () => {
  const page = createPage();
  page.run('configureServerConnection()');
  page.get('server').value = 'sap-cloud';
  await page.get('server').listeners.change();
  assert.equal(page.requests[0].payload, undefined);
  assert.equal(page.get('adt-password').value, 'test-password');
  page.get('server').value = 'sap-on-premise';
  await page.get('server').listeners.change();
  assert.equal(page.get('adt-user').value, 'test-user');
  assert.equal(page.get('adt-password').value, 'test-password');
});

test('leaving the page and restoring navigation cache clear credentials', () => {
  const page = createPage();
  page.lifecycle.pagehide();
  assert.equal(page.get('adt-user').value, '');
  assert.equal(page.get('adt-password').value, '');
  page.get('adt-user').value = 'restored-user';
  page.get('adt-password').value = 'restored-password';
  page.lifecycle.pageshow({persisted: true});
  assert.equal(page.get('adt-user').value, '');
  assert.equal(page.get('adt-password').value, '');
  const reopened = createPage();
  reopened.run('clearUiCredentials(); configureServerConnection()');
  assert.equal(reopened.get('adt-password').value, '');
});
