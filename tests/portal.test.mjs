import { readFileSync } from 'node:fs';
import { runInNewContext } from 'node:vm';
import { test } from 'node:test';
import assert from 'node:assert/strict';

test('the Forms portal trusts only readiness from its configured frame and sends no customer data', () => {
  const messages = [];
  const listeners = {};
  const origin = 'https://crm.example.invalid';
  const frame = {
    hidden: true, src: origin + '/forms-app/',
    contentWindow: { postMessage: (data, target) => messages.push({ data, target }) },
    addEventListener: (name, fn) => { listeners[name] = fn; },
  };
  const elements = {
    'forms-app': frame, connection: { hidden: false },
    'connection-help': { hidden: true }, 'connection-status': { textContent: '' },
    reload: { addEventListener: (name, fn) => { listeners.reload = fn; } },
  };
  runInNewContext(readFileSync(new URL('../static/portal.js', import.meta.url), 'utf8'), {
    document: { body: { dataset: { witnextOrigin: origin } }, getElementById: (id) => elements[id] },
    window: {
      setInterval: () => 1, setTimeout: () => 2, clearInterval: () => {}, clearTimeout: () => {},
      addEventListener: (name, fn) => { listeners[name] = fn; },
    },
  });
  assert.equal(JSON.stringify(messages), JSON.stringify([
    { data: { type: 'witforms:hello', version: 1 }, target: origin },
  ]));
  const valid = { origin, source: frame.contentWindow, data: { type: 'witforms:ready', version: 1 } };
  for (const forged of [
    { ...valid, origin: 'https://wrong.example.invalid' },
    { ...valid, source: {} },
    { ...valid, data: { type: 'witforms:ready', version: 2 } },
    { ...valid, data: { type: 'save', answers: 'SYNTHETIC-CANARY' } },
  ]) {
    listeners.message(forged);
    assert.equal(frame.hidden, true);
    assert.equal(elements.connection.hidden, false);
  }
  listeners.reload();
  assert.equal(frame.src, origin + '/forms-app/');
  listeners.message(valid);
  assert.equal(frame.hidden, false);
  assert.equal(elements.connection.hidden, true);
  frame.src = 'active-editor';
  listeners.reload();
  assert.equal(frame.src, 'active-editor');
});
