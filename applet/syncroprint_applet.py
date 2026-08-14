#!/usr/bin/env python3
"""SyncroPrint tray applet — optional chrome over syncroprintd (§6).

Pure client of the daemon's UNIX control socket; killing it never affects
printing. Requires: python3-gi, gir1.2-gtk-3.0, gir1.2-ayatanaappindicator3-0.1.
"""

from __future__ import annotations

import getpass
import sys
import threading
from datetime import datetime

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import GLib, Gtk  # noqa: E402

try:
    gi.require_version("AyatanaAppIndicator3", "0.1")
    from gi.repository import AyatanaAppIndicator3 as AppIndicator  # noqa: E402
except (ValueError, ImportError):
    gi.require_version("AppIndicator3", "0.1")
    from gi.repository import AppIndicator3 as AppIndicator  # noqa: E402

sys.path.insert(0, "/usr/lib/syncroprint")  # daemon package install location
from syncroprintd.control import ControlClient, ControlError  # noqa: E402

POLL_SECONDS = 3

# state -> (icon name, human text)
STATE_ICONS = {
    "connected":    ("printer", "Connected (realtime)"),
    "degraded":     ("printer-warning", "Degraded (polling)"),
    "disconnected": ("printer-error", "Disconnected"),
    "paused":       ("media-playback-pause", "Paused"),
    "error":        ("dialog-error", "Error"),
    "starting":     ("printer", "Starting…"),
    "unconfigured": ("preferences-system", "Not set up — open Settings"),
    "noaccess":     ("dialog-password", "No access to the daemon socket"),
}

DOC_TYPES = [  # canonical routing key, human title
    ("invoice", "Invoice"), ("estimate", "Estimate"), ("ticket", "Ticket"),
    ("intakeform", "Intake Form"), ("outtakeform", "Outtake Form"),
    ("receipt", "Receipt"), ("zreport", "Z Report"),
    ("ticketreceipt", "Ticket Receipt"), ("popdrawer", "Pop Drawer"),
    ("adjustment", "Adjustment"), ("customerid", "Customer ID"),
    ("asset", "Asset"), ("ticketlabel", "Ticket Label"),
]
LOGICAL_PRINTERS = ["a4", "label", "receipt"]
DUPLEX_CHOICES = ["off", "long-edge", "short-edge"]
STATUS_GLYPH = {"printed": "✓", "failed": "✗", "skipped": "–", "cancelled": "✗",
                "downloading": "↓", "printing": "⌛", "queued": "⏸", "received": "…"}


def fmt_time(iso: str | None) -> str:
    if not iso:
        return ""
    try:
        return datetime.fromisoformat(iso).astimezone().strftime("%H:%M:%S")
    except ValueError:
        return iso


class Applet:
    def __init__(self):
        self.client = ControlClient()
        self.status: dict = {}
        self.recent: list[dict] = []
        self.reachable = False
        self.indicator = AppIndicator.Indicator.new(
            "syncroprint", "printer",
            AppIndicator.IndicatorCategory.APPLICATION_STATUS)
        self.indicator.set_status(AppIndicator.IndicatorStatus.ACTIVE)
        self.menu = Gtk.Menu()
        self.indicator.set_menu(self.menu)
        self._rebuild_menu()
        GLib.timeout_add_seconds(POLL_SECONDS, self._tick)
        self._tick()

    # -- polling ----------------------------------------------------------

    def _tick(self):
        threading.Thread(target=self._poll, daemon=True).start()
        return True  # keep the GLib timer

    def _poll(self):
        try:
            status = self.client.call("status")
            recent = self.client.call("recent_jobs", limit=10)
            GLib.idle_add(self._apply_state, status, recent, True)
        except PermissionError:
            # Daemon is up but this user can't open the socket — almost
            # always a missing 'syncroprint' group membership.
            GLib.idle_add(self._apply_state, {"state": "noaccess"}, [], False)
        except (OSError, ControlError):
            GLib.idle_add(self._apply_state, {}, [], False)

    def _apply_state(self, status, recent, reachable):
        self.status, self.recent, self.reachable = status, recent, reachable
        if reachable:
            state = status.get("state", "error")
        elif status.get("state") == "noaccess":
            state = "noaccess"
        else:
            state = "disconnected"
        icon, text = STATE_ICONS.get(state, STATE_ICONS["error"])
        self.indicator.set_icon_full(icon, text)
        self.indicator.set_title(f"SyncroPrint — {text}" if reachable
                                 else "SyncroPrint — daemon unreachable")
        self._rebuild_menu()
        return False

    # -- menu -------------------------------------------------------------

    def _rebuild_menu(self):
        for child in self.menu.get_children():
            self.menu.remove(child)

        if self.reachable:
            header = STATE_ICONS.get(self.status.get("state", ""), ("", "Daemon unreachable"))[1]
        elif self.status.get("state") == "noaccess":
            header = STATE_ICONS["noaccess"][1]
        else:
            header = "Daemon unreachable"
        state_item = Gtk.MenuItem(label=header)
        state_item.set_sensitive(False)
        self.menu.append(state_item)
        if not self.reachable and self.status.get("state") == "noaccess":
            for line in (f"Fix: sudo usermod -aG syncroprint {getpass.getuser()}",
                         "then log out and back in"):
                item = Gtk.MenuItem(label=line)
                item.set_sensitive(False)
                self.menu.append(item)
        self.menu.append(Gtk.SeparatorMenuItem())

        if self.reachable and self.status.get("state") == "unconfigured":
            setup = Gtk.MenuItem(label="Set up account…")
            setup.connect("activate", self._on_settings)
            self.menu.append(setup)
            self.menu.append(Gtk.SeparatorMenuItem())

        stuck = set(self.status.get("stuck_jobs") or [])
        if not self.recent:
            empty = Gtk.MenuItem(label="No jobs yet")
            empty.set_sensitive(False)
            self.menu.append(empty)
        for job in self.recent:
            glyph = STATUS_GLYPH.get(job["status"], "?")
            warn = " ⚠" if job["job_id"] in stuck else ""
            label = (f"{glyph} {fmt_time(job['received_at'])}  {job['document_type']}"
                     f" → {job.get('printer') or '—'}{warn}")
            item = Gtk.MenuItem(label=label)
            if job["status"] in ("downloading", "printing", "queued", "received"):
                sub = Gtk.Menu()
                cancel = Gtk.MenuItem(label="Cancel")
                cancel.connect("activate", self._on_cancel, job["job_id"])
                sub.append(cancel)
                item.set_submenu(sub)
            else:
                item.set_sensitive(job["status"] in ("printed", "failed"))
                if item.get_sensitive():
                    sub = Gtk.Menu()
                    reprint = Gtk.MenuItem(label="Reprint")
                    reprint.connect("activate", self._on_reprint, job["job_id"])
                    sub.append(reprint)
                    item.set_submenu(sub)
            self.menu.append(item)

        self.menu.append(Gtk.SeparatorMenuItem())
        paused = self.status.get("paused", False)
        pause_item = Gtk.MenuItem(label="Resume printing" if paused else "Pause printing")
        pause_item.connect("activate", self._on_pause_toggle, paused)
        pause_item.set_sensitive(self.reachable)
        self.menu.append(pause_item)

        test_item = Gtk.MenuItem(label="Test print")
        test_menu = Gtk.Menu()
        cfg = self._get_config_quiet()
        for key in (cfg.get("printers") or {}):
            entry = Gtk.MenuItem(label=f"{key} ({cfg['printers'][key]['cups_name']})")
            entry.connect("activate", self._on_test_print, key)
            test_menu.append(entry)
        test_item.set_submenu(test_menu)
        test_item.set_sensitive(self.reachable and bool(cfg.get("printers")))
        self.menu.append(test_item)

        for label, cb in (("History…", self._on_history),
                          ("Error log…", self._on_error_log),
                          ("Settings…", self._on_settings)):
            item = Gtk.MenuItem(label=label)
            item.connect("activate", cb)
            item.set_sensitive(self.reachable)
            self.menu.append(item)

        # Deliberately NOT gated on reachability: a dead daemon is exactly
        # when you need these. Goes via systemctl/pkexec, not the socket.
        trouble = Gtk.MenuItem(label="Troubleshooting")
        trouble_menu = Gtk.Menu()
        for label, unit in (("Restart SyncroPrint daemon", "syncroprintd"),
                            ("Restart printing system (CUPS)", "cups")):
            entry = Gtk.MenuItem(label=label)
            entry.connect("activate", self._on_restart_service, unit)
            trouble_menu.append(entry)
        trouble.set_submenu(trouble_menu)
        self.menu.append(trouble)

        self.menu.append(Gtk.SeparatorMenuItem())
        quit_item = Gtk.MenuItem(label="Quit applet")
        quit_item.connect("activate", lambda *_: Gtk.main_quit())
        self.menu.append(quit_item)
        self.menu.show_all()

    def _get_config_quiet(self) -> dict:
        try:
            return self.client.call("get_config") or {}
        except (OSError, ControlError):
            return {}

    def _safe_call(self, cmd, **args):
        try:
            return self.client.call(cmd, **args)
        except (OSError, ControlError) as exc:
            dlg = Gtk.MessageDialog(message_type=Gtk.MessageType.ERROR,
                                    buttons=Gtk.ButtonsType.OK, text=str(exc))
            dlg.run()
            dlg.destroy()
            return None

    def _on_restart_service(self, _w, unit):
        """Restart a system service. pkexec pops the standard polkit
        password dialog; result reported when it finishes."""
        def work():
            import subprocess
            try:
                proc = subprocess.run(["pkexec", "systemctl", "restart", unit],
                                      capture_output=True, text=True, timeout=120)
                if proc.returncode == 0:
                    msg, kind = f"{unit} restarted.", Gtk.MessageType.INFO
                elif proc.returncode in (126, 127):
                    return  # user dismissed the password prompt — say nothing
                else:
                    msg = f"Restart of {unit} failed:\n{proc.stderr.strip() or proc.stdout.strip()}"
                    kind = Gtk.MessageType.ERROR
            except (OSError, subprocess.TimeoutExpired) as exc:
                msg, kind = f"Restart of {unit} failed: {exc}", Gtk.MessageType.ERROR

            def show():
                dlg = Gtk.MessageDialog(message_type=kind,
                                        buttons=Gtk.ButtonsType.OK, text=msg)
                dlg.run()
                dlg.destroy()
                return False
            GLib.idle_add(show)

        threading.Thread(target=work, daemon=True).start()

    def _on_pause_toggle(self, _w, currently_paused):
        self._safe_call("resume" if currently_paused else "pause")

    def _on_test_print(self, _w, printer_key):
        self._safe_call("test_print", printer=printer_key)

    def _on_cancel(self, _w, job_id):
        self._safe_call("cancel_job", id=job_id)

    def _on_reprint(self, _w, job_id):
        self._safe_call("reprint", id=job_id)

    # -- history window ---------------------------------------------------

    def _on_history(self, _w):
        HistoryWindow(self).show_all()

    def _on_error_log(self, _w):
        ErrorLogWindow(self).show_all()

    def _on_settings(self, _w):
        SettingsWindow(self).show_all()


class HistoryWindow(Gtk.Window):
    """Searchable audit view over the daemon's jobs table (§6)."""

    def __init__(self, applet: Applet):
        super().__init__(title="SyncroPrint — Print history")
        self.applet = applet
        self.set_default_size(860, 480)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin=8)
        self.add(box)

        bar = Gtk.Box(spacing=6)
        self.search = Gtk.SearchEntry(placeholder_text="Search id / title / error…")
        self.status_combo = Gtk.ComboBoxText()
        for s in ["(any status)", "printed", "failed", "skipped", "cancelled",
                  "queued", "downloading", "printing"]:
            self.status_combo.append_text(s)
        self.status_combo.set_active(0)
        self.type_combo = Gtk.ComboBoxText()
        self.type_combo.append_text("(any type)")
        for key, title in DOC_TYPES:
            self.type_combo.append_text(key)
        self.type_combo.set_active(0)
        refresh = Gtk.Button(label="Refresh")
        bar.pack_start(self.search, True, True, 0)
        bar.pack_start(self.status_combo, False, False, 0)
        bar.pack_start(self.type_combo, False, False, 0)
        bar.pack_start(refresh, False, False, 0)
        box.pack_start(bar, False, False, 0)

        # columns: received, type, title, printer, copies, status, error
        self.store = Gtk.ListStore(str, str, str, str, str, str, str, str)  # +job_id hidden
        self.view = Gtk.TreeView(model=self.store)
        for i, name in enumerate(["Received", "Type", "Title", "Printer",
                                  "Copies", "Status", "Error"]):
            col = Gtk.TreeViewColumn(name, Gtk.CellRendererText(), text=i)
            col.set_resizable(True)
            self.view.append_column(col)
        scroll = Gtk.ScrolledWindow()
        scroll.add(self.view)
        box.pack_start(scroll, True, True, 0)

        actions = Gtk.Box(spacing=6)
        reprint_btn = Gtk.Button(label="Reprint selected")
        cancel_btn = Gtk.Button(label="Cancel selected")
        actions.pack_start(reprint_btn, False, False, 0)
        actions.pack_start(cancel_btn, False, False, 0)
        box.pack_start(actions, False, False, 0)

        refresh.connect("clicked", lambda *_: self.reload())
        self.search.connect("activate", lambda *_: self.reload())
        self.status_combo.connect("changed", lambda *_: self.reload())
        self.type_combo.connect("changed", lambda *_: self.reload())
        reprint_btn.connect("clicked", self._selected_action, "reprint")
        cancel_btn.connect("clicked", self._selected_action, "cancel_job")
        self.reload()

    def reload(self):
        args = {"limit": 500}
        if self.status_combo.get_active() > 0:
            args["status"] = self.status_combo.get_active_text()
        if self.type_combo.get_active() > 0:
            args["document_type"] = self.type_combo.get_active_text()
        text = self.search.get_text().strip()
        if text:
            args["search"] = text
        rows = self.applet._safe_call("history", **args) or []
        self.store.clear()
        for j in rows:
            self.store.append([
                (j["received_at"] or "").replace("T", " ").split("+")[0],
                j["document_type"], j.get("title") or "", j.get("printer") or "",
                str(j.get("copies") or ""), j["status"], j.get("error") or "",
                j["job_id"],
            ])

    def _selected_action(self, _btn, cmd):
        model, it = self.view.get_selection().get_selected()
        if it:
            self.applet._safe_call(cmd, id=model[it][7])
            self.reload()


class ErrorLogWindow(Gtk.Window):
    def __init__(self, applet: Applet):
        super().__init__(title="SyncroPrint — Error log")
        self.set_default_size(820, 420)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin=8)
        self.add(box)
        self.buffer = Gtk.TextBuffer()
        view = Gtk.TextView(buffer=self.buffer, editable=False, monospace=True)
        scroll = Gtk.ScrolledWindow()
        scroll.add(view)
        box.pack_start(scroll, True, True, 0)
        bar = Gtk.Box(spacing=6)
        copy_btn = Gtk.Button(label="Copy to clipboard")
        refresh_btn = Gtk.Button(label="Refresh")
        bar.pack_start(copy_btn, False, False, 0)
        bar.pack_start(refresh_btn, False, False, 0)
        box.pack_start(bar, False, False, 0)

        def load(*_):
            lines = applet._safe_call("get_log_tail", n=300) or []
            shown = [l for l in lines if " WARNING " in l or " ERROR " in l] or lines
            self.buffer.set_text("\n".join(shown))

        def copy(*_):
            clip = Gtk.Clipboard.get_default(self.get_display())
            start, end = self.buffer.get_bounds()
            clip.set_text(self.buffer.get_text(start, end, False), -1)

        refresh_btn.connect("clicked", load)
        copy_btn.connect("clicked", copy)
        load()


class SettingsWindow(Gtk.Window):
    """Superset of AutoPrinter's documented options (§6)."""

    def __init__(self, applet: Applet):
        super().__init__(title="SyncroPrint — Settings")
        self.applet = applet
        self.set_default_size(880, 620)
        self.cfg = applet._get_config_quiet()
        try:
            printer_info = applet.client.call("printers") or []
        except (OSError, ControlError):
            printer_info = []   # no CUPS queues yet — settings must still open
        self.system_printers = [p["name"] for p in printer_info]
        self.duplex_capable = {p["name"] for p in printer_info if p.get("duplex")}

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, margin=10)
        self.add(outer)
        notebook = Gtk.Notebook()
        outer.pack_start(notebook, True, True, 0)
        notebook.append_page(self._account_page(), Gtk.Label(label="Account"))
        notebook.append_page(self._printers_page(), Gtk.Label(label="Printers"))
        notebook.append_page(self._events_page(), Gtk.Label(label="Events"))

        btns = Gtk.Box(spacing=6)
        save = Gtk.Button(label="Save")
        save.connect("clicked", self._on_save)
        close = Gtk.Button(label="Close")
        close.connect("clicked", lambda *_: self.destroy())
        btns.pack_end(save, False, False, 0)
        btns.pack_end(close, False, False, 0)
        outer.pack_start(btns, False, False, 0)

    # Account tab: host / subdomain / token / test connection / location
    def _account_page(self):
        grid = Gtk.Grid(column_spacing=8, row_spacing=8, margin=12)
        acct = self.cfg.get("account") or {}
        grid.attach(Gtk.Label(label="Host", xalign=1), 0, 0, 1, 1)
        self.host_combo = Gtk.ComboBoxText()
        for h in ["syncromsp.com", "repairshopr.com"]:
            self.host_combo.append_text(h)
        self.host_combo.set_active(0 if acct.get("host") != "repairshopr.com" else 1)
        grid.attach(self.host_combo, 1, 0, 1, 1)

        grid.attach(Gtk.Label(label="Subdomain", xalign=1), 0, 1, 1, 1)
        self.subdomain_entry = Gtk.Entry(text=acct.get("subdomain") or "")
        grid.attach(self.subdomain_entry, 1, 1, 1, 1)

        grid.attach(Gtk.Label(label="API token", xalign=1), 0, 2, 1, 1)
        self.token_entry = Gtk.Entry(text=acct.get("api_token") or "",
                                     visibility=False, width_chars=40)
        self.token_entry.set_tooltip_text(
            "Create in Syncro: Admin → App Center → AutoPrinter card. "
            "Leave as ******** to keep the saved token.")
        grid.attach(self.token_entry, 1, 2, 1, 1)

        test_btn = Gtk.Button(label="Test connection")
        self.test_result = Gtk.Label(label="", xalign=0)
        test_btn.connect("clicked", self._on_test_connection)
        grid.attach(test_btn, 1, 3, 1, 1)
        grid.attach(self.test_result, 1, 4, 2, 1)

        grid.attach(Gtk.Label(label="Location ID", xalign=1), 0, 5, 1, 1)
        self.location_entry = Gtk.Entry(
            text=str(self.cfg.get("location_id") or ""))
        self.location_entry.set_tooltip_text("Leave empty for single-location accounts")
        grid.attach(self.location_entry, 1, 5, 1, 1)

        grid.attach(Gtk.Label(label="Register printer (advanced)", xalign=1), 0, 6, 1, 1)
        self.register_combo = Gtk.ComboBoxText()
        self.register_combo.append_text("(none)")
        for key in LOGICAL_PRINTERS:
            self.register_combo.append_text(key)
        current = self.cfg.get("register_printer")
        self.register_combo.set_active(
            LOGICAL_PRINTERS.index(current) + 1 if current in LOGICAL_PRINTERS else 0)
        grid.attach(self.register_combo, 1, 6, 1, 1)
        return grid

    def _on_test_connection(self, _btn):
        """Tests the values currently in the form (nothing is saved); the
        daemon substitutes the stored token if the field is still masked."""
        host = self.host_combo.get_active_text()
        subdomain = self.subdomain_entry.get_text()
        token = self.token_entry.get_text()
        self.test_result.set_text("Testing…")

        def work():
            try:
                data = self.applet.client.call(
                    "test_account", host=host, subdomain=subdomain, api_token=token)
                msg = ("✓ " if data.get("ok_account") else "✗ ") + data.get("message", "")
            except (OSError, ControlError) as exc:
                msg = f"✗ {exc}"
            GLib.idle_add(self.test_result.set_text, msg)

        threading.Thread(target=work, daemon=True).start()

    # Printers tab: logical printer -> CUPS queue + extra options
    def _printers_page(self):
        grid = Gtk.Grid(column_spacing=8, row_spacing=8, margin=12)
        grid.attach(Gtk.Label(label="Role", xalign=0), 0, 0, 1, 1)
        grid.attach(Gtk.Label(label="CUPS queue", xalign=0), 1, 0, 1, 1)
        grid.attach(Gtk.Label(label="Extra lp options (comma-sep)", xalign=0), 2, 0, 1, 1)
        self.printer_rows = {}
        printers_cfg = self.cfg.get("printers") or {}
        for i, key in enumerate(LOGICAL_PRINTERS, start=1):
            grid.attach(Gtk.Label(label=key, xalign=0), 0, i, 1, 1)
            combo = Gtk.ComboBoxText()
            combo.append_text("(unset)")
            existing = (printers_cfg.get(key) or {}).get("cups_name")
            names = list(self.system_printers)
            if existing and existing not in names:
                names.insert(0, existing)  # printer currently offline
            for name in names:
                combo.append_text(name)
            combo.set_active(names.index(existing) + 1 if existing in names else 0)
            opts = Gtk.Entry(text=",".join((printers_cfg.get(key) or {}).get("options") or []))
            grid.attach(combo, 1, i, 1, 1)
            grid.attach(opts, 2, i, 1, 1)
            self.printer_rows[key] = (combo, opts)
        hint = Gtk.Label(
            label="Tip: Brother QL-570 with 29×90 mm die-cut labels → options "
                  "media=29x90,fit-to-page; with a 29 mm continuous roll → "
                  "media=Custom.29x90mm,fit-to-page. Check exact names with "
                  "`lpoptions -p <queue> -l`.",
            xalign=0, wrap=True)
        grid.attach(hint, 0, len(LOGICAL_PRINTERS) + 1, 3, 1)
        return grid

    # Events tab: the routing grid
    def _events_page(self):
        scroll = Gtk.ScrolledWindow()
        grid = Gtk.Grid(column_spacing=10, row_spacing=4, margin=12)
        scroll.add(grid)
        headers = ["Document type", "Enabled", "Auto Print", "Qty", "Printer",
                   "Duplex", "Rotate 90°"]
        for c, h in enumerate(headers):
            grid.attach(Gtk.Label(label=h, xalign=0), c, 0, 1, 1)
        routing = self.cfg.get("routing") or {}
        self.event_rows = {}
        for r, (key, title) in enumerate(DOC_TYPES, start=1):
            route = routing.get(key) or {}
            grid.attach(Gtk.Label(label=title, xalign=0), 0, r, 1, 1)
            enabled = Gtk.CheckButton(active=bool(route.get("enabled")))
            auto = Gtk.CheckButton(active=bool(route.get("auto_print")))
            qty = Gtk.SpinButton.new_with_range(1, 99, 1)
            qty.set_value(route.get("quantity") or 1)
            printer = Gtk.ComboBoxText()
            for p in LOGICAL_PRINTERS:
                printer.append_text(p)
            printer.set_active(LOGICAL_PRINTERS.index(route.get("printer"))
                               if route.get("printer") in LOGICAL_PRINTERS else 0)
            duplex = Gtk.ComboBoxText()
            for d in DUPLEX_CHOICES:
                duplex.append_text(d)
            duplex.set_active(DUPLEX_CHOICES.index(route.get("duplex"))
                              if route.get("duplex") in DUPLEX_CHOICES else 0)
            rotate = Gtk.CheckButton(active=bool(route.get("rotate")))
            for c, w in enumerate([enabled, auto, qty, printer, duplex, rotate], start=1):
                grid.attach(w, c, r, 1, 1)
            self.event_rows[key] = (enabled, auto, qty, printer, duplex, rotate)
        return scroll

    def _on_save(self, _btn):
        printers = {}
        for key, (combo, opts) in self.printer_rows.items():
            name = combo.get_active_text()
            if name and name != "(unset)":
                options = [o.strip() for o in opts.get_text().split(",") if o.strip()]
                printers[key] = {"cups_name": name, "options": options}
        routing = {}
        titles = dict(DOC_TYPES)
        problems = []
        for key, (enabled, auto, qty, printer, duplex, rotate) in self.event_rows.items():
            if not enabled.get_active() and not auto.get_active():
                continue  # unconfigured types stay out of the config entirely
            role = printer.get_active_text()
            if role not in printers:
                problems.append(f"• {titles.get(key, key)} uses printer role “{role}”, "
                                f"which has no CUPS queue assigned")
            routing[key] = {
                "enabled": enabled.get_active(),
                "auto_print": auto.get_active(),
                "quantity": int(qty.get_value()),
                "printer": role,
                "duplex": duplex.get_active_text(),
                "rotate": rotate.get_active(),
            }
        if problems:
            dlg = Gtk.MessageDialog(
                message_type=Gtk.MessageType.WARNING, buttons=Gtk.ButtonsType.OK,
                text="Fix these before saving:")
            dlg.format_secondary_text(
                "\n".join(problems)
                + "\n\nAssign a CUPS queue to that role in the Printers tab, "
                  "or pick a different printer for the document type.")
            dlg.run()
            dlg.destroy()
            return
        update = {
            "account": {
                "host": self.host_combo.get_active_text(),
                "subdomain": self.subdomain_entry.get_text().strip(),
                "api_token": self.token_entry.get_text().strip(),
            },
            "printers": printers,
            "routing": routing,
            "register_printer": (None if self.register_combo.get_active() == 0
                                 else self.register_combo.get_active_text()),
            "location_id": (int(self.location_entry.get_text())
                            if self.location_entry.get_text().strip().isdigit() else None),
        }
        result = self.applet._safe_call("set_config", config=update)
        if result is not None:
            self.cfg = result
            dlg = Gtk.MessageDialog(message_type=Gtk.MessageType.INFO,
                                    buttons=Gtk.ButtonsType.OK,
                                    text="Settings saved — daemon reloaded live.")
            dlg.run()
            dlg.destroy()


def main():
    Applet()
    Gtk.main()


if __name__ == "__main__":
    main()
