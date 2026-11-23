// Local, non-rendered contract for the shipped renderer. No host, network or fixtures.
const assert = require('node:assert/strict')
const fs = require('node:fs')
const vm = require('node:vm')
const source = fs.readFileSync('desktop/plugin.js', 'utf8')
assert.match(source, /Indigo:\s*\{\s*light:/, 'Indigo is an additive built-in preset')
assert.match(source, /Coast:\s*\{[\s\S]*?dark:/, 'Coast dark stays available')
assert.match(source, /grid-template-columns:minmax\(0,71fr\) minmax\(0,29fr\)/, 'split month and agenda')
assert.match(source, /@container \(max-width:799px\)/, 'narrow layout stacks')
assert.match(source, /Next recorded scheduled run only/, 'automation scope explained')
assert.match(source, /Unknown|unknown/, 'unknown feed state distinguished')
assert.match(source, /jsx\(WeatherIcon, \{ condition: view\.weather\[key\], size: 12 \}\)/, 'weather glyph retained')
assert.match(source, /name: source === 'automations' \? 'lightning'/, 'automation lightning is visible in date cells')
assert.match(source, /onKeyDown: event => \{[\s\S]*?ArrowDown: 7/, 'calendar date keyboard navigation retained')
assert.match(source, /data-custom.*forced-colors|forced-colors:active[\s\S]*?kal-special/, 'forced color special controls')
assert.doesNotMatch(source, /SAMPLE DATA|Fictional data ·/, 'no prototype labels in runtime')
async function main() {
  const jsx = (type, props, key) => ({ type, props: props || {}, key })
  const context = vm.createContext({ Date, Intl })
  const stubs = {
    '@hermes/plugin-sdk': Object.fromEntries(['Button', 'Checkbox', 'Badge', 'EmptyState', 'Codicon', 'ErrorState', 'GlyphSpinner', 'ROUTES_AREA', 'SIDEBAR_NAV_AREA', 'PALETTE_AREA'].map(key => [key, key])),
    react: { useEffect() {}, useRef: value => ({ current: value }), useState: value => [value, () => {}] },
    'react/jsx-runtime': { jsx, jsxs: jsx }
  }
  stubs['@hermes/plugin-sdk'].gatewayActivationEpoch = () => 1
  stubs['@hermes/plugin-sdk'].host = { state: { gateway: { get: () => 'open' } } }
  const module = new vm.SourceTextModule(source + '\nexport { PRESETS, defaultAppearance, inspectPalette, specialState, SpecialPanel, clientClock, monthGrid }\n', { context })
  await module.link(name => new vm.SyntheticModule(Object.keys(stubs[name]), function () {
    for (const [key, value] of Object.entries(stubs[name])) this.setExport(key, value)
  }, { context }))
  await module.evaluate()
  const { PRESETS, defaultAppearance, inspectPalette, specialState, SpecialPanel, clientClock, monthGrid } = module.namespace
  assert.equal(defaultAppearance().light.background, '#F8FAF7', 'existing saved default unchanged')
  for (const mode of ['light', 'dark']) assert.equal(inspectPalette(PRESETS.Indigo[mode]).errors.length, 0, `Indigo ${mode} contrast`)
  assert.equal(inspectPalette(PRESETS.Coast.dark).errors.length, 0, 'Coast dark contrast')
  assert.match(specialState('loaded', 0), /Empty/)
  assert.equal(specialState('off'), 'Off')
  assert.equal(specialState('not_configured'), 'Not configured')
  assert.equal(specialState('unknown'), 'Unknown')
  const flatten = n => !n || typeof n !== 'object' ? [] : [n, ...[n.props?.children].flat(Infinity).flatMap(flatten)]
  const render = n => flatten(n).map(n => typeof n.type === 'function' ? render(n.type(n.props)) : [n.props?.children].flat(Infinity).filter(v => typeof v !== 'object').join(' ')).flat().join(' ')
  const clock = clientClock(), grid = monthGrid(clock, '2026-11-03')
  const props = { mode: 'media', clock, grid, selected: '2026-11-03', today: '2026-11-03', select() {}, shift() {}, retry() {},
    enabled: true, checking: false, readiness: { feeds: { sonarr: 'ready', radarr: 'ready' } },
    mediaShown: { sonarr: true, radarr: true }, setMediaShown() {},
    sonarr: { state: 'unavailable', events: [] },
    radarr: { state: 'loaded', events: [{ id: 'm', title: 'Healthy film', start: '2026-11-03' }] } }
  const tree = SpecialPanel(props)
  const words = render(tree)
  assert.match(words, /Healthy film/)
  assert.match(words, /Sonarr unavailable/)
  assert.match(words, /partial results/)
  const waiting = render(SpecialPanel({ ...props, checking: true, readiness: null, sonarr: null, radarr: null }))
  assert.match(waiting, /Checking feed status/)
  assert.doesNotMatch(waiting, /0 releases|No items/)
  const unverified = SpecialPanel({ ...props, checking: true, readiness: null, sonarr: null, radarr: null })
  const dateButton = flatten(unverified).find(n => n.props?.className === 'caldav-date')
  assert.match(dateButton.props['aria-label'], /schedule not verified/)
  const off = render(SpecialPanel({ ...props, readiness: { feeds: { sonarr: 'off', radarr: 'not_configured' } }, sonarr: null, radarr: null }))
  assert.match(off, /No configured feeds are showing/)
  assert.doesNotMatch(off, /0 releases|No items/)
  const unavailable = render(SpecialPanel({ ...props, radarr: { state: 'unavailable', events: [] } }))
  assert.match(unavailable, /Release schedule unknown/)
  const pendingJobs = render(SpecialPanel({ ...props, mode: 'automations', automations: null }))
  assert.match(pendingJobs, /Checking scheduled runs/)
  assert.doesNotMatch(pendingJobs, /0 scheduled runs|No items/)
  assert(flatten(tree).some(n => n.props?.['data-calendar-day'] === '2026-11-03'), 'month grid retained')
  assert(flatten(tree).some(n => n.props?.className?.includes('kal-special-layout')), 'special split layout')
  const jobs = render(SpecialPanel({ ...props, mode: 'automations', automations: { state: 'loaded', events: [
    { id: 'a', title: 'Morning brief', start: '2026-11-03T12:30:00Z' }
  ] } }))
  assert.match(jobs, /Morning brief/)
  assert.match(jobs, /Next recorded scheduled run only/)
  console.log('PASS Indigo renderer local VM fixture (non-rendered)')
}
main().catch(error => { console.error(error); process.exitCode = 1 })
