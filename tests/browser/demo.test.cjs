const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

// Execute the actual shipped inline script with a minimal DOM and mock HTTP API.
// No browser packages or node_modules are needed for these regression tests.
async function demo() {
  const html = fs.readFileSync(path.join(__dirname, '../../quickmock/static/demo.html'), 'utf8');
  const script = [...html.matchAll(/<script\b[^>]*>([\s\S]*?)<\/script>/g)].at(-1)[1];
  const nodes = new Map(), requests = [], alerts = [];
  const element = id => {
    if (!nodes.has(id)) nodes.set(id, {
      value: '', innerHTML: '', textContent: '', style: {},
      classList: { toggle() {}, add() {}, remove() {} },
    });
    return nodes.get(id);
  };
  const context = vm.createContext({
    document: { getElementById: element }, console, performance,
    URLSearchParams, setTimeout, clearTimeout,
    alert: message => alerts.push(message),
    fetch: async (url, options = {}) => {
      const body = options.body ? JSON.parse(options.body) : null;
      requests.push({ url, method: options.method || 'GET', body });
      let data = [];
      if (url === '/_admin/state') data = { chaos: { delay_ms: 0, error_rate: 0 } };
      else if (url.includes('/_stats')) data = { total_records: 0, numeric_fields: {} };
      else if (options.method === 'POST' || options.method === 'PATCH') data = { id: 99, ...body };
      return { ok: true, status: 200, json: async () => data };
    },
  });
  vm.runInContext(script, context);
  await new Promise(resolve => setImmediate(resolve));
  element('newTitle').value = 'Test product';
  return { context, element, requests, alerts };
}

test('zero price and zero stock survive demo form submission', async () => {
  const d = await demo();
  d.element('newPrice').value = '0';
  d.element('newStock').value = '0';
  await vm.runInContext('createProduct()', d.context);
  const request = d.requests.find(r => r.method === 'POST' && r.url === '/api/products');
  assert.equal(request.body.price, 0);
  assert.equal(request.body.stock, 0);
  assert.equal(request.body.inStock, false);
});

test('blank price and stock retain documented demo defaults', async () => {
  const d = await demo();
  await vm.runInContext('createProduct()', d.context);
  const request = d.requests.find(r => r.method === 'POST' && r.url === '/api/products');
  assert.equal(request.body.price, 950);
  assert.equal(request.body.stock, 10);
});

for (const [price, stock] of [['-1', '1'], ['Infinity', '1'], ['10', '-1'], ['10', '1.5'], ['10', '9007199254740992']]) {
  test(`invalid demo quantity cannot create a record: ${price}/${stock}`, async () => {
    const d = await demo();
    d.element('newPrice').value = price;
    d.element('newStock').value = stock;
    await vm.runInContext('createProduct()', d.context);
    assert.equal(d.alerts.length, 1);
    assert.equal(d.requests.filter(r => r.method === 'POST').length, 0);
  });
}

test('buying a free product preserves its zero order total', async () => {
  const d = await demo();
  vm.runInContext('productsCache = {free: {id: "free", title: "Free", price: 0, stock: 1}}', d.context);
  await vm.runInContext('buyProductById("free")', d.context);
  const order = d.requests.find(r => r.method === 'POST' && r.url === '/api/orders');
  assert.equal(order.body.totalAmount, 0);
  const stock = d.requests.find(r => r.method === 'PATCH' && r.url === '/api/products/free');
  assert.equal(stock.body.stock, 0);
  assert.equal(stock.body.inStock, false);
});
