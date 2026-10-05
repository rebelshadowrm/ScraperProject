// Execute generated browser exporters with deterministic browser APIs and no network.
const vm = require('node:vm');
const fs = require('node:fs');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
let time = 0, requests = 0, downloaded, callbackResult;
const requestDetails = [];
const blobs = new Map();
const NativeDate = Date;
class Clock extends NativeDate {
  constructor(...args) { super(...(args.length ? args : [Date.UTC(2099, 0, 1) + time])); }
  static now() { return Date.UTC(2099, 0, 1) + time; }
}
const context = {
  Date: Clock, URL, URLSearchParams, Blob, AbortController,
  console: {log() {}}, alert() {},
  location: {href: input.domain + '/?season_ids=2099', origin: input.domain},
  performance: {getEntriesByType() {return [{name: input.domain + '/ajax-events?page=1&season_ids=2099'}];}},
  document: {
    querySelector(selector) {return selector.includes('_token') ? {value: 'fixture-token'} : null;},
    querySelectorAll() {return [];},
    createElement() {return {click() {downloaded = blobs.get(this.href);}, remove() {}};},
    body: {appendChild() {}}
  },
  setTimeout(fn, ms) {
    if (ms < 10000 || input.mode === 'hung') {
      time += ms;
      queueMicrotask(fn);
    }
    return 1;
  },
  clearTimeout() {},
  fetch: async (url, options) => {
    requests++;
    const params = new URLSearchParams(options.body || new URL(url).search);
    requestDetails.push({method: options.method, token: params.get('_token')});
    const page = Number(params.get('page'));
    if (input.mode === 'hung') {
      return new Promise((resolve, reject) => {
        if (options.signal.aborted) reject(new Error('aborted'));
        else options.signal.addEventListener('abort', () => reject(new Error('aborted')));
      });
    }
    let data = page === 1 ? {eventlist: [{url_key: 'classic', state: params.get('state') || 'TX', start_date: '07/20/2099', end_date: '07/22/2099'}]} : {eventlist: []};
    if (input.mode === 'repeat') data = {eventlist: [{url_key: 'classic'}]};
    if (input.mode === 'malformed') data = {error: 'blocked'};
    if (input.mode === 'empty') data = {eventlist: []};
    if (input.mode === 'middle-failure' && page === 2) throw new Error('network');
    return {ok: true, status: 200, text: async () => JSON.stringify(data)};
  }
};
context.URL = class extends URL {
  static createObjectURL(blob) {blobs.set('blob:fixture', blob); return 'blob:fixture';}
  static revokeObjectURL() {}
};
(async () => {
  let error;
  try {
    if (input.kind === 'manual') await vm.runInNewContext(input.script, context);
    else await new Promise(resolve => {
      context.done = result => {callbackResult = result; resolve();};
      vm.runInNewContext('(function(){' + input.script + '\n})(done)', context);
    });
  } catch (e) {error = String(e);}
  const dump = downloaded ? JSON.parse(await downloaded.text()) : callbackResult?.dump;
  process.stdout.write(JSON.stringify({dump, callback: callbackResult, error, requests, requestDetails, elapsed: time}));
})().catch(error => {process.stderr.write(String(error)); process.exitCode = 1;});
