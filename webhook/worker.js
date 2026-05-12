/**
 * Le Marathon Du Médoc 26 — Strava webhook receiver
 *
 * Strava sends two kinds of requests to this Worker:
 *
 *   1. GET  ?hub.mode=subscribe&hub.challenge=…&hub.verify_token=…
 *      — one-off subscription handshake. Echo back the challenge.
 *
 *   2. POST { "object_type": "activity", "aspect_type": "create"/"update"/"delete",
 *             "owner_id": ..., "object_id": ..., ... }
 *      — fired every time any connected athlete creates/edits/deletes an activity.
 *      We respond 200 immediately and fire off a GitHub Actions repository_dispatch
 *      to rebuild the dashboard.
 *
 * Required environment bindings (set via wrangler secrets):
 *   STRAVA_VERIFY_TOKEN — random string we choose and pass to Strava at subscribe time
 *   GITHUB_PAT          — fine-grained PAT with Contents:write + Actions:write on the repo
 *   GITHUB_REPO         — "fbmedoc/Marathon-Du-Medoc-Dashboard" (plain var, not secret)
 */

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);

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

      // We only care about activities — ignore athlete deauth events for now.
      if (body.object_type !== "activity") {
        return new Response("OK (ignored: non-activity event)", { status: 200 });
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
