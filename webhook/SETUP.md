# Webhook setup — one-time

End state: when any of your athletes finishes a run, Strava POSTs to your
Cloudflare Worker, which triggers the GitHub Actions rebuild. Dashboard
updates within ~60 seconds.

## 1. Cloudflare account + Wrangler CLI

If you already have a Cloudflare account, skip to step 1c.

**1a.** Sign up at <https://dash.cloudflare.com/sign-up> (free, no card required).

**1b.** Verify the email Cloudflare sends.

**1c.** Install the Wrangler CLI on your machine:

```powershell
npm install -g wrangler
```

(If you don't have Node.js installed: get it from <https://nodejs.org/> — pick "LTS".)

**1d.** Authenticate Wrangler with your Cloudflare account:

```powershell
wrangler login
```

A browser tab opens — sign in to Cloudflare and click "Allow". Terminal will show "Successfully logged in."

## 2. Generate the verify token

Pick a random string. Anything works — it's just a shared secret between you and Strava so others can't spoof the verification call.

```powershell
# Easy random string — copy this exact value, you'll need it twice
$verifyToken = "medoc26-" + [System.Guid]::NewGuid().ToString("N").Substring(0,16)
echo $verifyToken
```

Save the output somewhere — you'll paste it into both Wrangler and the Strava subscription command.

## 3. Generate a GitHub Personal Access Token (PAT)

This is what the Worker uses to trigger the GitHub Actions workflow.

1. Open <https://github.com/settings/personal-access-tokens/new>
2. Fill in:
   - **Token name:** `Medoc 26 webhook`
   - **Expiration:** *Custom* → pick something well past race day, e.g. `30 December 2026`
   - **Repository access:** *Only select repositories* → choose `fbmedoc/Marathon-Du-Medoc-Dashboard`
   - **Permissions** → *Repository permissions*:
     - **Contents:** Read and write
     - **Metadata:** Read-only (auto-selected)
     - **Actions:** Read and write
3. Click **Generate token**
4. **Copy the token immediately** (starts with `github_pat_...`) — GitHub won't show it again. Paste into a note for the next step.

## 4. Deploy the Worker

```powershell
cd C:\Users\bloem\Marathon-Du-Medoc-Dashboard\webhook
wrangler deploy
```

First time: Wrangler will ask to associate the project with your account. Say yes. After deploy completes, it prints the public URL — something like:

```
https://medoc-26-webhook.<your-account>.workers.dev
```

**Copy this URL** — you'll need it in step 6.

## 5. Set the Worker secrets

```powershell
# Paste the verify token from step 2 when prompted
wrangler secret put STRAVA_VERIFY_TOKEN

# Paste the GitHub PAT from step 3 when prompted
wrangler secret put GITHUB_PAT
```

Each command opens a hidden prompt; paste the value and hit Enter.

## 6. Subscribe to Strava webhooks

This is a one-time POST to Strava that registers your Worker's URL. Replace the placeholders with your actual values:

```powershell
$CLIENT_ID     = "243802"
$CLIENT_SECRET = "<your Strava client secret>"
$CALLBACK_URL  = "https://medoc-26-webhook.<your-account>.workers.dev"
$VERIFY_TOKEN  = "<the random string from step 2>"

curl.exe -X POST https://www.strava.com/api/v3/push_subscriptions `
  -F client_id=$CLIENT_ID `
  -F client_secret=$CLIENT_SECRET `
  -F callback_url=$CALLBACK_URL `
  -F verify_token=$VERIFY_TOKEN
```

If everything's right, Strava responds with:

```json
{"id": 12345}
```

That's your subscription ID. Strava also immediately sends a verification GET to your Worker — if the Worker responds correctly (it will, that's what `STRAVA_VERIFY_TOKEN` is for), the subscription becomes active.

If you get an error like `"already exists"`, you need to first delete the existing subscription (see "Managing subscriptions" below).

## 7. Test end-to-end

1. Go for a short run (or fake one in Strava's app).
2. Wait ~60 seconds.
3. Check <https://github.com/fbmedoc/Marathon-Du-Medoc-Dashboard/actions> — you should see a new "Daily dashboard rebuild" run triggered by `repository_dispatch`.
4. After it completes, the dashboard reflects your new run.

If nothing happens, see "Troubleshooting" below.

---

## Managing subscriptions

**List existing:**

```powershell
curl.exe "https://www.strava.com/api/v3/push_subscriptions?client_id=$CLIENT_ID&client_secret=$CLIENT_SECRET"
```

**Delete one:**

```powershell
curl.exe -X DELETE "https://www.strava.com/api/v3/push_subscriptions/<SUBSCRIPTION_ID>?client_id=$CLIENT_ID&client_secret=$CLIENT_SECRET"
```

You can only have one active subscription per app.

## Troubleshooting

**Worker not getting hit:**
- Open the Worker URL in your browser. You should see "Le Médoc 26 webhook receiver — online." If you don't, the deploy failed.

**Subscription failed (`callback_url verification failed`):**
- Make sure `STRAVA_VERIFY_TOKEN` is set as a Worker secret and matches what you passed to the subscribe POST. Use `wrangler tail` to watch live logs.

**Workflow not triggering:**
- Check Worker logs: `wrangler tail` then upload a fresh run.
- Likely cause: PAT is missing, expired, or doesn't have `Actions: write` permission.

**View live Worker logs:**

```powershell
cd C:\Users\bloem\Marathon-Du-Medoc-Dashboard\webhook
wrangler tail
```

This streams logs to your terminal — useful while testing.
