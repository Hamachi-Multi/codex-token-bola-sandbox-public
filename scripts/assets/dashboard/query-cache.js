import { getJSON } from './api.js';

export const QUERY_CACHE_MAX_ENTRIES = 100;
export const QUERY_CACHE_MAX_BYTES = 20 * 1024 * 1024;
export const QUERY_CACHE_TTL_MS = 10 * 60 * 1000;

export function canonicalQueryKey(path) {
  const url = new URL(path, 'http://dashboard.local');
  const entries = [...url.searchParams.entries()].sort(([leftKey, leftValue], [rightKey, rightValue]) => {
    const keyOrder = leftKey.localeCompare(rightKey);
    return keyOrder || leftValue.localeCompare(rightValue);
  });
  const query = new URLSearchParams(entries).toString();
  return `${url.pathname}${query ? `?${query}` : ''}`;
}

function payloadBytes(value) {
  const serialized = JSON.stringify(value);
  if (typeof TextEncoder === 'function') return new TextEncoder().encode(serialized).byteLength;
  return serialized.length * 2;
}

export function createQueryCache({
  fetcher = getJSON,
  maxEntries = QUERY_CACHE_MAX_ENTRIES,
  maxBytes = QUERY_CACHE_MAX_BYTES,
  ttlMs = QUERY_CACHE_TTL_MS,
  now = () => Date.now(),
} = {}) {
  const entries = new Map();
  const inFlight = new Map();
  let generation = null;
  let epoch = 0;
  let totalBytes = 0;

  function remove(key) {
    const entry = entries.get(key);
    if (!entry) return;
    totalBytes -= entry.bytes;
    entries.delete(key);
  }

  function trim() {
    while (entries.size > maxEntries || totalBytes > maxBytes) {
      const oldest = entries.keys().next().value;
      if (oldest === undefined) break;
      remove(oldest);
    }
  }

  function clear() {
    entries.clear();
    inFlight.clear();
    totalBytes = 0;
    epoch += 1;
  }

  function peek(path) {
    const key = canonicalQueryKey(path);
    const entry = entries.get(key);
    if (!entry) return { hit: false, data: undefined };
    if (entry.expiresAt <= now()) {
      remove(key);
      return { hit: false, data: undefined };
    }
    entries.delete(key);
    entries.set(key, entry);
    return { hit: true, data: entry.data };
  }

  function prime(path, data) {
    const key = canonicalQueryKey(path);
    const bytes = payloadBytes(data);
    remove(key);
    if (bytes > maxBytes) return false;
    entries.set(key, { data, bytes, expiresAt: now() + ttlMs });
    totalBytes += bytes;
    trim();
    return entries.has(key);
  }

  async function load(path) {
    const cached = peek(path);
    if (cached.hit) return cached.data;
    const key = canonicalQueryKey(path);
    if (inFlight.has(key)) return inFlight.get(key);
    const requestEpoch = epoch;
    const request = Promise.resolve(fetcher(path)).then(data => {
      if (requestEpoch === epoch) prime(path, data);
      return data;
    });
    inFlight.set(key, request);
    try {
      return await request;
    } finally {
      if (inFlight.get(key) === request) inFlight.delete(key);
    }
  }

  function prefetch(path) {
    return load(path).catch(() => undefined);
  }

  function setGeneration(nextGeneration) {
    const next = String(nextGeneration ?? '');
    if (generation === null) {
      generation = next;
      return false;
    }
    if (generation === next) return false;
    clear();
    generation = next;
    return true;
  }

  function stats() {
    return { entries: entries.size, bytes: totalBytes, inFlight: inFlight.size, generation };
  }

  return { clear, load, peek, prefetch, prime, setGeneration, stats };
}

export const analyticsQueryCache = createQueryCache();

export const clearAnalyticsQueryCache = () => analyticsQueryCache.clear();
export const getCachedJSON = path => analyticsQueryCache.load(path);
export const peekCachedJSON = path => analyticsQueryCache.peek(path);
export const prefetchJSON = path => analyticsQueryCache.prefetch(path);
export const primeCachedJSON = (path, data) => analyticsQueryCache.prime(path, data);
export const setAnalyticsCacheGeneration = generation => analyticsQueryCache.setGeneration(generation);
