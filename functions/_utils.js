/ Shared helpers used by every auth function.
// Cloudflare Pages Functions run on the Workers runtime, so Web Crypto
// (crypto.subtle) is available natively - no extra libraries needed.

const COOKIE_NAME = "gy_session";

// Turns a secret string into an HMAC signing key.
async function getKey(secret) {
  const enc = new TextEncoder();
  return crypto.subtle.importKey(
    "raw", enc.encode(secret),
    { name: "HMAC", hash: "SHA-256" },
    false, ["sign", "verify"]
  );
}

// Creates a signed session token: base64(payload).base64(signature)
// Payload is just { userId, exp } - keep it small, cookies have limits.
export async function createSessionToken(payload, secret) {
  const key = await getKey(secret);
  const enc = new TextEncoder();
  const payloadStr = JSON.stringify(payload);
  const payloadB64 = btoa(payloadStr);
  const sig = await crypto.subtle.sign("HMAC", key, enc.encode(payloadB64));
  const sigB64 = btoa(String.fromCharCode(...new Uint8Array(sig)));
  return `${payloadB64}.${sigB64}`;
}

// Verifies a token and returns the payload, or null if invalid/expired/tampered.
export async function verifySessionToken(token, secret) {
  if (!token || !token.includes(".")) return null;
  const [payloadB64, sigB64] = token.split(".");
  const key = await getKey(secret);
  const enc = new TextEncoder();
  const expectedSig = await crypto.subtle.sign("HMAC", key, enc.encode(payloadB64));
  const expectedSigB64 = btoa(String.fromCharCode(...new Uint8Array(expectedSig)));
  if (expectedSigB64 !== sigB64) return null; // tampered
  try {
    const payload = JSON.parse(atob(payloadB64));
    if (payload.exp && Date.now() > payload.exp) return null; // expired
    return payload;
  } catch {
    return null;
  }
}

export function readCookie(request, name) {
  const header = request.headers.get("Cookie") || "";
  const match = header.match(new RegExp(`(?:^|; )${name}=([^;]*)`));
  return match ? decodeURIComponent(match[1]) : null;
}

export function buildSetCookie(name, value, maxAgeSeconds) {
  // Secure + HttpOnly + SameSite=Lax: JS on the page can't read it (XSS
  // protection), and it's only sent on same-site/top-level navigations.
  return `${name}=${encodeURIComponent(value)}; Path=/; Max-Age=${maxAgeSeconds}; HttpOnly; Secure; SameSite=Lax`;
}

export function clearCookieHeader(name) {
  return `${name}=; Path=/; Max-Age=0; HttpOnly; Secure; SameSite=Lax`;
}

export { COOKIE_NAME };
