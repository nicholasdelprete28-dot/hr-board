// GET /api/auth/callback
// Whop redirects here after the user approves login, with ?code=... and
// ?state=... in the URL. We exchange the code for a token, confirm the
// user has an active Going Yard membership, then set our own session
// cookie and send them to the real site.
//
// NOTE: the exact endpoint paths below are best-known as of Aug 2026 -
// verify against the live docs at https://docs.whop.com/developer/guides/oauth
// if anything here starts failing. Deliberately NOT using the @whop/sdk
// npm package here - it's built for Node.js, and Cloudflare Workers runs
// a different, more limited JS runtime, so Node-only packages can crash
// at runtime even when the build succeeds. Plain fetch calls avoid that.

import { createSessionToken, buildSetCookie, readCookie, COOKIE_NAME } from "../../_utils.js";

// Your real Whop plan IDs (from the checkout links) - a membership to
// EITHER of these counts as "subscribed."
const VALID_PLAN_IDS = ["plan_LEQ37zyfLkFJz", "plan_KDslg6Hixequt"];
const ACTIVE_STATUSES = ["active", "trialing"];

export async function onRequestGet({ request, env }) {
  const url = new URL(request.url);

  try {
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
      const errText = await userInfoRes.text();
      return new Response(`Login failed: could not fetch user info. ${errText}`, { status: 502 });
    }
    const userInfo = await userInfoRes.json();
    const userId = userInfo.sub || userInfo.id;

    // 3. Check the allowlist first (comped/free access, no payment needed).
    let allowlisted = null;
    try {
      allowlisted = env.GY_ALLOWLIST ? await env.GY_ALLOWLIST.get(userId) : null;
    } catch (err) {
      allowlisted = null;
    }

    let hasAccess = Boolean(allowlisted);

    // 4. If not on the allowlist, check the user's real memberships using
    // their own OAuth token (plain fetch, no SDK).
    if (!hasAccess) {
      try {
        const membershipsRes = await fetch("https://api.whop.com/api/v1/memberships", {
          headers: { Authorization: `Bearer ${accessToken}` },
        });
        if (membershipsRes.ok) {
          const membershipsData = await membershipsRes.json();
          const memberships = membershipsData.data || membershipsData.memberships || membershipsData || [];
          hasAccess = Array.isArray(memberships) && memberships.some((m) => {
            const planId = m.plan_id || m.plan?.id || m.planId;
            const status = m.status;
            return VALID_PLAN_IDS.includes(planId) && ACTIVE_STATUSES.includes(status);
          });
        }
      } catch (err) {
        hasAccess = false;
      }
    }

    if (!hasAccess) {
      return Response.redirect(new URL("/subscribe", url.origin).toString(), 302);
    }

    // 5. Issue our own session cookie, valid for 7 days.
    const sessionToken = await createSessionToken(
      { userId, exp: Date.now() + 7 * 24 * 60 * 60 * 1000 },
      env.SESSION_SECRET
    );

    const headers = new Headers();
    headers.set("Location", "/");
    headers.append("Set-Cookie", buildSetCookie(COOKIE_NAME, sessionToken, 7 * 24 * 60 * 60));
    headers.append("Set-Cookie", "gy_oauth_state=; Path=/; Max-Age=0");

    return new Response(null, { status: 302, headers });
  } catch (err) {
    return new Response(`Login failed unexpectedly: ${err.message}`, { status: 500 });
  }
}
