# SyncroPrint for Linux

Native Linux replacement for the deprecated **AutoPrinter / AutoPrintr**
desktop app used by **RepairShopr** and **SyncroMSP**: documents generated
in your account (tickets, invoices, receipts, asset/ticket labels, intake
forms) print on the shop's CUPS printers within seconds — no Windows box,
no unmaintained legacy client.

Works identically with **both products** — pick your host at setup and
everything else is the same, because they share a backend. If you run a
repair shop on RepairShopr with a Linux counter PC, this is aimed squarely
at you; the vendor's official clients (Windows/macOS) have been
unmaintained since ~2021 and are flagged as legacy.

Speaks the original AutoPrintr wire protocol, extracted from RepairShopr's
MIT-licensed clients — see [PROTOCOL.md](PROTOCOL.md) for the full write-up
with source citations, and [NOTICE](NOTICE) for attribution.
[MIT licensed](LICENSE).

## Architecture

```
┌────────────────────────────┐        ┌───────────────────────────┐
│ syncroprintd (systemd svc) │◄──────►│ syncroprint-applet (tray) │
│  - Pusher WS listener      │  UNIX  │  GTK3 + AyatanaIndicator  │
│  - polling fallback        │ socket │  - status icon            │
│  - PDF fetch + CUPS submit │  JSON  │  - recent jobs / pause    │
│  - SQLite job log/queue    │ lines  │  - history / error log    │
│  - config owner            │        │  - settings window        │
└────────────────────────────┘        └───────────────────────────┘
```

The daemon does all the real work and prints with no desktop session; the
applet is optional chrome — killing it never affects printing.

- **Realtime**: subscribes to the account's Pusher channel (protocol 7,
  implemented directly over `websockets`, ~150 lines, no third-party SDK)
  and prints `print-job` events as they arrive.
- **Fallback**: exponential-backoff reconnect; while realtime is down, a
  best-effort REST poller sweeps for new documents, and every reconnect runs
  one gap-fill sweep. (The upstream protocol is fire-and-forget with no job
  list — see PROTOCOL.md §10 for what the poller can and cannot recover.)
- **Safety**: TLS everywhere, PDF downloads allowlisted to the vendor's
  domains only, content-type + 25 MB checks, dedupe via SQLite unique index,
  per-job timeouts so a stuck job never blocks the queue, full audit
  history with one-click reprint. After CUPS accepts a job the daemon
  re-checks its outcome and flips the History row to failed if CUPS
  cancelled/aborted it (e.g. a broken backend).

## Install (Linux Mint / Ubuntu)

> Full step-by-step walkthrough (printer drivers, token creation,
> troubleshooting): **[INSTALL.md](INSTALL.md)**. Short version:

```bash
sudo apt install git
git clone https://github.com/ken515151/linuxprintr.git
cd linuxprintr
sudo bash packaging/install.sh
sudo systemctl start syncroprintd
```

Then **all setup happens in the GUI**: log out/in once (group membership),
click the tray icon (it shows "Not set up"), open **Settings…** and:

1. **Account tab** — pick your host (`syncromsp.com` or `repairshopr.com`),
   then enter your subdomain and the API token created from
   **Admin → App Center → AutoPrinter card** (both products have this card;
   token *type* matters — a generic API token will not work). Click
   **Test connection**, then Save. The daemon connects live; no restart needed.
2. **Printers tab** — assign CUPS queues to the `a4` / `label` / `receipt`
   roles (the dropdowns list what `lpstat -a` sees).
3. **Events tab** — enable the document types you want. Nothing prints until
   a type is enabled here, and nothing auto-prints unless you also tick
   **Auto Print** for it — manual "Print" clicks in Syncro work with just
   **Enabled**.

Config lives at `/etc/syncroprint/config.json` (syncroprint:syncroprint 0640
— it holds the token; the daemon owns it and the applet edits it only through
the daemon's socket). Routing keys are the canonical lowercase wire names:
`invoice estimate ticket intakeform receipt zreport ticketreceipt popdrawer
adjustment customerid asset ticketlabel outtakeform`.

### Updating

```bash
cd linuxprintr && git pull
sudo bash packaging/install.sh      # re-copies code; config and history are kept
sudo systemctl restart syncroprintd
```

The daemon runs from `/usr/lib/syncroprint`, not the git checkout, so a
`git pull` alone changes nothing until `install.sh` re-copies it and the
service restarts. The tray applet doesn't hot-reload either: quit it from
the tray menu and relaunch (or log out/in).

### Brother QL-570 (labels)

Install Brother's official `ql570cupswrapper`/`ql570lpr` `.deb` driver so the
printer has a CUPS queue, then in the Printers tab give the `label` role
options matching the loaded roll:

- 29×90 mm die-cut labels (DK-11201): `media=29x90,fit-to-page`
- 29 mm continuous roll (DK-22210): `media=Custom.29x90mm,fit-to-page`

Exact media names vary by driver version — `lpoptions -p <queue> -l` lists
what the queue accepts.

## Try it without paper

```bash
# CUPS-PDF virtual printer: prints land as PDF files
sudo apt install printer-driver-cups-pdf

# or run the daemon in dry-run mode: fetch + route + log, no lp at all
sudo -u syncroprint PYTHONPATH=/usr/lib/syncroprint python3 -m syncroprintd --dry-run -v
```

cups-pdf caveat: AppArmor only lets it write inside real home directories
and `/var/spool/cups-pdf/`. The daemon prints as the `syncroprint` system
user, so with the default config its jobs get **cancelled by CUPS after
acceptance** (SyncroPrint detects this and marks the job failed). Fix for
testing — in `/etc/cups/cups-pdf.conf` set:

```
Out /var/spool/cups-pdf/${USER}
UserUMask 0022
```

then `sudo systemctl restart cups`. Daemon prints land in
`/var/spool/cups-pdf/syncroprint/`; symlink that somewhere handy:

```bash
ln -s /var/spool/cups-pdf/syncroprint ~/Documents/SyncroPrint-Test
```

## Development

```bash
pip install pytest requests websockets
python -m pytest tests -q        # 97 tests; includes a fake Pusher server
```

Runs and tests fine on any OS; only `lp`/`lpstat` calls and the AF_UNIX
socket are Linux-specific (tests stub the former and use TCP loopback for
the latter).

Layout:

```
syncroprintd/      daemon package
  __main__.py        wiring: transports ↔ pipeline ↔ control socket
  transport_pusher.py  Pusher protocol 7 client (asyncio thread)
  transport_poll.py    REST fallback poller + gap-fill cursor
  pipeline.py          validate → dedupe → download → route → lp
  cupsprint.py         lp / lpstat / lpoptions / cancel wrappers
  control.py           UNIX-socket JSON command surface + client
  config.py  store.py  api.py
applet/            GTK3 tray applet (pure control-socket client)
packaging/         systemd unit (hardened), installer, autostart
tests/             unit + integration (fake Pusher WS server)
```

## Go-live checklist

1. Brother QL-570 driver installed, queue visible in `lpstat -a`, media
   option set to the loaded roll (29 mm — see above).
2. A4 queue assigned to the `a4` role in the Printers tab.
3. Enable document types in the Events tab as needed — ships with everything
   off, and Auto Print stays off until deliberately enabled per type.
4. Test print from the tray menu on both printers, then generate a real
   ticket in Syncro and time it.
