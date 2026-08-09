// GET /api/auth/login
// Entry point for the "Log in with Whop" button. Sends the user to
// Whop's hosted login/authorize screen, with a random "state" value
// we can check on the way back to prevent CSRF.

export async function onRequestGet({ request, env }) {
  const state = crypto.randomUUID();

  const authorizeUrl = new URL("https://whop.com/oauth");
  authorizeUrl.searchParams.set("client_id", env.WHOP_CLIENT_ID);
  authorizeUrl.searchParams.set("redirect_uri", env.WHOP_REDIRECT_URI);
  authorizeUrl.searchParams.set("response_type", "code");
  authorizeUrl.searchParams.set("state", state);

  const headers = new Headers();
  headers.set("Location", authorizeUrl.toString());
  // Short-lived cookie just to carry the state value to the callback -
  // not the session cookie, this one expires in 10 minutes.
  headers.append(
    "Set-Cookie",
    `gy_oauth_state=${state}; Path=/; Max-Age=600; HttpOnly; Secure; SameSite=Lax`
  );

  return new Response(null, { status: 302, headers });
}
