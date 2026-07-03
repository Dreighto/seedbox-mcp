# Friend-request approval gate + attribution — design spec

Status: **spec, not yet built.** Blocked on the NASDOOM BFF's in-flight `nd-gated`
approval feature landing + committing (another agent's active work as of 2026-07-03).
Build this once that repo is clean.

## Goal (operator, 2026-07-03)

A friend asks the helper bot (@nasdoom_helperbot) for a title. Instead of it downloading
immediately, it should **wait as a pending item in the NASDOOM app**, tagged with **which
friend asked**, and only download **after the operator approves it in the app**. Movies and
TV, both. Approval handled on the NASDOOM side, not via Telegram.

## Verified constraints (learned the hard way this session — do not re-litigate)

1. **Jellyseerr cannot gate an API request.** There is exactly one Jellyseerr API key and it
   is the admin's (user id 1, GasDrawls, permission bit 2). _Every_ request created through
   the API auto-approves, no matter what. Tested directly: a request created on behalf of a
   freshly-imported friend user (permission bit 32 = REQUEST only, no auto-approve) still came
   back `status: 2` (approved), not pending. So the admin key's auto-approve wins over the
   on-behalf user's permissions. **There is no API path to a pending Jellyseerr request.**
2. **On-behalf attribution DOES work.** `POST /api/v1/request` with a `userId` sets
   `requestedBy` to that user. So Jellyseerr _can_ attribute, just not gate.
3. **Plex user import works without email.** Media server is Plex (`mediaServerType: 1`).
   `GET /api/v1/settings/plex/users` lists the 21 shared friends; `POST
/api/v1/user/import-from-plex {plexIds:[...]}` creates them as Jellyseerr users with
   permission 32. (Local-user creation via `POST /api/v1/user` is blocked — "Email
   notifications must be enabled".)
4. **The gate already exists in NASDOOM, for series.** `add.ts` gates large multi-season TV:
   adds UNMONITORED + tagged `nd-gated` + no search (`shouldGate('tv', size)`), so nothing
   grabs. `requests.ts::listGatedSeries()` surfaces `nd-gated` Sonarr series as
   `state: needs_approval`; `releaseGated(arrId, approve|decline)` monitors+searches or deletes.
   Route: `/v1/requests/gated/[arrId]/[action]`. Gaps vs this goal: **series only** (no Radarr
   movie gate) and **attributed to `'You'`** (hardcoded), not the requesting friend.

## The clean design (no Jellyseerr users needed)

Because the gate holds the title in Sonarr/Radarr (not Jellyseerr) until approval, and approval
just monitors+searches the arr entry, **attribution is pure NASDOOM-side data** — we do not need
Jellyseerr users or Plex import at all. Simpler, fewer moving parts.

### NASDOOM BFF changes

1. **Movie gate.** Mirror the TV gate for Radarr in `add.ts`: a `gated` add path that posts the
   movie UNMONITORED + tagged `nd-gated` (create the tag on Radarr via `gatedTagId('radarr')`) +
   no search. Add `listGatedMovies()` in `requests.ts` (Radarr `/api/v3/movie`, filter by the
   `nd-gated` tag), and extend `releaseGated()` to handle Radarr (approve: set `monitored:true`,
   drop tag, `MoviesSearch` command; decline: `DELETE /api/v3/movie/{id}?deleteFiles=true`).
2. **Requester attribution store.** A small JSON store (pattern: `data/push-tokens.json`), e.g.
   `data/gated-requests.json`, keyed by `"{service}:{arrId}"` → `{requestedBy, since}`. Written
   when a gated add is made on a friend's behalf; read by `listGatedSeries/Movies` to fill
   `requestedBy` (fall back to `'You'` when absent). Delete the entry on approve/decline.
3. **Add endpoint carries the requester.** `/v1/omni/add` (and `add.ts::AddOpts`) accept an
   optional `requestedBy` string and a `gated` flag. When the helper bot calls with
   `gated:true, requestedBy:"Alex"`, NASDOOM adds held + records the store entry.
4. **Docs/canon.** Update `nasdoom/docs/api-v1.md`: `/v1/omni/add` gains `gated` + `requestedBy`;
   document `listGatedMovies` + the movie gated route; note gated items carry `requestedBy`.

### Seedbox helper-bot changes (this repo, `tools/jellyseerr.py` + `telegram_bot_friend.py`)

1. Friend requests route through **NASDOOM's gated add** (`nasdoom_add` with `gated=true,
requestedBy=<friend display name>`), NOT `jellyseerr_request_add` (which auto-approves).
   Keep `jellyseerr_request_add` for any non-gated/operator path.
2. Friend identity: map Telegram `chat_id` → friend display name (extend the allowlist config
   from bare chat_ids to `{chat_id: name}`; the enrollment flow already adds chat_ids manually).
   The display name is all the gate needs — no Plex/Jellyseerr user mapping required.
3. The bot's reply reports the real gated result ("Sent to the owner to approve — I'll let you
   know") and never claims it's downloading. Single-step (per the request-fix already shipped).

## Non-goals / notes

- No Jellyseerr users, no Plex import, no email — the gate makes them unnecessary. (Plex import
  remains a valid fallback ONLY if per-user Jellyseerr attribution is ever wanted independently.)
- The operator approves in the NASDOOM app via the existing gated route; no Telegram approval.
- Interim behavior until this lands: friend requests still create real Jellyseerr requests that
  auto-approve (attributed to the operator). That is the current shipped state.
