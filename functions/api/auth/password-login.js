// POST /api/auth/password-login
// A separate bypass door, just for you - skips Whop entirely and checks
// a password you set yourself. Not linked anywhere public; only reachable
// if you type the URL or use the hidden form on subscribe.html.

import { createSessionToken, buildSetCookie, COOKIE_NAME } from "../../_utils.js";

async function sha256Hex(text) {
  const enc = new TextEncoder();
  const hashBuffer = await crypto.subtle.digest("SHA-256", enc.encode(text));
  return Array.from(new Uint8Array(hashBuffer))
    .map((b) => b.toString(16).padStart(2, "0"))
    .join("");
}

export async function onRequestPost({ request, env }) {
  const form = await request.formData();
  const password = form.get("password") || "";

  const hash = await sha256Hex(password);

  if (hash !== env.FOUNDER_PASSWORD_HASH) {
    // TEMPORARY debug page instead of a silent redirect - remove once
    // this is confirmed working.
    return new Response(
      `Password mismatch.\n\n` +
      `--- debug info ---\n` +
      `password received (length): ${password.length}\n` +
      `computed hash: ${hash}\n` +
      `env.FOUNDER_PASSWORD_HASH present: ${Boolean(env.FOUNDER_PASSWORD_HASH)}\n` +
      `env.FOUNDER_PASSWORD_HASH value: ${env.FOUNDER_PASSWORD_HASH}\n` +
      `hashes match: ${hash === env.FOUNDER_PASSWORD_HASH}`,
      { status: 200 }
    );
  }

  const sessionToken = await createSessionToken(
    { userId: "founder-bypass", exp: Date.now() + 30 * 24 * 60 * 60 * 1000 }, // 30 days
    env.SESSION_SECRET
  );

  const headers = new Headers();
  headers.set("Location", "/");
  headers.append("Set-Cookie", buildSetCookie(COOKIE_NAME, sessionToken, 30 * 24 * 60 * 60));

  return new Response(null, { status: 302, headers });
}
