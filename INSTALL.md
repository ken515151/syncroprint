# SyncroPrint — Installation Guide

Step-by-step setup for a Linux Mint (or Ubuntu-based) counter PC. Takes about
15 minutes plus printer driver installation. No config files need editing —
everything after the installer runs is done in the tray applet's GUI.

## Before you start

You'll need:

- The counter PC running Linux Mint 21/22 (XFCE or Cinnamon), with sudo access.
- Both printers connected and powered on (A4 printer + Brother QL-570).
- A Syncro admin login (to create the AutoPrinter API token).

## Step 1 — Set up the printers in CUPS

### A4 laser/inkjet

Most A4 printers are auto-detected. Check:

```bash
lpstat -a
```

If your A4 printer is listed and `accepting requests`, you're done. If not,
add it via **Menu → Printers** (system-config-printer) or CUPS at
`http://localhost:631`.

Print a test page from the printer settings dialog to confirm it works
before going further.

### Brother QL-570 label printer

The QL-570 needs Brother's official driver:

1. Download the **QL-570 LPR driver** and **cupswrapper** `.deb` files from
   Brother's support site (search "QL-570 Linux driver"), or use their
   Driver Install Tool.
2. Install both:
   ```bash
   sudo dpkg -i ql570lpr-*.deb ql570cupswrapper-*.deb
   ```
3. Confirm the queue exists:
   ```bash
   lpstat -a          # note the queue name, e.g. QL-570
   lpoptions -p QL-570 -l | head    # lists supported media sizes
   ```
4. Test-print a label directly to confirm the driver and roll work:
   ```bash
   echo "test" | lp -d QL-570
   ```

Note which media name matches the loaded roll (you'll enter it in Step 5):

| Roll loaded | Option to use |
|---|---|
| 29×90 mm die-cut labels (DK-11201) | `media=29x90,fit-to-page` |
| 29 mm continuous roll (DK-22210) | `media=Custom.29x90mm,fit-to-page` |

The exact media names vary by driver version — trust the `lpoptions` output
above over this table.

## Step 2 — Create the API token in Syncro

1. Log in to Syncro as an admin.
2. Go to **Admin → App Center** and open the **AutoPrinter** card.
3. Generate/copy the API token from that card.

⚠ The token **must** come from the AutoPrinter App Center card — a generic
API token (Admin → API → API Tokens) will not work, because the token *type*
matters. Set an expiry per your security policy and diary the renewal.

Also note your **subdomain**: if you sign in at
`exampleshop.syncromsp.com`, the subdomain is `exampleshop`.

## Step 3 — Install SyncroPrint

From a terminal on the counter PC (git isn't preinstalled on Mint, hence
the first line):

```bash
sudo apt install git
git clone https://github.com/ken515151/linuxprintr.git
cd linuxprintr
sudo bash packaging/install.sh
sudo systemctl start syncroprintd
```

The installer:
- installs the few dependencies (python3, requests, websockets, GTK bits, cups-client),
- creates the `syncroprint` service user and directories,
- installs and enables the hardened systemd service,
- adds your desktop user to the `syncroprint` group (needed to talk to the daemon),
- installs the tray applet autostart entry.

Now **log out and back in** — the group change and the applet autostart only
take effect on a fresh login.

(The applet then starts automatically on every login. To launch it by hand —
e.g. after quitting it, or to see startup errors — run:
`python3 /usr/lib/syncroprint/applet/syncroprint_applet.py`)

## Step 4 — Connect your Syncro account (in the GUI)

After logging back in you'll see the SyncroPrint tray icon showing
**"Not set up — open Settings"**.

1. Click the tray icon → **Set up account…** (or **Settings…**).
2. On the **Account** tab:
   - Host: `syncromsp.com` (or `repairshopr.com` for RepairShopr accounts)
   - Subdomain: e.g. `exampleshop`
   - API token: paste the token from Step 2
3. Click **Test connection** — you should see
   `✓ OK — channel acquired, N register(s) on account`.
   If you see ✗, re-check the subdomain and that the token came from the
   AutoPrinter card.
4. Click **Save**. The daemon connects immediately — within a few seconds the
   tray icon switches to **Connected (realtime)**.

Leave Location ID empty (single-location accounts) and Register printer as
`(none)` unless you use POS cash drawers.

## Step 5 — Assign printers

Settings → **Printers** tab:

- **a4** → pick your A4 printer's CUPS queue. Options: `fit-to-page`
- **label** → pick the QL-570 queue. Options: the media line from Step 1,
  e.g. `media=29x90,fit-to-page`
- **receipt** → leave `(unset)` unless you have a receipt printer.

Click **Save**.

## Step 6 — Enable document types

Settings → **Events** tab. Everything ships **off** — nothing prints until
you enable it here.

For each document type you want:

- **Enabled** ✓ — clicking "Print" in the Syncro web UI prints here.
- **Auto Print** ✓ — the document also prints automatically when the event
  fires in Syncro (e.g. label on asset creation). Leave off until you've
  proven the type prints correctly.
- **Qty** — copies per job (e.g. Ticket ×2 for a booking-in copy).
- **Printer** — `a4` for paper documents, `label` for Asset / Ticket Label.
- **Duplex / Rotate** — as needed per type.

Suggested starting point: enable Ticket, Invoice, Asset and Ticket Label
with Auto Print off; add Auto Print per type once each is proven.

Click **Save**.

## Step 7 — Verify

1. Tray menu → **Test print** → each configured printer. Both should output.
2. In Syncro, open a ticket and click **Print** — it should appear on the A4
   printer within a couple of seconds, and in the tray menu's recent jobs
   with a ✓.
3. Tray menu → **History…** shows the full audit log; any job can be
   reprinted from there.

## Troubleshooting

| Symptom | Check |
|---|---|
| Tray icon: "Daemon unreachable" | `systemctl status syncroprintd`; did you log out/in after install (group membership)? |
| Test connection fails with auth error | Token must be from the **AutoPrinter App Center card**, not a generic API token |
| Icon stuck on "Degraded (polling)" | Realtime blocked — check firewall allows outbound 443 to `ws-mt1.pusher.com` / `ws.pusherapp.com` |
| Jobs show `skipped` in History | That document type isn't Enabled in the Events tab (or Auto Print is off for an automated job) — this is the day-1 default |
| Labels print wrong size | `media=` option doesn't match the loaded roll: check `lpoptions -p <queue> -l` |
| Job stuck / printer jammed | Stuck jobs are flagged ⚠ after 60 s and can be cancelled from the tray menu or History; the queue keeps moving past them |
| Job goes printed → failed with "CUPS cancelled/aborted" | CUPS accepted the job but its backend then killed it. Check `journalctl -u cups` / `/var/log/cups/error_log`. On test machines using cups-pdf this is usually AppArmor: set `Out /var/spool/cups-pdf/${USER}` in `/etc/cups/cups-pdf.conf` and restart cups |
| Daemon or CUPS wedged | Tray menu → **Troubleshooting** → restart either service (asks for an admin password) |
| Deeper diagnosis | `journalctl -u syncroprintd -f`, or tray menu → **Error log…** (has a copy-to-clipboard button) |

## Updating

```bash
cd linuxprintr && git pull
sudo bash packaging/install.sh     # re-copies code; config/history are kept
sudo systemctl restart syncroprintd
```

The daemon runs from `/usr/lib/syncroprint`, not the git checkout — a
`git pull` alone changes nothing until `install.sh` re-copies and the
service restarts. The applet doesn't hot-reload either: tray menu →
**Quit applet**, then relaunch it (or log out/in).

## Uninstalling

```bash
sudo systemctl disable --now syncroprintd
sudo rm -rf /usr/lib/syncroprint /etc/systemd/system/syncroprintd.service
sudo rm -rf /etc/syncroprint /var/lib/syncroprint   # config + history — omit to keep
rm ~/.config/autostart/syncroprint-applet.desktop
sudo userdel syncroprint
```
