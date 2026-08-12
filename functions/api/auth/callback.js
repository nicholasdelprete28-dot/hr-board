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
//
// INCIDENT NOTE (Aug 12, 2026): a customer with a CONFIRMED active
// membership (verified directly in the Whop dashboard) was being denied
// access. Step 4 below (the membership check) was the only step in this
// file that did NOT return debug info on an unexpected result - every
// other step (token exchange, userinfo) already did. Added the same
// level of visibility here: if the memberships call fails, or succeeds
// but produces no access, the response now shows the raw data instead of
// silently redirecting to /subscribe?denied=1. This is almost certainly
// a field-name or response-shape mismatch in the untested guesses below
// (planId/status keys, or the shape of membershipsData itself) - the
// debug output will show exactly which.

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
    const codeVerifier = readCookie(request, "gy_pkce_verifier");

    // Whop may bounce back immediately with its own error instead of a
    // code, if something's misconfigured on the app/authorize side -
    // check for that FIRST, since it's more specific than our generic
    // state-mismatch message below.
    const whopError = url.searchParams.get("error");
    const whopErrorDesc = url.searchParams.get("error_description");
    if (whopError) {
      return new Response(`Whop rejected the login attempt: ${whopError} - ${whopErrorDesc || "(no description given)"}`, { status: 200 });
    }

    if (!code || !state || state !== savedState) {
      return new Response(
        `Login failed: invalid or expired state. Please try again.\n\n` +
        `--- debug info ---\n` +
        `code present: ${Boolean(code)}\n` +
        `state present: ${Boolean(state)}\n` +
        `savedState (cookie) present: ${Boolean(savedState)}\n` +
        `states match: ${state === savedState}`,
        { status: 200 }
      );
    }
    if (!codeVerifier) {
      return new Response("Login failed: missing PKCE verifier. Please try logging in again from the start.", { status: 200 });
    }

    // 1. Exchange the authorization code for an access token.
    // Using form-urlencoded (not JSON) - Whop's own curl example for this
    // endpoint uses -d flags, which send form-urlencoded by default.
    const tokenBody = new URLSearchParams({
      code,
      client_id: env.WHOP_CLIENT_ID,
      client_secret: env.WHOP_CLIENT_SECRET,
      redirect_uri: env.WHOP_REDIRECT_URI,
      grant_type: "authorization_code",
      code_verifier: codeVerifier,
    });
    const tokenRes = await fetch("https://api.whop.com/oauth/token", {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body: tokenBody.toString(),
    });

    if (!tokenRes.ok) {
      const errText = await tokenRes.text();
      return new Response(
        `Login failed at token exchange: ${errText}\n\n` +
        `--- debug info ---\n` +
        `redirect_uri sent: ${env.WHOP_REDIRECT_URI}\n` +
        `code present: ${Boolean(code)}\n` +
        `code_verifier present: ${Boolean(codeVerifier)}\n` +
        `code_verifier length: ${codeVerifier ? codeVerifier.length : 0}`,
        { status: 200 }
      );
    }
    const tokenData = await tokenRes.json();
    const accessToken = tokenData.access_token;

    // 2. Look up who this user is.
    const userInfoRes = await fetch("https://api.whop.com/oauth/userinfo", {
      headers: { Authorization: `Bearer ${accessToken}` },
    });
    if (!userInfoRes.ok) {
      const errText = await userInfoRes.text();
      return new Response(`Login failed: could not fetch user info. ${errText}`, { status: 200 });
    }
    const userInfo = await userInfoRes.json();
    const userId = userInfo.sub || userInfo.id;

    // 3. Check the allowlist first (comped/free access, no payment needed).
    // Checks BOTH the Whop user ID and the account email as keys - the
    // user ID is the "real" key this was designed around, but it's not
    // always easy to find quickly from the Whop dashboard UI, and email
    // is right there on every user/payment page. Either key working
    // means one less place to get stuck during an active incident.
    const userEmail = userInfo.email || null;
    let allowlisted = null;
    try {
      if (env.GY_ALLOWLIST) {
        allowlisted = await env.GY_ALLOWLIST.get(userId);
        if (!allowlisted && userEmail) {
          allowlisted = await env.GY_ALLOWLIST.get(userEmail.toLowerCase());
        }
      }
    } catch (err) {
      allowlisted = null;
    }

    let hasAccess = Boolean(allowlisted);

    // 4. If not on the allowlist, check the user's real memberships using
    // their own OAuth token (plain fetch, no SDK).
    //
    // INCIDENT FIX: this step now surfaces exactly what Whop returned
    // instead of silently falling through to hasAccess=false on any
    // unexpected shape or failure - see the incident note at the top of
    // this file. Once the real field-name/shape issue is identified and
    // fixed for good, this verbose branch can be trimmed back down, but
    // for now it only fires on the failure path (a real subscriber never
    // sees it - they hit the normal success path just like before).
    let membershipDebug = null;
    if (!hasAccess) {
      try {
        const membershipsRes = await fetch("https://api.whop.com/api/v1/memberships", {
          headers: { Authorization: `Bearer ${accessToken}` },
        });

        if (!membershipsRes.ok) {
          const errText = await membershipsRes.text();
          return new Response(
            `Login failed: the memberships lookup itself failed (this is different from ` +
            `"no active membership" - this means Whop rejected or errored on the request).\n\n` +
            `--- debug info ---\n` +
            `HTTP status: ${membershipsRes.status}\n` +
            `response body: ${errText}\n` +
            `userId: ${userId}`,
            { status: 200 }
          );
        }

        const membershipsData = await membershipsRes.json();
        const memberships = membershipsData.data || membershipsData.memberships || membershipsData || [];

        hasAccess = Array.isArray(memberships) && memberships.some((m) => {
          const planId = m.plan_id || m.plan?.id || m.planId;
          const status = m.status;
          return VALID_PLAN_IDS.includes(planId) && ACTIVE_STATUSES.includes(status);
        });

        if (!hasAccess) {
          // Capture what we actually saw, so if this really is a
          // no-access case (canceled, wrong product, etc.) OR a
          // field-name mismatch, both are immediately visible instead
          // of indistinguishable.
          membershipDebug =
            `--- debug info ---\n` +
            `userId: ${userId}\n` +
            `is memberships an array: ${Array.isArray(memberships)}\n` +
            `membership count: ${Array.isArray(memberships) ? memberships.length : "n/a"}\n` +
            `raw memberships data: ${JSON.stringify(memberships).slice(0, 3000)}\n` +
            `VALID_PLAN_IDS: ${JSON.stringify(VALID_PLAN_IDS)}\n` +
            `ACTIVE_STATUSES: ${JSON.stringify(ACTIVE_STATUSES)}`;
        }
      } catch (err) {
        return new Response(
          `Login failed: error while checking memberships: ${err.message}\n\n` +
          `--- debug info ---\n` +
          `userId: ${userId}`,
          { status: 200 }
        );
      }
    }

    if (!hasAccess) {
      // Show the debug info directly instead of silently redirecting to
      // /subscribe?denied=1 - a real subscriber getting bounced here with
      // zero explanation is exactly the incident that prompted this
      // change. Once the underlying cause is fixed, this can go back to
      // a clean redirect.
      return new Response(
        `Login failed: no active Going Yard membership found for this account.\n\n` +
        `If you HAVE paid and this is unexpected, this is very likely a bug in how ` +
        `we're reading Whop's membership data, not a real access issue - screenshot ` +
        `this whole page and send it over.\n\n` +
        (membershipDebug || "(no additional debug info captured)"),
        { status: 200 }
      );
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
    headers.append("Set-Cookie", "gy_pkce_verifier=; Path=/; Max-Age=0");

    return new Response(null, { status: 302, headers });
  } catch (err) {
    return new Response(`Login failed unexpectedly: ${err.message}`, { status: 200 });
  }
}
