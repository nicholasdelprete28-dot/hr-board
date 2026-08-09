// Runs before every request to the site. This is the real enforcement
// point - client-side checks can always be bypassed by viewing source,
// but this runs on Cloudflare's servers before any content is sent.

import { verifySessionToken, readCookie, COOKIE_NAME } from "./_utils.js";

// Paths that must stay reachable WITHOUT a valid session - the login
// flow itself, and the "please subscribe" page.
const PUBLIC_PATHS = [
  "/api/auth/login",
  "/api/auth/callback",
  "/subscribe.html",
  "/subscribe", // Cloudflare Pages auto-strips .html for "clean URLs" -
                // without this, /subscribe.html <-> /subscribe loops forever.
];

// File extensions that are safe to always allow (so the subscribe page
// itself can load its own CSS/images without a chicken-and-egg problem).
const PUBLIC_EXTENSIONS = [".css", ".svg", ".png", ".jpg", ".ico", ".woff2"];

export async function onRequest({ request, env, next }) {
  const url = new URL(request.url);

  if (PUBLIC_PATHS.includes(url.pathname)) return next();
  if (PUBLIC_EXTENSIONS.some((ext) => url.pathname.endsWith(ext))) return next();

  const token = readCookie(request, COOKIE_NAME);
  const session = await verifySessionToken(token, env.SESSION_SECRET);

  if (!session) {
    return Response.redirect(new URL("/subscribe", url.origin).toString(), 302);
  }

  return next();
}
