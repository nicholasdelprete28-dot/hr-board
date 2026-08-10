// Runs before every request to the site. This is the real enforcement
// point - client-side checks can always be bypassed by viewing source,
// but this runs on Cloudflare's servers before any content is sent.

import { verifySessionToken, readCookie, COOKIE_NAME } from "./_utils.js";

// Paths always reachable regardless of session state - the login flow
// itself must never redirect, or nobody could ever log in.
const AUTH_FLOW_PATHS = [
  "/api/auth/login",
  "/api/auth/callback",
  "/api/auth/password-login",
];

// Marketing/paywall pages that should redirect an ALREADY-logged-in
// person straight to the real site instead of showing them a pitch
// they don't need (e.g. someone clicks an old ad link while already
// subscribed). Unauthenticated visitors see the page normally.
const SMART_PUBLIC_PATHS = [
  "/subscribe.html",
  "/subscribe", // Cloudflare Pages auto-strips .html for "clean URLs" -
                // without this, /subscribe.html <-> /subscribe loops forever.
  "/landing.html",
  "/landing",
];

// Always-public data/assets - no session logic needed either way.
const ALWAYS_PUBLIC_PATHS = [
  "/history/accuracy_summary.json", // landing page fetches this for the live proof section
];

const PUBLIC_EXTENSIONS = [".css", ".svg", ".png", ".jpg", ".ico", ".woff2"];

export async function onRequest({ request, env, next }) {
  const url = new URL(request.url);

  if (AUTH_FLOW_PATHS.includes(url.pathname)) return next();
  if (ALWAYS_PUBLIC_PATHS.includes(url.pathname)) return next();
  if (PUBLIC_EXTENSIONS.some((ext) => url.pathname.endsWith(ext))) return next();

  const token = readCookie(request, COOKIE_NAME);
  const session = await verifySessionToken(token, env.SESSION_SECRET);

  if (SMART_PUBLIC_PATHS.includes(url.pathname)) {
    // Already logged in? Skip the pitch, go straight to the real site.
    if (session) {
      return Response.redirect(new URL("/", url.origin).toString(), 302);
    }
    return next();
  }

  if (!session) {
    // Default landing spot for anyone without a session - the persuasive
    // pitch page, not the bare pricing page. /subscribe is still directly
    // reachable (linked from here), just no longer the automatic first stop.
    return Response.redirect(new URL("/landing", url.origin).toString(), 302);
  }

  return next();
}
