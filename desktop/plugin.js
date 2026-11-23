import { Button, Checkbox, Badge, EmptyState, Codicon, ErrorState, GlyphSpinner, ROUTES_AREA, SIDEBAR_NAV_AREA, PALETTE_AREA, gatewayActivationEpoch, host } from '@hermes/plugin-sdk'
import { useEffect, useRef, useState } from 'react'
import { jsx, jsxs } from 'react/jsx-runtime'

const ENDPOINT = 'https://caldav.icloud.com'
const CAPABILITIES = ['read', 'create', 'edit', 'delete']
const offCapabilities = () => ({ read: false, create: false, edit: false, delete: false })
const CAPABILITY_LABELS = Object.freeze({ read: 'Read', create: 'Create', edit: 'Edit', delete: 'Delete' })
// Host deep links. Account credentials are entered ONLY on the host-rendered
// Agent settings form (config_schema); this plugin has no credential control,
// secret RPC or browser persistence of its own.
const CALENDAR_SETTINGS_HREF = '/settings?tab=plugins&plugin=icloud-caldav'
const ACCOUNT_SETUP_HREF = '/settings?tab=plugins&agent=icloud-caldav'
const CONNECTION_MESSAGES = Object.freeze({
  gateway: 'Hermes backend is disconnected. Reconnect the gateway, then refresh.',
  unverified: 'Backend owner is unverified. Refresh to continue.',
  not_configured: 'iCloud account is not set up. Add your Apple ID and app-specific password in account setup.',
  missing_apple_id: 'Apple ID is missing. Add it in account setup to continue.',
  missing_credential: 'App-specific password is missing. Add it in account setup to continue.'
})
const SETUP_STATES = ['not_configured', 'missing_apple_id', 'missing_credential']
const VIEW_LABELS = { personal: 'Personal', automations: 'Automations', media: 'Media' }
const MEDIA_LABELS = { sonarr: 'TV', radarr: 'Movies' }
const SPECIAL_STATES = {
  off: 'Off', not_configured: 'Not configured', ready: 'Ready',
  unavailable: 'Unavailable', loading: 'Loading…', hidden: 'Hidden', unknown: 'Unknown'
}

function SpecialIcon({ name, size = 20 }) {
  const paths = {
    personal: 'M20 22v-2a5 5 0 0 0-5-5H9a5 5 0 0 0-5 5v2 M16 7a4 4 0 1 1-8 0 4 4 0 1 1 8 0',
    automations: 'M12 6v6l4 2 M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0',
    lightning: 'm13 2-9 12h7l-1 8 10-12h-8z',
    settings: 'M12 9a3 3 0 1 0 0 6 3 3 0 0 0 0-6z M9 3h6l.7 3 2.8 1.6 2.9-.8 1 2-2.2 2.1v3.2l2.2 2.1-1 2-2.9-.8-2.8 1.6-.7 3H9l-.7-3-2.8-1.6-2.9.8-1-2 2.2-2.1v-3.2L1.6 8.8l1-2 2.9.8 2.8-1.6z',
    media: 'M3 5h18v14H3z M10 8l6 4-6 4z',
    sonarr: 'M3 4h18v13H3z M8 21h8 M12 17v4',
    radarr: 'm3 8 17-4-1-4L2 4z M3 8h18v12H3z M6 3l3 3 M13 2l3 3',
    check: 'm5 12 4 4L19 6'
  }
  return jsx('svg', { width: size, height: size, viewBox: '0 0 24 24', fill: 'none', stroke: 'currentColor', strokeWidth: 1.6, strokeLinecap: 'round', strokeLinejoin: 'round', 'aria-hidden': true, focusable: false, children: jsx('path', { d: paths[name] }) })
}

function ViewTabs({ mode, onChange }) {
  const views = Object.keys(VIEW_LABELS)
  return jsx('div', { className: 'kal-view-tabs', role: 'tablist', 'aria-label': 'Calendar views', children: views.map((key, index) => jsx('button', {
    role: 'tab', type: 'button', id: `kal-tab-${key}`, 'aria-controls': `kal-panel-${key}`,
    'aria-selected': mode === key, 'aria-label': VIEW_LABELS[key], title: VIEW_LABELS[key], tabIndex: mode === key ? 0 : -1,
    onClick: () => onChange(key),
    onKeyDown: event => {
      const next = ({ ArrowLeft: (index + 2) % 3, ArrowRight: (index + 1) % 3, Home: 0, End: 2 })[event.key]
      if (next === undefined) return
      event.preventDefault()
      onChange(views[next])
      event.currentTarget.parentElement.children[next]?.focus()
    },
    children: jsx(SpecialIcon, { name: key })
  }, key)) })
}

function specialState(state, count) {
  return state === 'loaded' ? (count ? `${count} ${count === 1 ? 'item' : 'items'}` : 'Empty · no items this month') :
    SPECIAL_STATES[state] || SPECIAL_STATES.unknown
}

// A matching reply owner is not proof that the backend still owns this runtime.
// Probe the scoped runtime after settlement, without discovering or querying iCloud.
async function verifySpecialOwner(status, current, ownerId, clear) {
  if (!current()) return false
  try {
    const next = await status(current)
    if (!current()) return false
    if (next?.owner_id === ownerId && next.ok !== false) return true
  } catch (_) { /* Failed verification never admits content. */ }
  if (current()) clear()
  return false
}

// Only publish the reply for this exact owner, view, month and filter state.
function useSpecialRead(key, current, read, verify, ownerId, fallback) {
  const [reply, setReply] = useState(null)
  const token = useRef({ key })
  if (token.current.key !== key) token.current = { key }
  const identity = token.current
  useEffect(() => {
    setReply(null)
    if (!key) return
    let live = true
    const valid = () => live && token.current === identity && current()
    void (async () => {
      if (!valid()) return
      let data
      try { data = await read(valid) } catch (_) { /* Use only the public fallback. */ }
      if (!await verify(valid) || !valid()) return
      setReply({ identity, data: data?.owner_id === ownerId && data.ok !== false ? data : fallback })
    })()
    return () => { live = false }
  }, [key])
  return key && current() && reply?.identity === identity ? reply.data : null
}

function specialRows(reply, source) {
  if (reply?.state !== 'loaded' || !Array.isArray(reply.events) || reply.events.length > 1000) return []
  return reply.events.filter(row => row && typeof row.id === 'string' && typeof row.title === 'string' &&
    typeof row.start === 'string' && Number.isFinite(Date.parse(row.start)))
    .map(row => ({ id: row.id, title: row.title, start: row.start, source }))
}
const gatewayOpen = () => !host.state?.gateway?.get || host.state.gateway.get() === 'open'
// Owner contract is never inferred from a boolean: a reply without owner_id is
// unverifiable, so it maps to its own recoverable state instead of "configured".
function connectionState(status, open = gatewayOpen()) {
  if (!open) return 'gateway'
  if (!status || !status.owner_id) return 'unverified'
  if (SETUP_STATES.includes(status.state)) return status.state
  return status.connected ? 'connected' : 'not_configured'
}
// "connected" is the legacy backend credential-presence contract, not an Apple
// authentication/network probe. Verification belongs only to this discovery scope.
const accountMessage = (connection, verified) => connection === 'connected'
  ? verified ? 'iCloud discovery verified for this session.' : 'iCloud credentials configured. Test discovery to verify access.'
  : CONNECTION_MESSAGES[connection]
const calendarLabel = (calendar, calendars) => {
  const name = literal(calendar.name) || 'Unnamed calendar'
  const duplicates = calendars.filter(c => (literal(c.name) || 'Unnamed calendar') === name).map(c => c.id).sort()
  return duplicates.length > 1 ? `${name} (Calendar ${duplicates.indexOf(calendar.id) + 1})` : name
}
// Appearance is device-local, never part of account or permission drafts.
const APPEARANCE_KEY = 'kalends.appearance.v1'
const PALETTE_FIELDS = { background: 'Background', panel: 'Panels', text: 'Text', accent: 'Accent', selected: 'Selected day', todayBackground: 'Today background', todayText: 'Today text' }
const PRESETS = {
  Indigo: {
    light: { background: '#F2F4FF', panel: '#E7ECFA', text: '#17254A', accent: '#3156A0', selected: '#D1DFFF', todayBackground: '#3156A0', todayText: '#FFFFFF' },
    dark: { background: '#222943', panel: '#303A59', text: '#F3F5FF', accent: '#91E5C5', selected: '#30515B', todayBackground: '#91E5C5', todayText: '#142A31' }
  },
  Daybook: {
    light: { background: '#F8FAF7', panel: '#EEF4EF', text: '#17302A', accent: '#27674E', selected: '#D5EBDD' },
    dark: { background: '#111A19', panel: '#1A2725', text: '#F3F8F5', accent: '#A8EBC9', selected: '#304D3D' }
  },
  Dusk: {
    light: { background: '#FCF6FA', panel: '#FFFFFF', text: '#37253F', accent: '#794526', selected: '#EEDDEC' },
    dark: { background: '#1B1725', panel: '#2A2337', text: '#F5EAF7', accent: '#FFBC91', selected: '#483753' }
  },
  Coast: {
    light: { background: '#F2FAFB', panel: '#FFFFFF', text: '#163B42', accent: '#166373', selected: '#D2EAF0' },
    dark: { background: '#10252C', panel: '#193640', text: '#E5F7FA', accent: '#8AD9E6', selected: '#2C4D58' }
  },
  Marigold: {
    light: { background: '#FFF9E4', panel: '#F6E7AE', text: '#352600', accent: '#725000', selected: '#F9DF82', todayBackground: '#563900', todayText: '#FFF9E4' },
    dark: { background: '#211900', panel: '#342800', text: '#FFF0BE', accent: '#FFCD4E', selected: '#523D00', todayBackground: '#FFCD4E', todayText: '#211900' }
  },
  Cobalt: {
    light: { background: '#F1F5FF', panel: '#E1E9FF', text: '#12234D', accent: '#234AC5', selected: '#CFDDFF', todayBackground: '#203CA0', todayText: '#FFFFFF' },
    dark: { background: '#10182F', panel: '#1B2850', text: '#E8F0FF', accent: '#9FB4FF', selected: '#304787', todayBackground: '#B5C5FF', todayText: '#10182F' }
  }
}
const hexColor = value => typeof value === 'string' && /^#[0-9a-f]{6}$/i.test(value) ? value.toUpperCase() : null
// Load-only migration preserves all five original colors.
function migrateToday(value) {
  const next = { ...value }
  if (!hexColor(next.todayBackground) || !hexColor(next.todayText) || contrast(next.todayBackground, next.todayText) < 4.5) {
    next.todayBackground = '#17302A'
    next.todayText = '#FFFFFF'
  }
  return next
}
for (const preset of Object.values(PRESETS)) {
  preset.light = Object.freeze(migrateToday(preset.light))
  preset.dark = Object.freeze(migrateToday(preset.dark))
  Object.freeze(preset)
}
Object.freeze(PRESETS)
function contrast(a, b) {
  const luminance = color => {
    const rgb = [1, 3, 5].map(i => parseInt(color.slice(i, i + 2), 16) / 255).map(n => n <= .04045 ? n / 12.92 : ((n + .055) / 1.055) ** 2.4)
    return rgb[0] * .2126 + rgb[1] * .7152 + rgb[2] * .0722
  }
  const x = luminance(a), y = luminance(b)
  return (Math.max(x, y) + .05) / (Math.min(x, y) + .05)
}
function inspectPalette(value) {
  const palette = {}, errors = []
  for (const [key, label] of Object.entries(PALETTE_FIELDS)) {
    palette[key] = hexColor(value?.[key])
    if (!palette[key]) errors.push(`${label}: enter #RRGGBB.`)
  }
  if (!errors.length) {
    for (const surface of ['background', 'panel', 'selected']) {
      if (contrast(palette.text, palette[surface]) < 4.5) errors.push(`Text / ${PALETTE_FIELDS[surface]} needs 4.5:1 (also used for focus and selected outlines).`)
    }
    // Accent is used as a filled control with background-colored text.
    if (contrast(palette.accent, palette.background) < 4.5) errors.push('Accent / Background needs 4.5:1.')
    if (contrast(palette.todayText, palette.todayBackground) < 4.5) errors.push('Today text / Today background needs 4.5:1.')
  }
  return { palette, errors }
}
const defaultAppearance = () => ({ version: 1, mode: 'desktop', light: { ...PRESETS.Daybook.light }, dark: { ...PRESETS.Daybook.dark } })
function normalizeAppearance(value) {
  if (!value || value.version !== 1) return defaultAppearance()
  const light = inspectPalette(migrateToday(value.light)), dark = inspectPalette(migrateToday(value.dark))
  return { version: 1, mode: ['desktop', 'system', 'light', 'dark'].includes(value.mode) ? value.mode : 'desktop', light: light.errors.length ? { ...PRESETS.Daybook.light } : light.palette, dark: dark.errors.length ? { ...PRESETS.Daybook.dark } : dark.palette }
}
function appearanceStore(ctx) {
  const listeners = new Set()
  const read = () => {
    try {
      return normalizeAppearance(ctx.storage.get(APPEARANCE_KEY, null))
    } catch (_) { return defaultAppearance() }
  }
  let current = read()
  return {
    read: () => current,
    subscribe: fn => { listeners.add(fn); return () => listeners.delete(fn) },
    dispose: () => listeners.clear(),
    save: patch => {
      try {
        if (!patch || typeof patch !== 'object' || Array.isArray(patch)) return false
        const keys = Reflect.ownKeys(patch)
        if (!keys.length || keys.some(key => !['mode', 'light', 'dark', 'version'].includes(key))) return false
        const candidate = { ...current, ...patch }
        if (candidate.version !== 1 || !['desktop', 'system', 'light', 'dark'].includes(candidate.mode)) return false
        const light = inspectPalette(candidate.light), dark = inspectPalette(candidate.dark)
        if (!candidate.light || typeof candidate.light !== 'object' || Array.isArray(candidate.light) ||
            !candidate.dark || typeof candidate.dark !== 'object' || Array.isArray(candidate.dark) ||
            light.errors.length || dark.errors.length) return false
        // Repair is load-only; writes use the checked canonical fields, never defaults.
        const next = { version: 1, mode: candidate.mode, light: light.palette, dark: dark.palette }
        ctx.storage.set(APPEARANCE_KEY, next)
        // SDK writes are best-effort. Never publish a theme until a complete,
        // valid canonical value can be read back (not a normalized fallback).
        const stored = ctx.storage.get(APPEARANCE_KEY, null)
        if (!stored || stored.version !== 1 || stored.mode !== next.mode ||
            inspectPalette(stored.light).errors.length || inspectPalette(stored.dark).errors.length ||
            JSON.stringify(normalizeAppearance(stored)) !== JSON.stringify(next)) return false
        current = next
        listeners.forEach(fn => fn(next))
        return true
      } catch (_) { return false }
    }
  }
}
function paletteStyle(p) {
  // Called only with fully validated palettes, including preview fallback.
  return {
    '--kal-bg': p.background, '--kal-panel': p.panel, '--kal-text': p.text, '--kal-accent': p.accent, '--kal-selected': p.selected,
    '--kal-today-bg': p.todayBackground, '--kal-today-text': p.todayText,
    '--kal-border': `color-mix(in srgb, ${p.text} 22%, ${p.background})`,
    '--ui-bg-primary': p.background, '--ui-bg-secondary': p.panel, '--ui-bg-tertiary': p.panel,
    '--ui-text-primary': p.text, '--ui-text-secondary': p.text, '--ui-text-tertiary': p.text,
    '--ui-stroke-primary': p.text, '--ui-stroke-secondary': `color-mix(in srgb, ${p.text} 22%, ${p.background})`
  }
}
const APPEARANCE_CSS = `
.caldav-page .kal-mode,.caldav-settings .kal-mode {min-width:0}
:is(.caldav-page,.caldav-settings) .kal-mode-buttons {display:flex; flex-wrap:wrap; gap:5px}
:is(.caldav-page,.caldav-settings) .kal-mode-buttons button {display:inline-flex; align-items:center; justify-content:center; min-width:32px; min-height:32px; font:inherit; padding:7px; border:1px solid var(--ui-stroke-secondary); border-radius:7px; color:inherit; background:transparent; cursor:pointer}
:is(.caldav-page,.caldav-settings) .kal-mode-buttons button[aria-pressed=true] {background:var(--kal-selected,var(--ui-bg-secondary)); border-color:var(--kal-accent,var(--ui-text-primary)); font-weight:600}
:is(.caldav-page,.caldav-settings) .kal-mode-select {display:none}
.caldav-settings button[data-kal-action=primary] {font-weight:700; padding:10px 18px; background:var(--kal-accent,#27674e); color:var(--kal-bg,#f8faf7)}
.caldav-settings .kal-library {margin:12px 0; padding:12px; border:1px solid var(--ui-stroke-secondary); border-radius:8px}
.caldav-settings .kal-today-sample {display:inline-block; padding:2px 4px; background:var(--kal-today-bg); color:var(--kal-today-text); font-size:11px}
.caldav-settings {container:kal-settings / inline-size; margin:0 auto; background:var(--ui-bg-primary); line-height:1.6}
.caldav-settings h1 {font-size:36px; line-height:1.2; letter-spacing:-.035em; font-weight:600; margin:20px 0 12px}
.caldav-settings h2,.caldav-settings h3 {font-size:18px; font-weight:600; margin:0 0 12px}
.caldav-settings > section {border:0; border-top:1px solid var(--ui-stroke-secondary); border-radius:0; padding:26px 0}
.caldav-settings .caldav-connection {border:0; padding:0; background:transparent}
.caldav-settings .kal-row {display:flex; align-items:center; flex-wrap:wrap; gap:12px; margin:14px 0}
.caldav-settings .kal-row > strong {min-width:120px}
.caldav-settings button,.caldav-settings select,.caldav-settings input[type=text] {font:inherit; border-radius:5px; padding:6px 12px}
.caldav-settings button:not([data-kal-action]),.caldav-settings select,.caldav-settings input[type=text] {color:var(--ui-text-primary); background:var(--ui-bg-secondary); border:1px solid var(--ui-text-primary)}
.caldav-settings .kal-settings-heading .kal-row {margin:0 0 14px; padding-bottom:16px; border-bottom:1px solid var(--kal-border,var(--ui-stroke-secondary)); font-size:13px}
.caldav-settings .kal-settings-heading .kal-row > strong {min-width:0; text-transform:uppercase; color:var(--kal-accent,var(--ui-text-primary))}
.caldav-settings .kal-back {display:inline-flex; align-items:center; gap:6px; color:var(--kal-accent,var(--ui-text-primary)); font-weight:600; text-underline-offset:3px}
.caldav-settings .kal-back::before {content:''; box-sizing:border-box; width:6px; height:6px; border-left:1.5px solid; border-bottom:1.5px solid; transform:rotate(45deg); margin-inline:3px}
.caldav-settings .kal-settings-heading h1 {margin:12px 0 0}
.caldav-settings .kal-account {display:grid; grid-template-columns:minmax(0,1fr) minmax(220px,30%); gap:6px 32px; padding:16px 0 12px; font-size:13px}
.caldav-settings .kal-account :is(h2,p) {margin:0}
.caldav-settings .kal-account h2 {font-size:16px}
.caldav-settings .kal-account .caldav-connection {grid-column:2; grid-row:1 / span 3; flex-direction:column; align-items:flex-end; text-align:right; margin-top:0}
.caldav-settings .kal-account > p:nth-last-child(-n+2) {grid-column:1 / -1; margin-top:2px}
.caldav-settings button:not(:disabled),.caldav-settings summary {cursor:pointer}
.caldav-settings button:disabled {opacity:.55; cursor:not-allowed}
.caldav-settings button:hover:not(:disabled) {text-decoration:underline}
.caldav-settings button[aria-pressed=true] {outline:2px solid var(--ui-text-primary); outline-offset:2px}
.caldav-settings :is(a,button,input,select,summary,[tabindex]):focus-visible,.caldav-shell[data-custom=true] :is(button,input,summary,[tabindex]):focus-visible {outline:3px solid var(--ui-text-primary); outline-offset:3px}
.caldav-settings .kal-appearance {padding:16px 0 24px; --kal-rule:var(--kal-border,var(--ui-stroke-secondary)); --kal-control:var(--kal-accent,var(--ui-text-primary)); --kal-edge:var(--ui-text-primary)}
.caldav-settings .kal-appearance :is(h2,h3,p) {margin:0}
.caldav-settings .kal-appearance :is(.kal-mode-row,.kal-palette) > :is(div,header) > p {margin-top:3px; color:var(--ui-text-secondary)}
.caldav-settings .kal-mode-row {display:flex; justify-content:space-between; align-items:flex-start; flex-wrap:wrap; gap:10px 20px; padding-bottom:8px; border-bottom:1px solid var(--kal-rule)}
.caldav-settings .kal-appearance h2 {font-size:16px}
.caldav-settings .kal-appearance .kal-mode-buttons {display:inline-flex; flex-wrap:nowrap; gap:2px; padding:2px; border:1px solid var(--kal-rule); border-radius:6px; background:var(--ui-bg-secondary)}
.caldav-settings .kal-appearance .kal-mode-buttons button {position:relative; width:38px; min-height:32px; padding:0; border:1px solid transparent; border-radius:4px; background:transparent}
.caldav-settings .kal-appearance .kal-mode-buttons button:not(:first-child)::before {content:''; position:absolute; left:-3px; height:16px; border-left:1px solid var(--kal-rule)}
.caldav-settings .kal-appearance :is(.kal-mode-buttons,.kal-presets) button[aria-pressed=true] {border-color:var(--kal-edge); background:var(--kal-selected,var(--ui-bg-secondary)); outline:none}
.caldav-settings .kal-appearance :is(.kal-mode-buttons,.kal-presets) button:focus-visible {outline:2px solid var(--ui-text-primary); outline-offset:2px; z-index:1}
.caldav-settings .kal-palette {position:relative; padding-top:12px}
.caldav-settings .kal-palette > header {padding-right:166px; min-height:40px}
.caldav-settings .kal-maker > summary {position:absolute; right:0; top:12px; display:flex; justify-content:space-between; align-items:center; gap:20px; min-width:150px; box-sizing:border-box; padding:7px 10px; border:1px solid var(--kal-rule); border-radius:5px; background:var(--ui-bg-secondary); font-size:13px; font-weight:600; list-style:none}
.caldav-settings .kal-maker > summary::-webkit-details-marker {display:none}
.caldav-settings .kal-maker:not([open]) > summary svg {transform:rotate(-90deg)}
.caldav-settings .kal-maker-panel {margin-top:16px; padding:14px 21px 10px; background:var(--ui-bg-secondary); border:1px solid var(--kal-rule); border-radius:8px}
.caldav-settings .kal-maker-heading {display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:8px 18px; margin-bottom:8px}
.caldav-settings .kal-maker h3 {font-size:20px; line-height:1.3}
.caldav-settings .kal-maker-mode {display:flex; flex-wrap:wrap; gap:6px 18px; align-items:center; font-size:13px}
.caldav-settings .kal-maker-mode select {font-weight:600; max-width:100%; padding:5px 8px; border-color:var(--kal-edge); background:var(--ui-bg-primary)}
.caldav-settings .kal-editor-layout {display:grid; grid-template-columns:minmax(0, 41fr) minmax(0, 54fr); gap:clamp(16px,4.7cqi,52px); align-items:start}
.caldav-settings .kal-editor-label {font-size:13px; display:block; margin-bottom:4px}
.caldav-settings .kal-presets {display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:8px 10px; margin-bottom:14px; max-width:460px}
.caldav-settings .kal-presets button {display:flex; align-items:center; gap:5px; text-align:left; min-width:0; min-height:36px; padding:5px 7px; border-color:var(--kal-rule); background:var(--ui-bg-primary); font-size:13px}
.caldav-settings .kal-presets button[aria-pressed=true] {font-weight:600}
.caldav-settings .kal-preset-dots {display:inline-flex; flex:none; align-items:center}
.caldav-settings .kal-preset-dots > span {width:9px; height:9px; border-radius:50%; border:1px solid color-mix(in srgb,currentColor 25%,transparent)}
.caldav-settings .kal-preset-check {color:var(--kal-edge); display:flex; flex:none; margin-left:auto}
.caldav-settings .kal-preset-check svg {width:12px;height:12px}
.caldav-settings .kal-field {display:grid; grid-template-columns:minmax(0,1fr) 34px minmax(85px,45%); align-items:center; gap:8px; margin:7px 0; font-size:13px}
.caldav-settings .kal-field input {min-width:0; width:100%; box-sizing:border-box; height:32px; border:1px solid var(--kal-edge); border-radius:5px; background:var(--ui-bg-primary); color:inherit}
.caldav-settings .kal-field input[type=color] {padding:2px 4px; cursor:pointer}
.caldav-settings .kal-field input[type=text] {padding:4px 10px; font-variant-numeric:tabular-nums; border-color:var(--kal-edge); background:var(--ui-bg-primary)}
.caldav-settings .kal-preset-tools {margin-top:10px}
.caldav-settings .kal-preset-tools button {font-size:13px; border-color:var(--kal-rule); overflow-wrap:anywhere}
.caldav-settings .kal-preset-tools :is(label,input[type=text]) {max-width:100%; box-sizing:border-box}
.caldav-settings .kal-sample {padding:12px 14px 8px; border:1px solid var(--kal-border); color:var(--kal-text); background:var(--kal-bg); border-radius:6px; font-size:12px}
.caldav-settings .kal-sample-heading {display:flex; flex-wrap:wrap; justify-content:space-between; align-items:center; gap:2px 8px; margin-bottom:8px}
.caldav-settings .kal-sample-layout {display:grid; grid-template-columns:minmax(0,1fr) minmax(0,1.1fr); gap:12px 18px}
.caldav-settings .kal-sample-grid {display:grid; grid-template-columns:repeat(7,minmax(0,1fr)); gap:5px 3px; text-align:center}
.caldav-settings .kal-sample-grid span {border-radius:4px; padding:3px 0}
.caldav-settings .kal-sample-grid .selected {background:var(--kal-selected); outline:1px solid var(--kal-text); outline-offset:-1px}
.caldav-settings .kal-sample-grid .kal-today-sample {padding:3px 0}
.caldav-settings .kal-sample-agenda {padding:9px 12px; background:var(--kal-panel); border-radius:5px; overflow-wrap:anywhere}
.caldav-settings .kal-sample-agenda .kal-sample-event {border-top:1px solid var(--kal-border); padding:6px 0; margin-top:6px}
.caldav-settings .kal-sample-agenda strong {display:block}
.caldav-settings .kal-sample-chip {display:flex; align-items:center; gap:5px; background:var(--kal-selected); font-weight:600; border-radius:4px; padding:4px 7px; margin-top:10px}
.caldav-settings .kal-category-dot {flex:none; width:9px; height:9px; border-radius:50%; background:#ADC7FF; border:1px solid #516595}
.caldav-settings .kal-sample-status {display:flex; flex-wrap:wrap; gap:4px 14px; margin-top:8px}
.caldav-settings .kal-sample-status > span {display:inline-flex; align-items:center; gap:4px}
.caldav-settings .kal-sample-status > span:first-child {color:var(--kal-accent)}
.caldav-settings .kal-sample-status svg {width:14px;height:14px}
.caldav-settings .kal-contrast {font-size:12px; margin-top:12px}
.caldav-settings .kal-contrast p + p {margin-top:3px}
.caldav-settings .kal-contrast-ok {display:flex; align-items:center; gap:5px; font-weight:600}
.caldav-settings .kal-contrast ul {padding-inline-start:20px; margin:4px 0 0}
.caldav-settings .kal-apply-footer {display:flex; flex-wrap:wrap; justify-content:space-between; align-items:center; gap:8px 16px; margin-top:10px; padding-top:10px; border-top:1px solid var(--kal-rule); font-size:13px}
.caldav-settings .kal-apply-footer > div {display:flex; flex-wrap:wrap; align-items:center; gap:8px 12px}
.caldav-settings .kal-apply-footer button {border:1px solid var(--kal-rule); padding:6px 10px; font-weight:600}
.caldav-settings .kal-apply-footer button[data-kal-action=primary] {background:var(--kal-control); color:var(--kal-bg,var(--ui-bg-primary)); padding-inline:12px}
.caldav-settings .kal-appearance-feedback {display:flex; flex-wrap:wrap; align-items:center; gap:8px 12px}
.caldav-settings .kal-appearance-feedback:has(> p:not(:empty)) {margin-top:10px}
.caldav-settings .caldav-bulk {display:flex; flex-direction:column; align-items:flex-start; gap:6px}
.caldav-shell {height:100%; width:100%; min-width:0}
.caldav-shell[data-custom=true] {color:var(--kal-text); background:var(--kal-bg)}
.caldav-settings[data-custom=true] button[data-kal-action=primary] {color:var(--kal-bg); background:var(--kal-accent); border:1px solid var(--kal-text)}
.caldav-settings button[data-kal-action=destructive] {color:var(--ui-text-primary); background:var(--ui-bg-secondary); border:2px dashed var(--ui-text-primary); text-decoration:underline}
.caldav-shell[data-custom=true] button *,.caldav-settings[data-custom=true] button * {color:inherit}
.caldav-shell[data-custom=true] :is([role=alert],[role=status]) *,.caldav-settings[data-custom=true] [role=alert] * {color:inherit}
.caldav-shell[data-custom=true] [role=alert],.caldav-settings[data-custom=true] [role=alert] {color:#202020; background:#F4F4F4; border:1px solid #555; padding:12px}
.caldav-shell[data-custom=true] input,.caldav-settings[data-custom=true] input {accent-color:var(--kal-accent)}
.caldav-shell[data-custom=true] button:hover:not(:disabled) {text-decoration:underline}
.caldav-shell[data-custom=true] [role=checkbox] {color:var(--kal-text); background:var(--kal-panel); border:1px solid var(--kal-text)}
.caldav-shell[data-custom=true] [role=status] {color:var(--kal-text)}
@container kal-settings (755px <= width <= 940px) {.caldav-settings .kal-presets {grid-template-columns:repeat(2,minmax(0,1fr))}}
@container kal-settings (max-width:754px) {.caldav-settings .kal-editor-layout {grid-template-columns:minmax(0,1fr); gap:16px}}
@container kal-settings (max-width:600px) {.caldav-settings .kal-account {grid-template-columns:1fr} .caldav-settings .kal-account .caldav-connection {grid-column:auto; grid-row:auto; align-items:flex-start; text-align:left; margin:4px 0} .caldav-settings h1 {font-size:30px} .caldav-settings .kal-presets {grid-template-columns:repeat(2,minmax(0,1fr))} .caldav-settings .kal-maker-panel {padding:12px} .caldav-settings .kal-palette > header {padding-right:0; min-height:0} .caldav-settings .kal-maker > summary {position:static; width:max-content; max-width:100%; margin-top:8px}}
@container kal-settings (max-width:440px) {.caldav-settings .kal-sample-layout {grid-template-columns:minmax(0,1fr)}}
`
const MODES = [['desktop', 'Match Desktop'], ['system', 'System'], ['light', 'Light'], ['dark', 'Dark']]
function ToolbarIcon({ name }) {
  const d = { calendar: 'M4 5h16v16H4z M8 3v4 M16 3v4 M4 10h16 M8 14h2 M14 14h2', previous: 'M14 6l-6 6 6 6', next: 'M10 6l6 6-6 6', down: 'M6 9l6 6 6-6', check: 'M4 12l5 5L20 6', eye: 'M2 12s4-6 10-6 10 6 10 6-4 6-10 6-10-6-10-6z M12 10a2 2 0 1 0 0 4 2 2 0 0 0 0-4z', trash: 'M4 6h16 M9 6V3h6v3 M6 6l1 15h10l1-15 M10 9v8 M14 9v8', refresh: 'M21 5v6h-6 M20.5 11a8 8 0 1 0-1.8 6.7' }[name]
  return jsx('svg', { width: 16, height: 16, viewBox: '0 0 24 24', fill: 'none', stroke: 'currentColor', strokeWidth: 1.7, strokeLinecap: 'round', strokeLinejoin: 'round', 'aria-hidden': true, focusable: false, children: jsx('path', { d }) })
}
function ModeSelector({ appearance = defaultAppearance(), onChange }) {
  return jsxs('div', { className: 'kal-mode', children: [
    jsx('div', { className: 'kal-mode-buttons', role: 'group', 'aria-label': 'Appearance mode', children: MODES.map(([value, label]) => jsxs('button', {
      type: 'button', 'aria-label': label, title: label, 'aria-pressed': appearance.mode === value, onClick: () => onChange(value),
      children: [jsx('svg', { width: 16, height: 16, viewBox: '0 0 24 24', fill: 'none', stroke: 'currentColor', strokeWidth: 1.6, 'aria-hidden': true, children:
        jsx('path', { d: { desktop: 'M3 4h18v13H3z M8 21h8 M12 17v4', system: 'M12 3a9 9 0 1 0 0 18V3z M12 3a9 9 0 0 1 0 18', light: 'M12 8a4 4 0 1 0 0 8 4 4 0 0 0 0-8 M12 1v3 M12 20v3 M1 12h3 M20 12h3 M4 4l2 2 M18 18l2 2 M4 20l2-2 M18 6l2-2', dark: 'M20 15A9 9 0 0 1 9 4a9 9 0 1 0 11 11z' }[value] }) })]
    }, value)) }),
  ] })
}
function AppearanceFrame({ store, settings, ctx, lifecycle }) {
  const [appearance, setAppearance] = useState(() => store?.read() || defaultAppearance())
  const [dark, setDark] = useState(false)
  useEffect(() => {
    const unsubscribe = store?.subscribe(setAppearance) || (() => {})
    if (store) setAppearance(store.read())
    let media
    try { media = globalThis.matchMedia?.('(prefers-color-scheme: dark)') } catch (_) {}
    const update = () => setDark(media?.matches === true)
    update()
    media?.addEventListener?.('change', update)
    const dispose = () => { unsubscribe(); media?.removeEventListener?.('change', update); lifecycle?.delete(dispose) }
    dispose.clear = () => {}
    lifecycle?.add(dispose)
    return dispose
  }, [store])
  const mode = appearance.mode === 'system' ? dark ? 'dark' : 'light' : appearance.mode
  const style = mode === 'desktop' ? undefined : { ...paletteStyle(appearance[mode]), colorScheme: mode }
  // Calendar's existing root is caldav-page; shell adds only scoped appearance.
  return jsxs('div', { className: settings ? 'caldav-settings' : 'caldav-shell', 'data-custom': mode !== 'desktop', style, children: [
    jsx('style', { children: APPEARANCE_CSS + SPECIAL_CSS }),
    settings ? jsx(SettingsPage, { ctx, lifecycle, appearance, store, activeMode: mode }) : jsx(CalendarPage, { ctx, lifecycle, appearance, store })
  ] })
}
const PRESET_KEY = 'kalends.presets.v1'
// Keep the original read restrictions: later built-ins may already be custom names.
const presetName = value => typeof value === 'string' && /^[\p{L}\p{N}][\p{L}\p{N} _.-]{0,39}$/u.test(value) && !['daybook', 'dusk', 'coast'].includes(value.toLowerCase())
function readLibrary(ctx) {
  const raw = ctx.storage.get(PRESET_KEY, null)
  if (raw === null) return { raw, entries: [] }
  if (!raw || raw.version !== 1 || !Number.isSafeInteger(raw.revision) || raw.revision < 0 || !Array.isArray(raw.entries) || raw.entries.length > 24) throw new Error('Invalid preset library.')
  const names = new Set()
  const entries = raw.entries.map(entry => {
    if (!entry || !presetName(entry.name) || !['light', 'dark'].includes(entry.mode) || names.has(entry.name.toLowerCase())) throw new Error('Invalid preset library.')
    names.add(entry.name.toLowerCase())
    const checked = inspectPalette(entry.palette)
    if (checked.errors.length) throw new Error('Invalid preset colors.')
    return { name: entry.name, mode: entry.mode, palette: checked.palette }
  })
  return { raw, entries }
}
function PresetManager({ ctx, mode, draft, load }) {
  const [open, setOpen] = useState(false)
  const [library, setLibrary] = useState(() => { try { return readLibrary(ctx) } catch (_) { return null } })
  const [message, setMessage] = useState('')
  const [form, setForm] = useState(null)
  const [undo, setUndo] = useState(null)
  useEffect(() => { setForm(null) }, [mode])
  const write = (entries, undoing = false) => {
    try {
      const latest = readLibrary(ctx)
      if (!library || JSON.stringify(latest.raw) !== JSON.stringify(library.raw)) {
        setLibrary(latest); setUndo(null)
        throw new Error('Presets changed elsewhere. Review the library and try again.')
      }
      const next = { version: 1, revision: (latest.raw?.revision || 0) + 1, entries }
      ctx.storage.set(PRESET_KEY, next)
      const checked = readLibrary(ctx)
      if (JSON.stringify(checked.raw) !== JSON.stringify(next)) throw new Error('Preset storage did not confirm the write.')
      setLibrary(checked)
      setUndo(undoing ? null : latest.entries)
      setForm(null); setMessage(undoing ? 'Preset change undone.' : 'Preset library saved.')
    } catch (error) { setMessage(error.message || 'Preset storage is unavailable.') }
  }
  const savePreset = () => {
    const checked = inspectPalette(draft), name = form?.name.trim()
    const unchangedName = name === form?.original && library?.entries.some(entry => entry.name === form.original)
    if (!library || !presetName(name) || (!unchangedName && Object.keys(PRESETS).some(builtin => builtin.toLowerCase() === name.toLowerCase())) || checked.errors.length || library.entries.some(entry => entry.name !== form.original && entry.name.toLowerCase() === name.toLowerCase()) || (!form.original && library.entries.length >= 24)) {
      setMessage('Use a unique plain name (1–40 characters), not a built-in name, and valid colors. Maximum 24 presets.'); return
    }
    const entry = { name, mode, palette: checked.palette }
    write(form.original ? library.entries.map(old => old.name === form.original ? entry : old) : [...library.entries, entry])
  }
  return jsxs('div', { children: [
    jsx('button', { type: 'button', 'aria-expanded': open, 'aria-controls': 'kal-preset-library', onClick: () => setOpen(!open), children: 'Manage presets' }),
    open && jsxs('section', { id: 'kal-preset-library', className: 'kal-library', 'aria-label': 'Manage presets', children: [
      jsx('p', { children: `${mode === 'light' ? 'Light' : 'Dark'} presets · local to this device` }),
      !library && jsx('p', { role: 'alert', children: 'Preset library is unavailable or malformed. Existing storage has not been changed.' }),
      jsx('button', { type: 'button', disabled: !library, onClick: () => setForm({ name: '', original: null }), children: 'New preset' }),
      ...(library?.entries || []).filter(entry => entry.mode === mode).map(entry => jsxs('div', { className: 'kal-row', children: [
        jsx('span', { children: entry.name }),
        jsx('button', { type: 'button', onClick: () => { load({ ...entry.palette }); setForm(null); setMessage(`${entry.name} loaded into draft only.`) }, children: `Load ${entry.name}` }),
        jsx('button', { type: 'button', onClick: () => { load({ ...entry.palette }); setForm({ name: entry.name, original: entry.name }); setMessage('Edit the palette below, then Save preset.') }, children: `Edit ${entry.name}` }),
        jsx('button', { type: 'button', onClick: () => write(library.entries.filter(old => old.name !== entry.name)), children: `Delete ${entry.name}` })
      ] }, entry.name)),
      form && jsxs('div', { className: 'kal-row', children: [
        jsxs('label', { children: ['Preset name', jsx('input', { type: 'text', 'aria-label': 'Preset name', maxLength: 40, value: form.name, onChange: event => setForm({ ...form, name: event.target.value }) })] }),
        jsx('button', { type: 'button', onClick: savePreset, children: 'Save preset' }),
        jsx('button', { type: 'button', onClick: () => setForm(null), children: 'Cancel' })
      ] }),
      undo && jsx('button', { type: 'button', onClick: () => write(undo, true), children: 'Undo' }),
      message && jsx('p', { role: 'status', children: message })
    ] })
  ] })
}
function PalettePreview({ preview, errors }) {
  return jsxs('div', { className: 'kal-preview-column', children: [
    jsx('strong', { className: 'kal-editor-label', children: 'Calendar preview' }),
    jsxs('section', { className: 'kal-sample', style: paletteStyle(preview), 'aria-label': 'Sample calendar and agenda preview', children: [
      jsxs('header', { className: 'kal-sample-heading', children: [jsx('h3', { children: 'November 2026' }), jsx('span', { children: 'Sample data' })] }),
      jsxs('div', { className: 'kal-sample-layout', children: [
        jsxs('div', { children: [
          jsxs('div', { className: 'kal-sample-grid', children: [
            ...['S', 'M', 'T', 'W', 'T', 'F', 'S'].map((day, i) => jsx('strong', { 'aria-hidden': true, children: day }, `weekday-${i}`)),
            ...Array.from({ length: 14 }, (_, i) => i + 8).map(day => jsx('span', { className: day === 12 ? 'selected' : day === 10 ? 'kal-today-sample' : undefined, 'aria-label': day === 12 ? '12, selected day' : day === 10 ? '10, Today' : undefined, children: day }, day))
          ] }),
          jsxs('div', { className: 'kal-sample-chip', children: [jsx('span', { className: 'kal-category-dot', 'aria-hidden': true }), '09:30 · Project check-in'] })
        ] }),
        jsxs('div', { className: 'kal-sample-agenda', children: [
          jsx('strong', { children: 'Thursday, Nov 12' }),
          jsx('p', { children: '67° / 54° · Rain' }),
          jsxs('div', { className: 'kal-sample-event', children: [jsx('time', { children: '09:30' }), jsx('strong', { children: 'Project check-in' })] })
        ] })
      ] }),
      jsxs('div', { className: 'kal-sample-status', children: [jsxs('span', { children: [jsx(ToolbarIcon, { name: 'eye' }), 'Read on'] }), jsxs('span', { children: [jsx(ToolbarIcon, { name: 'trash' }), 'Delete off'] })] })
    ] }),
    jsxs('div', { className: 'kal-contrast', children: [
      errors.length > 0 ? jsxs('div', { role: 'alert', children: [jsx('p', { children: 'Not ready to apply. Preview uses the saved palette.' }), jsx('ul', { children: errors.map(error => jsx('li', { children: error }, error)) })] })
        : jsxs('p', { className: 'kal-contrast-ok', children: [jsx(ToolbarIcon, { name: 'check' }), 'Preview text passes contrast checks'] }),
      jsx('p', { children: 'Calendar colors and status indicators stay the same.' })
    ] })
  ] })
}
function AppearanceSettings({ appearance = defaultAppearance(), store, ctx, activeMode }) {
  const [mode, setMode] = useState('light')
  const [drafts, setDrafts] = useState(() => ({ light: { ...appearance.light }, dark: { ...appearance.dark } }))
  const [presets, setPresets] = useState({ light: 'Daybook', dark: 'Daybook' })
  const [message, setMessage] = useState('')
  const [applied, setApplied] = useState(null)
  const inspected = inspectPalette(drafts[mode])
  const preview = inspected.errors.length ? appearance[mode] : inspected.palette
  const label = mode === 'light' ? 'Light' : 'Dark'
  const change = (key, value) => setDrafts(current => ({ ...current, [mode]: { ...current[mode], [key]: value } }))
  const save = (patch, success = 'Appearance saved on this device.') => {
    const ok = store?.save(patch) === true
    setMessage(ok ? success : 'Local storage is unavailable. Appearance was not saved.')
    return ok
  }
  const active = activeMode === mode
  const apply = () => {
    const checked = inspectPalette(drafts[mode])
    if (!checked.errors.length && save({ [mode]: checked.palette }, active
      ? `${label} palette saved and applied to Kalends.`
      : `${label} palette saved. Your current appearance hasn’t changed.`)) setApplied(mode)
  }
  return jsxs('section', { className: 'kal-appearance', children: [
    jsxs('div', { className: 'kal-mode-row', children: [
      jsxs('div', { children: [jsx('h2', { children: 'Appearance' }), jsx('p', { children: 'Match Desktop follows Hermes. System follows your device.' })] }),
      jsx(ModeSelector, { appearance, onChange: mode => save({ mode }) })
    ] }),
    jsxs('div', { className: 'kal-palette', children: [
      jsxs('header', { children: [jsx('h2', { children: 'Color palette' }), jsx('p', { children: 'Pick a preset or make your own. Only Kalends changes.' })] }),
      jsxs('details', { className: 'kal-maker', children: [
        jsxs('summary', { children: ['Theme Maker', jsx(ToolbarIcon, { name: 'down' })] }),
        jsxs('div', { className: 'kal-maker-panel', children: [
          jsxs('header', { className: 'kal-maker-heading', children: [
            jsx('h3', { children: 'Theme Maker' }),
            jsxs('label', { className: 'kal-maker-mode', children: ['Editing palette for', jsx('select', { 'aria-label': 'Edit palette', value: mode, onChange: e => setMode(e.target.value), children: [jsx('option', { value: 'light', children: 'Light appearance' }), jsx('option', { value: 'dark', children: 'Dark appearance' })] })] })
          ] }),
          jsxs('div', { className: 'kal-editor-layout', children: [
            jsxs('div', { className: 'kal-editor-fields', children: [
              jsx('strong', { className: 'kal-editor-label', children: 'Start from a preset' }),
              jsx('div', { className: 'kal-presets', children: Object.entries(PRESETS).map(([name, pair]) => {
                const palette = pair[mode]
                const chosen = Object.keys(PALETTE_FIELDS).every(key => hexColor(drafts[mode][key]) === hexColor(palette[key]))
                return jsxs('button', { type: 'button', 'aria-pressed': chosen, onClick: () => {
                  setPresets(current => ({ ...current, [mode]: name }))
                  setDrafts(current => ({ ...current, [mode]: { ...palette } }))
                }, children: [
                  jsx('span', { className: 'kal-preset-dots', 'aria-hidden': true, children: ['panel', 'accent', 'selected'].map(key => jsx('span', { style: { backgroundColor: palette[key] } }, key)) }),
                  jsx('span', { children: name }),
                  chosen && jsx('span', { className: 'kal-preset-check', children: jsx(ToolbarIcon, { name: 'check' }) })
                ] }, name)
              }) }),
              ...Object.entries(PALETTE_FIELDS).map(([key, text]) => jsxs('div', { className: 'kal-field', children: [
                jsx('span', { children: text }),
                jsx('input', { type: 'color', 'aria-label': `${label} ${text} swatch`, value: hexColor(drafts[mode][key]) || preview[key], onChange: e => change(key, e.target.value) }),
                jsx('input', { type: 'text', 'aria-label': `${label} ${text} hex`, value: drafts[mode][key], spellCheck: false, maxLength: 7, 'aria-invalid': !hexColor(drafts[mode][key]), onChange: e => change(key, e.target.value) })
              ] }, key)),
              jsx('div', { className: 'kal-preset-tools', children: jsx(PresetManager, { ctx, mode, draft: drafts[mode], load: palette => setDrafts(current => ({ ...current, [mode]: palette })) }) })
            ] }),
            jsx(PalettePreview, { preview, errors: inspected.errors })
          ] }),
          jsxs('footer', { className: 'kal-apply-footer', children: [
            jsx('p', { children: active ? 'Applies to Kalends now. Calendar access won’t change.' : `Saves for ${label} without switching appearance. Calendar access won’t change.` }),
            jsxs('div', { children: [
              jsx('button', { type: 'button', onClick: () => setDrafts(current => ({ ...current, [mode]: { ...PRESETS[presets[mode]][mode] } })), children: 'Reset to preset' }),
              jsx('button', { type: 'button', 'data-kal-action': 'primary', disabled: inspected.errors.length > 0, onClick: apply, children: `Apply to ${label}` })
            ] })
          ] })
        ] })
      ] })
    ] }),
    jsxs('div', { className: 'kal-appearance-feedback', children: [
      jsx('p', { role: 'status', children: message || null }),
      applied && appearance.mode !== applied &&
        jsx('button', { type: 'button', onClick: () => { if (save({ mode: applied })) setApplied(null) }, children: `Use ${applied === 'light' ? 'Light' : 'Dark'}` })
    ] })
  ] })
}
const ACCESS_CSS = `
.caldav-settings {box-sizing:border-box; width:100%; max-width:1100px; min-width:0; padding:24px clamp(18px,5.1%,56px); display:flex; flex-direction:column; gap:12px; color:var(--ui-text-primary)}
.caldav-settings .caldav-settings {padding:0}
.caldav-settings > section {min-width:0; overflow-wrap:anywhere}
.caldav-settings button {max-width:100%; white-space:normal; height:auto; min-height:32px}
.caldav-access-scroll {max-width:100%; overflow-x:auto}
.caldav-connection {display:flex; flex-wrap:wrap; align-items:center; gap:8px; padding:8px 12px; border:1px solid var(--ui-stroke-secondary); border-radius:8px; background:var(--ui-bg-secondary)}
.caldav-access {width:100%; min-width:540px; border-collapse:collapse; font-size:13px}
.caldav-access th,.caldav-access td {text-align:left; padding:6px 8px; border-bottom:1px solid var(--ui-stroke-secondary); vertical-align:top}
.caldav-access thead th {font-weight:600}
.caldav-bulk {display:inline-flex; align-items:center; gap:4px; font-weight:400}
.caldav-details summary {cursor:pointer}
.caldav-access-footer {position:sticky; bottom:0; display:flex; flex-wrap:wrap; align-items:center; gap:12px; margin-top:12px; padding:10px 4px; border-top:1px solid var(--ui-stroke-secondary); background:var(--ui-bg-primary)}
.caldav-access-footer p {flex:1 1 auto; margin:0}
.caldav-access button:focus-visible,.caldav-access input:focus-visible,.caldav-connection button:focus-visible {outline:2px solid var(--ui-text-primary); outline-offset:2px}
`

function SettingsPage({ ctx, lifecycle, appearance, store, activeMode }) {
  const [status, setStatus] = useState(null)
  const [calendars, setCalendars] = useState([])
  const [permissions, setPermissions] = useState({})
  const [savedPermissions, setSavedPermissions] = useState({})
  const dirty = JSON.stringify(permissions) !== JSON.stringify(savedPermissions)
  const [loading, setLoading] = useState(true)
  const [working, setWorking] = useState(false)
  const [error, setError] = useState('')
  const mounted = useRef(true)
  const generation = useRef(0)

  const owner = useRef(null)
  const activation = useRef(gatewayActivationEpoch())
  const busy = useRef(false)
  const renderGeneration = generation.current
  const renderOwner = owner.current
  const renderEpoch = gatewayActivationEpoch()

  const clear = () => {
    generation.current += 1
    owner.current = null
    activation.current = gatewayActivationEpoch()
    if (!mounted.current) return
    setStatus(null); setCalendars([]); setPermissions({}); setSavedPermissions({}); setError('')
    setLoading(false); setWorking(false)
  }
  const valid = (call, epoch) => mounted.current && generation.current === call && gatewayActivationEpoch() === epoch && (!host.state?.gateway?.get || host.state.gateway.get() === 'open')
  const check = async (call, epoch, expected, requireConfigured = false) => {
    const next = await ctx.rest('/status', { timeoutMs: 15000 })
    if (!valid(call, epoch)) return null
    if (!next.owner_id || (expected && next.owner_id !== expected)) { clear(); return null }
    if (requireConfigured && !next.connected) {
      setStatus(next)
      setError('Credentials are not configured. Your draft was kept; review account setup or discard local changes.')
      return null
    }
    return next
  }

  const refresh = async () => {
    if (busy.current || !mounted.current) return
    if (!gatewayOpen()) { clear(); return }
    const call = ++generation.current, epoch = gatewayActivationEpoch()
    setLoading(true)
    try {
      const next = await check(call, epoch, null)
      if (!next) return
      owner.current = next.owner_id; activation.current = epoch
      setStatus(next); setCalendars([]); setPermissions(next.permissions || {}); setSavedPermissions(next.permissions || {}); setError('')
    } catch (_) {
      if (valid(call, epoch)) { clear(); setError('Unable to verify backend owner. Refresh before continuing.') }
    } finally { if (valid(call, epoch)) setLoading(false) }
  }

  useEffect(() => {
    mounted.current = true
    void refresh()
    let polling = false
    const poll = async () => {
      if (polling || !mounted.current) return
      if (!gatewayOpen()) { clear(); return }
      if (activation.current !== gatewayActivationEpoch()) clear()
      polling = true
      const call = generation.current, epoch = gatewayActivationEpoch(), expected = owner.current
      try {
        const next = await check(call, epoch, expected)
        if (next && !busy.current) {
          if (!expected) {
            owner.current = next.owner_id; activation.current = epoch
            setStatus(next); setPermissions(next.permissions || {}); setSavedPermissions(next.permissions || {}); setLoading(false)
          } else {
            // Poll updates connection prose only; a same-owner dirty draft is sacred.
            setStatus(current => ({ ...next, network_verified: next.connected && current?.network_verified === true }))
          }
        }
      } catch (_) { if (valid(call, epoch)) clear() }
      finally { polling = false }
    }
    const unsubscribe = []
    for (const key of ['profile', 'connectionId', 'gateway']) {
      const atom = host.state?.[key]
      if (!atom?.subscribe) continue
      let previous = atom.get()
      unsubscribe.push(atom.subscribe(value => { if (value !== previous) { previous = value; clear() } }))
    }
    const timer = setInterval(poll, 1000)
    const dispose = () => { mounted.current = false; generation.current += 1; clearInterval(timer); unsubscribe.forEach(fn => fn()); lifecycle?.delete(dispose) }
    dispose.clear = clear
    lifecycle?.add(dispose)
    return dispose
  }, [ctx])

  const epochChanged = () => renderEpoch !== gatewayActivationEpoch()

  const operate = async (path, method, body, publish) => {
    if (!mounted.current || busy.current) return
    if (activation.current !== gatewayActivationEpoch()) { clear(); return }
    const expected = owner.current
    if (!expected || expected !== renderOwner || generation.current !== renderGeneration || epochChanged()) return
    const call = ++generation.current, epoch = gatewayActivationEpoch()
    busy.current = true; setWorking(true); setError('')
    if (path === '/discover') setStatus(current => ({ ...current, network_verified: false }))
    try {
      if (!await check(call, epoch, expected, true)) return
      if (!valid(call, epoch) || owner.current !== expected) return
      const result = await ctx.rest(path, { method, body: { ...body, owner_id: expected }, timeoutMs: 30000 })
      if (!valid(call, epoch)) return
      if (result.owner_id !== expected) { clear(); return }
      // A settled reply must still belong to the current Settings request scope.
      if (path === '/disconnect' && !result.owner_after) { clear(); return }
      if (!await check(call, epoch, path === '/disconnect' ? result.owner_after : expected, path !== '/disconnect')) return
      if (result.ok === false) throw result
      publish(result)
    } catch (_) {
      if (!valid(call, epoch)) return
      // Rejected promises can also belong to a previous Settings owner.
      try {
        if (await check(call, epoch, expected)) setError('Request failed. Refresh and verify the intended backend before retrying.')
      } catch (_) { if (valid(call, epoch)) clear() }
    } finally {
      busy.current = false
      if (valid(call, epoch)) setWorking(false)
    }
  }

  const discover = () => operate('/discover', 'POST', {}, result => {
    setCalendars(result.calendars || [])
    const next = {}
    for (const calendar of result.calendars || []) next[calendar.id] = calendar.capabilities || offCapabilities()
    setPermissions(next); setSavedPermissions(next)
    setStatus(current => ({ ...current, network_verified: true, migration_notice: result.migration_notice }))
  })
  const save = () => operate('/permissions', 'PUT', { permissions }, result => {
    setPermissions(result.permissions || {}); setSavedPermissions(result.permissions || {})
    setStatus(current => ({ ...current, migration_notice: result.migration_notice }))
    host.notify({ kind: 'success', message: 'Calendar access saved on this Hermes backend profile.' })
  })
  const disconnect = () => operate('/disconnect', 'POST', {}, result => {
    clear()
    host.notify({ kind: result.credential_removed ? 'success' : 'warning', message: result.credential_removed ? 'Calendar access revoked. No credential is currently available.' : 'Calendar access revoked. Clear retained credentials in Agent settings to finish disconnecting.' })
  })

  // --- Draft access state (render only; every mutation still passes operate) ---
  const capsOf = calendar => permissions[calendar.id] || offCapabilities()
  const readableCount = calendars.filter(calendar => capsOf(calendar).read === true).length
  const displayName = calendar => calendarLabel(calendar, calendars)
  // A bulk column is computed across the current draft: eligible = the rows it
  // may change (all rows for Read, readable rows for a write), on = how many
  // already carry it. Zero eligible rows is a disabled, never a vacuous "all".
  const bulk = capability => {
    const eligible = capability === 'read' ? calendars.length : readableCount
    const on = calendars.filter(calendar => (capability === 'read' || capsOf(calendar).read === true) && capsOf(calendar)[capability] === true).length
    return { eligible, on, all: eligible > 0 && on === eligible, mixed: on > 0 && on < eligible }
  }
  const lock = working || loading || !renderOwner || !status?.connected || !gatewayOpen() || epochChanged() || calendars.length === 0
  const bulkLocked = capability => lock || (capability !== 'read' && readableCount === 0)
  const localLock = working || loading || !renderOwner || epochChanged()
  const draftCurrent = () => !localLock && mounted.current && !busy.current && owner.current === renderOwner && generation.current === renderGeneration && !epochChanged()
  const editable = () => !lock && draftCurrent()
  const setRowCaps = (calendarId, capability, checked) => {
    if (!editable()) return
    setPermissions(current => {
      const caps = current[calendarId] || offCapabilities()
      if (capability !== 'read' && !caps.read) return current
      return { ...current, [calendarId]: capability === 'read'
        ? checked ? { ...(caps.read === true ? caps : offCapabilities()), read: true } : offCapabilities()
        : { ...caps, [capability]: checked } }
    })
  }
  const setAllCaps = (capability, checked) => {
    if (!editable()) return
    setPermissions(current => {
      const next = { ...current }
      for (const calendar of calendars) {
        const caps = next[calendar.id] || offCapabilities()
        if (capability === 'read') next[calendar.id] = checked ? { ...(caps.read === true ? caps : offCapabilities()), read: true } : offCapabilities()
        else if (caps.read) next[calendar.id] = { ...caps, [capability]: checked }
      }
      return next
    })
  }
  const discard = () => { if (draftCurrent()) setPermissions(savedPermissions) }
  const connection = connectionState(status)
  const needsSetup = SETUP_STATES.includes(connection)
  const discoverDisabled = working || loading || dirty || !renderOwner || !gatewayOpen() || !status?.connected

  return jsxs('div', { className: 'caldav-settings text-sm', children: [
    jsx('style', { children: APPEARANCE_CSS + ACCESS_CSS }),
    jsxs('header', { className: 'kal-settings-heading', children: [
      jsxs('div', { className: 'kal-row', children: [jsx('strong', { children: 'Kalends' }), jsx('span', { children: 'v0.3.0' })] }),
      jsx('a', { className: 'kal-back', href: '/icloud-caldav', onClick: event => { event.preventDefault(); host.navigate('/icloud-caldav') }, children: 'Back to Calendar' }),
      jsx('h1', { children: 'Kalends Settings' })
    ] }),
    loading && jsx('p', { role: 'status', children: 'Loading account and calendar access…' }),
    jsxs('section', { className: 'kal-account', children: [
      jsx('h2', { children: 'Accounts' }),
      jsx('p', { className: 'text-(--ui-text-secondary)', children: 'iCloud calendars and optional Pirate Weather now. More calendar and weather providers are on the roadmap.' }),
      jsx('p', { className: 'mt-2 text-(--ui-text-secondary)', children: `Endpoint: ${ENDPOINT}` }),
      jsxs('div', { className: 'caldav-connection mt-3', children: [
        jsx('p', { role: 'status', children: connection === 'connected' && !status?.network_verified ? 'iCloud credentials configured. Access has not been verified in this settings session.' : accountMessage(connection, status?.network_verified) }),
        !needsSetup && jsxs(Button, { variant: 'secondary', onClick: () => host.navigate(ACCOUNT_SETUP_HREF), children: [jsx(Codicon, { name: 'gear', 'aria-hidden': true }), 'Update Accounts'] }),
        needsSetup && jsx(Button, { variant: 'primary', onClick: () => host.navigate(ACCOUNT_SETUP_HREF), children: 'Set up iCloud account' })
      ] }),
      jsx('p', { className: 'mt-2 text-(--ui-text-tertiary)', children: 'Your Apple ID and app-specific password are entered on the secured Hermes Agent settings form. Kalends never receives, displays or stores them.' }),
      jsx('p', { className: 'mt-2 text-(--ui-text-tertiary)', children: 'Apple requires two-factor authentication. Generate an app-specific password at account.apple.com → Sign-In and Security → App-Specific Passwords. Never paste it into chat.' }),
    ] }),
    jsx(AppearanceSettings, { appearance, store, ctx, activeMode }),
    jsx(SpecialSettings, { ctx, ownerId: renderOwner,
      current: () => valid(renderGeneration, renderEpoch) && activation.current === renderEpoch && owner.current === renderOwner,
      clear }, JSON.stringify([renderOwner, renderGeneration, renderEpoch])),
    jsxs('section', { children: [
      jsx('h2', { children: 'Calendar access' }),
      jsx('p', { children: 'Choose exactly what Kalends may do with your iCloud calendars.' }),
      jsxs('div', { className: 'mt-3 flex flex-wrap gap-2', children: [
        jsx(Button, { variant: 'secondary', onClick: discover, disabled: discoverDisabled, children: 'Test and discover calendars' }),
        jsx(Button, { variant: 'secondary', onClick: refresh, disabled: working || loading || dirty || !gatewayOpen(), children: 'Refresh status' }),
        jsx(Button, { variant: 'destructive', 'data-kal-action': 'destructive', onClick: disconnect, disabled: working || loading || !renderOwner || !gatewayOpen() || !status?.connected, children: 'Revoke calendar access' })
      ] })
    ] }),
    status?.migration_notice && jsx('p', { role: 'status', children: status.migration_notice }),
    error && jsx(ErrorState, { title: 'Kalends error', description: error }),
    calendars.length === 0 && !discoverDisabled && !error && jsx('p', { children: status?.network_verified ? 'No calendars were found.' : 'Choose Test and discover calendars to check iCloud access.' }),
    calendars.length > 0 && jsxs('section', { children: [
      jsx('p', { className: 'mb-3 text-(--ui-text-secondary)', children: 'Every newly discovered calendar defaults to Off. Enable Read before independently allowing Create, Edit, or Delete. All Read changes every discovered row; All Create, Edit and Delete only change readable rows and never grant Read. Turning Read off also turns all writes off. Changes apply only after Save.' }),
      jsx('p', { children: `All Read: ${calendars.length} eligible calendars. All writes: ${readableCount} readable calendars. Scroll the permission table horizontally on narrow screens.` }),
      jsx('div', { className: 'caldav-access-scroll', role: 'region', tabIndex: 0, 'aria-label': 'Calendar permissions, horizontally scrollable', children: jsxs('table', { className: 'caldav-access', 'aria-label': 'Calendar access by capability', children: [
        jsx('thead', { children: jsxs('tr', { children: [
          jsx('th', { scope: 'col', children: 'Calendar' }),
          ...CAPABILITIES.map(capability => {
            const state = bulk(capability)
            return jsx('th', { scope: 'col', children: jsxs('div', { className: 'caldav-bulk', children: [
              jsx('strong', { children: CAPABILITY_LABELS[capability] }),
              jsx('span', { children: `${state.all ? 'All' : state.mixed ? 'Mixed' : 'Off'} · ${state.on}/${state.eligible} eligible` }),
              jsx('button', { type: 'button', 'aria-label': `Select all ${CAPABILITY_LABELS[capability]}`, disabled: bulkLocked(capability), onClick: () => setAllCaps(capability, true), children: 'Select all' }),
              jsx('button', { type: 'button', 'aria-label': `Clear all ${CAPABILITY_LABELS[capability]}`, disabled: bulkLocked(capability), onClick: () => setAllCaps(capability, false), children: 'Clear all' })
            ] }) }, capability)
          })
        ] }) }),
        jsx('tbody', { children: calendars.map(calendar => jsxs('tr', { children: [
          jsxs('th', { scope: 'row', className: 'align-top', children: [
            jsx('span', { className: 'block', children: displayName(calendar) }),
            jsxs('details', { className: 'caldav-details', children: [
              jsx('summary', { children: 'Details' }),
              jsx('span', { children: 'iCloud · ' + displayName(calendar) })
            ] })
          ] }),
          ...CAPABILITIES.map(capability => {
            const caps = capsOf(calendar)
            return jsx('td', { children: jsx('input', {
              type: 'checkbox',
              'aria-label': `${CAPABILITY_LABELS[capability]} for ${displayName(calendar)}`,
              checked: caps[capability] === true,
              disabled: lock || (capability !== 'read' && !caps.read),
              onChange: event => setRowCaps(calendar.id, capability, event.target.checked)
            }) }, capability)
          })
        ] }, calendar.id)) })
      ] }) }),
      jsxs('div', { className: 'caldav-access-footer', children: [
        jsx('p', { role: 'status', children: dirty ? 'Unsaved changes. Save to apply these permissions.' : 'No unsaved changes.' }),
        jsx(Button, { variant: 'secondary', disabled: localLock || !dirty, onClick: discard, children: 'Discard unsaved changes' }),
        jsx(Button, { 'data-kal-action': 'primary', onClick: save, disabled: lock || (!dirty && !status?.migration_notice), children: 'Save calendar access' })
      ] })
    ] })
  ] })
}

// Calendar data stays in this mounted page only. No browser persistence or cache.
const PUBLIC_ERRORS = Object.freeze({
  auth: 'Authentication failed. Review credentials in Agent settings.',
  permission: 'Calendar access denied. Review access in Kalends Settings.',
  connectivity: 'Cannot reach the calendar server. Check the connection and retry.',
  response_format: 'Unreadable calendar response. Retry; report the format issue if it continues.',
  work_budget: 'Calendar processing limit reached. Reduce the shown calendar scope in this view.',
  busy: 'The backend is busy. Retry shortly.',
  owner_changed: 'Backend owner changed. Refresh to verify the current owner.',
  admission: 'Request admission failed. Review the backend connection.',
  invalid_request: 'The calendar request could not be processed. Review settings and retry.',
  security: 'Calendar resource validation failed. Review the backend configuration.',
  request_failed: 'Calendar request failed. Review configuration and access, then retry.'
})
function publicError(error) {
  // hermesApi rejects through Electron IPC: custom Error fields may be lost.
  // Parse only a complete JSON suffix and retain ONLY a fixed category string.
  let code = error?.error
  if (!code && typeof error?.message === 'string' && error.message.length <= 65536) {
    const start = error.message.indexOf('{')
    if (start >= 0) { try { code = JSON.parse(error.message.slice(start)).error } catch (_) {} }
  }
  return Object.prototype.hasOwnProperty.call(PUBLIC_ERRORS, code) ? PUBLIC_ERRORS[code] : PUBLIC_ERRORS.request_failed
}
const isBusy = error => publicError(error) === PUBLIC_ERRORS.busy || error?.status === 429 || error?.statusCode === 429 || /(?:^|\s)429:/.test(error?.message || '')
const WEATHER_GLYPHS = Object.freeze({ 'clear-day': '☀', 'clear-night': '☾', rain: '☂', snow: '❄', sleet: '❄', wind: '≋', fog: '≋', cloudy: '☁', 'partly-cloudy-day': '⛅', 'partly-cloudy-night': '☁' })
const WEATHER_MESSAGES = Object.freeze({
  off: 'Weather is off.',
  setup_needed: 'Weather needs setup. Review forecast location and provider key in settings.',
  checking: 'Checking forecast…',
  unavailable: 'Forecast is temporarily unavailable.',
  available: 'No forecast for this date in the available window.'
})
function WeatherIcon({ condition, size = 16 }) {
  const cloud = 'M6 17h12a4 4 0 0 0 0-8 6 6 0 0 0-11-1 4.5 4.5 0 0 0-1 9'
  const sun = 'M12 8a4 4 0 1 0 0 8 4 4 0 0 0 0-8 M12 2v2 M12 20v2 M2 12h2 M20 12h2 M5 5l1 1 M18 18l1 1 M5 19l1-1 M18 6l1-1'
  const moon = 'M20 15A9 9 0 0 1 9 4a9 9 0 1 0 11 11z'
  const paths = {
    'clear-day': sun, 'clear-night': moon, cloudy: cloud,
    'partly-cloudy-day': `${cloud} M5 2v2 M1 6h2 M9 3l-1 1`,
    'partly-cloudy-night': `${cloud} M13 2a4 4 0 0 0 6 4`,
    rain: `${cloud} M8 20l-1 2 M13 20l-1 2 M18 20l-1 2`,
    snow: `${cloud} M12 19v4 M10 20l4 2 M10 22l4-2`,
    sleet: `${cloud} M7 20l-1 2 M14 19v4 M12 20l4 2 M12 22l4-2`,
    fog: `${cloud} M4 20h16 M7 23h10`,
    wind: 'M3 8h13a3 3 0 1 0-3-3 M3 12h17 M3 16h11a3 3 0 1 1-3 3'
  }
  return jsx('svg', { width: size, height: size, viewBox: '0 0 24 24', fill: 'none', stroke: 'currentColor', strokeWidth: 1.6, strokeLinecap: 'round', strokeLinejoin: 'round', 'aria-hidden': true, focusable: false, children: jsx('path', { d: paths[condition] }) })
}
function weatherDetails(day) {
  const detail = {}
  const unit = day.temperature_unit
  const integer = (value, min, max) => Number.isInteger(value) && value >= min && value <= max
  if (unit === 'F' || unit === 'C') {
    for (const key of ['high', 'low']) {
      if (integer(day[key], unit === 'F' ? -130 : -90, unit === 'F' ? 140 : 60)) {
        detail[key] = day[key]
        detail.temperature_unit = unit
      }
    }
  }
  if (integer(day.precipitation_percent, 0, 100)) detail.precipitation_percent = day.precipitation_percent
  return detail
}
function Forecast({ state = 'checking', condition, detail = {}, retry }) {
  return jsxs('section', { className: 'caldav-forecast', 'aria-label': 'Forecast', children: [
    jsx('h3', { children: 'Forecast' }),
    state === 'available' && condition ? jsxs('div', { children: [
       jsxs('p', { className: 'caldav-forecast-condition', children: [jsx(WeatherIcon, { condition, size: 40 }), `Forecast · ${condition.replaceAll('-', ' ')}`] }),
      (detail.high !== undefined || detail.low !== undefined) && jsx('p', { children: [
        detail.high !== undefined ? `High ${detail.high}°${detail.temperature_unit}` : '',
        detail.low !== undefined ? `Low ${detail.low}°${detail.temperature_unit}` : ''
      ].filter(Boolean).join(' · ') }),
      detail.precipitation_percent !== undefined && jsx('p', { children: `Rain chance ${detail.precipitation_percent}%` })
    ] }) : jsxs('div', { children: [jsx('p', { role: 'status', children: WEATHER_MESSAGES[state] || WEATHER_MESSAGES.unavailable }),
      state === 'unavailable' && jsx('button', { type: 'button', className: 'caldav-nav', onClick: retry, children: 'Retry forecast' })] })
  ] })
}
// FIFO transport admission, including status probes. Old generations retain their
// slots until the transport settles; clearing a view never creates extra capacity.
function readAdmission() {
  let active = 0, content = 0
  const queue = []
  const drain = () => {
    while (active < 3 && queue.length) {
      // Two content reads maximum; the third slot remains available to owner
      // status probes even when transports do not honor cancellation.
      const index = queue.findIndex(task => task.status || content < 2 || !task.current())
      if (index < 0) break
      const [task] = queue.splice(index, 1)
      if (!task.current()) { task.reject({ error: 'owner_changed' }); continue }
      active++
      if (!task.status) content++
      Promise.resolve().then(() => {
        if (!task.current()) throw { error: 'owner_changed' }
        return task.run()
      }).then(task.resolve, task.reject).finally(() => { active--; if (!task.status) content--; drain() })
    }
  }
  return (run, current, status) => new Promise((resolve, reject) => { queue.push({ run, current, status, resolve, reject }); drain() })
}
const literal = value => typeof value === 'string' ? value : ''
const dateOnly = value => /^\d{4}-\d{2}-\d{2}$/.test(value || '')
const occurrenceKey = event => JSON.stringify([event.calendar_id, event.href, event.start, event.recurrence_id || ''])
function sourceColor(value) {
  if (typeof value !== 'string' || !/^#[\da-f]{6}([\da-f]{2})?$/i.test(value)) return 'var(--ui-text-tertiary)'
  if (value.length === 9 && parseInt(value.slice(7), 16) < 128) return 'var(--ui-text-tertiary)'
  return value
}
function clientClock() {
  let zone = 'UTC', utc = true, firstDay = 0
  try { zone = Intl.DateTimeFormat().resolvedOptions().timeZone; if (!zone) throw new Error(); new Intl.DateTimeFormat(undefined, { timeZone: zone }); utc = false } catch (_) { zone = 'UTC' }
  try { const locale = new Intl.Locale(Intl.DateTimeFormat().resolvedOptions().locale); firstDay = (locale.getWeekInfo?.() || locale.weekInfo)?.firstDay % 7 || 0 } catch (_) {}
  const parts = d => utc ? [d.getUTCFullYear(), d.getUTCMonth(), d.getUTCDate()] : [d.getFullYear(), d.getMonth(), d.getDate()]
  const make = (y, m, d) => utc ? new Date(Date.UTC(y, m, d)) : new Date(y, m, d)
  const key = d => { const [y, m, day] = parts(d); return `${y}-${String(m + 1).padStart(2, '0')}-${String(day).padStart(2, '0')}` }
  const parse = s => { const [y, m, d] = s.split('-').map(Number); return make(y, m - 1, d) }
  const add = (d, n) => { const [y, m, day] = parts(d); return make(y, m, day + n) }
  const format = (d, options) => d.toLocaleDateString(undefined, { ...options, timeZone: zone })
  return { zone, utc, firstDay, parts, make, key, parse, add, format }
}
function monthGrid(clock, selected) {
  const [y, m] = clock.parts(clock.parse(selected)), first = clock.make(y, m, 1)
  const weekday = clock.utc ? first.getUTCDay() : first.getDay()
  const start = clock.add(first, -((weekday - clock.firstDay + 7) % 7))
  const last = clock.make(y, m + 1, 0), days = []
  let cursor = start
  do { days.push(cursor); cursor = clock.add(cursor, 1) } while (cursor <= last || days.length % 7)
  return { days, start: start.toISOString(), end: cursor.toISOString(), month: m, first }
}
function occupies(event, day, clock) {
  if (dateOnly(event.start) && dateOnly(event.end)) return event.start <= clock.key(day) && event.end > clock.key(day)
  const start = Date.parse(event.start), end = Date.parse(event.end)
  return Number.isFinite(start) && Number.isFinite(end) && start < +clock.add(day, 1) && end > +day
}
const CALENDAR_CSS = `
.caldav-page {container-type:inline-size; min-width:0; width:100%; height:100%; overflow:auto; color:var(--ui-text-primary); background:var(--ui-bg-primary); font-size:14px}
.caldav-inner {padding:12px; min-width:0}
.caldav-toolbar {display:flex; flex-wrap:wrap; align-items:center; gap:8px; margin-bottom:12px}
.caldav-layout {display:grid; grid-template-columns:minmax(0,1fr); gap:16px; min-width:0}
.caldav-layout > * {min-width:0}
.caldav-calendars summary {cursor:pointer; padding:8px 0; font-weight:600}
.caldav-calendar-row {display:flex; align-items:center; gap:8px; padding:8px 0; overflow-wrap:anywhere}
.caldav-swatch {display:inline-block; width:10px; height:10px; flex-shrink:0; border:1px solid var(--ui-stroke-primary); border-radius:50%}
.caldav-month {width:100%; table-layout:fixed; border-collapse:collapse}
.caldav-month th {font-weight:500; padding:6px 0; font-size:12px}
.caldav-month td {vertical-align:top; border:1px solid var(--ui-stroke-secondary); padding:2px; min-width:0}
.caldav-date {width:100%; min-height:36px; border:1px solid transparent; border-radius:4px; background:transparent; color:inherit; cursor:pointer; padding:3px}
.caldav-date[aria-pressed=true] {border:2px solid var(--ui-text-primary); background:var(--ui-bg-secondary)}
.caldav-page button:focus-visible,.caldav-page summary:focus-visible {outline:2px solid var(--ui-text-primary); outline-offset:2px}
.caldav-adjacent {color:var(--ui-text-tertiary)}
.caldav-today {display:block; font-size:10px; text-decoration:underline}
.caldav-preview {display:none}
.caldav-count {display:block; font-size:10px}
.caldav-agenda {overflow-wrap:anywhere; border-top:1px solid var(--ui-stroke-secondary); padding-top:12px}
.caldav-event {display:block; width:100%; white-space:normal; height:auto; text-align:left; margin:6px 0; padding:8px; overflow-wrap:anywhere}
.caldav-detail {white-space:pre-wrap; overflow-wrap:anywhere}
@container (min-width:640px) { .caldav-preview {display:block; width:100%; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; text-align:left; font-size:11px; padding:3px; background:transparent; color:inherit; border:0; cursor:pointer} .caldav-count {display:none} .caldav-month td {height:110px} }
.caldav-connection {display:flex; flex-wrap:wrap; align-items:center; gap:8px; margin-bottom:12px; padding:8px 12px; border:1px solid var(--ui-stroke-secondary); border-radius:8px; background:var(--ui-bg-secondary)}
.caldav-connection p {margin:0; flex:1 1 auto; min-width:0; overflow-wrap:anywhere}
.caldav-connection button:focus-visible {outline:2px solid var(--ui-text-primary); outline-offset:2px}
.caldav-month-title {font-size:36px; font-weight:500; margin:0; letter-spacing:-.03em}
.caldav-year {font-size:20px; color:var(--ui-text-secondary)}
.caldav-page .caldav-connection {border:0; border-bottom:1px solid var(--ui-stroke-secondary); border-radius:0; background:transparent; padding:8px 0; font-size:12px}
.caldav-page .caldav-month td {height:104px; padding:2px; border-left:0; border-right:0}
.caldav-date {box-sizing:border-box; text-align:left; border-radius:0; min-height:32px; overflow:hidden}
.caldav-date[aria-current=date] {color:var(--ui-text-primary); text-decoration:underline; text-decoration-thickness:2px; text-underline-offset:4px}
.caldav-date[aria-pressed=true] {border:1px solid var(--ui-stroke-primary); background:var(--ui-bg-secondary)}
.caldav-today,.caldav-count {display:none}
.caldav-weather {display:inline-block; margin-left:3px; font-size:12px; color:var(--ui-text-secondary); font-weight:400}
.caldav-weather svg {vertical-align:middle}
.caldav-forecast {padding:0 0 16px; margin-bottom:16px; border-bottom:1px solid var(--ui-stroke-secondary)}
.caldav-forecast h3 {font-size:12px; font-weight:500; margin:0 0 8px}
.caldav-forecast p {font-size:13px; margin:6px 0}
.caldav-forecast-condition {display:flex; align-items:center; gap:10px}
.caldav-page .caldav-preview {box-sizing:border-box; display:block; width:100%; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; text-align:left; font-size:11px; padding:3px 4px; margin:2px 0; background:var(--ui-bg-secondary); color:inherit; border:0; border-left:3px solid var(--ui-text-tertiary); border-radius:0; cursor:pointer}
.caldav-event {border:0; border-bottom:1px solid var(--ui-stroke-secondary); border-radius:0; background:transparent}
.caldav-agenda-group h3 {font-size:12px; font-weight:500; color:var(--ui-text-secondary); margin:8px 0 2px}
.caldav-agenda .caldav-event {box-sizing:border-box; margin:2px 0; padding:5px}
.caldav-agenda-line {display:grid; grid-template-columns:64px minmax(0,1fr); gap:12px; align-items:baseline}
.caldav-agenda-time {font-size:12px; color:var(--ui-text-secondary); font-variant-numeric:tabular-nums}
.caldav-agenda-title {min-width:0; overflow-wrap:anywhere}
.caldav-calendars {order:2}
.caldav-agenda {order:1}
@container (max-width:480px) { .caldav-inner {padding:8px} .caldav-page .caldav-month td {height:88px} .caldav-page .caldav-preview {font-size:10px; padding:2px} .caldav-month-title {font-size:30px} }
@media (forced-colors:active) { .caldav-swatch {border:1px solid CanvasText} .caldav-date[aria-pressed=true] {border:2px solid Highlight} }
.caldav-page {--kal-daybook-bg:var(--ui-bg-primary,#f8faf7); --kal-daybook-panel:var(--ui-bg-secondary,#eef4ef); color:var(--ui-text-primary,#17302a); background:var(--kal-daybook-bg); box-sizing:border-box}
.caldav-inner {padding:28px; box-sizing:border-box; max-width:100%}
.caldav-brand {display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:16px; margin-bottom:16px; padding-bottom:16px; border-bottom:1px solid var(--ui-stroke-secondary)}
.caldav-brand strong {font-size:20px; letter-spacing:-.04em}
.caldav-version {font-size:11px; margin-left:10px; opacity:.65}
.caldav-toolbar {position:relative; gap:8px; margin-bottom:8px}
.caldav-navigation {display:flex; gap:6px; align-items:center}
.caldav-toolbar .caldav-calendars {margin-left:auto}
.caldav-jump {margin-right:auto; min-width:0; max-width:100%}
.caldav-jump summary,.caldav-calendars summary {cursor:pointer; list-style:none}
.caldav-jump summary::-webkit-details-marker,.caldav-calendars summary::-webkit-details-marker {display:none}
.caldav-jump summary {font-size:clamp(32px,4cqw,48px); letter-spacing:-.045em; font-weight:650}
.caldav-jump h1 {display:inline-flex; align-items:baseline; gap:8px; font:inherit; margin:0; flex-wrap:wrap}
.caldav-jump .caldav-year {font-weight:400; margin-left:4px; letter-spacing:normal}
.caldav-toolbar :is(.caldav-nav,.caldav-calendars summary) {display:inline-flex; align-items:center; justify-content:center; gap:6px; min-height:34px; padding:4px 9px; color:inherit; background:var(--kal-daybook-panel); border:1px solid var(--ui-stroke-secondary,#b9c7bf); border-radius:6px; font:inherit; font-size:12px; cursor:pointer; box-sizing:border-box}
.caldav-toolbar .caldav-nav:disabled {opacity:.5; cursor:default}
.caldav-jump svg,.caldav-nav svg,.caldav-calendars summary svg {flex:none; align-self:center}
.caldav-jump label {display:inline-flex; gap:6px; align-items:center; margin:8px 10px 8px 0}
.caldav-page select {font:inherit; color:inherit; background:var(--kal-daybook-panel); border:1px solid var(--ui-stroke-secondary,#b9c7bf); border-radius:5px; max-width:100%; padding:5px}
.caldav-calendars {position:static; order:0}
.caldav-calendar-list {position:absolute; right:0; top:100%; z-index:5; width:300px; max-width:100%; max-height:360px; overflow:auto; padding:16px; box-sizing:border-box; background:var(--kal-daybook-panel); border:1px solid var(--ui-stroke-secondary,#b9c7bf); border-radius:10px; box-shadow:0 8px 24px #0002}
.caldav-calendar-list p {font-size:12px; margin-top:0}
.caldav-layout {grid-template-columns:minmax(0,1fr); gap:24px; align-items:start}
.caldav-page .caldav-month th {color:var(--ui-text-secondary,inherit); opacity:.65; text-align:left; padding:10px 6px}
.caldav-page .caldav-month td {border:1px solid var(--ui-stroke-secondary,#b9c7bf); height:90px; padding:2px; box-sizing:border-box}
.caldav-month td[data-weekend=true] {background:var(--kal-daybook-panel)}
.caldav-page .caldav-month td[data-selected=true] {background:var(--kal-selected,var(--kal-daybook-panel)); box-shadow:inset 0 3px var(--ui-text-primary)}
.caldav-page .caldav-date {border:0; padding:3px; min-height:26px; display:flex; align-items:center; justify-content:space-between}
.caldav-page .caldav-date[aria-pressed=true] {border:0; background:transparent; font-weight:700}
.caldav-page .caldav-date[aria-current=date] {text-decoration:none}
.caldav-shell .caldav-page button .caldav-today {display:inline-block; padding:2px 4px; border-radius:4px; text-decoration:none; background:var(--kal-today-bg,#17302A); color:var(--kal-today-text,#FFFFFF); font-size:14px; line-height:1.3; max-width:100%; box-sizing:border-box; white-space:nowrap}
.caldav-meta {display:flex; align-items:center; justify-content:space-between; flex-wrap:wrap; gap:8px; margin:-8px 0 16px; font-size:12px}
.caldav-brand > div:first-child {display:flex; align-items:center; gap:6px}
.caldav-page .caldav-preview {border-radius:4px; background:var(--kal-daybook-panel); background:color-mix(in srgb,var(--event-color,var(--kal-daybook-panel)) 12%,var(--kal-daybook-bg)); color:inherit; font-size:10px; line-height:1.3; padding:2px 4px}
.caldav-agenda {order:1; max-width:100%; padding:20px; box-sizing:border-box; background:var(--kal-daybook-panel); border:1px solid var(--ui-stroke-secondary,#b9c7bf); border-radius:12px}
.caldav-agenda-heading {display:flex; align-items:center; flex-wrap:wrap; gap:10px; margin-bottom:12px}
.caldav-agenda h2 {font-size:16px; line-height:1.25; margin:0; font-weight:600}
.caldav-agenda h2 span {display:block; font-size:12px; font-weight:400; margin-top:3px}
.caldav-agenda-number {font-size:56px; font-weight:650; line-height:1; letter-spacing:-.04em}
.caldav-agenda-count {font-size:12px; margin-left:auto}
.caldav-event-source {display:flex; align-items:center; gap:6px; font-size:12px; font-weight:400; margin-top:3px; color:var(--ui-text-secondary)}
.caldav-agenda-day {font-size:12px; opacity:.7; margin:0}
.caldav-agenda .caldav-event {padding:12px 0; color:inherit}
.caldav-agenda-title {font-size:15px; font-weight:600}
.caldav-agenda-line {grid-template-columns:64px minmax(0,1fr); gap:8px}
.caldav-calendar-error {font-size:12px; margin:8px 0; padding:8px; border:1px solid var(--ui-stroke-secondary,#b9c7bf); border-radius:6px}
.caldav-calendar-error p {margin:4px 0}
.caldav-troubleshoot {border-top:1px solid var(--kal-border,var(--ui-stroke-secondary)); margin-top:8px; padding-top:4px; overflow-wrap:anywhere}
.caldav-troubleshoot summary {cursor:pointer; font-weight:600; padding:6px 0}
.caldav-troubleshoot ul {margin:4px 0 8px; padding-inline-start:18px}
.caldav-troubleshoot li + li {margin-top:6px}
.caldav-shell[data-custom=true] .caldav-calendar-error[role=alert] {color:var(--kal-text); background:var(--kal-panel); border:1px solid var(--kal-border); padding:10px}
.caldav-forecast {background:var(--kal-selected,var(--kal-daybook-bg)); padding:12px; margin-bottom:12px; border-radius:7px}
.caldav-forecast-condition {text-transform:capitalize}
.caldav-forecast h3 {position:absolute; width:1px; height:1px; overflow:hidden; clip-path:inset(50%)}
.caldav-forecast > div {display:flex; flex-wrap:wrap; gap:2px 8px; align-items:center}
.caldav-forecast p {font-size:12px; margin:0}
.caldav-forecast .caldav-forecast-condition {width:100%; font-size:14px; font-weight:600; gap:8px}
.caldav-forecast-condition svg {width:20px; height:20px}
.caldav-preview-list {display:flex; flex-wrap:wrap; align-items:center}
.caldav-footer {display:flex; align-items:center; flex-wrap:wrap; gap:12px; margin-top:24px; padding-top:16px; border-top:1px solid var(--ui-stroke-secondary,#b9c7bf); font-size:12px}
.caldav-footer details {flex:1 1 180px; min-width:0}
.caldav-footer summary {cursor:pointer}
.caldav-footer button {display:inline-flex; align-items:center; gap:6px; min-height:34px; padding:5px 10px; font:inherit; color:inherit; background:var(--kal-daybook-panel); border:1px solid var(--ui-stroke-secondary); border-radius:5px}
.caldav-page :is(button,summary,select,a):focus-visible {outline:2px solid currentColor; outline-offset:3px}
.caldav-page .caldav-date[aria-pressed=true]:focus-visible {outline:2px solid currentColor; outline-offset:-2px}
@media (forced-colors:active) {
  .caldav-page .caldav-month td[data-selected=true] {outline:2px solid Highlight; outline-offset:-2px}
}
.caldav-date-status {margin:0}
.caldav-date-status > p {margin:1em 0}
.caldav-date-status > span {position:absolute; width:1px; height:1px; overflow:hidden; clip-path:inset(50%); white-space:nowrap}
@container (min-width:800px) {.caldav-layout {grid-template-columns:minmax(0,71fr) minmax(0,29fr)} .caldav-agenda {align-self:stretch; margin-top:37px}}
@container (max-width:520px) {
  .caldav-inner {padding:14px}
  .caldav-brand {gap:8px; margin-bottom:20px}
  .caldav-jump {flex-basis:100%}
  .caldav-jump summary {font-size:36px}
  .caldav-page .caldav-month td {height:54px; padding:1px}
  .caldav-page .caldav-preview[style] {display:inline-flex; align-items:center; justify-content:center; width:24px; height:24px; padding:0; margin:0; border:0; background:transparent; font-size:0; flex:none}
  .caldav-page .caldav-preview[style]::before {content:''; width:6px; height:6px; border-radius:50%; background:var(--event-color,var(--ui-text-primary))}
  .caldav-page .caldav-preview:not([style]) {width:100%; min-height:24px; margin:0; padding:1px; border:0; font-size:9px; background:transparent; white-space:normal}
  .caldav-shell .caldav-page button .caldav-today {padding:1px 3px; font-size:13px}
  .caldav-page .caldav-date {padding-left:1px; padding-right:1px}
  .caldav-agenda {padding:16px}
}
`
function readSkippedEvents(value, bounds) {
  const bad = () => { throw { error: 'response_format' } }
  // The CalDAV query is limited to 100 resources and skips at most one event per resource.
  if (!value || typeof value !== 'object' || Array.isArray(value) || Object.keys(value).length !== 2 ||
      !Number.isInteger(value.count) || value.count < 1 || value.count > 100 ||
      !Array.isArray(value.examples) || value.examples.length !== Math.min(value.count, 5)) bad()
  const first = Date.parse(bounds.start), last = Date.parse(bounds.end)
  const examples = value.examples.map(example => {
    if (!example || typeof example !== 'object' || Array.isArray(example) || Object.keys(example).length !== 3 ||
        example.code !== 'equal_start_end' || typeof example.title !== 'string' || example.title.length > 240 ||
        [...example.title].length > 120 || /[\p{Cc}\p{Cf}\p{Zl}\p{Zp}]/u.test(example.title) ||
        typeof example.start !== 'string' || !/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z$/.test(example.start)) bad()
    const instant = Date.parse(example.start)
    if (!(instant >= first && instant < last) || new Date(instant).toISOString().slice(0, 19) !== example.start.slice(0, 19)) bad()
    return { title: example.title, instant }
  })
  return { count: value.count, examples }
}

function CalendarProblem({ name, result, clock, retry }) {
  const skipped = result.skipped
  return jsxs('div', { className: 'caldav-calendar-error', role: 'alert', children: [
    jsx('strong', { children: name }),
    jsx('p', { children: skipped ? `${skipped.count} ${skipped.count === 1 ? 'event' : 'events'} couldn't load. ${result.events.length ? 'Other events from this calendar are shown.' : 'No events from this calendar could be shown for this range.'}` : result.error }),
    jsx(Button, { onClick: retry, children: `Retry ${name}` }),
    skipped && jsxs('details', { className: 'caldav-troubleshoot', children: [
      jsx('summary', { children: 'Troubleshoot' }),
      jsx('p', { children: `${skipped.count === 1 ? 'This event starts' : 'These events start'} and ${skipped.count === 1 ? 'ends' : 'end'} at the same time. Times shown in ${clock.zone}.` }),
      jsx('ul', { children: skipped.examples.map(({ title, instant }, index) => jsxs('li', { children: [
        jsx('strong', { children: title || 'Untitled event' }),
        jsx('div', { children: clock.format(new Date(instant), { month: 'short', day: 'numeric', year: 'numeric', hour: 'numeric', minute: '2-digit' }) })
      ] }, index)) }),
      skipped.count > skipped.examples.length && jsx('p', { children: `Showing ${skipped.examples.length} of ${skipped.count} skipped events.` }),
      jsx('p', { children: 'If an appointment should have a duration, set its intended end time in Apple Calendar and retry this calendar. If the timing is intentional, leave it as is and check it in Apple Calendar.' })
    ] })
  ] })
}

function CalendarPage({ ctx, lifecycle, appearance = defaultAppearance(), store }) {
  const [appearanceMessage, setAppearanceMessage] = useState('')
  const [mode, setMode] = useState('personal')
  const [returningPersonal, setReturningPersonal] = useState(false)
  const [mediaShown, setMediaShown] = useState({ sonarr: true, radarr: true })
  const [specialRefresh, setSpecialRefresh] = useState(0)
  const activeMode = useRef('personal'), personalMonth = useRef(null), tabSwitch = useRef(0)
  const clockRef = useRef(null)
  if (!clockRef.current) clockRef.current = clientClock()
  const clock = clockRef.current
  const [view, setView] = useState(() => ({ selected: clock.key(new Date()), status: null, calendars: [], hidden: {}, results: {}, detail: null, loading: true, needsRefresh: false, error: '' }))
  const state = useRef(view), alive = useRef(false), scope = useRef(0), reads = useRef(0)
  const epoch = useRef(gatewayActivationEpoch()), owner = useRef(null), revision = useRef('')
  const agenda = useRef(null), dateButtons = useRef({}), restoreDay = useRef(false)
  const admission = useRef(null)
  const retryWaits = useRef(new Map()), weatherBusy = useRef(false), weatherDue = useRef(0)
  if (!admission.current) admission.current = readAdmission()
  const request = async (path, options, current) => {
    for (let attempt = 0; ; attempt++) {
      try {
        const result = await admission.current(async () => {
          // Revalidate at the actual queued/retried content start, not only when
          // it was enqueued. Status has a reserved slot, so this cannot deadlock.
          if (path === '/query') {
            const status = await request('/status', { timeoutMs: 15000 }, current)
            if (!current()) throw { error: 'owner_changed' }
            if (status.owner_id !== options.body.owner_id) { clear(); throw { error: 'owner_changed' } }
            if (status.permissions?.[options.body.calendar_id]?.read !== true) throw { error: 'permission' }
          }
          if (!current()) throw { error: 'owner_changed' }
          return ctx.rest(path, options)
        }, current, path === '/status')
        if (!current()) throw { error: 'owner_changed' }
        if (result?.ok === false) throw result
        return result
      } catch (error) {
        if (current() && path === '/status' && isBusy(error) && attempt >= 2) {
          // Exhausted owner/permission verification is not a query failure.
          // Purge the scope (including pending reads); only explicit Refresh
          // may establish a fresh owner and resume content loads.
          clear()
          publish({ statusError: PUBLIC_ERRORS.busy, needsRefresh: true })
        }
        if (!current() || !isBusy(error) || attempt >= 2) throw error
        await new Promise(resolve => {
          const timer = setTimeout(() => { retryWaits.current.delete(timer); resolve() }, 150 * 2 ** attempt)
          retryWaits.current.set(timer, resolve)
        })
      }
    }
  }
  const publish = patch => { if (alive.current) { state.current = { ...state.current, ...patch }; setView(state.current) } }
  const connected = () => !host.state?.gateway?.get || host.state.gateway.get() === 'open'
  const clear = () => {
    scope.current++; reads.current++; owner.current = null; revision.current = ''; epoch.current = gatewayActivationEpoch()
    personalMonth.current = null
    if (alive.current) {
      setMediaShown({ sonarr: true, radarr: true })
      setReturningPersonal(false)
    }
    for (const [timer, resolve] of retryWaits.current) { clearTimeout(timer); resolve() }
    retryWaits.current.clear()
    weatherDue.current = 0
    publish({ selected: clock.key(new Date()), status: null, verified: false, calendars: [], hidden: {}, results: {}, weather: {}, weatherDetails: {}, weatherState: 'checking', weatherRevision: null, detail: null, error: '', statusError: '', loading: false, needsRefresh: false })
  }
  const valid = (s, e) => alive.current && scope.current === s && gatewayActivationEpoch() === e && connected()
  const permissionRevision = status => JSON.stringify(Object.entries(status.permissions || {}).sort(([a], [b]) => a.localeCompare(b)))
  const check = async (s, e, expected) => {
    const requestRevision = reads.current
    let status
    try {
      status = await request('/status', { timeoutMs: 15000 }, () => valid(s, e) && reads.current === requestRevision)
    } catch (error) {
      if (!valid(s, e) || reads.current !== requestRevision) return null
      throw error
    }
    if (!valid(s, e) || reads.current !== requestRevision) return null
    if (!status.owner_id || (expected && status.owner_id !== expected)) { clear(); return null }
    if (state.current.statusError) publish({ statusError: '' })
    if (status.weather_revision !== state.current.weatherRevision) {
      weatherDue.current = 0
      publish({ weather: {}, weatherDetails: {}, weatherState: status.weather_enabled ? 'checking' : 'off', weatherRevision: status.weather_revision })
    }
    const configurationChanged = (status.connected === true) !== (state.current.status?.connected === true)
    if (expected && (configurationChanged || permissionRevision(status) !== revision.current)) {
      // Any effective permission change invalidates both resolved and pending reads.
      reads.current++; revision.current = permissionRevision(status)
      publish({ status, verified: configurationChanged ? false : state.current.verified, calendars: status.connected ? state.current.calendars.filter(c => Object.prototype.hasOwnProperty.call(status.permissions || {}, c.id)) : [], results: {}, detail: null, error: '', loading: false, needsRefresh: true })
      if (!status.connected) { publish({ verified: false }); return null }
    }
    return status
  }
  const readCalendar = async (calendar, s, e, r, expected, bounds) => {
    const current = () => valid(s, e) && reads.current === r && owner.current === expected && !state.current.hidden[calendar.id]
    try {
      if (!current()) return
      const before = await check(s, e, expected)
      if (!before || !current() || before.permissions?.[calendar.id]?.read !== true) return
      const result = await request('/query', { method: 'POST', body: { owner_id: expected, calendar_id: calendar.id, start: bounds.start, end: bounds.end }, timeoutMs: 30000 }, current)
      if (!current()) return
      if (result.owner_id !== expected) { clear(); return }
      if (!await check(s, e, expected) || !current()) return
      if (result.ok === false) throw result
      if (!Array.isArray(result.events)) throw { error: 'response_format' }
      const validTemporal = value => typeof value === 'string' && (dateOnly(value) || /T.*(?:Z|[+-]\d{2}:\d{2})$/.test(value)) && Number.isFinite(Date.parse(value))
      if (result.events.length > 1000 || result.events.some(event => !event || event.calendar_id !== calendar.id || typeof event.href !== 'string' || !validTemporal(event.start) || !validTemporal(event.end) || dateOnly(event.start) !== dateOnly(event.end) || Date.parse(event.end) <= Date.parse(event.start))) throw { error: 'response_format' }
      const seen = new Set()
      const events = result.events.filter(event => { const key = occurrenceKey(event); if (seen.has(key)) return false; seen.add(key); return true })
      const skipped = result.skipped === undefined ? null : readSkippedEvents(result.skipped, bounds)
      publish({ results: { ...state.current.results, [calendar.id]: { events, skipped } } })
    } catch (error) {
      if (!current()) return
      if (isBusy(error)) {
        publish({ results: { ...state.current.results, [calendar.id]: { error: PUBLIC_ERRORS.busy, events: [] } } })
        return
      }
      try {
        if (await check(s, e, expected) && current()) publish({ results: { ...state.current.results, [calendar.id]: { error: publicError(error), events: [] } } })
      } catch (error) {
        if (current()) {
          if (isBusy(error)) publish({ results: { ...state.current.results, [calendar.id]: { error: PUBLIC_ERRORS.busy, events: [] } } })
          else clear()
        }
      }
    }
  }
  const load = (only = null) => {
    if (!alive.current || !owner.current || activeMode.current !== 'personal') return
    if (!connected() || epoch.current !== gatewayActivationEpoch()) { clear(); return }
    // Page-local request identity: plugin context, owner scope/activation,
    // permission revision (invalidates reads), visible IDs, grid interval and zone.
    const s = scope.current, e = epoch.current, expected = owner.current
    const calendars = state.current.calendars.filter(c => state.current.status?.permissions?.[c.id]?.read === true && !state.current.hidden[c.id] && (!only || c.id === only))
    const r = only ? reads.current : ++reads.current
    personalMonth.current = state.current.selected.slice(0, 7)
    const results = only ? { ...state.current.results } : {}
    for (const c of calendars) results[c.id] = { loading: true, events: [] }
    publish({ results, detail: null, needsRefresh: false })
    const bounds = monthGrid(clock, state.current.selected)
    let next = 0
    const worker = async () => {
      while (next < calendars.length && valid(s, e) && reads.current === r && owner.current === expected) {
        const calendar = calendars[next++]
        await readCalendar(calendar, s, e, r, expected, bounds)
      }
    }
    for (let i = 0; i < Math.min(2, calendars.length); i++) void worker()
  }
  const loadWeather = async (s, e, expected, status) => {
    if (!status?.weather_enabled) { if (valid(s, e) && owner.current === expected) publish({ weather: {}, weatherDetails: {}, weatherState: 'off' }); return }
    if (weatherBusy.current || Date.now() < weatherDue.current) return
    weatherBusy.current = true
    const revision = status.weather_revision
    const current = () => valid(s, e) && owner.current === expected && state.current.weatherRevision === revision
    try {
      // Independent single-flight admission: a slow forecast must never occupy
      // the calendar transport's content slots. No automatic busy retries here.
      if (!current()) return
      publish({ weather: {}, weatherDetails: {}, weatherState: 'checking' })
      weatherDue.current = Date.now() + 60000
      const result = await ctx.rest('/weather', { method: 'POST', body: { owner_id: expected }, timeoutMs: 15000 })
      if (!current() || result.owner_id !== expected) return
      if (result.ok === false) throw result
      const after = await check(s, e, expected)
      if (!after || !after.weather_enabled || !current() || after.weather_revision !== revision) return
      // Legacy minimal replies omit weather_state; unknown explicit states fail closed.
      const weatherState = result.weather_state === undefined ? 'available' :
        ['off', 'setup_needed', 'unavailable', 'available'].includes(result.weather_state) ? result.weather_state : 'unavailable'
      if (weatherState === 'available' && (!Array.isArray(result.days) || result.days.length > 16)) throw new Error()
      const weather = {}, details = {}
      for (const day of (Array.isArray(result.days) ? result.days.slice(0, 16) : [])) {
        if (weatherState === 'available' && day && typeof day.date === 'string' && dateOnly(day.date) && Number.isFinite(Date.parse(day.date)) && new Date(day.date).toISOString().slice(0, 10) === day.date && typeof day.condition === 'string' && Object.prototype.hasOwnProperty.call(WEATHER_GLYPHS, day.condition)) {
          weather[day.date] = day.condition
          details[day.date] = weatherDetails(day)
        }
      }
      publish({ weather, weatherDetails: details, weatherState })
      weatherDue.current = Date.now() + (weatherState === 'unavailable' ? 60000 : 30 * 60 * 1000)
    } catch (_) { if (current()) { weatherDue.current = Date.now() + 60000; publish({ weather: {}, weatherDetails: {}, weatherState: 'unavailable' }) } }
    finally { weatherBusy.current = false }
  }
  const initialize = async (preserve = false) => {
    const previous = preserve ? { owner: owner.current, selected: state.current.selected, hidden: state.current.hidden } : null
    clear()
    if (!connected()) return
    const s = scope.current, e = epoch.current
    publish({ loading: true })
    try {
      const status = await check(s, e, null)
      if (!status) return
      owner.current = status.owner_id; revision.current = permissionRevision(status)
      publish({ status, ...(previous?.owner === status.owner_id ? { selected: previous.selected, hidden: previous.hidden } : {}) })
      if (activeMode.current === 'personal') void loadWeather(s, e, status.owner_id, status)
      if (!status.connected || activeMode.current !== 'personal') { publish({ loading: false }); return }
      const result = await request('/discover', { method: 'POST', body: { owner_id: status.owner_id }, timeoutMs: 30000 }, () => valid(s, e) && owner.current === status.owner_id)
      if (!valid(s, e)) return
      if (result.owner_id !== owner.current) { clear(); return }
      const after = await check(s, e, owner.current)
      if (!after) return
      if (result.ok === false) throw result
      if (!Array.isArray(result.calendars)) throw { error: 'response_format' }
      const seen = new Set()
      const calendars = result.calendars.filter(c => c && typeof c.id === 'string' && !seen.has(c.id) && seen.add(c.id))
      publish({ calendars, status: after, verified: true, loading: false }); load()
    } catch (error) {
      if (!valid(s, e)) return
      if (isBusy(error)) { publish({ loading: false, error: PUBLIC_ERRORS.busy }); return }
      try {
        if (owner.current && !await check(s, e, owner.current)) return
        if (valid(s, e)) publish({ calendars: [], results: {}, detail: null, loading: false, error: publicError(error) })
      } catch (_) { if (valid(s, e)) clear() }
    }
  }
  useEffect(() => {
    alive.current = true
    void initialize()
    const unsubscribe = []
    for (const key of ['profile', 'connectionId', 'gateway']) {
      const atom = host.state?.[key]
      if (!atom?.subscribe) continue
      let previous = atom.get()
      unsubscribe.push(atom.subscribe(value => { if (value !== previous) { previous = value; clear(); if (connected()) void initialize() } }))
    }
    // Preserve the existing owner fallback guard; this checks status only,
    // never periodically discovers calendars or fetches event content.
    let checking = false
    const timer = setInterval(async () => {
      if (checking || !alive.current) return
      if (epoch.current !== gatewayActivationEpoch()) { clear(); return }
      if (!connected()) { clear(); return }
      checking = true
      const s = scope.current, e = epoch.current
      try { if (owner.current) { const status = await check(s, e, owner.current); if (status && activeMode.current === 'personal') void loadWeather(s, e, owner.current, status) } }
      catch (error) {
        if (valid(s, e)) {
          if (isBusy(error)) publish({ statusError: PUBLIC_ERRORS.busy })
          else clear()
        }
      }
      finally { checking = false }
    }, 1000)
    const dispose = () => { alive.current = false; scope.current++; reads.current++; for (const [timer, resolve] of retryWaits.current) { clearTimeout(timer); resolve() }; retryWaits.current.clear(); state.current = { selected: clock.key(new Date()), calendars: [], hidden: {}, results: {}, detail: null }; clearInterval(timer); unsubscribe.forEach(fn => fn()); lifecycle?.delete(dispose) }
    dispose.clear = clear
    lifecycle?.add(dispose)
    return dispose
  }, [ctx])
  useEffect(() => { if (restoreDay.current && !view.detail) { restoreDay.current = false; dateButtons.current[view.selected]?.focus() } }, [view.detail, view.selected])

  const renderScope = scope.current, renderReads = reads.current
  const actionable = () => valid(renderScope, epoch.current) && renderReads === reads.current
  const changeMode = async next => {
    if (activeMode.current === next) return
    const run = ++tabSwitch.current
    activeMode.current = next
    setMode(next)
    const s = scope.current, e = epoch.current, expected = owner.current
    setReturningPersonal(next === 'personal' && !!expected)
    if (next !== 'personal') return
    const current = () => run === tabSwitch.current && valid(s, e)
    // Don't reveal cached Personal rows before checking for an unannounced
    // backend replacement. This is a local status probe, not an iCloud read.
    if (expected) {
      try {
        const nextStatus = await request('/status', { timeoutMs: 15000 }, current)
        if (!current()) return
        if (nextStatus.owner_id !== expected) { void initialize(); return }
        if (permissionRevision(nextStatus) !== revision.current ||
            nextStatus.connected !== state.current.status?.connected) {
          reads.current++; revision.current = permissionRevision(nextStatus)
          publish({ status: nextStatus, calendars: [], results: {}, detail: null, verified: false, needsRefresh: true, loading: false })
        }
      } catch (_) { if (current()) clear(); return }
      setReturningPersonal(false)
    }
    if (state.current.needsRefresh || state.current.loading) return
    if (state.current.verified) {
      if (personalMonth.current !== state.current.selected.slice(0, 7)) load()
    } else if (state.current.status?.connected) void initialize(true)
  }
  const specialOwner = view.status?.owner_id
  const specialMonth = view.selected.slice(0, 7)
  const specialEpoch = epoch.current
  const specialTab = tabSwitch.current
  const specialCurrent = () => !!specialOwner && activeMode.current === mode && tabSwitch.current === specialTab &&
    valid(renderScope, specialEpoch) && owner.current === specialOwner && !state.current.statusError &&
    state.current.selected.slice(0, 7) === specialMonth
  const specialKey = specialCurrent() ? `${renderScope}:${specialTab}:${specialOwner}:${specialMonth}:${specialRefresh}` : null
  const specialVerify = current => verifySpecialOwner(
    valid => request('/status', { timeoutMs: 15000 }, valid), current, specialOwner, clear)
  const specialRead = (path, bounds = false) => current => request(path, {
    method: 'POST', timeoutMs: 15000, body: { owner_id: specialOwner, ...(bounds ? { month: specialMonth, zone: clock.zone } : {}) }
  }, current)
  const readiness = useSpecialRead(mode === 'media' ? specialKey : null, specialCurrent, specialRead('/special/readiness'), specialVerify, specialOwner, { feeds: {} })
  const sonarr = useSpecialRead(mode === 'media' && mediaShown.sonarr && readiness?.feeds?.sonarr === 'ready' ? specialKey : null,
    specialCurrent, specialRead('/special/media/sonarr', true), specialVerify, specialOwner, { state: 'unavailable' })
  const radarr = useSpecialRead(mode === 'media' && mediaShown.radarr && readiness?.feeds?.radarr === 'ready' ? specialKey : null,
    specialCurrent, specialRead('/special/media/radarr', true), specialVerify, specialOwner, { state: 'unavailable' })
  const automations = useSpecialRead(mode === 'automations' ? specialKey : null, specialCurrent,
    specialRead('/special/automations', true), specialVerify, specialOwner, { state: 'unavailable' })
  const select = (selected, focus = false) => {
    if (!actionable()) return
    const previous = state.current.selected.slice(0, 7)
    publish({ selected, detail: null })
    if (previous !== selected.slice(0, 7)) load()
    if (focus) agenda.current?.focus()
  }
  const shift = amount => {
    const [y, m, d] = clock.parts(clock.parse(state.current.selected))
    const last = clock.parts(clock.make(y, m + amount + 1, 0))[2]
    select(clock.key(clock.make(y, m + amount, Math.min(d, last))))
  }
  const grid = monthGrid(clock, view.selected), today = clock.key(new Date())
  const [selectedYear, selectedMonth] = clock.parts(grid.first)
  const currentYear = clock.parts(new Date())[0]
  const jump = (year, month) => {
    if (!Number.isInteger(year) || year < currentYear - 100 || year > currentYear + 100 || !Number.isInteger(month) || month < 0 || month > 11) return
    const day = clock.parts(clock.parse(view.selected))[2]
    select(clock.key(clock.make(year, month, Math.min(day, clock.parts(clock.make(year, month + 1, 0))[2]))))
  }
  const readable = view.calendars.filter(c => view.status?.permissions?.[c.id]?.read === true)
  const shown = readable.filter(c => !view.hidden[c.id])
  const pending = !view.needsRefresh && shown.some(c => !view.results[c.id] || view.results[c.id].loading)
  const failed = shown.filter(c => view.results[c.id]?.error || view.results[c.id]?.skipped)
  const events = shown.flatMap(c => (view.results[c.id]?.events || []).map(event => ({ ...event, calendar: c })))
  events.sort((a, b) => Number(dateOnly(b.start)) - Number(dateOnly(a.start)) || (dateOnly(a.start) ? a.start.localeCompare(b.start) : Date.parse(a.start) - Date.parse(b.start)) || literal(a.calendar.name).localeCompare(literal(b.calendar.name)) || literal(a.title).localeCompare(literal(b.title)) || occurrenceKey(a).localeCompare(occurrenceKey(b)))
  const dayEvents = day => events.filter(event => occupies(event, day, clock))
  const selectedEvents = dayEvents(clock.parse(view.selected))
  const fullDate = day => clock.format(day, { month: 'long', day: 'numeric', year: 'numeric' })
  const swatch = calendar => jsx('span', { className: 'caldav-swatch', 'aria-hidden': true, style: { backgroundColor: sourceColor(calendar.color) } })
  const calendarName = calendar => calendarLabel(calendar, view.calendars)
  const eventTitle = event => literal(event.title).trim() ? event.title : 'Untitled event'
  const eventTime = event => dateOnly(event.start) ? `All day · ${event.start} to ${clock.key(clock.add(clock.parse(event.end), -1))}` : `${new Date(event.start).toLocaleString(undefined, { timeZone: clock.zone })} – ${new Date(event.end).toLocaleString(undefined, { timeZone: clock.zone })}`
  const openEvent = event => { if (actionable()) publish({ detail: occurrenceKey(event) }) }
  const detail = events.find(event => occurrenceKey(event) === view.detail)
  const connection = connectionState(view.status)
  let empty = ''
   if (!view.loading && !view.error && !view.statusError) {
    if (connection === 'gateway') empty = 'Hermes backend is disconnected. Reconnect the gateway, then refresh.'
    else if (connection === 'unverified') empty = 'Backend owner is unverified. Refresh to continue.'
    else if (connection === 'not_configured') empty = 'iCloud account is not set up. Use account setup to add your Apple ID and app-specific password.'
    else if (connection === 'missing_apple_id') empty = 'Apple ID is missing. Use account setup to add it, then refresh.'
    else if (connection === 'missing_credential') empty = 'App-specific password is missing. Use account setup to add it, then refresh.'
    else if (view.needsRefresh && !view.verified) empty = 'Credentials configured. Refresh to verify calendar discovery.'
     else if (!view.calendars.length) empty = 'No calendars discovered. Open Kalends Settings to test and discover.'
     else if (!readable.length) empty = 'Calendar access is Off. Choose access in Kalends Settings.'
    else if (!shown.length) empty = 'No calendars shown. Choose calendars to display.'
  }
  return jsxs('div', { className: 'caldav-page', children: [
    jsx('style', { children: APPEARANCE_CSS + CALENDAR_CSS + INDIGO_CSS }),
    jsxs('div', { className: 'caldav-inner', children: [
      jsxs('div', { className: 'caldav-brand', children: [
        jsxs('div', { children: [jsx(ToolbarIcon, { name: 'calendar' }), jsx('strong', { children: 'Kalends' }), jsx('span', { className: 'caldav-version', children: 'v0.3.0' })] }),
        jsxs('div', { className: 'kal-brand-actions', children: [
          jsx(ModeSelector, { appearance, onChange: mode => setAppearanceMessage(store?.save({ mode }) ? '' : 'Local storage is unavailable. Appearance was not saved.') }),
          jsx('button', { type: 'button', className: 'kal-settings-shortcut', 'aria-label': 'Open Kalends Settings', title: 'Open Kalends Settings', onClick: () => host.navigate(CALENDAR_SETTINGS_HREF), children: jsx(SpecialIcon, { name: 'settings', size: 18 }) })
        ] })
      ] }),
      appearanceMessage && jsx('p', { role: 'status', children: appearanceMessage }),
      jsx(ViewTabs, { mode, onChange: changeMode }),
      ...Object.keys(VIEW_LABELS).filter(key => key !== mode).map(key => jsx('div', {
        id: `kal-panel-${key}`, role: 'tabpanel', 'aria-labelledby': `kal-tab-${key}`, hidden: true
      }, key)),
      mode !== 'personal' ? jsx(SpecialPanel, {
        mode, clock, grid, selected: view.selected, today, select, shift,
        retry: () => { if (!specialCurrent()) void initialize(true); else setSpecialRefresh(n => n + 1) },
        enabled: specialCurrent(), checking: view.loading && !specialOwner,
        readiness, mediaShown, setMediaShown, sonarr, radarr, automations
      }) : jsxs('div', { id: 'kal-panel-personal', role: 'tabpanel', 'aria-labelledby': 'kal-tab-personal', children: returningPersonal
        ? jsx('p', { role: 'status', children: 'Checking calendar owner…' }) : [
      jsxs('header', { className: 'caldav-toolbar', children: [
        jsxs('details', { className: 'caldav-jump', children: [
          jsx('summary', { 'aria-label': 'Jump to month and year', children: jsxs('h1', { className: 'caldav-month-title', children: [clock.format(grid.first, { month: 'long' }), ' ', jsx('span', { className: 'caldav-year', children: clock.format(grid.first, { year: 'numeric' }) }), jsx(ToolbarIcon, { name: 'down' })] }) }),
          jsxs('label', { children: ['Month', jsx('select', { 'aria-label': 'Month', value: selectedMonth, disabled: view.loading || connection !== 'connected', onChange: e => jump(selectedYear, Number(e.target.value)), children: Array.from({ length: 12 }, (_, month) => jsx('option', { value: month, children: clock.format(clock.make(selectedYear, month, 1), { month: 'long' }) }, month)) })] }),
          jsxs('label', { children: ['Year', jsx('select', { 'aria-label': 'Year', value: selectedYear, disabled: view.loading || connection !== 'connected', onChange: e => jump(Number(e.target.value), selectedMonth), children: Array.from({ length: 201 }, (_, i) => currentYear - 100 + i).map(year => jsx('option', { value: year, children: year }, year)) })] })
        ] }),
        jsxs('div', { className: 'caldav-navigation', role: 'group', 'aria-label': 'Month navigation', children: [
        jsx('button', { type: 'button', className: 'caldav-nav', 'aria-label': 'Previous month', title: 'Previous month', disabled: view.loading || connection !== 'connected', onClick: () => shift(-1), children: jsx(ToolbarIcon, { name: 'previous' }) }),
        jsx('button', { type: 'button', className: 'caldav-nav', disabled: view.loading || connection !== 'connected', onClick: () => select(today), children: 'Today' }),
        jsx('button', { type: 'button', className: 'caldav-nav', 'aria-label': 'Next month', title: 'Next month', disabled: view.loading || connection !== 'connected', onClick: () => shift(1), children: jsx(ToolbarIcon, { name: 'next' }) }),
        ] }),
        jsxs('details', { className: 'caldav-calendars', children: [
          jsxs('summary', { children: ['Calendars', jsx(ToolbarIcon, { name: 'down' })] }),
          jsxs('div', { className: 'caldav-calendar-list', children: [
            jsx('p', { children: 'Hiding a calendar only changes this view. Change access in Kalends Settings.' }),
            ...view.calendars.map(calendar => {
              const permitted = view.status?.permissions?.[calendar.id]?.read === true
              return jsxs('label', { className: 'caldav-calendar-row', children: [
                jsx(Checkbox, { 'aria-label': `Show ${calendarName(calendar)} in this view`, checked: permitted && !view.hidden[calendar.id], disabled: !permitted || view.loading, onCheckedChange: checked => {
                  if (!actionable()) return
                  publish({ hidden: { ...state.current.hidden, [calendar.id]: checked !== true }, detail: null }); load()
                } }), swatch(calendar), jsx('span', { children: calendarName(calendar) }), jsx(Badge, { children: permitted ? 'Read' : 'Off' })
              ] }, calendar.id)
            })
          ] })
        ] }),
        jsx('button', { type: 'button', className: 'caldav-nav', 'aria-label': 'Refresh', title: 'Refresh', disabled: view.loading || !connected(), onClick: () => { if (!actionable()) return; void initialize(true) }, children: jsx(ToolbarIcon, { name: 'refresh' }) }),
      ] }),
      (connection !== 'connected' || view.statusError || (!view.verified && view.needsRefresh)) && jsxs('section', { className: 'caldav-connection', 'aria-label': 'iCloud account', children: [
         jsx('p', { role: 'status', children: view.statusError ? 'Backend owner verification is delayed. Refresh to retry.' : accountMessage(connection, view.verified) }),
        !SETUP_STATES.includes(connection) && jsx(Button, { variant: 'secondary', onClick: () => host.navigate(ACCOUNT_SETUP_HREF), children: 'Update Apple account' }),
        SETUP_STATES.includes(connection) && jsx(Button, { variant: 'primary', onClick: () => host.navigate(ACCOUNT_SETUP_HREF), children: 'Set up iCloud account' })
      ] }),
       jsx('div', { className: 'caldav-date-status', role: 'status', 'aria-live': 'polite', children: view.loading || pending || failed.length || view.statusError || view.needsRefresh
         ? jsx('p', { children: view.loading || pending ? 'Loading calendars and events…' : failed.length ? 'Results incomplete. Some events or calendars could not be loaded.' : view.statusError ? 'Calendar content hidden until owner verification succeeds. Use Refresh to retry.' : 'Calendar access changed. Refresh to load permitted calendars.' })
         : jsx('span', { children: clock.format(clock.parse(view.selected), { weekday: 'long', month: 'long', day: 'numeric', year: 'numeric' }) }) }),
      (view.loading || pending) && jsx(GlyphSpinner, { 'aria-label': 'Loading calendar' }),
      view.error && jsx('div', { role: 'alert', children: jsx(ErrorState, { title: 'Calendar could not load', description: view.error }) }),
      view.statusError && jsx('p', { role: 'status', children: `Results incomplete: owner verification is delayed. ${view.statusError} Use Refresh to retry.` }),
      ...failed.map(calendar => jsx(CalendarProblem, {
        name: calendarName(calendar), result: view.results[calendar.id], clock,
        retry: () => { if (actionable()) load(calendar.id) }
      }, calendar.id)),
      empty && jsx(EmptyState, { title: empty }),
      jsxs('div', { className: 'caldav-layout', children: [
        jsx('div', { children: jsxs('table', { className: 'caldav-month', 'aria-label': clock.format(grid.first, { month: 'long', year: 'numeric' }), children: [
          jsx('thead', { children: jsx('tr', { children: grid.days.slice(0, 7).map(day => jsx('th', { scope: 'col', children: clock.format(day, { weekday: 'short' }) }, clock.key(day))) }) }),
          jsx('tbody', { children: Array.from({ length: grid.days.length / 7 }, (_, week) => jsx('tr', { children: grid.days.slice(week * 7, week * 7 + 7).map(day => {
            const key = clock.key(day), entries = dayEvents(day), isToday = key === today
            return jsxs('td', { 'data-calendar-day': key, 'data-selected': key === view.selected, 'data-weekend': [0, 6].includes(clock.utc ? day.getUTCDay() : day.getDay()), className: clock.parts(day)[1] !== grid.month ? 'caldav-adjacent' : '', children: [
              jsxs('button', { type: 'button', className: 'caldav-date', tabIndex: key === view.selected ? 0 : -1, onKeyDown: event => {
                const offset = { ArrowLeft: -1, ArrowRight: 1, ArrowUp: -7, ArrowDown: 7, Home: -(grid.days.indexOf(day) % 7), End: 6 - grid.days.indexOf(day) % 7 }[event.key]
                if (offset === undefined) return
                event.preventDefault(); restoreDay.current = true; select(clock.key(clock.add(day, offset)))
              }, disabled: view.loading || connection !== 'connected', ref: node => { if (node) dateButtons.current[key] = node; else delete dateButtons.current[key] }, 'aria-pressed': key === view.selected, 'aria-current': isToday ? 'date' : undefined, 'aria-label': `${fullDate(day)}${isToday ? ', today' : ''}${key === view.selected ? ', selected' : ''}, ${entries.length} events${pending || failed.length ? ', incomplete' : ''}${view.weather?.[key] ? `, Forecast: ${view.weather[key].replaceAll('-', ' ')}` : ''}`, onClick: () => select(key), children: [jsx('span', { className: isToday ? 'caldav-today' : undefined, children: clock.parts(day)[2] }), view.weather?.[key] && jsx('span', { className: 'caldav-weather', 'aria-hidden': true, children: jsx(WeatherIcon, { condition: view.weather[key], size: 12 }) }), jsx('span', { className: 'caldav-count', children: entries.length ? `${entries.length} events` : '' })] }),
              jsxs('div', { className: 'caldav-preview-list', children: [
              ...entries.slice(0, 3).map(event => jsx('button', { type: 'button', className: 'caldav-preview', style: { '--event-color': sourceColor(event.calendar.color), borderLeftColor: sourceColor(event.calendar.color) }, 'aria-label': `${eventTitle(event)}, ${calendarName(event.calendar)}, ${fullDate(day)}`, onClick: () => { select(key); openEvent(event) }, children: eventTitle(event) }, occurrenceKey(event))),
              entries.length > 3 && jsx('button', { type: 'button', className: 'caldav-preview', onClick: () => select(key, true), children: `+${entries.length - 3} more` })
              ] })
            ] }, key)
          }) }, week)) })
        ] }) }),
        jsxs('section', { className: 'caldav-agenda', role: 'region', 'aria-label': 'Selected day agenda', tabIndex: -1, ref: agenda, children: [
          jsxs('div', { className: 'caldav-agenda-heading', children: [
            jsx('span', { className: 'caldav-agenda-number', 'aria-hidden': true, children: clock.parts(clock.parse(view.selected))[2] }),
             jsxs('h2', { 'aria-label': clock.format(clock.parse(view.selected), { weekday: 'long', month: 'long', day: 'numeric', year: 'numeric' }), children: [clock.format(clock.parse(view.selected), { weekday: 'long' }), ' ', jsx('span', { children: clock.format(clock.parse(view.selected), { month: 'long', day: 'numeric' }) })] }),
            view.verified && connection === 'connected' && !view.loading && !view.needsRefresh && !pending && !failed.length && !empty && !view.error && !view.statusError && jsx('span', { className: 'caldav-agenda-count', children: `${selectedEvents.length} ${selectedEvents.length === 1 ? 'event' : 'events'}` })
          ] }),
           jsx(Forecast, { state: view.weatherState, condition: view.weather?.[view.selected], detail: view.weatherDetails?.[view.selected],
             retry: () => { if (!actionable() || !owner.current || !view.status?.weather_enabled) return; weatherDue.current = 0; void loadWeather(scope.current, epoch.current, owner.current, view.status) } }),
          detail ? jsxs('article', { className: 'caldav-detail', children: [
            jsx(Button, { variant: 'secondary', onClick: () => { if (actionable()) { restoreDay.current = true; publish({ detail: null }) } }, children: 'Back to day' }),
            jsx('h3', { children: eventTitle(detail) }), swatch(detail.calendar), jsx('p', { children: calendarName(detail.calendar) }),
            jsx('p', { children: eventTime(detail) }), jsx('p', { children: clock.zone }),
            literal(detail.location) && jsx('p', { children: `Location: ${detail.location}` }),
            literal(detail.description) && jsx('p', { children: detail.description })
          ] }) : jsx('div', { className: 'caldav-agenda-group', children: selectedEvents.map(event => jsx(Button, { variant: 'secondary', className: 'caldav-event', onClick: () => openEvent(event), children: jsxs('span', { className: 'caldav-agenda-line', children: [
              jsx('span', { className: 'caldav-agenda-time', children: dateOnly(event.start) ? 'All day' : new Date(event.start).toLocaleTimeString(undefined, { timeZone: clock.zone, hour: 'numeric', minute: '2-digit' }) }),
              jsxs('span', { className: 'caldav-agenda-title', children: [eventTitle(event), (dateOnly(event.start) ? event.start < view.selected : Date.parse(event.start) < +clock.parse(view.selected)) && ' · Continues from an earlier day', (dateOnly(event.end) ? event.end > clock.key(clock.add(clock.parse(view.selected), 1)) : Date.parse(event.end) > +clock.add(clock.parse(view.selected), 1)) && ' · Continues on a later day', jsxs('span', { className: 'caldav-event-source', children: [swatch(event.calendar), calendarName(event.calendar)] })] })
            ] }) }, occurrenceKey(event)))
          }),
          !detail && !selectedEvents.length && !view.loading && !view.needsRefresh && !pending && !failed.length && !empty && !view.error && !view.statusError && jsx(EmptyState, { title: `No events on ${fullDate(clock.parse(view.selected))} in shown calendars.` })
        ] })
      ] }),
      jsxs('footer', { className: 'caldav-footer', children: [
        jsxs('details', { children: [
          jsx('summary', { children: 'Saved calendar access · read-only summary' }),
          view.calendars.length ? jsx('ul', { children: view.calendars.map(calendar => jsx('li', { children: `${calendarName(calendar)} · ${CAPABILITIES.map(capability => `${CAPABILITY_LABELS[capability]} ${view.status?.permissions?.[calendar.id]?.[capability] === true ? 'on' : 'off'}`).join(' · ')}` }, calendar.id)) }) : jsx('p', { children: 'No verified calendar access to display.' })
        ] }),
        jsx('span', { children: `${clock.zone}${clock.utc ? ' (fallback)' : ''}` }),
        jsxs(Button, { variant: 'secondary', 'aria-label': 'Kalends Settings', onClick: () => host.navigate(CALENDAR_SETTINGS_HREF), children: [jsx(Codicon, { name: 'gear', 'aria-hidden': true }), 'Kalends Settings'] })
      ] })
      ] })
    ] })
  ] })
}

const SPECIAL_CSS = `
.kal-view-tabs,.kal-special-bar,.kal-special-status {display:flex;flex-wrap:wrap;gap:.5rem;align-items:center;margin-block:.5rem}
.kal-view-tabs {width:fit-content;max-width:100%}
.kal-view-tabs button {display:inline-flex;align-items:center;justify-content:center;gap:.25rem;min-width:44px;min-height:44px;border:1px solid var(--ui-stroke-secondary);border-radius:.5rem;background:var(--ui-bg-secondary);color:var(--ui-text-primary);padding:.4rem .65rem}
.kal-view-tabs button[aria-selected=true],.kal-view-tabs button[aria-pressed=true] {box-shadow:inset 0 -3px currentColor;background:var(--ui-bg-tertiary)}
.kal-special-bar a {margin-inline-start:auto;text-decoration:underline}
.kal-special-status {color:var(--ui-text-secondary);column-gap:1.25rem}
.kal-special-month .caldav-date {min-width:0}
.kal-special-markers {display:flex;flex-wrap:wrap;gap:2px;padding:2px}
.kal-special-markers span {display:inline-flex;padding:3px;border-radius:.35rem;background:var(--ui-bg-tertiary)}
.kal-special-agenda {margin-block:.8rem}
.kal-special-agenda li,.kal-special-jobs label {display:flex;flex-wrap:wrap;align-items:center;gap:.5rem;overflow-wrap:anywhere;margin-block:.35rem}
.kal-special-jobs {min-width:0;max-height:24rem;overflow:auto}
`
// Layout and surfaces follow the scoped appearance variables; desktop mode
// continues to inherit Hermes, and saved palettes remain entirely user-owned.
const INDIGO_CSS = `
.caldav-page .caldav-inner {padding:28px;max-width:1440px;margin:auto}
.caldav-page .caldav-brand {margin:-28px -28px 8px;padding:12px 28px;min-height:56px;box-sizing:border-box}
.caldav-brand > div:first-child > svg {width:32px;height:32px;padding:7px;box-sizing:border-box;border-radius:7px;background:var(--kal-selected,var(--ui-bg-secondary));color:var(--kal-accent,var(--ui-text-primary))}
.caldav-brand .caldav-version {margin-left:0}
.kal-brand-actions {display:flex;align-items:center;gap:6px}
.caldav-page .kal-mode-buttons button,.kal-settings-shortcut,.caldav-page .kal-view-tabs button {width:44px;height:44px;border:1px solid var(--ui-stroke-secondary);border-radius:6px;background:var(--ui-bg-secondary);color:var(--ui-text-primary);cursor:pointer}
.kal-settings-shortcut {display:inline-flex;align-items:center;justify-content:center;padding:0}
.caldav-page .kal-mode-buttons button[aria-pressed=true],.caldav-page .kal-view-tabs button[aria-selected=true] {background:var(--kal-selected,var(--ui-bg-tertiary));color:var(--kal-accent,var(--ui-text-primary));border-color:var(--ui-stroke-primary);box-shadow:inset 0 -3px currentColor}
.caldav-page .kal-mode-buttons {gap:4px}
.caldav-page .kal-view-tabs {margin:0 0 8px;gap:4px}
.caldav-page .caldav-toolbar {margin:0 0 22px}
.caldav-page .caldav-jump summary,.kal-special-title h1 {font-size:clamp(32px,4cqw,44px)}
.caldav-page .caldav-layout {gap:24px}
.caldav-page .caldav-month {border:1px solid var(--ui-stroke-secondary);border-radius:10px;border-collapse:separate;border-spacing:0;overflow:hidden}
.caldav-page .caldav-month th {box-sizing:border-box;height:36px;text-align:center;background:var(--ui-bg-secondary);padding:0 2px;font-weight:600;opacity:1}
.caldav-page .caldav-month td {height:74px;border:0;border-top:1px solid var(--ui-stroke-secondary);border-right:1px solid var(--ui-stroke-secondary);padding:3px}
.caldav-page .caldav-month td:last-child {border-right:0}
.caldav-month td[data-weekend=true] {background:transparent}
.caldav-page .caldav-month td[data-selected=true] {background:var(--kal-selected,var(--ui-bg-secondary));box-shadow:inset 0 0 0 1px var(--kal-accent,var(--ui-text-primary));border-radius:6px}
.caldav-page .caldav-date {width:100%;min-height:24px}
.caldav-page .caldav-date[aria-current=date]:not([aria-pressed=true]) {text-decoration:underline}
.caldav-page .caldav-preview {border-radius:4px;background:color-mix(in srgb,var(--event-color,var(--ui-text-primary)) 22%,var(--ui-bg-secondary));font-weight:600}
.caldav-page .caldav-agenda {margin-top:0;min-height:408px;border-radius:12px;background:var(--ui-bg-secondary);padding:16px}
.caldav-page .caldav-agenda-heading {gap:0;margin-bottom:12px}
.caldav-page .caldav-agenda-number {display:none}
.caldav-page .caldav-agenda h2 {font-size:24px}
.caldav-page .caldav-agenda h2 span {display:inline;font-size:24px;font-weight:600;margin:0}
.caldav-page .caldav-agenda .caldav-event,.kal-special-agenda li {background:var(--kal-selected,var(--ui-bg-primary));border:0;border-radius:6px;padding:9px;margin:5px 0}
.caldav-page .caldav-agenda .caldav-event:hover {outline:1px solid var(--ui-text-primary)}
.caldav-forecast {border:1px solid var(--ui-stroke-secondary);padding:12px;margin-bottom:12px;background:var(--kal-selected,var(--ui-bg-primary))}
.caldav-forecast .caldav-forecast-condition {font-size:14px;gap:10px}
.caldav-forecast-condition svg {width:40px;height:40px;flex:none}
.caldav-forecast > div:has(> [role=status]) {justify-content:space-between}
.caldav-forecast button {font:inherit;color:inherit;background:var(--ui-bg-secondary);border:1px solid var(--ui-stroke-secondary);border-radius:5px;padding:5px}
.kal-special-title {margin-right:auto}
.kal-special-title p {font-size:12px;color:var(--ui-text-secondary);margin:4px 0 0}
.caldav-page .kal-special-layout {align-items:stretch}
.kal-special-grid {min-width:0}
.caldav-page .kal-special-month .caldav-date {min-height:26px}
.kal-special-markers {padding:2px;gap:3px}
.kal-special-markers > span {display:flex;align-items:center;gap:3px;max-width:100%;min-width:0;background:var(--kal-selected,var(--ui-bg-secondary));color:var(--kal-accent,var(--ui-text-primary))}
.kal-special-markers > span > svg {flex:none}
.kal-special-markers .kal-marker-title {overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:10px;padding:0;background:none}
.kal-special-more {font-size:10px}
.kal-special-footnote {font-size:12px;color:var(--ui-text-secondary);margin:14px 0}
.caldav-page .kal-special-agenda {margin:0}
.kal-special-agenda h2 {font-size:22px}
.kal-special-agenda-count {color:var(--kal-accent,var(--ui-text-secondary));font-size:12px}
.kal-special-agenda ul {padding:0;list-style:none}
.kal-special-agenda li {display:grid;grid-template-columns:72px minmax(0,1fr);gap:8px;align-items:center}
.kal-special-agenda li > span:first-child {font-size:12px;color:var(--ui-text-secondary);border-right:1px solid var(--ui-stroke-secondary)}
.kal-special-agenda li strong,.kal-special-agenda li small {display:block}
.kal-special-agenda li small {color:var(--ui-text-secondary)}
.kal-special-bar a {margin:0;border:1px solid var(--ui-stroke-secondary);padding:6px 8px;border-radius:5px;color:inherit;text-decoration:none}
.caldav-page .kal-special-bar .kal-view-tabs button {width:auto;min-height:32px;height:auto;gap:5px;padding:5px 8px;font-size:12px}
.kal-special-bar .kal-view-tabs button[aria-pressed=true] {background:var(--kal-selected,var(--ui-bg-secondary));box-shadow:none}
.kal-special-status {font-size:12px;margin:8px 0}
.kal-special-warning {display:flex;align-items:center;flex-wrap:wrap;gap:6px 14px;border:1px solid var(--ui-stroke-secondary);background:var(--ui-bg-secondary);padding:10px 12px;border-radius:7px;margin-bottom:18px;font-size:12px}
.kal-special-warning strong {color:var(--ui-text-primary)}
.kal-special-warning button {margin-left:auto}
@container (min-width:800px) {.caldav-page .caldav-layout {grid-template-columns:minmax(0,71fr) minmax(0,29fr)} .caldav-page .caldav-agenda:not(.kal-special-agenda) {margin-top:37px}}
@container (max-width:799px) {.caldav-page .caldav-layout {grid-template-columns:minmax(0,1fr)}}
@container (max-width:520px) {
 .caldav-page .caldav-inner {padding:16px}
 .caldav-page .caldav-brand {margin:-16px -16px 8px;padding:12px 16px}
 .caldav-page .caldav-month td {height:38px}
 .caldav-page .caldav-preview[style] {background:transparent}
 .caldav-page .kal-special-markers > span {padding:0;background:transparent}
 .caldav-page .kal-marker-title {display:none}
 .caldav-page .kal-special-more {font-size:9px}
 .caldav-page .caldav-agenda h2,.caldav-page .caldav-agenda h2 span {font-size:22px}
}
@media (forced-colors:active) {
 .caldav-shell[data-custom=true] .kal-special-markers > span,.caldav-shell[data-custom=true] .caldav-preview {forced-color-adjust:auto;border:1px solid CanvasText}
 .caldav-page .kal-special-month td[data-selected=true],.caldav-page .kal-view-tabs button[aria-selected=true],.caldav-page .kal-mode-buttons button[aria-pressed=true] {outline:2px solid Highlight}
 .caldav-page .kal-special-bar button[aria-pressed=true] {outline:2px solid Highlight}
}
`

function SpecialSettings({ ctx, ownerId, current, clear }) {
  const [mediaOpen, setMediaOpen] = useState(null)
  const [jobsOpen, setJobsOpen] = useState(null)
  const [draft, setDraft] = useState(null)
  const [saving, setSaving] = useState(null)
  const [notice, setNotice] = useState(null)
  const saveLock = useRef(null)
  const enabled = !!ownerId && current()
  const mediaKey = enabled ? mediaOpen : null, jobsKey = enabled ? jobsOpen : null
  useEffect(() => {
    saveLock.current = null
    setDraft(null); setSaving(null); setNotice(null)
    return () => { saveLock.current = null }
  }, [jobsKey])
  const open = set => { if (current()) set(before => (before || 0) + 1) }
  const read = path => () => ctx.rest(path, { method: 'POST', timeoutMs: 10000, body: { owner_id: ownerId } })
  const verify = valid => verifySpecialOwner(() => ctx.rest('/status', { timeoutMs: 15000 }), valid, ownerId, clear)
  const feeds = useSpecialRead(mediaKey, current, read('/special/readiness'), verify, ownerId, { feeds: {} })
  const jobs = useSpecialRead(jobsKey, current, read('/special/automation-settings'), verify, ownerId, { state: 'unavailable' })
  const selected = draft?.key === jobsKey ? draft.ids : jobs?.selected || []
  const busy = saving === jobsKey && !!jobsKey
  const save = async () => {
    if (!jobsKey || !current() || saveLock.current || jobs?.state !== 'loaded') return
    const attempt = {}
    saveLock.current = attempt
    const valid = () => saveLock.current === attempt && current()
    setSaving(jobsKey)
    try {
      let result
      try {
        result = await ctx.rest('/special/automation-settings', { method: 'PUT', timeoutMs: 10000, body: { owner_id: ownerId, ids: selected } })
      } catch (_) { /* Verify the owner before showing even a generic failure. */ }
      if (!await verify(valid) || !valid()) return
      if (result?.owner_id === ownerId && result.state === 'loaded' && result.ok === true && Array.isArray(result.selected)) {
        setDraft({ key: jobsKey, ids: result.selected })
        setNotice({ key: jobsKey, text: 'Automation selection saved.' })
      } else setNotice({ key: jobsKey, text: 'Could not save automation selection.' })
    } finally {
      if (saveLock.current === attempt) {
        saveLock.current = null
        if (current()) setSaving(null)
      }
    }
  }
  return jsxs('section', { children: [
    jsx('h2', { children: 'Special views' }),
    jsx('h3', { children: 'Media feeds' }),
    jsx('p', { children: 'Enter the Sonarr iCalendar URL and Radarr iCalendar URL separately in Hermes Agent settings. Enable only the feeds you want. Unencrypted HTTP is a separate opt-in for a trusted private LAN only.' }),
    jsx(Button, { variant: 'secondary', onClick: () => host.navigate(ACCOUNT_SETUP_HREF), children: 'Open Agent settings for media feeds' }),
    jsx(Button, { variant: 'secondary', disabled: !enabled, onClick: () => open(setMediaOpen), children: 'Check media setup' }),
    mediaKey && jsx('ul', { 'aria-live': 'polite', children: Object.entries(MEDIA_LABELS).map(([key, label]) => jsx('li', {
      children: `${label}: ${specialState(feeds ? feeds.feeds?.[key] || 'unavailable' : 'loading')}`
    }, key)) }),
    jsx('h3', { children: 'Automations' }),
    jsx('p', { children: 'Choose jobs from this backend profile. Only their next scheduled run is shown, not run history or completion.' }),
    jsx(Button, { variant: 'secondary', disabled: !enabled || busy, onClick: () => open(setJobsOpen), children: 'Load automation checklist' }),
    jobsKey && (jobs?.state === 'loaded' ? jsxs('div', { children: [
      jsx('fieldset', { className: 'kal-special-jobs', children: [
        jsx('legend', { children: 'Jobs to show' }),
        !jobs.jobs?.length && jsx('p', { children: 'No jobs in this profile.' }),
        ...jobs.jobs.map(row => jsxs('label', { children: [
          jsx('input', { type: 'checkbox', checked: selected.includes(row.id), disabled: busy || (!selected.includes(row.id) && selected.length >= 100),
            onChange: event => { if (current()) setDraft({ key: jobsKey, ids: event.target.checked ? [...selected, row.id] : selected.filter(id => id !== row.id) }) } }),
          `${row.title} · ${row.enabled ? 'Enabled' : 'Paused'}`,
          row.start && ` · Next: ${new Date(row.start).toLocaleString()}`
        ] }, row.id))
      ] }),
      jsx(Button, { variant: 'secondary', disabled: busy, onClick: save, children: 'Save automation selection' }),
      notice?.key === jobsKey && jsx('p', { role: 'status', children: notice.text })
    ] }) : jsx('p', { role: 'status', children: jobs ? 'Automation checklist unavailable.' : 'Loading automation checklist…' }))
  ] })
}

function SpecialPanel({ mode, clock, grid, selected, today, select, shift, retry, enabled,
  checking, readiness, mediaShown, setMediaShown, sonarr, radarr, automations }) {
  const media = mode === 'media'
  const replies = { sonarr, radarr }
  const rows = (media ? Object.keys(MEDIA_LABELS).flatMap(source => mediaShown[source] ? specialRows(replies[source], source) : [])
    : specialRows(automations, 'automations')).sort((a, b) => a.start.localeCompare(b.start) || a.title.localeCompare(b.title))
  const byDay = new Map()
  for (const row of rows) {
    const day = dateOnly(row.start) ? row.start : clock.key(new Date(row.start))
    if (!byDay.has(day)) byDay.set(day, [])
    byDay.get(day).push(row)
  }
  const status = state => checking ? 'loading' : !enabled ? 'unavailable' : state || 'loading'
  const feedState = key => readiness?.feeds?.[key] === 'ready'
    ? !mediaShown[key] ? 'hidden' : status(replies[key]?.state)
    : readiness ? status(readiness.feeds?.[key] || 'unknown') : status()
  const incomplete = media && Object.keys(MEDIA_LABELS).some(key =>
    mediaShown[key] && ['unavailable', 'unknown'].includes(feedState(key)))
  const healthy = media && Object.keys(MEDIA_LABELS).some(key => feedState(key) === 'loaded')
  const unknownSchedule = incomplete && !healthy
  const agendaRows = byDay.get(selected) || []
  const jobsState = status(automations?.state)
  const confirmedSchedule = media ? healthy && !incomplete && !Object.keys(MEDIA_LABELS).some(key => mediaShown[key] && feedState(key) === 'loading') : jobsState === 'loaded'
  const feedWaiting = media && Object.keys(MEDIA_LABELS).some(key => mediaShown[key] && feedState(key) === 'loading')
  const countLabel = media
    ? healthy ? `${agendaRows.length} ${agendaRows.length === 1 ? 'release' : 'releases'} · read only${incomplete || feedWaiting ? ' · partial results' : ''}`
      : unknownSchedule ? 'Release schedule unknown' : feedWaiting ? 'Checking feed status…' : 'No configured feeds are showing'
    : jobsState === 'loaded' ? `${agendaRows.length} ${agendaRows.length === 1 ? 'scheduled run' : 'scheduled runs'} · read only`
      : jobsState === 'loading' ? 'Checking scheduled runs…' : 'Scheduled runs unavailable'
  const emptyMessage = media
    ? unknownSchedule ? 'No release dates could be verified. Refresh feeds to try again.'
      : feedWaiting ? 'Waiting for feed results.' : !healthy ? 'Show a feed or check its setup in Settings.'
      : incomplete ? 'No releases shown for this day. An unavailable feed may still have releases.' : 'No releases on this day.'
    : jobsState === 'loaded' ? 'No selected jobs are scheduled for this day.'
      : jobsState === 'loading' ? 'Waiting for the Hermes backend.' : 'Check the Hermes connection and refresh Automations.'
  const dateLabel = day => clock.format(day, { weekday: 'long', month: 'long', day: 'numeric', year: 'numeric' })
  const nav = (label, name, action) => jsx('button', {
    type: 'button', className: 'caldav-nav', 'aria-label': label, title: label, disabled: !enabled, onClick: action,
    children: name ? jsx(ToolbarIcon, { name }) : label
  })
  const sourceLabel = key => ({ sonarr: 'TV release', radarr: 'Movie release', automations: 'Next scheduled run' }[key])
  return jsxs('section', { id: `kal-panel-${mode}`, role: 'tabpanel', 'aria-labelledby': `kal-tab-${mode}`, children: [
     jsxs('header', { className: 'caldav-toolbar', children: [
       jsxs('div', { className: 'kal-special-title', children: [
         jsx('h1', { className: 'caldav-month-title', children: clock.format(grid.first, { month: 'long', year: 'numeric' }) }),
         jsx('p', { children: media ? 'Release dates from your enabled feeds' : 'One upcoming run per selected enabled job' })
       ] }),
      jsxs('div', { className: 'caldav-navigation', role: 'group', 'aria-label': `${VIEW_LABELS[mode]} month navigation`, children: [
        nav('Previous month', 'previous', () => shift(-1)), nav('Today', null, () => select(today)), nav('Next month', 'next', () => shift(1))
      ] }),
      jsx('button', { type: 'button', className: 'caldav-nav', 'aria-label': `Refresh ${VIEW_LABELS[mode]}`, title: `Refresh ${VIEW_LABELS[mode]}`, disabled: !gatewayOpen() || checking, onClick: retry, children: jsx(ToolbarIcon, { name: 'refresh' }) }),
     ] }),
     incomplete && jsxs('div', { className: 'kal-special-warning', role: 'status', children: [
       jsx('strong', { children: `${Object.keys(MEDIA_LABELS).filter(key => mediaShown[key] && ['unavailable', 'unknown'].includes(feedState(key))).map(key => key === 'sonarr' ? 'Sonarr' : 'Radarr').join(' and ')} unavailable` }),
       jsx('span', { children: healthy ? 'Other release dates are still shown. Some dates may be missing.' : 'Some release dates may be missing.' }),
       jsx('button', { type: 'button', className: 'caldav-nav', onClick: retry, children: 'Refresh feeds' })
     ] }),
     jsx('div', { className: 'caldav-layout kal-special-layout', children: [
     jsxs('div', { className: 'kal-special-grid', children: [
     jsxs('table', { className: 'caldav-month kal-special-month', 'aria-label': `${VIEW_LABELS[mode]} · ${clock.format(grid.first, { month: 'long', year: 'numeric' })}`, children: [
       jsx('thead', { children: jsx('tr', { children: grid.days.slice(0, 7).map(day => jsx('th', { scope: 'col', children: clock.format(day, { weekday: 'short' }) }, clock.key(day))) }) }),
       jsx('tbody', { children: Array.from({ length: grid.days.length / 7 }, (_, week) => jsx('tr', { children: grid.days.slice(week * 7, week * 7 + 7).map(day => {
         const key = clock.key(day), entries = byDay.get(key) || []
         const sources = [...new Set(entries.map(row => row.source))]
         const index = grid.days.indexOf(day)
         return jsxs('td', { 'data-calendar-day': key, 'data-selected': key === selected, className: clock.parts(day)[1] !== grid.month ? 'caldav-adjacent' : '', children: [
           jsx('button', { type: 'button', className: 'caldav-date', tabIndex: key === selected ? 0 : -1,
             onKeyDown: event => {
               const offset = { ArrowLeft: -1, ArrowRight: 1, ArrowUp: -7, ArrowDown: 7, Home: -(index % 7), End: 6 - index % 7 }[event.key]
               if (offset === undefined) return
               event.preventDefault()
               select(clock.key(clock.add(day, offset)))
               requestAnimationFrame(() => document.querySelector(`#kal-panel-${mode} .caldav-date[tabindex="0"]`)?.focus())
             },
             'aria-label': `${dateLabel(day)}${key === today ? ', today' : ''}${key === selected ? ', selected' : ''}${confirmedSchedule || entries.length ? `, ${entries.length} ${entries.length === 1 ? 'item' : 'items'}${!confirmedSchedule ? ' shown' : ''}` : ', schedule not verified'}`,
             'aria-current': key === today ? 'date' : undefined, 'aria-pressed': key === selected,
             disabled: !enabled, onClick: () => select(key), children: clock.parts(day)[2] }),
           sources.length > 0 && jsx('div', { className: 'kal-special-markers', children: sources.map(source => {
             const entry = entries.find(item => item.source === source)
             return jsxs('span', { title: `${sourceLabel(source)} · ${entry.title}`, role: 'img', 'aria-label': sourceLabel(source),
               children: [jsx(SpecialIcon, { name: source === 'automations' ? 'lightning' : source, size: 16 }),
                 jsx('span', { className: 'kal-marker-title', 'aria-hidden': true, children: entry.title })]
             }, source)
           }) }),
           entries.length > sources.length && jsx('span', { className: 'kal-special-more', children: `+${entries.length - sources.length} more` })
         ] }, key)
       }) }, week)) })
     ] }),
     jsx('p', { className: 'kal-special-footnote', children: media ? 'Sonarr · TV episodes / Radarr · movie releases' : 'Hermes Cron · next recorded scheduled runs' })
     ] }),
     jsxs('section', { className: 'caldav-agenda kal-special-agenda', role: 'region', tabIndex: -1, 'aria-label': 'Selected day agenda', children: [
       jsx('h2', { children: dateLabel(clock.parse(selected)) }),
       jsx('p', { className: 'kal-special-agenda-count', children: countLabel }),
       jsxs('div', { className: 'kal-special-bar', children: [
       media && jsx('div', { className: 'kal-view-tabs', role: 'group', 'aria-label': 'Media filters', children: Object.entries(MEDIA_LABELS).map(([key, label]) => jsxs('button', {
         type: 'button', 'aria-label': `Show ${label}`, title: `Show ${label}`, 'aria-pressed': mediaShown[key],
         onClick: () => setMediaShown(before => ({ ...before, [key]: !before[key] })),
         children: [jsx('span', { 'aria-hidden': true, children: '●' }), key === 'sonarr' ? 'Sonarr' : 'Radarr']
       }, key)) }),
       jsx('a', { href: CALENDAR_SETTINGS_HREF, onClick: event => { event.preventDefault(); host.navigate(CALENDAR_SETTINGS_HREF) },
         children: media ? 'Manage feeds' : 'Choose automations' })
     ] }),
     jsx('div', { className: 'kal-special-status', role: 'status', children: media ?
       Object.entries(MEDIA_LABELS).map(([key, label]) => {
         return jsx('span', { children: `${label}: ${specialState(feedState(key), rows.filter(row => row.source === key).length)}` }, key)
       }) : `Next scheduled run only · ${specialState(jobsState, rows.length)}. Next recorded scheduled run only for selected enabled jobs.` }),
       agendaRows.length ? jsx('ul', { children: agendaRows.map(row => jsxs('li', { children: [
         jsx('span', { children: dateOnly(row.start) ? 'All day' : new Date(row.start).toLocaleTimeString(undefined, { timeZone: clock.zone, hour: 'numeric', minute: '2-digit' }) }),
         jsxs('span', { children: [jsx('strong', { children: row.title }), jsx('small', { children: row.source === 'automations' ? 'Hermes Cron · next scheduled run' : `${row.source === 'sonarr' ? 'Sonarr · TV episode' : 'Radarr · movie release'}` })] })
       ] }, `${row.source}:${row.id}`)) }) : jsx('p', { children: emptyMessage })
     ] })
     ] })
  ] })
}

export default {
  id: 'icloud-caldav',
  name: 'Kalends',
  defaultEnabled: false,
  register(ctx) {
    // Optional registration keeps the legacy Settings-only host seam usable.
    const lifecycle = new Set()
    const appearance = appearanceStore(ctx)
    ctx.onDispose?.(() => appearance.dispose())
    ctx.onDispose?.(() => { for (const dispose of [...lifecycle]) { dispose.clear(); dispose() } })
    if (ctx.register) {
      ctx.register({ id: 'calendar', area: ROUTES_AREA, title: 'Kalends', data: { path: '/icloud-caldav' }, render: () => jsx(AppearanceFrame, { ctx, lifecycle, store: appearance }) })
      ctx.register({ id: 'calendar-nav', area: SIDEBAR_NAV_AREA, data: { path: '/icloud-caldav', label: 'Kalends', codicon: 'calendar' } })
      ctx.register({ id: 'open-calendar', area: PALETTE_AREA, data: { id: 'open-calendar', label: 'Open Kalends', run: () => host.navigate('/icloud-caldav') } })
    }
    ctx.registerSettingsPage?.({
      id: 'settings',
      title: 'Kalends',
      icon: 'calendar',
      order: 0,
      render: () => jsx(AppearanceFrame, { ctx, lifecycle, store: appearance, settings: true })
    })
  }
}
