/**
 * Le Marathon Du Médoc 26 — Strava webhook receiver + OAuth exchange
 *
 * This Worker serves three routes:
 *
 *   1. GET  /  (or any path) with ?hub.mode=subscribe&hub.challenge=…
 *      — Strava's one-off subscription handshake. Echo back the challenge.
 *
 *   2. POST /  with Strava webhook payload
 *      — fired on activity/athlete events. Triggers a GitHub Actions rebuild.
 *
 *   3. POST /exchange  with {code: "..."}
 *      — connect.html calls this server-side to exchange an OAuth code for
 *      a refresh_token. The client_secret never leaves this Worker.
 *
 * Required environment bindings:
 *   STRAVA_CLIENT_ID    — plain var, the app's public client ID
 *   STRAVA_CLIENT_SECRET — secret, used for OAuth exchange server-side
 *   STRAVA_VERIFY_TOKEN — secret, random string for subscription handshake
 *   GITHUB_PAT          — secret, PAT with Actions:write on the dashboard repo
 *   GITHUB_REPO         — plain var, "owner/repo"
 *   ALLOWED_ORIGIN      — plain var, the dashboard's origin for CORS
 */

const CORS_BASE = {
  "Access-Control-Allow-Methods": "POST, OPTIONS",
  "Access-Control-Allow-Headers": "Content-Type",
  "Access-Control-Max-Age": "86400",
};

function corsHeaders(env) {
  return {
    "Access-Control-Allow-Origin": env.ALLOWED_ORIGIN || "https://fbmedoc.github.io",
    ...CORS_BASE,
  };
}

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);

    // ─── Access-code check (called before Strava authorise) ─────
    if (url.pathname === "/check-code") {
      if (request.method === "OPTIONS") {
        return new Response(null, { status: 204, headers: corsHeaders(env) });
      }
      if (request.method === "POST") {
        return handleAccessCodeCheck(request, env);
      }
      return new Response("Method not allowed", { status: 405 });
    }

    // ─── OAuth code exchange (server-side, keeps client_secret hidden) ─
    if (url.pathname === "/exchange") {
      if (request.method === "OPTIONS") {
        return new Response(null, { status: 204, headers: corsHeaders(env) });
      }
      if (request.method === "POST") {
        return handleOAuthExchange(request, env);
      }
      return new Response("Method not allowed", { status: 405 });
    }

    // ─── Strava subscription verification ────────────────────────
    if (request.method === "GET") {
      const mode      = url.searchParams.get("hub.mode");
      const token     = url.searchParams.get("hub.verify_token");
      const challenge = url.searchParams.get("hub.challenge");

      if (mode === "subscribe" && token === env.STRAVA_VERIFY_TOKEN && challenge) {
        return Response.json({ "hub.challenge": challenge });
      }

      // A bare GET (no params) is also useful for "is this Worker alive?" checks.
      if (!mode && !token && !challenge) {
        return new Response("Le Médoc 26 webhook receiver — online.", { status: 200 });
      }

      return new Response("Verification failed", { status: 403 });
    }

    // ─── Activity event ──────────────────────────────────────────
    if (request.method === "POST") {
      let body;
      try {
        body = await request.json();
      } catch {
        return new Response("Malformed JSON", { status: 400 });
      }

      // Basic shape check — Strava always sends these fields.
      if (!body.object_type || !body.aspect_type || !body.owner_id) {
        return new Response("Bad request", { status: 400 });
      }

      // We trigger a rebuild on TWO kinds of events:
      //   1. Any activity create / update / delete (object_type === "activity")
      //   2. An athlete deauthorising the app
      //      (object_type === "athlete" AND updates.authorized === "false")
      // The rebuild script's existing logic handles both — a deauthorised
      // athlete's token refresh will fail and they'll be marked disconnected
      // on the dashboard within ~60s.
      const isActivity = body.object_type === "activity";
      const isDeauth   = body.object_type === "athlete"
                      && body.aspect_type === "update"
                      && body.updates
                      && body.updates.authorized === "false";

      if (!isActivity && !isDeauth) {
        return new Response(`OK (ignored: ${body.object_type}/${body.aspect_type})`, { status: 200 });
      }

      // Strava expects sub-2-second responses. Fire the GitHub dispatch in
      // the background and acknowledge immediately.
      ctx.waitUntil(triggerRebuild(env, body));
      return new Response("OK", { status: 200 });
    }

    return new Response("Method not allowed", { status: 405 });
  },
};

/**
 * Validate the shared access code without performing any Strava action.
 * Used by connect.html before showing the Connect-with-Strava button, so
 * we don't burn Strava athlete-limit slots on people who don't have the code.
 */
async function handleAccessCodeCheck(request, env) {
  const cors = corsHeaders(env);
  const jsonHeaders = { "Content-Type": "application/json", ...cors };

  let body;
  try {
    body = await request.json();
  } catch {
    return new Response(JSON.stringify({ ok: false, error: "Malformed body" }), { status: 400, headers: jsonHeaders });
  }

  const supplied = (body.access_code || "").toString().trim().toLowerCase();
  const expected = (env.ACCESS_CODE || "").toString().trim().toLowerCase();

  if (expected && supplied === expected) {
    return new Response(JSON.stringify({ ok: true }), { status: 200, headers: jsonHeaders });
  }
  return new Response(JSON.stringify({ ok: false, error: "Wrong access code" }), { status: 401, headers: jsonHeaders });
}

/**
 * Server-side OAuth code → refresh_token exchange. The browser sends just the
 * Strava authorisation code; the Worker adds the client_secret (held here only)
 * and forwards to Strava. Returns the refresh_token plus a friendly athlete name.
 *
 * Also requires the shared access_code as defence-in-depth — even if someone
 * bypasses the client-side gate, they can't get a token without it.
 */
async function handleOAuthExchange(request, env) {
  const cors = corsHeaders(env);
  const jsonHeaders = { "Content-Type": "application/json", ...cors };

  let body;
  try {
    body = await request.json();
  } catch {
    return new Response(JSON.stringify({ error: "Malformed JSON body" }), { status: 400, headers: jsonHeaders });
  }

  // Defence-in-depth: also require the access code here.
  const supplied = (body.access_code || "").toString().trim().toLowerCase();
  const expected = (env.ACCESS_CODE || "").toString().trim().toLowerCase();
  if (!expected || supplied !== expected) {
    return new Response(JSON.stringify({ error: "Wrong or missing access code" }), { status: 401, headers: jsonHeaders });
  }

  if (!body.code || typeof body.code !== "string") {
    return new Response(JSON.stringify({ error: "Missing or invalid `code` field" }), { status: 400, headers: jsonHeaders });
  }

  if (!env.STRAVA_CLIENT_ID || !env.STRAVA_CLIENT_SECRET) {
    return new Response(JSON.stringify({ error: "Worker not configured: missing Strava credentials" }), { status: 500, headers: jsonHeaders });
  }

  try {
    const stravaResp = await fetch("https://www.strava.com/oauth/token", {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body: new URLSearchParams({
        client_id: env.STRAVA_CLIENT_ID,
        client_secret: env.STRAVA_CLIENT_SECRET,
        code: body.code,
        grant_type: "authorization_code",
      }).toString(),
    });

    const data = await stravaResp.json();

    if (!stravaResp.ok || !data.refresh_token) {
      console.error(`Strava exchange failed: ${stravaResp.status}`, data);
      return new Response(JSON.stringify({
        error: data.message || `Strava exchange failed (HTTP ${stravaResp.status})`,
        details: data,
      }), { status: stravaResp.status >= 500 ? 502 : 400, headers: jsonHeaders });
    }

    // Return only what the browser needs — never expose the access_token,
    // which the dashboard build will derive itself from the refresh_token.
    const athleteName = data.athlete
      ? `${data.athlete.firstname || ""} ${data.athlete.lastname || ""}`.trim()
      : null;

    return new Response(JSON.stringify({
      refresh_token: data.refresh_token,
      athlete_name: athleteName,
    }), { status: 200, headers: jsonHeaders });
  } catch (err) {
    console.error(`OAuth exchange error: ${err.message}`);
    return new Response(JSON.stringify({ error: err.message }), { status: 502, headers: jsonHeaders });
  }
}

/**
 * Call GitHub's repository_dispatch endpoint to kick off the dashboard rebuild.
 * The workflow listens for `event_type: "strava-webhook"`.
 */
async function triggerRebuild(env, payload) {
  const url = `https://api.github.com/repos/${env.GITHUB_REPO}/dispatches`;
  const body = {
    event_type: "strava-webhook",
    client_payload: {
      athlete_id:   payload.owner_id,
      object_type:  payload.object_type,
      object_id:    payload.object_id,
      aspect_type:  payload.aspect_type,
      event_time:   payload.event_time,
    },
  };

  try {
    const resp = await fetch(url, {
      method: "POST",
      headers: {
        "Authorization": `Bearer ${env.GITHUB_PAT}`,
        "Accept":        "application/vnd.github+json",
        "Content-Type":  "application/json",
        "User-Agent":    "medoc-26-webhook-worker",
      },
      body: JSON.stringify(body),
    });

    if (!resp.ok) {
      const text = await resp.text();
      console.error(`GitHub dispatch failed: ${resp.status} ${text}`);
    } else {
      console.log(`Triggered rebuild for athlete ${payload.owner_id}, activity ${payload.object_id} (${payload.aspect_type})`);
    }
  } catch (err) {
    console.error(`Trigger error: ${err.message}`);
  }
}
