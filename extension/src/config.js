// Settings shared by the service worker, the toolbar popup and the options page.

// The plain-HTTP listener `./start.sh` always runs (the bookmarklet bridge uses
// it too). A server deployment sets its public origin in the options page.
export const DEFAULT_BACKEND = "http://127.0.0.1:12359";

export function originOf(url) {
  try {
    const u = new URL(url);
    return u.protocol === "http:" || u.protocol === "https:" ? u.origin : null;
  } catch {
    return null;
  }
}

export async function getBackend() {
  const { backend } = await chrome.storage.sync.get("backend");
  return originOf(backend) || DEFAULT_BACKEND;
}

export async function setBackend(value) {
  const origin = originOf(value);
  if (!origin) throw new Error("Enter an http(s) origin, for example http://127.0.0.1:12359");
  await chrome.storage.sync.set({ backend: origin });
  return origin;
}

// Runtime-message bodies are JSON, so binary payloads cross between the relay
// and the service worker as base64.
export function bufToB64(buf) {
  const bytes = new Uint8Array(buf);
  let s = "";
  for (let i = 0; i < bytes.length; i += 0x8000) {
    s += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
  }
  return btoa(s);
}

export function b64ToBuf(b64) {
  const s = atob(b64);
  const bytes = new Uint8Array(s.length);
  for (let i = 0; i < s.length; i++) bytes[i] = s.charCodeAt(i);
  return bytes.buffer;
}
