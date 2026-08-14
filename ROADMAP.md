# Roadmap / future ideas

Things considered and parked — not committed to, roughly in the order
they'd likely happen. Design notes captured while they were fresh.

## Web interface for setup and history

A small HTTP server that is a *second client of the daemon's control
socket*, exactly like the tray applet — the daemon itself stays unchanged.
Serves the applet's three settings tabs (Account with Test connection,
Printers, Events) as a web form, plus a history page with reprint buttons.
Every operation it needs already exists as a control-socket command.

- Closes the Docker gap (no GTK applet there) and helps headless/cupboard
  installs; coexists with the applet on desktops.
- Security is the part that needs care: the page can read/write the API
  token, so default to bind-to-localhost + admin password (env var on
  first run); remote access via the user's reverse proxy.
- Estimate: a day or two — mostly the HTML form and auth plumbing.

## Docker image for the daemon

Run syncroprintd on an always-on box (NAS, Proxmox, shop server) instead
of the counter PC. The daemon is container-ready by design (headless, no
desktop session; systemd hardening replaced by container isolation).

- Small `python:3-slim` image + requests/websockets + cups-client.
- Volumes: `/etc/syncroprint` (config/token), `/var/lib/syncroprint`
  (history/queue).
- Printer access: `CUPS_SERVER=<host>:631` against any shared CUPS
  server, or bind-mount the host's `/run/cups/cups.sock`.
- Without the web UI above, first-run setup means hand-editing
  `config.json` (or copying one produced on a desktop install) — so this
  probably lands *after* the web UI.

## APT repository / PPA

Today updates are "download the new .deb from Releases". A hosted apt repo
(or PPA) would let shop machines pick up updates via `apt upgrade` /
unattended-upgrades. Only worth it if the install base grows beyond
machines we touch ourselves.

## Remote applet (probably not)

The control server already supports TCP addresses (the tests use it), so a
tray applet on one machine driving a daemon on another is technically
close — but the control surface has no auth, so exposing it over TCP is
off the table unless that's designed properly. The web UI covers the same
need more safely.
