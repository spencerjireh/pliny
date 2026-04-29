const STORAGE_KEY = "pliny.api_key";
const UNAUTHORIZED_EVENT = "pliny:unauthorized";

export function loadApiKey(): string | null {
  try {
    return localStorage.getItem(STORAGE_KEY);
  } catch {
    return null;
  }
}

export function saveApiKey(key: string): void {
  localStorage.setItem(STORAGE_KEY, key);
}

export function clearApiKey(): void {
  localStorage.removeItem(STORAGE_KEY);
}

export function emitUnauthorized(): void {
  window.dispatchEvent(new CustomEvent(UNAUTHORIZED_EVENT));
}

export function onUnauthorized(handler: () => void): () => void {
  const listener = () => handler();
  window.addEventListener(UNAUTHORIZED_EVENT, listener);
  return () => window.removeEventListener(UNAUTHORIZED_EVENT, listener);
}
