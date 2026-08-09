// GET /api/auth/login
// Entry point for the "Log in with Whop" button. Sends the user to
// Whop's hosted login/authorize screen, using PKCE (required by Whop's
// OAuth implementation) plus a "state" value to prevent CSRF.

import { generateCodeVerifier, generateCodeChallenge } from "../../_utils.js";

export async function onRequestGet({ request, env }) {
  const state = crypto.randomUUID();
  const nonce = crypto.randomUUID();
  const codeVerifier = generateCodeVerifier();
  const codeChallenge = await generateCodeChallenge(codeVerifier);

  const authorizeUrl = new URL("https://api.whop.com/oauth/authorize");
  authorizeUrl.searchParams.set("client_id", env.WHOP_CLIENT_ID);
  authorizeUrl.searchParams.set("redirect_uri", env.WHOP_REDIRECT_URI);
  authorizeUrl.searchParams.set("response_type", "code");
  authorizeUrl.searchParams.set("state", state);
  authorizeUrl.searchParams.set("code_challenge", codeChallenge);
  authorizeUrl.searchParams.set("code_challenge_method", "S256");
  // Whop requires a scope param - "openid profile email" covers basic
  // login/identity. If the memberships check later fails to authorize,
  // check the "View available scopes" button on Whop's OAuth settings
  // page for a Whop-specific scope (e.g. something membership-related)
  // that may need adding here too.
  authorizeUrl.searchParams.set("scope", "openid profile email");
  authorizeUrl.searchParams.set("nonce", nonce);

  const headers = new Headers();
  headers.set("Location", authorizeUrl.toString());
  // Short-lived cookies just to carry state + verifier to the callback -
  // both expire in 10 minutes, separate from the real session cookie.
  headers.append(
    "Set-Cookie",
    `gy_oauth_state=${state}; Path=/; Max-Age=600; HttpOnly; Secure; SameSite=Lax`
  );
  headers.append(
    "Set-Cookie",
    `gy_pkce_verifier=${codeVerifier}; Path=/; Max-Age=600; HttpOnly; Secure; SameSite=Lax`
  );

  return new Response(null, { status: 302, headers });
}
