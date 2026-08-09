// GET /api/auth/callback
// Whop redirects here after the user approves login, with ?code=... and
// ?state=... in the URL. We exchange the code for a token, confirm the
// user has an active Going Yard membership, then set our own session
// cookie and send them to the real site.
//
// NOTE: verify the exact token/userinfo endpoint paths against the live
// docs at https://docs.whop.com/developer/guides/oauth before relying on
// this in production - Whop's API surface has shifted across their doc
// pages, so treat the URLs below as "best known as of Aug 2026" rather
// than guaranteed-stable.

import { createSessionToken, buildSetCookie, readCookie, COOKIE_NAME } from "../../_utils.js";
import Whop from "@whop/sdk";

export async function onRequestGet({ request, env }) {
  const url = new URL(request.url);
  const code = url.searchParams.get("code");
  const state = url.searchParams.get("state");
  const savedState = readCookie(request, "gy_oauth_state");

  if (!code || !state || state !== savedState) {
    return new Response("Login failed: invalid or expired state. Please try again.", { status: 400 });
  }

  // 1. Exchange the authorization code for an access token.
  const tokenRes = await fetch("https://api.whop.com/oauth/token", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      code,
      client_id: env.WHOP_CLIENT_ID,
      client_secret: env.WHOP_CLIENT_SECRET,
      redirect_uri: env.WHOP_REDIRECT_URI,
      grant_type: "authorization_code",
    }),
  });

  if (!tokenRes.ok) {
    const errText = await tokenRes.text();
    return new Response(`Login failed at token exchange: ${errText}`, { status: 502 });
  }
  const tokenData = await tokenRes.json();
  const accessToken = tokenData.access_token;

  // 2. Look up who this user is.
  const userInfoRes = await fetch("https://api.whop.com/oauth/userinfo", {
    headers: { Authorization: `Bearer ${accessToken}` },
  });
  if (!userInfoRes.ok) {
    return new Response("Login failed: could not fetch user info.", { status: 502 });
  }
  const userInfo = await userInfoRes.json();
  const userId = userInfo.sub || userInfo.id;

  // 3. Check the allowlist first (comped/free access, no payment needed).
  const allowlisted = await env.GY_ALLOWLIST.get(userId);

  let hasAccess = Boolean(allowlisted);

  // 4. If not on the allowlist, check real membership status via Whop's API.
  if (!hasAccess) {
    const client = new Whop({ apiKey: env.WHOP_API_KEY });
    const access = await client.users.checkAccess(env.WHOP_PRODUCT_ID, { id: userId });
    hasAccess = access.has_access === true || access.access_level === "customer";
  }

  if (!hasAccess) {
    // Redirect to a "you need to subscribe" page instead of the real site.
    return Response.redirect(new URL("/subscribe.html", url.origin).toString(), 302);
  }

  // 5. Issue our own session cookie, valid for 7 days.
  const sessionToken = await createSessionToken(
    { userId, exp: Date.now() + 7 * 24 * 60 * 60 * 1000 },
    env.SESSION_SECRET
  );

  const headers = new Headers();
  headers.set("Location", "/");
  headers.append("Set-Cookie", buildSetCookie(COOKIE_NAME, sessionToken, 7 * 24 * 60 * 60));
  // Clear the short-lived state cookie now that it's served its purpose.
  headers.append("Set-Cookie", "gy_oauth_state=; Path=/; Max-Age=0");

  return new Response(null, { status: 302, headers });
}
