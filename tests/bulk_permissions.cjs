// NON-RENDERED: real ESM module, explicit SDK/hook stubs. Not visual proof.
// Run ONLY via tests/run-isolated-docker.sh tests/bulk_permissions.cjs.
const fs = require('node:fs'), vm = require('node:vm'), assert = require('node:assert/strict');

const LABEL = { read: 'Read', create: 'Create', edit: 'Edit', delete: 'Delete' };
const CAPS = ['read', 'create', 'edit', 'delete'];
const caps = (...on) => ({ read: on.includes('read'), create: on.includes('create'), edit: on.includes('edit'), delete: on.includes('delete') });
const failure = category => new Error(`Error invoking remote method 'hermes:api': Error: 400: ${JSON.stringify({ ok: false, error: category, message: 'SECRET_RAW_BODY' })}`);
const settle = () => new Promise(r => setImmediate(r));

function nodes(n, out = []) { if (!n || typeof n !== 'object') return out; out.push(n); for (const child of [n.props?.children].flat(Infinity)) nodes(child, out); return out; }
function text(n) { if (n == null || typeof n === 'boolean') return ''; if (typeof n !== 'object') return String(n); return [n.props?.title, n.props?.description, n.props?.children].flat(Infinity).map(text).join(' '); }
const same = (a, b) => a && b && a.length === b.length && a.every((v, i) => Object.is(v, b[i]));

async function harness(options = {}) {
  const f = {
    owner: 'A', epoch: 1, connected: true, state: undefined, migration: null, gateway: 'open',
    rows: [], calls: [], notices: [], navigation: [], failures: {}, hold: null, pending: [], permissions: null,
    ...options,
  };
  f.rows = f.rows.map(row => ({ color: '#123456', access: 'off', ...row }));
  let dead = false, cursor = 0, slots = [], effects = [], timers = new Map(), serial = 0, dirty = true;

  const ctx = {
    rest: async (path, opts = {}) => {
      f.calls.push({ path, opts });
      if (f.failures[path]) throw failure(f.failures[path]);
      if (f.hold === path && ['/status', '/discover'].includes(path)) return new Promise((resolve, reject) => f.pending.push({ resolve, reject }));
      if (path === '/status') return { owner_id: f.owner, connected: f.connected, state: f.state, migration_notice: f.migration, permissions: f.permissions ?? Object.fromEntries(f.rows.map(r => [r.id, r.capabilities])) };
      if (path === '/discover') { assert.equal(opts.body?.owner_id, f.owner, 'discover requires verified owner'); return { ok: true, owner_id: f.owner, calendars: f.rows.map(r => ({ ...r })), migration_notice: f.migration }; }
      if (path === '/permissions') {
        assert.equal(opts.body?.owner_id, f.owner, 'save requires verified owner');
        const reply = { ok: true, owner_id: f.owner, permissions: opts.body.permissions, migration_notice: null };
        if (f.hold === '/permissions') return new Promise((resolve, reject) => f.pending.push({ resolve, reject, reply }));
        return reply;
      }
      if (path === '/disconnect') return { ok: true, owner_id: f.owner, owner_after: f.owner, credential_removed: false, message: 'synthetic' };
      throw new Error('unexpected REST operation ' + path);
    },
    registerSettingsPage: () => () => {},
    register: undefined,
    onDispose: () => {},
  };
  const atom = value => ({ get: () => (typeof value === 'function' ? value() : value), subscribe: () => () => {} });
  const hooks = {
    useCallback: fn => fn,
    useEffect(fn, deps) { const i = cursor++; if (!slots[i] || !same(slots[i].deps, deps)) { const prior = slots[i]; slots[i] = { deps, cleanup: prior?.cleanup }; effects.push(() => { slots[i].cleanup?.(); slots[i].cleanup = fn() }); } },
    useMemo(fn) { cursor++; return fn() },
    useRef(value) { const i = cursor++; return slots[i] || (slots[i] = { current: value }) },
    useState(initial) { const i = cursor++; if (!slots[i]) slots[i] = { value: typeof initial === 'function' ? initial() : initial }; return [slots[i].value, v => { if (dead) return; const next = typeof v === 'function' ? v(slots[i].value) : v; if (!Object.is(next, slots[i].value)) { slots[i].value = next; dirty = true } }] },
  };
  const box = {
    console, Date, Intl, URL, JSON, Object, Math, Set, Map, Array, String, Number, Boolean, Error, Promise,
    Button: 'Button', Checkbox: 'Checkbox', ErrorState: 'ErrorState', EmptyState: 'EmptyState', GlyphSpinner: 'GlyphSpinner', Badge: 'Badge', Codicon: 'Codicon',
    gatewayActivationEpoch: () => f.epoch,
    host: {
      state: { profile: atom('GLOBAL'), connectionId: atom('conn'), gateway: atom(() => f.gateway) },
      navigate: path => f.navigation.push(path),
      notify: notice => { if (!dead) f.notices.push(notice) },
    },
    setTimeout: fn => { timers.set(++serial, fn); return serial },
    clearTimeout: id => timers.delete(id),
    setInterval: fn => { timers.set(++serial, fn); return serial },
    clearInterval: id => timers.delete(id),
    jsx: (type, props) => ({ type, props: props || {} }),
    jsxs: (type, props) => ({ type, props: props || {} }),
    ...hooks,
  };
  const source = fs.readFileSync('desktop/plugin.js', 'utf8').replace(/^import .*\n/gm, '').replace('export default {', 'globalThis.plugin = {');
  vm.createContext(box);
  vm.runInContext(source + '\nglobalThis.testAppearanceStore = appearanceStore;', box);
  f.appearanceStore = box.testAppearanceStore;
  let renderPage;
  box.plugin.register({ registerSettingsPage: page => { renderPage = page } });
  assert(renderPage?.render, 'settings registration preserved');
  const lifecycle = { add() {}, delete() {} };
  const wrapper = renderPage.render();
  const inner = nodes(wrapper.type({ ...wrapper.props, ctx, lifecycle })).find(n => typeof n.type === 'function');
  assert(inner, 'registered AppearanceFrame contains actual settings');
  slots = []; effects = []; cursor = 0;
  const Component = inner.type;
  let appearanceSlots = [];
  let tree;
  const render = () => {
    cursor = 0;
    const page = Component({ ...inner.props, ctx, lifecycle });
    const appearance = nodes(page).find(n => typeof n.type === 'function');
    if (appearance) {
      const pageSlots = slots, pageCursor = cursor;
      slots = appearanceSlots; cursor = 0;
      const expanded = appearance.type(appearance.props);
      appearanceSlots = slots; slots = pageSlots; cursor = pageCursor;
      Object.assign(appearance, expanded);
    }
    return page;
  };
  f.flush = async () => { for (let i = 0; i < 40; i++) { if (dirty && !dead) { dirty = false; tree = render(); for (const fn of effects.splice(0)) fn() } await settle() } assert(!dirty || dead, 'hook render did not settle') };
  f.tick = async () => { for (const fn of [...timers.values()]) fn(); await f.flush() };
  f.all = () => nodes(tree);
  f.nodes = () => f.all();
  f.inputs = () => f.all().filter(n => n.type === 'input');
  f.buttons = () => f.all().filter(n => ['Button', 'button'].includes(n.type));
  f.text = () => text(tree);
  f.bulk = (cap, on = true) => {
    const label = `${on ? 'Select all' : 'Clear all'} ${LABEL[String(cap).toLowerCase()]}`;
    const matches = f.buttons().filter(n => n.props['aria-label'] === label);
    assert.equal(matches.length, 1, label);
    assert.equal(matches[0].props.children, on ? 'Select all' : 'Clear all');
    return matches[0];
  };
  f.row = (cap, name) => { const n = f.inputs().find(n => n.props['aria-label'] === `${LABEL[String(cap).toLowerCase()]} for ${name}`); assert(n, `Missing row control ${LABEL[String(cap).toLowerCase()]} for ${name}`); return n };
  f.hasBulk = () => f.buttons().some(n => /^(Select|Clear) all (Read|Create|Edit|Delete)$/.test(n.props['aria-label'] || ''));
  f.bulkState = cap => {
    const select = f.bulk(cap), clear = f.bulk(cap, false);
    assert.equal(clear.props.disabled, select.props.disabled, 'both actions share eligibility lock');
    const column = f.all().find(n => n.props.className === 'caldav-bulk' && nodes(n).includes(select));
    const eligible = f.inputs().filter(n => n.props['aria-label']?.startsWith(`${LABEL[cap]} for `))
      .filter(n => cap === 'read' || f.row('read', n.props['aria-label'].split(' for ')[1]).props.checked);
    const on = eligible.filter(n => n.props.checked).length;
    const state = eligible.length && on === eligible.length ? 'All' : on ? 'Mixed' : 'Off';
    assert(text(column).includes(`${state} · ${on}/${eligible.length} eligible`), 'visible column state and exact eligible numerator');
    return { checked: state === 'All', mixed: state === 'Mixed', disabled: select.props.disabled === true };
  };
  f.toggleBulk = async (cap, on) => { const n = f.bulk(cap, on); assert(!n.props.disabled); n.props.onClick(); await f.flush() };
  f.toggleRow = async (cap, name, on) => { f.row(cap, name).props.onChange({ target: { checked: on } }); await f.flush() };
  f.click = async label => { const n = f.buttons().find(n => n.props.children === label); assert(n, `Missing button ${label}`); assert(!n.props.disabled || n.props.onClick, `Button ${label} disabled with no handler`); n.props.onClick?.(); await f.flush() };
  f.saveButton = () => { const n = f.buttons().find(n => n.props.children === 'Save calendar access'); assert(n, 'Missing Save'); return n };
  f.saves = () => f.calls.filter(c => c.path === '/permissions');
  f.discover = async () => { await f.click('Test and discover calendars') };
  f.unmount = () => { if (dead) return; dead = true; for (const s of slots) s?.cleanup?.(); };
  f.flushClean = async () => { await f.flush(); assert.equal(timers.size, 0, 'timers disposed') };
  await f.flush();
  return f;
}

const cases = [];
const test = (label, run) => cases.push([label, run]);
const rows = (...specs) => specs.map((s, i) => ({ id: `https://caldav.icloud.com/c${i + 1}/`, name: s.name, capabilities: s.caps, access: 'off' }));
const copy = value => JSON.parse(JSON.stringify(value));
// Deliberately not Daybook: fallback replacement must not look like preservation.
const savedAppearance = {
  version: 1, mode: 'dark',
  light: { background: '#FCF6FA', panel: '#FFFFFF', text: '#37253F', accent: '#794526', selected: '#EEDDEC' },
  dark: { background: '#10252C', panel: '#193640', text: '#E5F7FA', accent: '#8AD9E6', selected: '#2C4D58' }
};
async function storageFixture(initial = savedAppearance) {
  const f = await harness();
  const disk = { persisted: copy(initial), writes: 0, fault: null, publications: [] };
  const storage = {
    get(key) {
      assert.equal(key, 'kalends.appearance.v1');
      if (disk.fault === 'get') throw Error('synthetic get failure');
      return copy(disk.fault === 'readback' ? savedAppearance : disk.persisted);
    },
    set(key, value) {
      assert.equal(key, 'kalends.appearance.v1');
      if (disk.fault === 'set') throw Error('synthetic set failure');
      disk.writes++;
      disk.persisted = copy(value);
    }
  };
  const store = f.appearanceStore({ storage });
  store.subscribe(value => disk.publications.push(copy(value)));
  f.unmount();
  return { store, disk };
}
for (const [name, patch] of [
  ['full palette contrast', { light: { ...savedAppearance.light, text: savedAppearance.light.background } }],
  ['partial palette', { light: { background: '#FFFFFF' } }],
  ['invalid hex', { light: { ...savedAppearance.light, accent: '#GGGGGG' } }],
  ['missing Today pair', { light: { ...savedAppearance.light } }],
  ['failing Today contrast', { light: { ...savedAppearance.light, todayBackground: '#123456', todayText: '#123456' } }],
  ['invalid mode', { mode: 'automatic' }],
  ['version 2', { version: 2 }]
]) test(`appearance rejects ${name} without persistence or publication`, async () => {
  const { store, disk } = await storageFixture();
  const before = copy(store.read()), persisted = copy(disk.persisted);
  assert.equal(store.save(copy(patch)), false, `${name} must return false`);
  assert.deepEqual(disk.persisted, persisted, 'exact saved value unchanged');
  assert.equal(disk.writes, 0);
  assert.deepEqual(disk.publications, []);
  assert.deepEqual(copy(store.read()), before);
});
const withToday = palette => ({ ...palette, todayBackground: '#17302A', todayText: '#FFFFFF' });
const upgradedAppearance = { ...savedAppearance, light: withToday(savedAppearance.light), dark: withToday(savedAppearance.dark) };
test('appearance migrates a corrupted Today pair without changing valid prior colors or writing on load', async () => {
  const invalid = { ...savedAppearance, light: { ...savedAppearance.light, todayBackground: '#AAAAAA', todayText: '#AAAAAA' } };
  const { store, disk } = await storageFixture(invalid);
  assert.deepEqual(copy(store.read()), upgradedAppearance);
  assert.deepEqual(disk.persisted, invalid);
  assert.equal(disk.writes, 0);
});
test('appearance accepts mode-only without changing either palette', async () => {
  const { store, disk } = await storageFixture();
  assert.equal(store.save({ mode: 'system' }), true);
  const expected = { ...upgradedAppearance, mode: 'system' };
  assert.deepEqual(disk.persisted, expected);
  assert.deepEqual(copy(store.read()), expected);
  assert.deepEqual(disk.publications, [expected]);
  assert.equal(disk.writes, 1);
});
test('appearance accepts lowercase full palette without changing mode or other palette', async () => {
  const { store, disk } = await storageFixture();
  const light = { ...upgradedAppearance.light, background: '#fffafa' };
  assert.equal(store.save({ light: Object.fromEntries(Object.entries(light).map(([k, v]) => [k, v.toLowerCase()])) }), true);
  const expected = { ...upgradedAppearance, light: { ...light, background: '#FFFAFA' } };
  assert.deepEqual(disk.persisted, expected);
  assert.deepEqual(copy(store.read()), expected);
  assert.deepEqual(disk.publications, [expected]);
  assert.equal(disk.writes, 1);
});
test('appearance corrupt load falls back without repairing storage', async () => {
  const corrupt = { version: 2, mode: 'bogus' };
  const { store, disk } = await storageFixture(corrupt);
  const { store: fallback } = await storageFixture(null);
  assert.deepEqual(copy(store.read()), copy(fallback.read()));
  assert.equal(store.read().version, 1);
  assert.equal(store.read().mode, 'desktop');
  assert.deepEqual(disk.persisted, corrupt);
  assert.equal(disk.writes, 0);
  assert.deepEqual(disk.publications, []);
});
for (const fault of ['set', 'get', 'readback']) test(`appearance ${fault} failure retains last active state without publication`, async () => {
  const { store, disk } = await storageFixture();
  assert.equal(store.save({ mode: 'light' }), true);
  const before = copy(store.read());
  disk.publications.length = 0; disk.writes = 0; disk.fault = fault;
  assert.equal(store.save({ mode: 'system' }), false);
  assert.deepEqual(copy(store.read()), before);
  assert.deepEqual(disk.publications, []);
  assert.equal(disk.writes, fault === 'set' ? 0 : 1);
  // A successful SDK write cannot be rolled back by a failed readback.
});

const prematureDiscovery = /No calendars|(?:Choose )?Test and discover calendars to|Test discovery to verify access/i;
for (const [name, options, guidance] of [
  ['initial status pending', { hold: '/status' }, /Loading account/],
  ['profile gateway disconnected', { gateway: 'closed' }, /Reconnect the gateway/],
  ['neither credential configured', { connected: false, state: 'not_configured' }, /account is not set up/],
  ['Apple ID missing', { connected: false, state: 'missing_apple_id' }, /Apple ID is missing/],
  ['password missing', { connected: false, state: 'missing_credential' }, /App-specific password is missing/],
  ['owner unverified', { owner: null }, /owner is unverified/]
]) test(`settings suppresses discovery guidance while ${name}`, async () => {
  const f = await harness(options);
  assert.match(f.text(), guidance, 'state-specific account guidance remains');
  assert.equal(f.buttons().find(n => n.props.children === 'Test and discover calendars').props.disabled, true);
  assert.doesNotMatch(f.text(), prematureDiscovery);
  f.unmount();
});
test('settings configured but untested invites access test, not empty success', async () => {
  const f = await harness();
  assert.match(f.text(), /Choose Test and discover calendars to check iCloud access\./);
  assert.match(f.text(), /credentials configured/);
  assert.doesNotMatch(f.text(), /No calendars|discovery verified/);
  assert.equal(f.buttons().find(n => n.props.children === 'Test and discover calendars').props.disabled, false);
});
test('settings initial Discover pending offers neither prompt nor empty claim', async () => {
  const f = await harness({ hold: '/discover' });
  await f.discover();
  assert.equal(f.pending.length, 1, 'real Discover is pending');
  assert.equal(f.buttons().find(n => n.props.children === 'Test and discover calendars').props.disabled, true);
  // Account credential presence is not a discovery result.
  assert.doesNotMatch(f.text(), prematureDiscovery);
  assert.doesNotMatch(f.text(), /discovery verified/);
  f.unmount();
});
test('settings successful empty discovery reports empty success, not untested or failure', async () => {
  const f = await harness();
  await f.discover();
  assert.match(f.text(), /No calendars were found\./);
  assert.match(f.text(), /discovery verified/);
  assert.doesNotMatch(f.text(), /Test and discover calendars to|Test discovery to verify|Request failed|Kalends error/);
});

test('native settings opens Settings and Appearance with collapsed Theme Maker and explicit bulk actions', async () => {
  const f = await harness({ rows: rows({ name: 'Alpha', caps: caps('read') }) });
  // Tracer bullet for the registered native page, not rendered/visual proof.
  // Gear parity target: /settings?tab=plugins&plugin=icloud-caldav.
  // Account navigation stays unchanged; rendered route coverage follows later.
  const content = n => {
    if (n == null || typeof n === 'boolean') return '';
    if (typeof n !== 'object') return String(n);
    return [n.props?.children].flat(Infinity).map(content).join(' ').trim();
  };
  const visible = (n, out = []) => {
    if (!n || typeof n !== 'object' || n.props?.hidden || n.props?.['aria-hidden'] === true ||
        n.props?.['aria-hidden'] === 'true' || n.props?.style?.display === 'none' ||
        n.props?.style?.visibility === 'hidden') return out;
    out.push(n);
    const children = [n.props?.children].flat(Infinity);
    for (const child of children) {
      if (n.type !== 'details' || n.props.open || child?.type === 'summary') visible(child, out);
    }
    return out;
  };
  const shown = () => visible(f.nodes()[0]);
  const name = n => n.props['aria-label'] || content(n);
  const isButton = n => n.type === 'button' || n.type === 'Button';
  assert(shown().some(n => n.type === 'h1' && /^Kalends settings$/i.test(content(n))),
    'native settings must open the approved page with a visible Kalends settings h1');
  assert(shown().some(n => /^h[1-6]$/.test(n.type) && /Appearance/i.test(content(n))),
    'native settings must include the Appearance section');
  const disclosure = shown().find(n => (isButton(n) || n.type === 'summary') && /^Theme maker$/i.test(name(n)));
  assert(disclosure, 'Theme Maker must be a button or summary disclosure');
  if (disclosure.type === 'summary') {
    const details = shown().find(n => n.type === 'details' && nodes(n).includes(disclosure));
    assert(details && !details.props.open, 'Theme Maker details must start collapsed');
  } else {
    assert([false, 'false'].includes(disclosure.props['aria-expanded']),
      'Theme Maker button must expose its initially collapsed state');
  }
  assert(!shown().some(n => n.type === 'textarea' || (n.type === 'input' && n.props.type === 'color')),
    'no visible theme editor on initial entry');
  await f.discover();
  const actions = [];
  for (const cap of CAPS) for (const action of ['Select all', 'Clear all']) {
    const label = `${action} ${LABEL[cap]}`;
    const matches = shown().filter(n => isButton(n) && name(n) === label);
    assert.equal(matches.length, 1, `expected one real button named ${label}`);
    assert.equal(typeof matches[0].props.onClick, 'function', `${label} must be actionable`);
    actions.push(matches[0]);
  }
  assert.equal(new Set(actions).size, 8, 'each capability needs separate Select all and Clear all buttons');
});

test('migration Save persists unchanged effective rights without new grants', async () => {
  const f = await harness({ migration: 'Legacy permissions: review and Save.', rows: rows({ name: 'Legacy', caps: caps('read', 'create') }, { name: 'Off', caps: caps() }) });
  await f.discover();
  assert.equal(f.saveButton().props.disabled, false);
  assert.equal(f.saves().length, 0);
  await f.click('Save calendar access');
  assert.equal(f.saves().length, 1);
  assert.deepEqual(JSON.parse(JSON.stringify(f.saves()[0].opts.body.permissions)), {
    'https://caldav.icloud.com/c1/': caps('read', 'create'),
    'https://caldav.icloud.com/c2/': caps()
  });
  assert.equal(f.saveButton().props.disabled, true);
});

test('bulk matrix all/mixed/off across the draft', async () => {
  const f = await harness({ rows: rows({ name: 'Alpha', caps: caps('read') }, { name: 'Beta', caps: caps('read') }, { name: 'Gamma', caps: caps() }) });
  await f.discover();
  assert.deepEqual(f.bulkState('read'), { checked: false, mixed: true, disabled: false }, 'Read mixed');
  assert.deepEqual(f.bulkState('create'), { checked: false, mixed: false, disabled: false }, 'Create off, readable rows exist');
  assert.deepEqual(f.bulkState('edit'), { checked: false, mixed: false, disabled: false }, 'Edit off');
  assert.deepEqual(f.bulkState('delete'), { checked: false, mixed: false, disabled: false }, 'Delete off');
  // A write All turns on only readable rows and never grants Read.
  await f.toggleBulk('create', true);
  assert.equal(f.row('Create', 'Alpha').props.checked, true);
  assert.equal(f.row('Create', 'Beta').props.checked, true);
  assert.equal(f.row('Read', 'Gamma').props.checked, false, 'Create must not grant Read');
  assert.equal(f.row('Create', 'Gamma').props.checked, false, 'Create must skip unreadable rows');
  assert.deepEqual(f.bulkState('create'), { checked: true, mixed: false, disabled: false }, 'Create all');
  // All Read on must not grant writes.
  await f.toggleBulk('read', true);
  assert.equal(f.row('Create', 'Gamma').props.checked, false, 'All Read turned on a write');
  assert.deepEqual(f.bulkState('create'), { checked: false, mixed: true, disabled: false }, 'Create now mixed (Beta/Alpha on, Gamma off)');
  // Read off clears every write.
  await f.toggleBulk('read', false);
  for (const cap of CAPS) assert.equal(f.bulkState(cap).checked, false, `Read-off left ${cap} on`);
  assert.deepEqual(f.bulkState('create'), { checked: false, mixed: false, disabled: true }, 'no readable rows disables writes');
  assert.equal(f.row('Create', 'Alpha').props.disabled, true, 'write row disabled without read');
  assert.equal(f.saves().length, 0, 'bulk edits are draft-only');
});

test('zero rows never shows a vacuous all-checked control', async () => {
  const f = await harness({ rows: [] });
  await f.discover();
  assert.equal(f.hasBulk(), false, 'no bulk controls with zero rows');
  assert.equal(f.inputs().filter(n => n.props.type === 'checkbox').length, 0);
  assert(!f.buttons().some(n => n.props.children === 'Save calendar access'));
});

test('zero eligible write rows leaves write bulk controls disabled and unchecked', async () => {
  const f = await harness({ rows: rows({ name: 'Alpha', caps: caps() }, { name: 'Beta', caps: caps() }) });
  await f.discover();
  assert.deepEqual(f.bulkState('read'), { checked: false, mixed: false, disabled: false }, 'Read off but available');
  for (const cap of ['create', 'edit', 'delete']) assert.deepEqual(f.bulkState(cap), { checked: false, mixed: false, disabled: true }, `${cap} disabled`);
  for (const cap of ['create', 'edit', 'delete']) for (const on of [true, false]) {
    f.bulk(cap, on).props.onClick(); await f.flush();
    assert.equal(f.row('Read', 'Alpha').props.checked, false, 'disabled write cannot grant Read');
    assert.equal(f.row(cap, 'Alpha').props.checked, false, 'disabled handler cannot alter draft');
  }
  assert.equal(f.saves().length, 0);
});

test('every write column Select/Clear affects only itself and readable rows', async () => {
  for (const cap of ['create', 'edit', 'delete']) {
    const f = await harness({ rows: rows({ name: 'Readable', caps: caps('read') }, { name: 'Off', caps: caps() }) });
    await f.discover();
    await f.toggleBulk(cap, true);
    for (const other of CAPS) {
      assert.equal(f.row(other, 'Readable').props.checked, other === 'read' || other === cap);
      assert.equal(f.row(other, 'Off').props.checked, false);
    }
    assert.deepEqual(f.bulkState(cap), { checked: true, mixed: false, disabled: false });
    await f.toggleBulk(cap, false);
    for (const other of CAPS) assert.equal(f.row(other, 'Readable').props.checked, other === 'read');
    assert.deepEqual(f.bulkState(cap), { checked: false, mixed: false, disabled: false });
    assert.equal(f.saves().length, 0);
    f.unmount(); await f.flushClean();
  }
});

test('duplicate calendar names stay distinguishable and share bulk scope', async () => {
  const f = await harness({ rows: rows({ name: 'Personal', caps: caps() }, { name: 'Personal', caps: caps() }) });
  await f.discover();
  assert(f.row('Read', 'Personal (Calendar 1)'), 'first duplicate labelled');
  assert(f.row('Read', 'Personal (Calendar 2)'), 'second duplicate labelled');
  await f.toggleBulk('read', true);
  assert.equal(f.row('Read', 'Personal (Calendar 1)').props.checked, true);
  assert.equal(f.row('Read', 'Personal (Calendar 2)').props.checked, true);
});

test('draft is not saved until Save and Discard reverts it', async () => {
  const f = await harness({ rows: rows({ name: 'Alpha', caps: caps('read') }) });
  await f.discover();
  assert.equal(f.saves().length, 0, 'discovery does not save');
  await f.toggleRow('create', 'Alpha', true);
  assert.match(f.text(), /Unsaved changes/);
  assert(f.nodes().some(n => String(n.props?.className || '').includes('caldav-access-footer')), 'sticky footer present');
  assert.equal(f.saveButton().props.disabled, false, 'Save enabled while dirty');
  await f.click('Discard unsaved changes');
  assert.match(f.text(), /No unsaved changes/);
  assert.equal(f.row('Create', 'Alpha').props.checked, false, 'Discard reverts the draft');
  await f.toggleRow('create', 'Alpha', true);
  await f.click('Save calendar access');
  assert.equal(f.saves().length, 1);
  assert.deepEqual(JSON.parse(JSON.stringify(f.saves()[0].opts.body.permissions)), { 'https://caldav.icloud.com/c1/': caps('read', 'create') });
  assert.match(f.text(), /No unsaved changes/);
});

test('save failure keeps the dirty draft and surfaces an error', async () => {
  const f = await harness({ rows: rows({ name: 'Alpha', caps: caps('read') }) });
  await f.discover();
  await f.toggleRow('edit', 'Alpha', true);
  f.failures['/permissions'] = 'invalid_request';
  await f.click('Save calendar access');
  assert.match(f.text(), /Kalends error/);
  assert.doesNotMatch(f.text(), /SECRET_RAW_BODY/);
  assert.match(f.text(), /Unsaved changes/, 'draft retained after failure');
  assert.equal(f.row('Edit', 'Alpha').props.checked, true, 'draft value retained');
  delete f.failures['/permissions'];
  await f.click('Save calendar access');
  assert.match(f.text(), /No unsaved changes/);
  assert.equal(f.saves().length, 2);
});

for (const kind of ['owner', 'epoch']) test(`stale ${kind} change blocks a captured save`, async () => {
  const f = await harness({ rows: rows({ name: 'Alpha', caps: caps('read') }) });
  await f.discover();
  await f.toggleRow('create', 'Alpha', true);
  const staleSave = f.saveButton().props.onClick;
  if (kind === 'owner') f.owner = 'B'; else f.epoch += 1;
  await f.tick();
  assert.doesNotMatch(f.text(), /Alpha/, 'changed ' + kind + ' clears the draft');
  staleSave();
  await f.flush();
  assert.equal(f.saves().length, 0, 'stale save must not submit');
  assert.equal(f.notices.length, 0, 'stale save must not notify');
});

test('save held across an owner change never publishes', async () => {
  const f = await harness({ rows: rows({ name: 'Alpha', caps: caps('read') }) });
  await f.discover();
  await f.toggleRow('create', 'Alpha', true);
  f.hold = '/permissions';
  f.saveButton().props.onClick();
  await f.flush();
  assert.equal(f.pending.length, 1, 'save in flight');
  f.owner = 'B';
  await f.tick();
  f.pending[0].resolve(f.pending[0].reply);
  await f.flush();
  assert.equal(f.notices.length, 0, 'late save reply must not toast');
  assert.doesNotMatch(f.text(), /Alpha/);
  f.hold = null;
});

test('a failed status check reports recoverable trouble without navigation', async () => {
  const f = await harness({ rows: rows({ name: 'Alpha', caps: caps('read') }), failures: { '/status': 'connectivity' } });
  assert.match(f.text(), /Unable to verify backend owner/i);
  assert.doesNotMatch(f.text(), /SECRET_RAW_BODY/);
  assert.equal(f.navigation.length, 0);
});

for (const state of ['not_configured', 'missing_apple_id', 'missing_credential']) test(`account setup CTA navigates for ${state}`, async () => {
  const f = await harness({ connected: false, state });
  assert.match(f.text(), /account setup/i);
  const setup = f.buttons().find(n => n.props.children === 'Set up iCloud account');
  assert(setup, 'setup CTA shown');
  setup.props.onClick();
  await f.flush();
  assert.deepEqual(f.navigation, ['/settings?tab=plugins&agent=icloud-caldav']);
  assert.equal(f.inputs().some(n => n.props.type === 'password'), false, 'no credential control');
});

test('legacy connected means configured, only discovery verifies access', async () => {
  const f = await harness();
  assert.match(f.text(), /credentials configured/);
  assert.doesNotMatch(f.text(), /iCloud account connected|discovery verified/);
  assert.equal(f.buttons().some(n => n.props.children === 'Set up iCloud account'), false);
  assert(f.buttons().some(n => text(n).trim() === 'Update Accounts'));
  await f.discover();
  assert.match(f.text(), /discovery verified/);
  f.failures['/discover'] = 'auth';
  await f.discover();
  assert.doesNotMatch(f.text(), /discovery verified/);
});

test('setup polling retains the owner and setup state without oscillation', async () => {
  const f = await harness({ connected: false, state: 'missing_credential' });
  for (let i = 0; i < 4; i++) {
    await f.tick();
    assert.match(f.text(), /App-specific password is missing/);
    assert.doesNotMatch(f.text(), /owner is unverified/);
  }
  assert.equal(f.calls.some(c => c.path !== '/status'), false);
});

test('same-owner polling never clobbers a dirty draft', async () => {
  const f = await harness({ rows: rows({ name: 'Alpha', caps: caps('read') }) });
  await f.discover();
  await f.toggleRow('edit', 'Alpha', true);
  f.permissions = {};
  await f.tick();
  assert.equal(f.row('Edit', 'Alpha').props.checked, true);
  assert.match(f.text(), /Unsaved changes/);
  assert.equal(f.saves().length, 0);
  f.connected = false; f.state = 'missing_credential';
  await f.tick();
  assert.equal(f.saveButton().props.disabled, true);
  assert.equal(f.row('Edit', 'Alpha').props.disabled, true);
  assert.equal(f.row('Edit', 'Alpha').props.checked, true);
  assert.equal(f.buttons().find(n => n.props.children === 'Discard unsaved changes').props.disabled, false);
  const calls = f.calls.length;
  await f.click('Discard unsaved changes');
  assert.equal(f.calls.length, calls, 'Discard is local even when credentials are missing');
  assert.equal(f.row('Edit', 'Alpha').props.checked, false);
  assert.match(f.text(), /No unsaved changes/);
});

test('local Discard works with closed gateway while owner and epoch still match', async () => {
  const f = await harness({ rows: rows({ name: 'Alpha', caps: caps('read') }) });
  await f.discover();
  await f.toggleRow('edit', 'Alpha', true);
  f.gateway = 'closed'; // Before a gateway subscription invalidates the owner scope.
  const calls = f.calls.length;
  await f.click('Discard unsaved changes');
  assert.equal(f.calls.length, calls);
  assert.equal(f.row('Edit', 'Alpha').props.checked, false);
});

test('local Discard cannot race a save or use a stale epoch', async () => {
  const f = await harness({ rows: rows({ name: 'Alpha', caps: caps('read') }) });
  await f.discover();
  await f.toggleRow('edit', 'Alpha', true);
  const discard = f.buttons().find(n => n.props.children === 'Discard unsaved changes').props.onClick;
  f.hold = '/permissions';
  f.saveButton().props.onClick();
  await f.flush();
  assert.equal(f.buttons().find(n => n.props.children === 'Discard unsaved changes').props.disabled, true);
  discard(); await f.flush();
  assert.equal(f.row('Edit', 'Alpha').props.checked, true);
  f.pending[0].resolve(f.pending[0].reply); await f.flush();
  await f.toggleRow('edit', 'Alpha', false);
  const stale = f.buttons().find(n => n.props.children === 'Discard unsaved changes').props.onClick;
  f.epoch++;
  stale(); await f.flush();
  assert.equal(f.row('Edit', 'Alpha').props.checked, false, 'stale epoch cannot restore saved values');
});

test('captured local Discard cannot restore a previous owner draft', async () => {
  const f = await harness({ rows: rows({ name: 'Alpha', caps: caps('read') }) });
  await f.discover();
  await f.toggleRow('edit', 'Alpha', true);
  const stale = f.buttons().find(n => n.props.children === 'Discard unsaved changes').props.onClick;
  f.owner = 'B'; await f.tick();
  const before = f.text(), calls = f.calls.length;
  stale(); await f.flush();
  assert.equal(f.text(), before);
  assert.equal(f.calls.length, calls);
  assert.doesNotMatch(f.text(), /Alpha/);
});

test('configuration precheck blocks save with local explanation and keeps draft', async () => {
  const f = await harness({ rows: rows({ name: 'Alpha', caps: caps('read') }) });
  await f.discover();
  await f.toggleRow('edit', 'Alpha', true);
  f.connected = false; f.state = 'missing_credential';
  await f.click('Save calendar access');
  assert.equal(f.saves().length, 0);
  assert.equal(f.row('Edit', 'Alpha').props.checked, true);
  assert.match(f.text(), /Credentials are not configured.*draft was kept/i);
});

for (const bulk of [false, true]) test(`Read enable clears latent writes, bulk=${bulk}`, async () => {
  // Malformed response only; backend validation already rejects write-without-read.
  const f = await harness({ rows: rows(
    { name: 'Unsafe', caps: caps('create', 'edit', 'delete') },
    { name: 'Readable', caps: caps('read', 'create') }
  ) });
  await f.discover();
  if (bulk) await f.toggleBulk('read', true);
  else await f.toggleRow('read', 'Unsafe', true);
  assert.equal(f.row('Read', 'Unsafe').props.checked, true);
  for (const cap of ['Create', 'Edit', 'Delete']) assert.equal(f.row(cap, 'Unsafe').props.checked, false);
  assert.equal(f.row('Create', 'Readable').props.checked, true, 'existing readable row retains explicit write');
  assert.equal(f.saves().length, 0);
});

test('gateway is local only; closed gateway has guidance not a setup-only CTA', async () => {
  const f = await harness({ gateway: 'closed' });
  assert.match(f.text(), /Reconnect the gateway/);
  assert.equal(f.buttons().find(n => n.props.children === 'Refresh status').props.disabled, true);
  assert.equal(f.buttons().some(n => n.props.children === 'Set up iCloud account'), false);
  await f.tick();
  assert.equal(f.calls.length, 0);
  for (const state of ['gateway', 'unverified', 'arbitrary']) {
    const spoof = await harness({ state });
    assert.match(spoof.text(), /credentials configured/);
    assert.doesNotMatch(spoof.text(), /Reconnect the gateway|owner is unverified/);
  }
});

test('missing owner offers refresh and blocks operations', async () => {
  const f = await harness({ owner: null });
  assert.match(f.text(), /owner is unverified/);
  assert.equal(f.buttons().find(n => n.props.children === 'Refresh status').props.disabled, false);
  assert.equal(f.buttons().find(n => n.props.children === 'Test and discover calendars').props.disabled, true);
  assert(f.buttons().some(n => text(n).trim() === 'Update Accounts'), 'host credential form remains reachable without a verified calendar owner');
  await f.discover();
  assert.equal(f.calls.some(c => c.path === '/discover'), false);
});

test('bulk numerator uses only eligible rows and scope counts are visible', async () => {
  // Defensive UI-only case. Backend validate_permissions already rejects writes
  // without Read; this malformed synthetic row must not inflate the All count.
  const f = await harness({ rows: rows({ name: 'Alpha', caps: caps('read') }, { name: 'Beta', caps: caps('create') }) });
  await f.discover();
  assert.deepEqual(f.bulkState('create'), { checked: false, mixed: false, disabled: false });
  assert.match(f.text(), /All Read: 2 eligible calendars.*All writes: 1 readable calendars/);
  assert.match(f.text(), /All Read changes every discovered row/);
  assert(f.nodes().some(n => n.props?.role === 'region' && n.props.tabIndex === 0 && /scrollable/.test(n.props['aria-label'])));
});

test('duplicate labels survive discovery reordering without exposing URLs', async () => {
  const data = rows({ name: 'Personal', caps: caps('read') }, { name: 'Personal', caps: caps() });
  const f = await harness({ rows: data });
  await f.discover();
  assert.equal(f.row('Read', 'Personal (Calendar 1)').props.checked, true);
  f.rows.reverse();
  await f.discover();
  assert.equal(f.row('Read', 'Personal (Calendar 1)').props.checked, true);
  assert.equal(f.row('Read', 'Personal (Calendar 2)').props.checked, false);
  assert.doesNotMatch(f.text(), /https:\/\/caldav.icloud.com\/c[12]/);
});

(async () => {
  console.log('NON-RENDERED bulk-permission contract; NOT visual or Desktop integration proof.');
  let failed = 0;
  for (const [label, run] of cases) {
    try { await run(); console.log(`PASS ${label}`) }
    catch (error) { failed++; console.error(`FAIL ${label}: ${error.message}`) }
  }
  console.log(`${cases.length - failed}/${cases.length} bulk-permission cases passed`);
  process.exitCode = failed ? 1 : 0;
})().catch(error => { console.error(error); process.exitCode = 1 });
