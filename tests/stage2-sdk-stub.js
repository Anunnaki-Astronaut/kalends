// Explicit documented SDK surface, never connected to Hermes/Electron.
import { createElement as h } from 'react'
export const ROUTES_AREA = 'routes'
export const SIDEBAR_NAV_AREA = 'sidebar.nav'
export const PALETTE_AREA = 'palette'
// Synchronous plugin namespace, deliberately detached from browser site storage.
export function appearanceStorage(backing, writes) {
  return {
    get(key, fallback) {
      if (!['kalends.appearance.v1', 'kalends.presets.v1'].includes(key)) throw Error('Unexpected storage key');
      return backing[key] === undefined ? fallback : JSON.parse(backing[key]);
    },
    set(key, value) {
      if (key === 'kalends.presets.v1') {
        backing[key] = JSON.stringify(value);
        return;
      }
      if (key !== 'kalends.appearance.v1') throw Error('Unexpected storage key');
      if (JSON.stringify(Object.keys(value).sort()) !== JSON.stringify(['dark', 'light', 'mode', 'version'])) throw Error('Unexpected appearance shape');
      if (value.version !== 1 || !['desktop', 'system', 'light', 'dark'].includes(value.mode)) throw Error('Invalid appearance version/mode');
      for (const mode of ['light', 'dark']) {
        if (Object.keys(value[mode]).sort().join() !== 'accent,background,panel,selected,text,todayBackground,todayText') throw Error('Unexpected palette shape');
        if (!Object.values(value[mode]).every(v => /^#[0-9A-F]{6}$/.test(v))) throw Error('Invalid stored color');
      }
      backing[key] = JSON.stringify(value);
      writes.push(JSON.parse(backing[key]));
    }
  };
}
export const gatewayActivationEpoch = () => window.fixture.epoch
export const host = {
  navigate: path => window.fixture.navigation.push(path),
  notify: notice => window.fixture.notices.push(notice),
}
export function Button({ variant, size, children, ...props }) { return h('button', props, children) }
export function Checkbox({ checked, onCheckedChange, ...props }) {
  return h('input', { ...props, type: 'checkbox', checked: checked === true,
    onChange: event => onCheckedChange?.(event.target.checked) })
}
export function Codicon() { return null }
export function GlyphSpinner() { return h('span', { role: 'status' }, 'Loading') }
export function ErrorState({ title, description, children }) {
  return h('section', { role: 'alert' }, title, description, children)
}
export function EmptyState({ title, description, children }) {
  return h('section', {}, title, description, children)
}
export function Badge({ children }) { return h('span', {}, children) }
export function ScrollArea({ children, ...props }) { return h('div', props, children) }
export function Skeleton() { return h('span', { role: 'status' }, 'Loading') }
