const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

class Element {
  constructor() { this.value = ''; this.handlers = {}; this.children = []; this.textContent = ''; }
  addEventListener(name, callback) { this.handlers[name] = callback; }
  appendChild(child) { this.children.push(child); }
  replaceChildren() { this.children = []; }
  closest() { return this; }
  focus() {}
  select() {}
}
const elements = new Map();
const get = id => { if (!elements.has(id)) elements.set(id, new Element()); return elements.get(id); };
const calls = [];
const fetch = async (path, options = {}) => {
  let data = {};
  if (path === '/api/v1/session') data = {role:'submitter', username:'test', csrfToken:'csrf'};
  if (path === '/api/v1/events') data = {events:[]};
  if (path === '/api/v1/requests') data = {requests:[]};
  if (path === '/api/v1/receipts') data = {requests:[{batchId:'sender-batch-1', batchIndex:0, status:'SUCCEEDED', googleEventId:'id'}]};
  if (path === '/api/v1/requests/import' || path === '/api/v1/requests/delete-batch') {
    const body = JSON.parse(options.body);
    calls.push({path, body, headers:options.headers});
    data = {batchId:body.batchId, requests:[{status:'SUCCEEDED'}]};
  }
  return {status:200, ok:true, json:async () => data};
};
const context = {document:{getElementById:get, createElement:() => new Element()}, fetch,
  crypto:{randomUUID:() => 'generated-id'}, location:{}, confirm:() => true, Intl, console};
vm.runInNewContext(fs.readFileSync('app/static/app.js', 'utf8'), context);

(async () => {
  await new Promise(resolve => setImmediate(resolve));
  get('importText').value = JSON.stringify({batchId:'sender-batch-1', events:[{subject:'sample'}]});
  await get('sendBatch').handlers.click();
  get('importText').value = JSON.stringify({batchId:'sender-delete-1', deletions:[{eventId:'id'}]});
  await get('sendBatch').handlers.click();
  await get('exportReceipts').handlers.click();
  assert.equal(calls[0].path, '/api/v1/requests/import');
  assert.equal(calls[0].body.batchId, 'sender-batch-1');
  assert.equal(calls[1].path, '/api/v1/requests/delete-batch');
  assert.equal(calls[1].body.batchId, 'sender-delete-1');
  assert.equal(calls[0].headers['X-CSRF-Token'], 'csrf');
  assert.equal(JSON.parse(get('receiptText').value).requests[0].batchId, 'sender-batch-1');
  console.log('Browser sync workflow passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
