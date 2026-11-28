// Run after build.py with node --test test-terminal.js.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { test } = require('node:test');
const vm = require('node:vm');

function terminal(file) {
  const html = fs.readFileSync(path.join(__dirname, file), 'utf8');
  const start = html.indexOf('/* terminal */');
  const end = html.indexOf('/* body */', start);
  const rule = html.split('\n').find(line => line.includes(",runLastScript,'run the last script']"));
  assert(start >= 0 && end > start && rule, 'the terminal section and its run rule must be present');
  const printed = [], spoken = [], timers = [];
  const S = { lastScript: null, guest: false };
  const out = { isConnected: true, appendChild: item => printed.push(item) };
  const sandbox = vm.createContext({
    S, W: { terminal: true }, openApp() {}, mood() {}, sleep: async () => {},
    document: { createElement: () => ({ textContent: '' }), addEventListener() {} },
    el: () => ({ textContent: '', classList: { remove() {} } }),
    say: message => spoken.push(message), setTimeout: fn => timers.push(fn),
  });
  const code = html.slice(start, end) + `
    T = { out, stat: { textContent: '' }, stop: {}, typing: false, abort: false };
    ({ writeScript, tExec, TFILES, stat: T.stat, recipes: RECIPES, voiceRule: ${rule.trim().slice(0, -1)} });`;
  sandbox.out = out;
  const api = vm.runInContext(code, sandbox, { timeout: 1000 });
  return { ...api, S, timers, spoken, printed };
}

for (const file of ['src/shell.src.html', 'index.html']) {
  test(`${file}: built-in script files wait and voice can start them`, async () => {
    const app = terminal(file);
    for (const [name, recipe] of Object.entries(app.recipes)) {
      const output = app.printed.length;
      await app.writeScript(name);
      assert.equal(app.TFILES[recipe.file], recipe.code);
      assert.equal(app.S.lastScript, recipe.file);
      assert.match(app.stat.textContent, /say “run it”/);
      assert(app.printed.slice(output).every(line => /^(?:nano |#!|saved )/.test(line.textContent)));
      assert.equal(app.timers.length, 0, 'writing must not arm an automatic run');
    }
    const [phrase, run] = app.voiceRule;
    assert(phrase.test('run it'));
    assert(phrase.test('run it please'));
    assert(!phrase.test('run itinerary'));
    app.S.guest = true;
    const before = app.printed.length;
    await run();
    assert.equal(app.printed.length, before);
    app.S.guest = false;
    await run();
    assert(app.printed.some(line => line.textContent === 'run hello.sh'));
    assert(app.printed.some(line => line.textContent === 'Hello from Pip (3)'));
    assert(app.spoken.includes('Running it.'));
    await app.tExec('run box.sh');
    assert(app.printed.some(line => line.textContent === '|   Pip    |'));
  });
}
