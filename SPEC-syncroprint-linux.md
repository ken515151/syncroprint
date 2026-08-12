# SyncroPrint for Linux — Build Specification

**Project:** Native Linux replacement for Syncro's deprecated Windows "AutoPrinter" app.
**Owner:** a small computer repair shop. **Handoff target:** Claude Code session.
**Date:** 12 August 2026. **Status:** Design complete, Phase 0 pending.

---

## 1. Purpose and background

Syncro's AutoPrinter instantly prints documents (tickets, invoices, receipts, labels)
the moment they are generated in Syncro. It is Windows-only and flagged by Syncro as a
**legacy integration, no longer updated, may be deprecated**. The shop wants the same
capability on a Linux Mint desktop (XFCE/Cinnamon) at the shop counter.

Crucially, the predecessor app **AutoPrintr is MIT-licensed open source**, published by
RepairShopr (Syncro's sibling product; the WPF client explicitly supports
`syncromsp.com` accounts). This means the wire protocol does not need reverse
engineering — it can be read directly from the source:

- `https://github.com/RepairShopr/AutoPrintr-wpf`  (C#/WPF — most recent, supports Syncro; **primary reference**)
- `https://github.com/RepairShopr/AutoPrintr-win`  (older Windows version; has `docs/Develop.md`, ~3,000 lines of class docs)
- `https://github.com/RepairShopr/AutoPrintr-mac`  (Objective-C; README confirms Pusher key is a build-time constant in `AppKeys.h`)

Known facts already established:

- Realtime delivery is via **Pusher Channels** (WebSocket). The client subscribes to a
  channel and receives job events pushed by the Syncro/RepairShopr backend; original
  app spools jobs in <1.5 s.
- The Windows app's advertised configuration surface: account login, **which printers
  receive which document types**, an optional "register" printer (cash drawer pop),
  location selection for multi-site accounts, logs, job queue.
- The Pusher *app key* is embedded in the clients as a constant. Since the clients are
  open source and the key ships in every public binary, it is a public identifier
  (Pusher app keys are not secrets); account security comes from channel scoping/auth.

**Note on GitHub access:** github.com blocks automated fetchers on `/tree/`, `/blob/`
raw paths, so this spec could not embed the extracted constants. Claude Code must
`git clone` the repos locally in Phase 0 — this works fine.

---

## 2. Phase 0 — Protocol extraction (do this before writing any code)

Clone all three repos. Produce a `PROTOCOL.md` in the new project recording each item
below with the exact source file and line references:

1. **Login flow.** How the client exchanges username/password or API token for
   credentials. Look for an HTTP POST in the WPF `Services` (likely names:
   `UserService`, `LoginService`, `ApiService`). Record URL(s), request body, response
   schema. Note how `syncromsp.com` vs `repairshopr.com` hosts are selected
   (release notes: "multiple hosts support").
2. **Pusher connection parameters.** App key constant, cluster, whether TLS
   (`wss://ws.pusherapp.com/app/{key}?protocol=7...`), and the client's protocol
   version. In WPF look for `PusherService`/`ChannelsService` or a Pusher NuGet client;
   in mac repo it's `AppKeys.h` usage.
3. **Channel name format.** Likely derived from account/user id (e.g. `user_{id}` or
   `private-...`). If it is a **private channel**, record the auth endpoint the client
   POSTs to for the channel signature. If it is a public channel keyed by an
   unguessable id, record how that id is obtained at login.
4. **Event name(s)** the client binds to, and the **job payload JSON schema**:
   document type field, file/PDF URL, location id, register flag, quantity/copies,
   job id, anything else. Capture a verbatim example if test fixtures exist.
5. **Document type enum.** The canonical list of job/document types the backend sends
   (expected to include at least: ticket, invoice, receipt, label/asset label, intake
   form, estimate, PO). Record exact string values — the config file keys off these.
6. **PDF retrieval.** Whether the payload URL is pre-signed/public or needs the auth
   token in a header. Record timeout/retry behaviour in the original.
7. **Register / cash drawer** mechanics — how a drawer "pop" job differs from a
   document job.
8. **Location handling** for multi-location accounts (the shop is single-location;
   implement but default it away).
9. **Acknowledgement/dedupe** — does the client ACK jobs back to the API, or is
   delivery fire-and-forget? This decides our idempotency design.
10. **"Enabled" vs "Auto Print" signalling.** The original distinguishes manually
    triggered prints (user clicks print in the Syncro UI and the job routes to the
    client) from event-triggered auto prints (e.g. asset label on asset creation).
    Determine whether these arrive as differently-typed Pusher events, or whether the
    payload carries a flag, or whether Auto Print enrolment is configured server-side
    per client. This decides whether our enabled/auto_print config toggles filter
    locally or need to be registered with the API.
11. **Licence compliance** — copy the MIT licence text and attribute RepairShopr in
    the new repo's README/NOTICE.

Auth details confirmed from Syncro's docs: client authenticates with the account
**subdomain + an API token created specifically from the AutoPrinter App Center card**
(no API permission scopes needed; generic tokens reportedly don't work — token *type*
matters). Set a token expiry as good practice and diary the renewal.

Also verify against Syncro's current doc (`https://docs.syncromsp.com/legacy/autoprinter-printing-in-syncro`):
AutoPrinter authenticates with an **API token generated in Admin → App Center →
AutoPrinter card** plus subdomain. The modern app's flow may differ from old
AutoPrintr's username/password login — prefer the token flow if both exist.

> **Fallback if the realtime channel proves unusable** (key rotated, channel auth
> impossible, or Syncro shuts the legacy backend): the poller (§5.4) becomes the
> primary transport. The build must be structured so transports are pluggable.

---

## 3. Target environment

- Linux Mint (Ubuntu-based), XFCE or Cinnamon desktop, x86-64 desktop PC at the counter.
- Printers (via CUPS):
  - **A4 laser/inkjet** — default for tickets, invoices, intake forms, estimates, etc.
  - **Brother thermal label printer** — labels only.
    - ⚠️ Open question for the owner: exact model (QL-570/700/800/1100, or a TD/PT series?).
      QL-series have official Brother CUPS drivers (`.deb`) and also work with the
      `brother_ql` Python tool. If the label PDFs from Syncro are already sized for
      the label media, the CUPS driver route is simplest: print with
      `-o media=<label size>` and `-o fit-to-page`. Decide in Phase 1 after a test PDF
      is inspected.
- Python 3.10+ (system python3 on Mint 21/22 is fine).

---

## 4. Architecture

Two cooperating components, deliberately decoupled:

```
┌────────────────────────────┐        ┌───────────────────────────┐
│ syncroprintd (systemd svc) │◄──────►│ syncroprint-applet (tray) │
│  - Pusher WS listener      │  UNIX  │  GTK3 + AyatanaAppIndicator│
│  - polling fallback        │ socket │  - status icon             │
│  - PDF fetch + CUPS submit │  JSON  │  - recent jobs / pause     │
│  - SQLite job log/queue    │ lines  │  - settings window         │
│  - config owner            │        │  (pure client of daemon)   │
└────────────────────────────┘        └───────────────────────────┘
```

- **The daemon does all real work.** Printing continues with no desktop session.
- **The applet is optional chrome.** It connects to the daemon's UNIX socket; killing
  it never affects printing. This is the "applet not app" model the owner asked for.
- Written in **Python**, standard/boring dependencies only:
  - `websockets` (or `websocket-client`) — implement Pusher protocol v7 directly.
    The protocol is small and publicly documented (connect URL, `pusher:connection_established`,
    `pusher:subscribe`, `pusher:ping`/`pusher:pong`, event dispatch). Avoid the
    unmaintained third-party Pusher SDKs; ~150 lines of our own transport is more
    auditable and matches the "proven, secure, reliable" brief.
  - `requests` — API calls and PDF download.
  - `PyGObject` (`python3-gi`) + `gir1.2-ayatanaappindicator3-0.1` — applet only.
  - `sqlite3`, `configparser`/`json`, `logging` — stdlib.
- CUPS submission via `lp` subprocess (simplest, rock-solid) — not pycups, to avoid a
  compiled dependency. `lp -d <printer> -n <copies> [-o media=... -o fit-to-page] file.pdf`.

## 5. Daemon specification (`syncroprintd`)

### 5.1 Lifecycle
- systemd **system** service (not user service) so it runs sans login:
  runs as dedicated user `syncroprint`, member of `lp` group.
- Start → load config → open SQLite → start control socket → start transport
  (Pusher; on repeated failure degrade to poller) → run until SIGTERM.

### 5.2 Transport A — Pusher realtime (primary)
- Connect `wss://` per Phase-0 findings; implement ping/pong keepalive; exponential
  backoff reconnect (1 s → 60 s cap, jitter). On every (re)connect, run one poller
  sweep to catch anything missed while down (per §5.4) — this is the gap-fill.
- On job event: parse, validate against schema, enqueue.

### 5.3 Job pipeline
1. **Dedupe:** job id checked against SQLite `jobs` table (unique index). Seen → drop.
2. **Fetch PDF** over HTTPS (TLS verification ON, no exceptions), to
   `/var/lib/syncroprint/spool/`, with 3 retries/backoff. Enforce content-type and a
   sane max size (e.g. 25 MB).
3. **Route:** `document_type` → printer + options from config. Unmapped type → log
   as `skipped` (visible in applet), do not print.
4. **Print:** `lp` with configured options; record CUPS job id. Non-zero exit →
   retry ×2 then mark `failed`.
5. **Record:** row in `jobs` (id, type, received_at, printed_at, printer, status,
   error). Spool files retained for a short window (default 7 days, configurable)
   to support one-click Reprint from History; cleaned up by a daily sweep. Job rows
   are kept indefinitely (they're tiny) so the audit history is complete.
6. **Pause mode:** daemon flag (set from applet) queues jobs instead of printing;
   resume flushes queue.

### 5.4 Transport B — API poller (fallback + gap-fill)
- Poll Syncro REST API on interval (default 60 s as fallback transport; also invoked
  once on every websocket reconnect) for recently generated printable documents.
- Exact endpoints depend on Phase 0 (the old client may have a job-list endpoint —
  AutoPrinter's docs mention a Job Queue). If no dedicated endpoint exists, this
  degrades to "new tickets/invoices since last cursor" via standard API and fetching
  their PDFs — acceptable, since dedupe makes overlap harmless.
- Cursor persisted in SQLite.

### 5.5 Control socket
- UNIX domain socket `/run/syncroprint/control.sock`, mode `0660`, group
  `syncroprint` (add the shop's desktop user to the group). Newline-delimited JSON:
  `status`, `recent_jobs`, `history {filters}`, `pause`, `resume`,
  `test_print {printer}`, `cancel_job {id}`, `reprint {id}`, `get_log_tail {n}`,
  `get_config`, `set_config {…}`, `reload`. No TCP port anywhere.

### 5.6 Security requirements
- API token in `/etc/syncroprint/config.json`, root:syncroprint `0640`. (Keyring is
  wrong here — daemon runs headless; file with strict perms is the honest design.)
- systemd hardening: `NoNewPrivileges=yes`, `ProtectSystem=strict` with
  `ReadWritePaths=/var/lib/syncroprint /run/syncroprint`, `PrivateTmp=yes`,
  `ProtectHome=yes`, `RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6`,
  `CapabilityBoundingSet=`.
- Validate every field of inbound job JSON; treat the channel as untrusted input.
  Only ever download from HTTPS URLs on the account's own Syncro host (allowlist the
  subdomain) — refuse arbitrary URLs even if the payload supplies them.
- Log rotation via `logging.handlers.RotatingFileHandler` or journald (prefer journald).

## 6. Applet specification (`syncroprint-applet`)

- GTK3 + AyatanaAppIndicator3; autostarts via `~/.config/autostart/*.desktop`.
- Icon states: connected (realtime) / degraded (poller) / disconnected / paused / error.
- Menu: last 10 jobs (type, time, printer, status), Pause/Resume, Test print ▸ (per
  printer), **History…**, **Error log…**, Settings…, Quit applet.
- **History window:** full searchable print history from the SQLite `jobs` table
  (date, document type, source id, printer, copies, status, error text). Filter by
  status/type/date; "Reprint" action on any completed or failed row (re-fetches or
  reuses retained spool file). This is the "what has it printed" audit view.
- **Error log window:** tail of daemon journal filtered to warnings/errors, with a
  "copy to clipboard" button for support tickets.
- **Stuck/active job handling:** any job in `downloading`/`printing` state longer
  than a threshold (default 60 s) is flagged ⚠ in the menu and History. Each active
  job row exposes **Cancel** — daemon aborts the download, or issues `cancel <cups-job-id>`
  if already spooled, marks the row `cancelled`, and moves on to the next job. A
  stuck job must never block the queue: the pipeline processes jobs on a worker with
  per-job timeouts (download 120 s, print submit 30 s), auto-failing past them.
- **Settings window** (superset of AutoPrinter's documented options):
  - Account: **host selector (SyncroMSP / RepairShopr)** + subdomain + API token,
    "Test connection", "Remember me" (token persisted — always on in our design).
    Host selection changes API base URL and PDF-URL allowlist; both hosts must be
    supported like the original client ("multiple hosts support").
  - **Events grid** — rows = document types / triggers (from Phase 0 enum), columns:
    - **Enabled** ✓ — manually triggered prints in the Syncro UI are routed to this
      client. (In the original this is separate from auto-printing.)
    - **Auto Print** ✓ — the document prints automatically when the associated event
      happens in Syncro (e.g. asset label on asset creation, booking-in sheet on
      ticket creation).
    - **Quantity** — copies per job (e.g. invoice ×1, booking-in sheet ×2). Maps to
      `lp -n`.
    - **Printer** — dropdown from `lpstat -a`.
    - **Duplex** — off / long-edge / short-edge → `lp -o sides=…`; greyed out if the
      printer's PPD reports no duplexer (`lpoptions -p X -l | grep -i duplex`).
    - **Rotate 90° CW** ✓ — original had this as a printer setting; implement via
      `lp -o orientation-requested=…` or a pdftk/qpdf rotate pass if CUPS option
      proves unreliable for a given driver (decide in testing).
  - **Register** printer selection for POS cash-drawer pops (behind "advanced" —
    unused at this shop).
  - Location dropdown (Big Chain multi-location; single-location default).
  - Writes via `set_config` over the socket; daemon reloads live.

## 7. Configuration schema (example)

```json
{
  "account": { "host": "syncromsp.com", "subdomain": "YOUR_SUBDOMAIN", "api_token": "…" },
  "transport": { "mode": "auto", "poll_interval_s": 60 },
  "printers": {
    "a4":    { "cups_name": "HP_LaserJet", "options": ["fit-to-page"] },
    "label": { "cups_name": "Brother_QL", "options": ["media=Custom.62x100mm", "fit-to-page"] }
  },
  "routing": {
    "ticket":      { "enabled": true, "auto_print": true,  "printer": "a4",    "quantity": 2, "duplex": "off",       "rotate": false },
    "invoice":     { "enabled": true, "auto_print": true,  "printer": "a4",    "quantity": 1, "duplex": "long-edge", "rotate": false },
    "receipt":     { "enabled": true, "auto_print": false, "printer": "a4",    "quantity": 1, "duplex": "off",       "rotate": false },
    "asset_label": { "enabled": true, "auto_print": true,  "printer": "label", "quantity": 1, "duplex": "off",       "rotate": false }
  },
  "timeouts": { "download_s": 120, "print_submit_s": 30, "stuck_flag_s": 60 },
  "retention": { "failed_spool_days": 7 }
}
```
(Real document-type keys come from Phase 0. `media=` value comes from the Brother
model's PPD — check `lpoptions -p Brother_QL -l`.)

## 8. Deliverables / repo layout

```
syncroprint/
├── PROTOCOL.md               # Phase 0 output, with source citations
├── NOTICE                    # MIT attribution to RepairShopr AutoPrintr
├── syncroprintd/             # daemon package
│   ├── __main__.py  transport_pusher.py  transport_poll.py
│   ├── pipeline.py  cupsprint.py  control.py  config.py  store.py
├── applet/syncroprint_applet.py
├── packaging/
│   ├── syncroprintd.service  # hardened unit per §5.6
│   ├── install.sh            # user/dirs/deps/unit/autostart
│   └── syncroprint-applet.desktop
└── tests/                    # incl. fake Pusher server + sample payloads
```

## 9. Test plan

1. Unit: payload validation, routing, dedupe, config round-trip.
2. Integration: local fake Pusher WS server replaying captured payloads; CUPS-PDF
   virtual printer as target (prints land as files — no paper wasted).
3. Live: `--dry-run` flag (fetch + route + log, no `lp`); then test print button on
   real A4 and Brother; then generate a real ticket in Syncro and time it.
4. Failure drills: kill network mid-download; wrong token; CUPS printer offline
   (job should queue in CUPS, not vanish); daemon restart mid-queue.

## 10. Risks

| Risk | Mitigation |
|---|---|
| Syncro retires legacy Pusher backend | Poller transport is first-class, auto-degrade; architecture keeps transports pluggable |
| Pusher key rotation | Key in config (Phase 0 value as default), not hardcoded |
| Channel requires private auth we can't sign | Poller-only mode is an acceptable v1 (30–60 s latency) |
| Label PDFs mis-sized for Brother media | Phase 1 inspects a real label PDF before choosing driver vs `brother_ql` route |
| Robots/ToS | Repos are MIT; attribute properly in NOTICE |

## 11. Open questions for the owner (answer before/at build)

1. Exact Brother label printer model (and label roll size in use)?
2. Which document types should auto-print day one? (Suggest: ticket + label on;
   invoice/receipt off until proven.)
3. A4 printer model / its CUPS queue name (`lpstat -a` output).
4. Should the shop PC's daemon start paused outside opening hours? (Trivial to add
   via systemd timer if wanted; suggest not in v1.)

## 12. Suggested build order for Claude Code

1. Phase 0 extraction → `PROTOCOL.md` (clone repos, cite files/lines).
2. `config.py` + `store.py` + `cupsprint.py` with tests (no network needed).
3. Poller transport + pipeline end-to-end against CUPS-PDF. **← useful product already**
4. Pusher transport + reconnect/gap-fill.
5. Control socket + applet.
6. Packaging, hardening pass, README, live tests at the counter.
