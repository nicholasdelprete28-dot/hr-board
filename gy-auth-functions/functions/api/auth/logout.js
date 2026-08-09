// GET /api/auth/logout
import { clearCookieHeader, COOKIE_NAME } from "../../_utils.js";

export async function onRequestGet({ request }) {
  const url = new URL(request.url);
  const headers = new Headers();
  headers.set("Location", "/subscribe.html");
  headers.set("Set-Cookie", clearCookieHeader(COOKIE_NAME));
  return new Response(null, { status: 302, headers });
}
