/**
 * Le Marathon Du Médoc 26 — OAuth proxy + access-code gate + cron pinger
 *
 * Each runner brings their own personal Strava app (because Strava's free
 * tier caps shared apps at 1 connected athlete). This Worker has three jobs
 * in the per-runner-app world:
 *
 *   1. POST /check-code     — validates the shared friend-group access code
 *                             before connect.html shows any Strava UI, so
 *                             random visitors can't even start the flow.
 *
 *   2. POST /exchange-personal
 *                           — proxies the runner's OAuth code → refresh_token
 *                             exchange. The client_secret IS the runner's own
 *                             personal one (sent in the request); the Worker
 *                             just relays to Strava because the Strava token
 *                             endpoint doesn't speak CORS to browsers.
 *
 *   3. scheduled() handler  — Cloudflare cron fires this every 5 minutes.
 *                             We POST to GitHub's repository_dispatch endpoint
 *                             with event_type "cron-refresh", which the
 *                             dashboard workflow listens for, so the dashboard
 *                             actually rebuilds every 5 min. GitHub's own
 *                             5-minute cron is throttled to 4-7 hour intervals
 *                             on free tier, hence driving it from here.
 *
 * The Worker also still answers Strava's webhook subscription handshake (GET
 * with hub.* params) — left in place in case we ever pivot back to a shared
 * app or one runner wants to set up their own webhook subscription pointed
 * here. The activity-event POST path triggers a GitHub repository_dispatch
 * if invoked, but with per-runner apps no one is subscribed to it by default.
 *
 * Required environment bindings:
 *   ACCESS_CODE         — secret, the shared friend-group code (e.g. medoc26)
 *   STRAVA_VERIFY_TOKEN — secret, random string for webhook handshake (unused
 *                         under per-runner-app architecture; safe to leave set)
 *   GITHUB_PAT          — secret, PAT with Actions:write on the dashboard repo
 *                         (used by both the webhook POST path AND the cron)
 *   GITHUB_REPO         — plain var, "owner/repo"
 *   ALLOWED_ORIGIN      — plain var, the dashboard's origin for CORS
 */

const CORS_BASE = {
  "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
  "Access-Control-Allow-Headers": "Content-Type",
  "Access-Control-Max-Age": "86400",
};

// Drinks tracker window: the self-select logger covers a rolling 7-day strip
// starting on this date. Logs are stored per-runner in the DRINKS KV namespace
// under key `drinks:<runner_id>` as a JSON map of { "YYYY-MM-DD": <int drinks> }.
const DRINK_WINDOW_START = "2026-06-01";

function corsHeaders(env) {
  return {
    "Access-Control-Allow-Origin": env.ALLOWED_ORIGIN || "https://fbmedoc.github.io",
    ...CORS_BASE,
  };
}

export default {
  /**
   * Scheduled trigger — fires per the cron in wrangler.toml ([triggers]).
   * Pings GitHub repository_dispatch so the dashboard workflow rebuilds.
   * GitHub Actions' own 5-minute cron is unreliable (delayed by hours under
   * load); Cloudflare's cron is honoured to the minute, so we drive the
   * refresh cadence from here instead.
   */
  async scheduled(event, env, ctx) {
    ctx.waitUntil(triggerRebuild(env, "cron-refresh", {
      cron:     event.cron,
      fired_at: new Date(event.scheduledTime).toISOString(),
      source:   "cloudflare-cron",
    }));
  },

  async fetch(request, env, ctx) {
    const url = new URL(request.url);

    // ─── Access-code check (called before any Strava UI is shown) ──
    if (url.pathname === "/check-code") {
      if (request.method === "OPTIONS") {
        return new Response(null, { status: 204, headers: corsHeaders(env) });
      }
      if (request.method === "POST") {
        return handleAccessCodeCheck(request, env);
      }
      return new Response("Method not allowed", { status: 405 });
    }

    // ─── Per-runner OAuth exchange (CORS-proxy to Strava) ───────────
    if (url.pathname === "/exchange-personal") {
      if (request.method === "OPTIONS") {
        return new Response(null, { status: 204, headers: corsHeaders(env) });
      }
      if (request.method === "POST") {
        return handlePersonalExchange(request, env);
      }
      return new Response("Method not allowed", { status: 405 });
    }

    // ─── Drinks tracker: log self-selected drinks (access-code gated) ─
    if (url.pathname === "/log-drinks") {
      if (request.method === "OPTIONS") {
        return new Response(null, { status: 204, headers: corsHeaders(env) });
      }
      if (request.method === "POST") {
        return handleLogDrinks(request, env);
      }
      return new Response("Method not allowed", { status: 405 });
    }

    // ─── Drinks tracker: read all logs (open; build script + logger UI) ─
    if (url.pathname === "/drinks-data") {
      if (request.method === "OPTIONS") {
        return new Response(null, { status: 204, headers: corsHeaders(env) });
      }
      if (request.method === "GET") {
        return handleDrinksData(request, env);
      }
      return new Response("Method not allowed", { status: 405 });
    }

    // ─── Strava webhook subscription handshake (vestigial) ─────────
    if (request.method === "GET") {
      const mode      = url.searchParams.get("hub.mode");
      const token     = url.searchParams.get("hub.verify_token");
      const challenge = url.searchParams.get("hub.challenge");

      if (mode === "subscribe" && token === env.STRAVA_VERIFY_TOKEN && challenge) {
        return Response.json({ "hub.challenge": challenge });
      }

      // Bare GET = liveness probe.
      if (!mode && !token && !challenge) {
        return new Response("Le Médoc 26 Worker — online (per-runner-app mode).", { status: 200 });
      }

      return new Response("Verification failed", { status: 403 });
    }

    // ─── Strava webhook event (vestigial) ──────────────────────────
    if (request.method === "POST") {
      let body;
      try {
        body = await request.json();
      } catch {
        return new Response("Malformed JSON", { status: 400 });
      }
      if (!body.object_type || !body.aspect_type || !body.owner_id) {
        return new Response("Bad request", { status: 400 });
      }

      const isActivity = body.object_type === "activity";
      const isDeauth   = body.object_type === "athlete"
                      && body.aspect_type === "update"
                      && body.updates
                      && body.updates.authorized === "false";

      if (!isActivity && !isDeauth) {
        return new Response(`OK (ignored: ${body.object_type}/${body.aspect_type})`, { status: 200 });
      }
      ctx.waitUntil(triggerRebuild(env, "strava-webhook", {
        athlete_id:   body.owner_id,
        object_type:  body.object_type,
        object_id:    body.object_id,
        aspect_type:  body.aspect_type,
        event_time:   body.event_time,
      }));
      return new Response("OK", { status: 200 });
    }

    return new Response("Method not allowed", { status: 405 });
  },
};

/**
 * Validate the shared access code. No Strava action — purely a gate to
 * stop random visitors progressing to the rest of connect.html.
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
  const expected = (env.ACCESS_CODE   || "").toString().trim().toLowerCase();

  if (expected && supplied === expected) {
    return new Response(JSON.stringify({ ok: true }), { status: 200, headers: jsonHeaders });
  }
  return new Response(JSON.stringify({ ok: false, error: "Wrong access code" }), { status: 401, headers: jsonHeaders });
}

/**
 * Proxy the OAuth `code` → `refresh_token` exchange for a runner using
 * their OWN personal Strava app credentials.
 *
 * Why this exists: Strava's /oauth/token endpoint doesn't include CORS
 * headers, so a browser can't POST to it directly. This Worker stands in
 * the middle and forwards the call. The Worker doesn't store the runner's
 * client_secret anywhere — it just passes it through to Strava.
 *
 * Defence-in-depth: also requires the shared access_code, so even if
 * someone bypasses the client-side gate they can't burn through this
 * endpoint.
 */
async function handlePersonalExchange(request, env) {
  const cors = corsHeaders(env);
  const jsonHeaders = { "Content-Type": "application/json", ...cors };

  let body;
  try {
    body = await request.json();
  } catch {
    return new Response(JSON.stringify({ error: "Malformed JSON body" }), { status: 400, headers: jsonHeaders });
  }

  // Access code gate (defence-in-depth).
  const supplied = (body.access_code || "").toString().trim().toLowerCase();
  const expected = (env.ACCESS_CODE   || "").toString().trim().toLowerCase();
  if (!expected || supplied !== expected) {
    return new Response(JSON.stringify({ error: "Wrong or missing access code" }), { status: 401, headers: jsonHeaders });
  }

  // Required runner-supplied fields.
  const code          = (body.code          || "").toString().trim();
  const clientId      = (body.client_id     || "").toString().trim();
  const clientSecret  = (body.client_secret || "").toString().trim();

  if (!code || !clientId || !clientSecret) {
    return new Response(JSON.stringify({
      error: "Missing one of: code, client_id, client_secret",
    }), { status: 400, headers: jsonHeaders });
  }

  // Light shape check on the credentials — Strava client_ids are numeric,
  // client_secrets are 40-char hex.
  if (!/^\d+$/.test(clientId)) {
    return new Response(JSON.stringify({ error: "client_id should be all digits" }), { status: 400, headers: jsonHeaders });
  }
  if (clientSecret.length < 20) {
    return new Response(JSON.stringify({ error: "client_secret looks too short — copy it again from Strava" }), { status: 400, headers: jsonHeaders });
  }

  try {
    const stravaResp = await fetch("https://www.strava.com/oauth/token", {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body: new URLSearchParams({
        client_id:     clientId,
        client_secret: clientSecret,
        code:          code,
        grant_type:    "authorization_code",
      }).toString(),
    });

    const data = await stravaResp.json();

    if (!stravaResp.ok || !data.refresh_token) {
      console.error(`Strava exchange failed: ${stravaResp.status}`, data);
      return new Response(JSON.stringify({
        error: data.message || `Strava exchange failed (HTTP ${stravaResp.status}). ` +
               `Most common cause: client_id / client_secret don't match the Strava ` +
               `app you authorised, or the code has already been used — start the ` +
               `Strava authorisation again from Step 2.`,
        details: data,
      }), { status: stravaResp.status >= 500 ? 502 : 400, headers: jsonHeaders });
    }

    const athleteName = data.athlete
      ? `${data.athlete.firstname || ""} ${data.athlete.lastname || ""}`.trim()
      : null;

    return new Response(JSON.stringify({
      refresh_token: data.refresh_token,
      athlete_name:  athleteName,
    }), { status: 200, headers: jsonHeaders });
  } catch (err) {
    console.error(`OAuth exchange error: ${err.message}`);
    return new Response(JSON.stringify({ error: err.message }), { status: 502, headers: jsonHeaders });
  }
}

/**
 * Log self-selected drinks for a runner.
 *
 * Access-code gated (same shared friend-group code as the OAuth flow), so
 * only people with the code can write. Stores one JSON blob per runner in
 * the DRINKS KV namespace under key `drinks:<runner_id>`, merging the
 * supplied day→count entries over whatever's already stored.
 *
 * Request body:
 *   {
 *     access_code: "medoc26",
 *     runner_id:   "louis",
 *     entries:     { "2026-06-01": 3, "2026-06-02": 0, ... }   // ints
 *   }
 *
 * Drink counts are clamped to 0..30 ints. Dates must be YYYY-MM-DD on/after
 * the DRINK_WINDOW_START — anything else is ignored.
 */
async function handleLogDrinks(request, env) {
  const cors = corsHeaders(env);
  const jsonHeaders = { "Content-Type": "application/json", ...cors };

  if (!env.DRINKS) {
    return new Response(JSON.stringify({ error: "DRINKS KV namespace not bound" }), { status: 500, headers: jsonHeaders });
  }

  let body;
  try {
    body = await request.json();
  } catch {
    return new Response(JSON.stringify({ error: "Malformed JSON body" }), { status: 400, headers: jsonHeaders });
  }

  // Access code gate.
  const supplied = (body.access_code || "").toString().trim().toLowerCase();
  const expected = (env.ACCESS_CODE   || "").toString().trim().toLowerCase();
  if (!expected || supplied !== expected) {
    return new Response(JSON.stringify({ error: "Wrong or missing access code" }), { status: 401, headers: jsonHeaders });
  }

  const runnerId = (body.runner_id || "").toString().trim();
  if (!/^[a-z0-9_]+$/i.test(runnerId)) {
    return new Response(JSON.stringify({ error: "Bad or missing runner_id" }), { status: 400, headers: jsonHeaders });
  }

  const entries = body.entries;
  if (!entries || typeof entries !== "object" || Array.isArray(entries)) {
    return new Response(JSON.stringify({ error: "entries must be an object of { date: count }" }), { status: 400, headers: jsonHeaders });
  }

  // Load existing blob, merge, write back.
  const key = `drinks:${runnerId}`;
  let stored = {};
  try {
    const raw = await env.DRINKS.get(key);
    if (raw) stored = JSON.parse(raw);
  } catch {
    stored = {};
  }

  const dateRe = /^\d{4}-\d{2}-\d{2}$/;
  let written = 0;
  for (const [day, val] of Object.entries(entries)) {
    if (!dateRe.test(day)) continue;
    if (day < DRINK_WINDOW_START) continue;
    let n = Math.round(Number(val));
    if (!Number.isFinite(n)) continue;
    n = Math.max(0, Math.min(30, n));
    stored[day] = n;
    written++;
  }

  try {
    await env.DRINKS.put(key, JSON.stringify(stored));
  } catch (err) {
    return new Response(JSON.stringify({ error: `KV write failed: ${err.message}` }), { status: 502, headers: jsonHeaders });
  }

  return new Response(JSON.stringify({ ok: true, runner_id: runnerId, written, days: stored }), { status: 200, headers: jsonHeaders });
}

/**
 * Return all drinks logs as a single JSON object, keyed by runner_id:
 *   { "louis": { "2026-06-01": 3, ... }, "matt": { ... }, ... }
 *
 * Open (no access code) — the build script reads this server-side at build
 * time, and the logger page reads it to prefill. Nothing sensitive here.
 */
async function handleDrinksData(request, env) {
  const cors = corsHeaders(env);
  const jsonHeaders = { "Content-Type": "application/json", ...cors };

  if (!env.DRINKS) {
    // Graceful empty payload so the build script can no-op cleanly.
    return new Response(JSON.stringify({}), { status: 200, headers: jsonHeaders });
  }

  const out = {};
  try {
    let cursor;
    do {
      const listed = await env.DRINKS.list({ prefix: "drinks:", cursor });
      for (const k of listed.keys) {
        const runnerId = k.name.slice("drinks:".length);
        const raw = await env.DRINKS.get(k.name);
        if (raw) {
          try { out[runnerId] = JSON.parse(raw); } catch { /* skip bad blob */ }
        }
      }
      cursor = listed.list_complete ? undefined : listed.cursor;
    } while (cursor);
  } catch (err) {
    return new Response(JSON.stringify({ error: err.message }), { status: 502, headers: jsonHeaders });
  }

  return new Response(JSON.stringify(out), { status: 200, headers: jsonHeaders });
}

/**
 * Fire a GitHub repository_dispatch to rebuild the dashboard.
 *
 * Called from two paths:
 *   - scheduled() — every 5 min via Cloudflare cron, event_type "cron-refresh"
 *   - fetch()/webhook POST — on Strava activity events, event_type "strava-webhook"
 *
 * The workflow (daily.yml) is configured to listen for both types.
 */
async function triggerRebuild(env, eventType, payload) {
  const url = `https://api.github.com/repos/${env.GITHUB_REPO}/dispatches`;
  const body = {
    event_type:     eventType,
    client_payload: payload || {},
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
      console.error(`GitHub dispatch (${eventType}) failed: ${resp.status} ${text}`);
    } else {
      console.log(`Triggered rebuild (${eventType})`);
    }
  } catch (err) {
    console.error(`Trigger error (${eventType}): ${err.message}`);
  }
}
