(() => {
  'use strict';
  const origin = document.body.dataset.witnextOrigin;
  const frame = document.getElementById('forms-app');
  if (!origin || !frame) return;
  const connection = document.getElementById('connection');
  const help = document.getElementById('connection-help');
  const status = document.getElementById('connection-status');
  let ready = false;
  // Status only: never request answers, document data, customer IDs or tokens.
  const hello = () => frame.contentWindow.postMessage({ type: 'witforms:hello', version: 1 }, origin);
  const retry = window.setInterval(hello, 1000);
  const showHelp = window.setTimeout(() => {
    if (!ready) {
      help.hidden = false;
      status.textContent = 'Your saved forms need a connection.';
    }
  }, 6000);
  frame.addEventListener('load', hello);
  window.addEventListener('message', (event) => {
    if (event.origin !== origin || event.source !== frame.contentWindow ||
        event.data?.type !== 'witforms:ready' || event.data.version !== 1) return;
    ready = true;
    window.clearInterval(retry);
    window.clearTimeout(showHelp);
    connection.hidden = true;
    frame.hidden = false;
  });
  document.getElementById('reload').addEventListener('click', () => {
    // A successful connection hides this control so it cannot discard active edits.
    if (ready) return;
    status.textContent = 'Connecting to your saved forms…';
    frame.src = origin + '/forms-app/';
  });
  hello();
})();
