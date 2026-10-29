import '@testing-library/jest-dom';
import { configure } from '@testing-library/react';
import { vi } from 'vitest';

// The suite runs several times slower under v8 coverage with a full worker
// pool (and on a busy shared runner). RTL's default 1s async-util timeout and
// vitest's 5s test timeout then make multi-step interaction tests flake.
// Widen both -- a passing assertion still returns immediately, so this only
// changes how long a genuinely failing test waits before it reports.
configure({ asyncUtilTimeout: 5000 });
vi.setConfig({ testTimeout: 20000 });

/**
 * Guarantees a working localStorage/sessionStorage under every Node major.
 * Node 25+ ships its own experimental Web Storage global that, without
 * --localstorage-file, shadows the jsdom one and resolves to undefined, so any
 * localStorage.clear() throws. Installs an in-memory Storage only when the
 * global is unusable, leaving jsdom's real implementation alone otherwise.
 */
function ensureStorage(name) {
  let usable = false;
  try {
    usable = typeof globalThis[name]?.clear === 'function';
  } catch {
    usable = false;
  }
  if (usable) return;
  const store = new Map();
  const methods = {
    getItem: (key) => (store.has(String(key)) ? store.get(String(key)) : null),
    setItem: (key, value) => void store.set(String(key), String(value)),
    removeItem: (key) => void store.delete(String(key)),
    clear: () => store.clear(),
    key: (i) => Array.from(store.keys())[i] ?? null,
  };
  // Proxy so stored keys are enumerable own properties, like a real Storage
  // (tests call Object.keys(localStorage)).
  const impl = new Proxy(methods, {
    get: (target, prop) => (prop === 'length' ? store.size : target[prop]),
    ownKeys: () => Array.from(store.keys()),
    has: (target, prop) => prop in target || store.has(prop),
    getOwnPropertyDescriptor: (target, prop) =>
      store.has(prop)
        ? { value: store.get(prop), enumerable: true, configurable: true, writable: true }
        : undefined,
  });
  Object.defineProperty(globalThis, name, { value: impl, configurable: true, writable: true });
}
ensureStorage('localStorage');
ensureStorage('sessionStorage');
