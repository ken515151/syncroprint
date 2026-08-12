# AutoPrintr Wire Protocol — Phase 0 Extraction

Extracted 12 August 2026 from the three MIT-licensed RepairShopr repositories
(no AutoPrintr code is reused in this project; see NOTICE):

| Ref | Repo | Language | Role |
|---|---|---|---|
| `[WPF]` | github.com/RepairShopr/AutoPrintr-wpf | C#/WPF | newest client, multi-host (Syncro) support — **primary reference** |
| `[WIN]` | github.com/RepairShopr/AutoPrintr-win | C#/WinForms | older client; `docs/Develop.md` class docs |
| `[MAC]` | github.com/RepairShopr/AutoPrintr-mac | Objective-C | confirms constants; prints via `lp` like we do |

Citations are `repo-relative-path:line` in the respective repo at their current
HEADs (WPF `84f3872`).

---

## 1. Overview — minimum viable protocol

```
POST https://admin.{host}/api/v1/sign_in            (email+password, form-encoded)   [legacy]
        → user_token, subdomain, locations_allowed[], default_location
GET  https://{subdomain}.{host}/api/v1/settings/printing?api_key={token}
        → messaging_channel, registers[]
CONNECT wss://ws-mt1.pusher.com/app/4a12d53c136a2d3dade7?protocol=7...
SUBSCRIBE {messaging_channel}                        (public channel, no auth endpoint)
BIND "print-job"
        → {document, type, file, location, register, autoprinted}
GET  {file}                                          (pre-signed URL, no auth header)
        → PDF → print
```

Everything else — location filtering, register routing, per-type enable/auto-print,
copy counts — is **client-side**. The server broadcasts every job on the account's
single channel; every connected client receives every job and decides locally.

## 2. Login flow

Two auth paths:

**a) Legacy email/password** (all three clients):
`POST https://admin.{host}/api/v1/sign_in`, `application/x-www-form-urlencoded`
body `email`, `password`, header `Accept: application/json`, 30 s timeout.
- `[WPF]` AutoPrintr.Core/Services/UserService.cs:60-66, ApiService.cs:28-45
- `[WIN]` AutoPrintr/modules/LoginServer.cs:11,69-90
- `[MAC]` Requests/LoginRequest.m:26-49 (note: mac uses plain `http://` — we always use HTTPS)

Response (JSON): `user_token`, `user_name`, `user_id`, `subdomain`,
`default_location` (int, nullable), `enable_multi_locations` (bool),
`locations_allowed` `[{id, name}]`, `admin`, `can_use_app`, `permissions`,
`two_factor_required` (parsed by mac, unhandled by all).
- `[WPF]` AutoPrintr.Core/Models/User.cs:6-31 · `[WIN]` modules/LoginResponse.cs:54-65 · `[MAC]` Models/User.m:4-26

**Multi-host support** `[WPF]`: hosts are tried in order `repairshopr.com`,
`syncromsp.com`; the first successful login wins and its host is stamped on the
account (AutoPrintr.Core/Services/AppSettings.cs:12-17, UserService.cs:29-49).
401 → "Failed to Authenticate" (`[WIN]` LoginServer.cs:124-133).

**b) Token flow (what we use for Syncro):** current Syncro docs
(docs.syncromsp.com/legacy/autoprinter-printing-in-syncro) say AutoPrinter
authenticates with subdomain + an API token generated from **Admin → App Center
→ AutoPrinter card** (token *type* matters; generic tokens don't work). That
token slots into the same `api_key` parameter of `settings/printing` (§3) that
the original used its session `user_token` for — so with a subdomain + card
token, **no sign_in call is needed at all**. This is SyncroPrint's primary auth
path; email/password login is kept only as a RepairShopr fallback.

## 3. Channel acquisition

`GET https://{subdomain}.{host}/api/v1/settings/printing?api_key={token}`
(token as query parameter, not a header).

Response: `{"messaging_channel": "<opaque>", "registers": [{"id", "name",
"location_id", "location_name"}], "error": null}` — a real sample is preserved
as a comment at `[WIN]` LoginServer.cs:186-202.
- `[WPF]` UserService.cs:51-58, Models/Channel.cs:6-16 · `[WIN]` LoginServer.cs:165-189 · `[MAC]` Requests/GetRegistersRequest.m:19-45

**The channel name has no client-side format** — it is an opaque server-issued
string, subscribed verbatim. It is a **public** channel: no client configures a
Pusher auth endpoint, and a `private-`/`presence-` name would make the WPF lib
throw (Authorizer is null). Account security = unguessable channel name + the
token needed to learn it.
- `[WPF]` JobsService.cs:463-491 · `[WIN]` LoginServer.cs:167-176, JobsServer.cs:70-77 · `[MAC]` PusherManager.m:55-57

The win client persists the channel string and treats "have channel" as
"logged in" across restarts (`[WIN]` loginTab.cs:292,434; Config.cs:28) — the
channel is long-lived, not per-session.

## 4. Pusher connection

- **App key: `4a12d53c136a2d3dade7`** — verbatim constant at `[WIN]`
  AutoPrintr/modules/Credentials.cs:11 (`SrvXT`); recovered from the shipped
  `[WPF]` `publish/.../AutoPrintr.Core.dll` string heap; `[MAC]` expects it as
  build-time `Pusher_KEY` in a gitignored `AppKeys.h` (README.md:16-25). It
  ships in every public binary — a public identifier, not a secret.
- **Host/cluster**: `[WPF]`'s vendored PusherClient 0.5.1 defaults to
  `ws-mt1.pusher.com` (cluster mt1, protocol 5) — and `Encrypted=false`, i.e.
  the newest official client runs **plaintext ws://**. `[MAC]`'s libPusher
  uses `wss://ws.pusherapp.com` (protocol 6, TLS). Same app either way.
  **SyncroPrint always uses TLS**: `wss://ws-mt1.pusher.com/app/{key}?protocol=7...`,
  falling back to `ws.pusherapp.com` on connect failure.
- **Protocol version**: originals use 5 (`[WPF]`) / 6 (`[MAC]`) / 0.3.0-era
  (`[WIN]`); we speak the current documented version 7 (fully backward-compatible
  for this usage: `pusher:connection_established` carries `activity_timeout`,
  `pusher:ping`/`pusher:pong` keepalive, data double-encoded as JSON strings).
- **Reconnect**: `[WIN]` sleeps 20 s on `unavailable` state (mainWin.cs:26,141-143);
  `[WPF]` warns after 5 consecutive reconnect waits (JobsService.cs:556-581);
  `[MAC]` auto-reconnects + a Reachability probe. We use exponential backoff
  1 s → 60 s with jitter (§spec 5.2).

## 5. Job payload — the `print-job` event

Exactly one event name is bound by all three clients: **`print-job`**
(`[WPF]` JobsService.cs:491-492 · `[WIN]` Jobs.cs:26 · `[MAC]` PusherManager.m:16).

Wire schema (6 keys):

| key | type | meaning |
|---|---|---|
| `document` | string, required | document type (§6) — the routing key |
| `type` | string | paper-size class: `Letter`, `Label`, `Receipt`, `Intake Form`, `Pop Drawer`, `Outtake Form` (spaces/hyphens stripped, case-insensitive — `[WPF]` DocumentSizeJsonConverter.cs:14-23) |
| `file` | string URL, required | the PDF; pre-signed/public |
| `location` | int, nullable | location id; null ⇒ every location prints it |
| `register` | int, nullable | POS register id; null ⇒ any register |
| `autoprinted` | bool, nullable→false | true = fired by a server automation trigger; false = a human clicked Print |

- `[WPF]` Models/Document.cs:7-34 · `[WIN]` Jobs.cs:212-239 (validation 40-93) · `[MAC]` Models/PrintJob.m:11-22

**There is no job id, no copies count, and no callback URL on the wire.**
`[WPF]` mints a local `Guid` per event (Job.cs:29-34); `[WIN]`'s id is a local
counter. Copies are a purely local per-type setting.

**SyncroPrint consequence — dedupe id**: we derive a stable job id as
`sha256(document_type + "|" + file_url)[:24]`. Pre-signed URLs are unique per
job event, so a redelivered event (Pusher retry, reconnect overlap) hashes
identically and is dropped by the SQLite unique-index gate, while two genuine
prints of the same document get distinct pre-signed URLs and distinct ids.

No test fixtures exist in any repo; the sample in `[WIN]` LoginServer.cs:186-202
covers only `settings/printing`.

## 6. Document type enum

`[WPF]` DocumentType.cs:3-18 (newest, 13 values — adds `OuttakeForm` over the
12 in `[WIN]` Printers.cs:123-137 / `[MAC]` PrintJob.m:24-40):

```
Invoice  Estimate  Ticket  IntakeForm  Receipt  ZReport  TicketReceipt
PopDrawer  Adjustment  CustomerID  Asset  TicketLabel  OuttakeForm
```

The wire value is matched **case-insensitively against the enum member name**
(no converter on the `document` field in WPF; exact-match in win with error log
on miss, Printers.cs:155-168). Values with spaces/underscores would fail in WPF.
SyncroPrint normalizes to lowercase and uses these as config routing keys:
`invoice estimate ticket intakeform receipt zreport ticketreceipt popdrawer
adjustment customerid asset ticketlabel outtakeform`.

(Beware `[MAC]` PrintJob.m:24-40: unknown types silently become Invoice — a
bug, not a spec. `[WIN]`/`[WPF]` skip unknown types; so do we.)

## 7. PDF retrieval

The `file` URL is fetched **verbatim and unauthenticated** — no Authorization
header, no api_key, no cookies in any client. It must be pre-signed/public.
- `[WPF]` FileService.cs:252-276 (bare WebClient, default 100 s timeout, **no retries**)
- `[WIN]` Jobs.cs:347-388 (bare WebClient async, no retry — failures just log)
- `[MAC]` PusherManager.m:113-125 (synchronous `dataWithContentsOfURL`, no timeout handling)

SyncroPrint hardens this (§spec 5.3): TLS verification on, HTTPS-only, host
allowlist, content-type check, 25 MB cap, 3 retries with backoff, 120 s
per-attempt timeout.

**Live finding (12 Aug 2026):** real job payloads carry `file` URLs on
**`pdf.repairshopr.com`** — a shared vendor PDF host, not the account's
subdomain, and RepairShopr-domained even for Syncro accounts (shared
backend). The allowlist therefore accepts subdomains of both product
domains (`repairshopr.com`, `syncromsp.com`) plus the account's own
subdomain — still never arbitrary hosts.

**Rotation quirk** `[WPF]` JobsService.cs:375-378: if a target printer has
Rotation on, the client appends `&orientation=portrait` to the file URL —
rotation is done **server-side** via query param (and assumes an existing query
string). SyncroPrint rotates locally via `lp -o orientation-requested=4`
instead, with the URL-param trick documented here as a fallback if a driver
misbehaves.

## 8. Register / cash drawer

A drawer pop is **not special-cased anywhere**: it is an ordinary `print-job`
with `document: "PopDrawer"` and a `file` PDF, printed like any other document;
the kick comes from the receipt printer driver/PDF. No ESC/POS code exists in
any client (`[WPF]` PrinterService has no drawer branch).

The `register` payload field is a *routing filter*: `[WPF]` JobsService.cs:227
— if the job carries a register id, only printers locally bound to that id are
eligible; a null register matches every printer. (`[WIN]` Printers.cs:86-103
inverts the direction — a printer bound to "None"/0 takes everything; `[MAC]`
DataManager.m:75-94 matches WPF.) SyncroPrint follows WPF, and simply routes
`popdrawer` like any other type (point it at the receipt printer).

Registers list (for the settings UI) comes from `settings/printing` (§3).

## 9. Location handling

Locations come **only from the login response** (`locations_allowed` +
`default_location`); no separate endpoint. Filtering is client-side at event
receipt — `[WPF]` JobsService.cs:537:
a job with no `location` is accepted by every client; a job with one is
accepted only if the id is locally selected. `[WPF]` allows a multi-select;
`[MAC]` a single selection; `enable_multi_locations` is parsed but drives no
logic. SyncroPrint: optional single `location_id` in config (null = accept all
— correct for single-location shops).

## 10. Acknowledgement / dedupe / polling

**Fire-and-forget. Nothing goes back to the API.** Exhaustive endpoint
inventories of all three repos found exactly two RepairShopr endpoints
(sign_in, settings/printing) — no job-state POST, no ACK, no job-list/queue
endpoint, no device registration, no log upload. The "Job Queue" in
AutoPrinter's docs is the client's *local* queue (`[WPF]` keeps it in
`Data/NewJobs.json`/`DownloadedJobs.json`/`DoneJobs.json`).

None of the clients dedupe — a redelivered event prints twice (`[WPF]`
JobsService.cs:539 unconditionally creates a new Job). SyncroPrint's derived-id
dedupe (§5) is therefore strictly safer than the original.

**Poller consequence**: with no job-list endpoint, a missed realtime event
**cannot be replayed** from the AutoPrintr surface. The spec's §5.4 fallback
degrades as anticipated: the poller sweeps standard Syncro REST resources
(tickets/invoices since a cursor) on a best-effort basis, and its real v1 role
is fallback liveness + visibility, not guaranteed recovery. Structured as a
pluggable source list so a proper job feed can be dropped in if Syncro ever
exposes one.

## 11. Enabled vs Auto Print

Both toggles are **purely local**; nothing is registered server-side. The only
on-the-wire difference between a manual print and an automation-triggered one
is the `autoprinted` flag. The decisive line — `[WPF]` JobsService.cs:228:

```csharp
d.Enabled && (job.Document.AutoPrint ? d.AutoPrint : true)
```

i.e. **print iff `enabled && (auto_print || !autoprinted)`** — `enabled` always
required; `auto_print` additionally required only when the job was
automation-triggered. Same predicate in `[MAC]` PusherManager.m:81-108 and
`[WIN]` Jobs.cs:400-416. If no printer qualifies, the job is silently dropped
(`[WPF]` JobsService.cs:541-543) — SyncroPrint records it as `skipped` instead
so it shows in History.

The intent, from `[WIN]`'s tooltip (TriggerCheckBox.cs:23): auto-print off =
"you can send print jobs here by clicking the 'Print' button in the web app,
but jobs won't auto-print when things happen — like after a payment."

Quantity: local per-type setting, applied as `lp -n` (`[MAC]`
PusherManager.m:131) or a print-N-times loop (`[WIN]` Printers.cs:332-339,
`[WPF]` PrinterService.cs:86-91 with 500 ms between copies). Quantity 0
silently never prints in the originals; SyncroPrint clamps to ≥1.

## 12. Complete endpoint inventory

| Purpose | Request | Source |
|---|---|---|
| Login (legacy) | `POST https://admin.{host}/api/v1/sign_in` (form: email, password) | `[WPF]` UserService.cs:60-66 |
| Channel + registers | `GET https://{sub}.{host}/api/v1/settings/printing?api_key={token}` | `[WPF]` UserService.cs:51-58 |
| Realtime | `ws(s)://ws-mt1.pusher.com/app/4a12d53c136a2d3dade7?protocol=5..7` | `[WPF]` vendored PusherClient 0.5.1 defaults |
| PDF | `GET {payload.file}` (as-is; `&orientation=portrait` appended for rotation) | `[WPF]` FileService.cs:252-276, JobsService.cs:378 |
| Update check | GitHub releases API (`[WIN]` only, Autoupdate.cs:17) / ClickOnce (`[WPF]`, disabled) | — |

Not present anywhere: ack, job list, device registration, Pusher channel auth,
log upload, locations endpoint.

## 13. Design deltas (SyncroPrint vs originals)

| Original behaviour | SyncroPrint |
|---|---|
| Plaintext `ws://` (WPF) / `http://` API (mac) | TLS everywhere, verification on |
| Download any URL the channel supplies | HTTPS + account-host allowlist + content-type + size cap |
| No dedupe — redelivery prints twice | sha256(document|file_url) unique-index gate |
| No download retries | 3 × with backoff, per-job timeouts |
| Silent drop when no printer matches | recorded as `skipped`, visible in History |
| Quantity 0 silently never prints | quantity clamped to 1..99 |
| Rotation via server query param | local `lp -o orientation-requested=4` |
| Win register semantics ≠ mac/WPF | WPF semantics (job's register selects bound printers; null matches all) |
| Password stored plaintext in NSUserDefaults (mac) | token in root:syncroprint 0640 config, no password stored |
