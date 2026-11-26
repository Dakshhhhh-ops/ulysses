// Ulysses signing surface: Ed25519 keys that never leave this browser.
// Keys are generated with WebCrypto as non-extractable and kept in IndexedDB
// under the signer's name. Only the public key and signatures are sent.

const KEY_DB = "ulysses-signing-keys";

function openDb() {
  return new Promise((resolve, reject) => {
    const req = indexedDB.open(KEY_DB, 1);
    req.onupgradeneeded = () => req.result.createObjectStore("keys");
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
}

async function idbDo(mode, fn) {
  const db = await openDb();
  return new Promise((resolve, reject) => {
    const store = db.transaction("keys", mode).objectStore("keys");
    const req = fn(store);
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
}

const b64 = (buf) => btoa(String.fromCharCode(...new Uint8Array(buf)));

export function webCryptoReady() {
  return !!(window.crypto && crypto.subtle && window.indexedDB && window.isSecureContext);
}

export async function keyNames() {
  try { return await idbDo("readonly", (s) => s.getAllKeys()); } catch { return []; }
}

export async function publicKeyOf(name) {
  const kp = await idbDo("readonly", (s) => s.get(name));
  if (!kp) return null;
  const raw = await crypto.subtle.exportKey("raw", kp.publicKey);
  return "ed25519:" + b64(raw);
}

export async function createKey(name) {
  const kp = await crypto.subtle.generateKey({ name: "Ed25519" }, false, ["sign", "verify"]);
  await idbDo("readwrite", (s) => s.put(kp, name));
  return publicKeyOf(name);
}

export async function signText(name, text) {
  const kp = await idbDo("readonly", (s) => s.get(name));
  if (!kp) throw new Error(`This browser holds no signing key for ${name}. Open your enrolment link first.`);
  const sig = await crypto.subtle.sign({ name: "Ed25519" }, kp.privateKey, new TextEncoder().encode(text));
  return b64(sig);
}

export async function postJson(url, body) {
  const r = await fetch(url, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  });
  let data = null;
  try { data = await r.json(); } catch { data = { detail: r.statusText }; }
  if (!r.ok) {
    const msg = data && (data.detail || data.error) ? (typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail)) : r.statusText;
    throw new Error(msg);
  }
  return data;
}

export function say(el, text, kind = "info") {
  el.textContent = text;
  el.dataset.kind = kind;
  el.hidden = false;
}
