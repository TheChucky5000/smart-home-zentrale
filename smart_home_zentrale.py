#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Smart Home Zentrale 5.0
=======================
Desktop-Steuerzentrale für dein Smart Home über Home Assistant.

Neu in 5.0 - Design:
  - Komplett modernisierte Oberfläche: Seitenleisten-Navigation mit
    Zählern statt Tab-Leiste, Hover-Effekte auf allen Karten,
    echte Kippschalter statt An/Aus-Knöpfe, neue Farbpalette
  - Kompakte Ansicht zuschaltbar (mehr Geräte auf einen Blick)

Neu in 5.0 - Updater & Installation:
  - 🔄 Eingebauter Updater: prüft eine version.json im Internet (z. B. auf
    GitHub), meldet neue Versionen und installiert sie auf Wunsch selbst -
    als Exe über einen Austausch-Mechanismus, als .py durch Neustart
  - 📦 Inno-Setup-Skript (installer.iss) für einen richtigen
    Windows-Installer mit Startmenü, Desktop-Symbol und Autostart-Option

Neu in 5.0 - Funktionen:
  - 🌡️ Heizung: Solltemperatur direkt an der Karte mit −/+ in
    0,5-°C-Schritten verstellen
  - 🚪 "Fenster lange offen"-Warnung: meldet, wenn Tür/Fenster länger als
    X Minuten offen steht (in den Einstellungen einstellbar)
  - 📜 Protokoll mit Tagesstatistik (Schaltvorgänge, Türöffnungen heute)
  - 📋 Rechtsklick -> Entity-ID kopieren

Aus 4.0: Performance-Engine (Änderungserkennung, Lazy Rendering, gzip),
Farbwahl, Ausschalt-Timer, Energie-Kostenschätzung, Diagramm-Zeiträume,
Backup. Aus 3.0: Favoriten, Szenen, Benachrichtigungen, Verläufe, Räume,
Protokoll, Tray, Mini-Modus. Aus 2.0: Auto-Aufräumen, eigene Kategorien.

Benötigt nur die Python-Standardbibliothek. Optional für das Tray-Symbol:
    pip install pystray pillow
Als .exe baubar mit:
    pyinstaller --onefile --noconsole smart_home_zentrale.py
"""

import gzip
import json
import os
import subprocess
import sys
import threading
import time
import webbrowser
from collections import deque
from datetime import datetime, timedelta, timezone
import tkinter as tk
from tkinter import ttk, messagebox, simpledialog, colorchooser, filedialog
from urllib import request, error, parse

APP_NAME = "Smart Home Zentrale"
APP_VERSION = "5.0"

try:
    import pystray
    from PIL import Image, ImageDraw
    TRAY_AVAILABLE = True
except Exception:
    TRAY_AVAILABLE = False

try:
    import winsound
    SOUND_AVAILABLE = True
except Exception:
    SOUND_AVAILABLE = False


# ----------------------------------------------------------------------------
# Konfiguration (%APPDATA%\SmartHomeZentrale\config.json)
# ----------------------------------------------------------------------------

def config_dir():
    if sys.platform.startswith("win"):
        base = os.environ.get("APPDATA", os.path.expanduser("~"))
    else:
        base = os.path.join(os.path.expanduser("~"), ".config")
    d = os.path.join(base, "SmartHomeZentrale")
    os.makedirs(d, exist_ok=True)
    return d


CONFIG_PATH = os.path.join(config_dir(), "config.json")
EVENTLOG_PATH = os.path.join(config_dir(), "ereignisse.log")

DEFAULT_CONFIG = {
    "ha_url": "http://homeassistant.local:8123",
    "token": "",
    "refresh_seconds": 10,
    "custom_categories": [],
    "entity_overrides": {},
    "favorites": [],
    "show_hidden": False,
    "view_mode": "Kategorien",
    "notify_doors": True,
    "notify_locks": True,
    "notify_battery": True,
    "notify_sound": True,
    "fuel_alert_enabled": False,
    "fuel_alert_price": 1.65,
    "minimize_to_tray": True,
    "start_minimized": False,
    "strompreis": 0.0,
    "compact": False,
    "window_open_minutes": 0,     # 0 = Warnung aus
    "update_url": "",             # Adresse einer version.json (z. B. GitHub)
    "auto_update_check": True,
}


def load_config():
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        merged = dict(DEFAULT_CONFIG)
        merged.update(cfg)
        if not isinstance(merged.get("entity_overrides"), dict):
            merged["entity_overrides"] = {}
        for key in ("custom_categories", "favorites"):
            if not isinstance(merged.get(key), list):
                merged[key] = []
        return merged
    except Exception:
        return dict(DEFAULT_CONFIG)


def save_config(cfg):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)


def version_tuple(v):
    """'5.1.2' -> (5, 1, 2) für Versionsvergleiche."""
    parts = []
    for chunk in str(v).split("."):
        digits = "".join(ch for ch in chunk if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts)


# ----------------------------------------------------------------------------
# Home Assistant REST-API Client (mit gzip-Kompression)
# ----------------------------------------------------------------------------

class HAClient:
    def __init__(self, base_url, token, timeout=10):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout

    def _request(self, path, payload=None):
        url = self.base_url + path
        data = None
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
            "Accept-Encoding": "gzip",
        }
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
        req = request.Request(url, data=data, headers=headers,
                              method="POST" if payload is not None else "GET")
        with request.urlopen(req, timeout=self.timeout) as resp:
            body = resp.read()
            if resp.headers.get("Content-Encoding", "") == "gzip":
                body = gzip.decompress(body)
            return body.decode("utf-8")

    def _json(self, path, payload=None):
        body = self._request(path, payload)
        return json.loads(body) if body else None

    def states(self):
        return self._json("/api/states")

    def call_service(self, domain, service, data):
        return self._json(f"/api/services/{domain}/{service}", payload=data)

    def history(self, entity_id, hours=24):
        start = (datetime.now(timezone.utc) - timedelta(hours=hours))
        start_iso = start.isoformat().replace("+00:00", "Z")
        path = (f"/api/history/period/{parse.quote(start_iso)}"
                f"?filter_entity_id={parse.quote(entity_id)}"
                f"&minimal_response&no_attributes")
        return self._json(path)

    def template(self, tmpl):
        return self._request("/api/template", payload={"template": tmpl})

    def fetch_areas(self):
        tmpl = ("{% for s in states %}{{ s.entity_id }}|"
                "{{ area_name(s.entity_id) or '' }}\n{% endfor %}")
        text = self.template(tmpl)
        areas = {}
        for line in text.splitlines():
            if "|" in line:
                eid, area = line.split("|", 1)
                areas[eid.strip()] = area.strip()
        return areas


def http_get_bytes(url, timeout=60):
    """Einfacher Download (für Updater)."""
    req = request.Request(url, headers={"User-Agent": f"{APP_NAME}/{APP_VERSION}",
                                        "Cache-Control": "no-cache"})
    with request.urlopen(req, timeout=timeout) as resp:
        body = resp.read()
        if resp.headers.get("Content-Encoding", "") == "gzip":
            body = gzip.decompress(body)
        return body


# ----------------------------------------------------------------------------
# Automatische Kategorisierung
# ----------------------------------------------------------------------------

HIDDEN = "__hidden__"
TAB_FAVORITES = "Favoriten"
TAB_SCENES = "Szenen & Aktionen"
TAB_LOG = "Protokoll"
TAB_HIDDEN = "Ausgeblendet"
TAB_NO_ROOM = "Ohne Raum"

TAB_ICONS = {
    TAB_FAVORITES: "⭐",
    "Lichter": "💡",
    "Schalter & Steckdosen": "🔌",
    "Türschloss": "🔒",
    "Tür & Fenster": "🚪",
    "Heizung": "🌡️",
    "Tankstellen": "⛽",
    "Energie": "⚡",
    "Wetter": "🌤️",
    "Sensoren": "📊",
    TAB_SCENES: "🎬",
    TAB_LOG: "📜",
    TAB_HIDDEN: "🙈",
    TAB_NO_ROOM: "❔",
}
DEFAULT_ICON = "📁"

STANDARD_TAB_ORDER = [
    "Lichter", "Schalter & Steckdosen", "Türschloss", "Tür & Fenster",
    "Heizung", "Tankstellen", "Energie", "Wetter", "Sensoren", TAB_SCENES,
]

DOOR_KEYWORDS = ("tür", "tuer", "door", "fenster", "window", "contact", "kontakt")

FUEL_KEYWORDS = ("tankstelle", "tankerkoenig", "tankerkönig", "benzin",
                 "diesel", "super_e", "_e5", "_e10", " e5", " e10",
                 "spritpreis", "kraftstoff", "fuel_price")

ENERGY_KEYWORDS = ("stromverbrauch", "energieverbrauch", "energy", "power",
                   "leistung", "verbrauch", "einspeisung", "solar", "pv_",
                   "wallbox")

WEATHER_KEYWORDS = ("wetter", "weather", "regen", "rain", "wind",
                    "außentemperatur", "aussentemperatur", "outdoor_temp",
                    "luftfeucht", "humidity_out", "uv_index", "pollen")

JUNK_DEVICE_CLASSES = ("battery", "signal_strength", "timestamp", "data_size",
                       "data_rate", "duration")
JUNK_KEYWORDS = ("rssi", "linkquality", "lqi", "signal", "firmware", "uptime",
                 "ip_address", "ip-adresse", "ssid", "wifi", "wlan_", "mac",
                 "duty_cycle", "dutycycle", "duty cycle", "carrier_sense",
                 "carriersense", "last_seen", "zuletzt gesehen", "restart",
                 "neustart", "reboot", "heartbeat", "sabotage",
                 "config_pending", "update_available", "operating_voltage",
                 "betriebsspannung", "cloud_conn", "verbindung", "connectivity",
                 "identify", "bluetooth", "_led", "hersteller")
JUNK_DOMAINS = ("update", "button", "event", "tts", "stt", "conversation",
                "person", "zone", "sun", "device_tracker", "remote",
                "select", "number", "input_boolean", "input_number",
                "input_select", "input_text", "camera", "media_player",
                "todo", "calendar")

SCENE_DOMAINS = ("scene", "script", "automation")

COLOR_MODES = ("hs", "rgb", "rgbw", "rgbww", "xy")


def friendly_name(entity):
    return entity.get("attributes", {}).get("friendly_name", entity["entity_id"])


def _matches(entity, keywords):
    name = friendly_name(entity).lower()
    eid = entity["entity_id"].lower()
    return any(k in name or k in eid for k in keywords)


def is_junk(entity):
    eid = entity["entity_id"]
    domain = eid.split(".", 1)[0]
    attrs = entity.get("attributes", {})
    if domain in JUNK_DOMAINS:
        return True
    if attrs.get("device_class") in JUNK_DEVICE_CLASSES:
        return True
    if domain in ("sensor", "binary_sensor") and _matches(entity, JUNK_KEYWORDS):
        return True
    return False


def auto_category(entity):
    eid = entity["entity_id"]
    domain = eid.split(".", 1)[0]
    attrs = entity.get("attributes", {})
    device_class = attrs.get("device_class", "")

    if domain in SCENE_DOMAINS:
        return TAB_SCENES
    if is_junk(entity):
        return HIDDEN

    if domain == "light":
        return "Lichter"
    if domain == "switch":
        return "Schalter & Steckdosen"
    if domain == "lock":
        return "Türschloss"
    if domain == "climate":
        return "Heizung"
    if domain == "cover":
        return "Schalter & Steckdosen"
    if domain == "weather":
        return "Wetter"

    if domain == "binary_sensor":
        name = friendly_name(entity).lower()
        if device_class in ("door", "window", "opening", "garage_door") or \
           any(k in name for k in DOOR_KEYWORDS) or \
           any(k in eid.lower() for k in DOOR_KEYWORDS):
            return "Tür & Fenster"
        return "Sensoren"

    if domain == "sensor":
        unit = str(attrs.get("unit_of_measurement", ""))
        if _matches(entity, FUEL_KEYWORDS) or unit in ("€/L", "EUR/L", "ct/L"):
            return "Tankstellen"
        if device_class in ("power", "energy", "current", "voltage") or \
           unit in ("W", "kW", "kWh", "Wh", "A", "V") or \
           _matches(entity, ENERGY_KEYWORDS):
            return "Energie"
        if _matches(entity, WEATHER_KEYWORDS):
            return "Wetter"
        return "Sensoren"

    return HIDDEN


def categorize(entity, cfg):
    ov = cfg.get("entity_overrides", {}).get(entity["entity_id"], {})
    if ov.get("hidden"):
        return HIDDEN
    if ov.get("category"):
        return ov["category"]
    return auto_category(entity)


def is_door_or_window(entity):
    attrs = entity.get("attributes", {})
    if attrs.get("device_class") in ("door", "window", "opening", "garage_door"):
        return True
    return entity["entity_id"].startswith("binary_sensor.") and \
        _matches(entity, DOOR_KEYWORDS)


def is_fuel(entity):
    unit = str(entity.get("attributes", {}).get("unit_of_measurement", ""))
    return entity["entity_id"].startswith("sensor.") and \
        (_matches(entity, FUEL_KEYWORDS) or unit in ("€/L", "EUR/L", "ct/L"))


def power_watts(entity):
    attrs = entity.get("attributes", {})
    unit = str(attrs.get("unit_of_measurement", ""))
    val = numeric_state(entity)
    if val is None:
        return None
    if unit == "W" or attrs.get("device_class") == "power" and unit != "kW":
        return val
    if unit == "kW":
        return val * 1000.0
    return None


def state_text(entity):
    s = entity["state"]
    attrs = entity.get("attributes", {})
    unit = attrs.get("unit_of_measurement", "")
    mapping = {
        "on": "An", "off": "Aus",
        "locked": "Verriegelt", "unlocked": "Entriegelt",
        "locking": "Verriegelt…", "unlocking": "Entriegelt…",
        "open": "Offen", "closed": "Geschlossen",
        "unavailable": "Nicht erreichbar", "unknown": "Unbekannt",
        "heat": "Heizen", "auto": "Automatik", "idle": "Bereit",
    }
    txt = mapping.get(s, s)
    if unit:
        txt = f"{txt} {unit}"
    return txt


def numeric_state(entity):
    try:
        return float(entity["state"])
    except (ValueError, TypeError):
        return None


def sort_key(entity):
    unavailable = entity["state"] in ("unavailable", "unknown")
    return (1 if unavailable else 0, friendly_name(entity).lower())


def entity_sig(entity):
    """Kompakte Signatur zur Änderungserkennung (Performance)."""
    return (entity["entity_id"],
            entity.get("last_updated") or entity["state"])


# ----------------------------------------------------------------------------
# Modernes Design: Farbpalette & Kippschalter
# ----------------------------------------------------------------------------

BG = "#13141a"          # Fensterhintergrund
SIDEBAR = "#0e0f14"     # Seitenleiste
NAV_SEL = "#1e2130"     # ausgewählter Navigationspunkt
CARD = "#1d1f28"        # Karten
CARD_HOVER = "#252836"  # Karten bei Maus darüber
CARD_ON = "#20304a"     # aktive Geräte
CARD_ON_HOVER = "#273a58"
FG = "#eceef4"
FG_DIM = "#8f93a3"
FG_OFF = "#565a68"
ACCENT = "#6d8dff"      # modernes Indigo-Blau
ACCENT_DARK = "#4f6fe0"
OK = "#4ecb8d"
WARN = "#e5a63c"
ERR = "#e56767"
TRACK_OFF = "#3a3e4d"


class ToggleSwitch(tk.Canvas):
    """Moderner Kippschalter statt An/Aus-Knopf."""

    W, H = 46, 24

    def __init__(self, parent, is_on, command, bg):
        super().__init__(parent, width=self.W, height=self.H, bg=bg,
                         highlightthickness=0, cursor="hand2")
        self._on = bool(is_on)
        self._command = command
        self._draw()
        self.bind("<Button-1>", self._click)

    def _draw(self):
        self.delete("all")
        track = ACCENT if self._on else TRACK_OFF
        r = self.H // 2
        # Abgerundete "Pille": zwei Kreise + Rechteck
        self.create_oval(1, 1, self.H - 1, self.H - 1, fill=track, outline="")
        self.create_oval(self.W - self.H + 1, 1, self.W - 1, self.H - 1,
                         fill=track, outline="")
        self.create_rectangle(r, 1, self.W - r, self.H - 1, fill=track,
                              outline="")
        # Knopf
        if self._on:
            x0 = self.W - self.H + 3
        else:
            x0 = 3
        self.create_oval(x0, 3, x0 + self.H - 6, self.H - 3,
                         fill="#ffffff", outline="")

    def _click(self, _event):
        # Sofortiges optisches Feedback, Aktion im Hintergrund
        self._on = not self._on
        self._draw()
        self._command()


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(f"{APP_NAME} {APP_VERSION}")
        self.geometry("1120x740")
        self.minsize(880, 560)
        self.configure(bg=BG)

        self.cfg = load_config()
        self.client = None
        self.entities = []
        self.entity_by_id = {}
        self.areas = {}
        self.filter_text = tk.StringVar()
        self.status_var = tk.StringVar(value="Nicht verbunden")
        self.view_var = tk.StringVar(value=self.cfg.get("view_mode", "Kategorien"))
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._prev_states = {}
        self._alerted = {}
        self._toasts = []
        self._tray = None
        self._mini = None
        self._timers = {}
        self._open_since = {}             # Tür/Fenster offen seit (Zeitstempel)
        self._open_alerted = set()
        self.event_log = deque(maxlen=300)
        self._load_event_log()

        # Performance / Rendering
        self._snapshot = None
        self._tab_data = {}               # Name -> (kind, items, extra)
        self._tab_sigs = {}               # Name -> Signatur
        self._visible_tabs = []
        self._sidebar_sig = None
        self._nav_items = {}              # Name -> (frame, indicator, lbl, cnt)
        self.current_tab = TAB_FAVORITES
        self._content_tab = None
        self._content_sig = None
        self._search_job = None
        self._status_extra = ""
        self._mini_sig = None

        self._build_ui()
        self._bind_keys()

        self.protocol("WM_DELETE_WINDOW", self.on_close)

        if not self.cfg.get("token"):
            self.after(300, self.open_settings)
        else:
            self.connect()

        self._poll_thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._poll_thread.start()

        if TRAY_AVAILABLE and self.cfg.get("minimize_to_tray"):
            self._setup_tray()
            if self.cfg.get("start_minimized"):
                self.after(200, self.withdraw)

        if self.cfg.get("auto_update_check") and \
           self.cfg.get("update_url", "").strip():
            self.after(3000, lambda: self.check_updates(silent=True))

    # ---------------- Oberfläche ----------------
    def _build_ui(self):
        # ----- Seitenleiste -----
        self.sidebar = tk.Frame(self, bg=SIDEBAR, width=216)
        self.sidebar.pack(side="left", fill="y")
        self.sidebar.pack_propagate(False)

        logo = tk.Frame(self.sidebar, bg=SIDEBAR)
        logo.pack(fill="x", padx=16, pady=(18, 6))
        tk.Label(logo, text="🏠", bg=SIDEBAR, fg=FG,
                 font=("Segoe UI", 20)).pack(side="left")
        box = tk.Frame(logo, bg=SIDEBAR)
        box.pack(side="left", padx=(8, 0))
        tk.Label(box, text="Smart Home", bg=SIDEBAR, fg=FG, anchor="w",
                 font=("Segoe UI Semibold", 13)).pack(fill="x")
        tk.Label(box, text=f"Zentrale {APP_VERSION}", bg=SIDEBAR, fg=FG_DIM,
                 anchor="w", font=("Segoe UI", 9)).pack(fill="x")

        tk.Frame(self.sidebar, bg="#1c1e29", height=1).pack(fill="x",
                                                            padx=14, pady=8)

        self.nav_container = tk.Frame(self.sidebar, bg=SIDEBAR)
        self.nav_container.pack(fill="both", expand=True)

        # Untere Knöpfe der Seitenleiste
        bottom = tk.Frame(self.sidebar, bg=SIDEBAR)
        bottom.pack(fill="x", side="bottom", pady=10)
        for text, cmd in (("⚙  Einstellungen", self.open_settings),
                          ("🔄  Nach Updates suchen",
                           lambda: self.check_updates(silent=False)),
                          ("🗕  Mini-Modus", self.open_mini)):
            b = tk.Label(bottom, text=text, bg=SIDEBAR, fg=FG_DIM, anchor="w",
                         padx=18, pady=6, cursor="hand2",
                         font=("Segoe UI", 10))
            b.pack(fill="x")
            b.bind("<Button-1>", lambda e, c=cmd: c())
            b.bind("<Enter>", lambda e, w=b: w.config(bg=NAV_SEL, fg=FG))
            b.bind("<Leave>", lambda e, w=b: w.config(bg=SIDEBAR, fg=FG_DIM))

        # ----- Rechter Bereich -----
        right = tk.Frame(self, bg=BG)
        right.pack(side="left", fill="both", expand=True)

        topbar = tk.Frame(right, bg=BG)
        topbar.pack(fill="x", padx=18, pady=(14, 6))

        self.section_title = tk.Label(topbar, text="⭐ Favoriten", bg=BG, fg=FG,
                                      font=("Segoe UI Semibold", 16))
        self.section_title.pack(side="left")

        self.view_btn = tk.Button(topbar, text="", command=self.toggle_view,
                                  bg=CARD, fg=FG, relief="flat", padx=12,
                                  pady=5, cursor="hand2",
                                  activebackground=ACCENT,
                                  activeforeground="#fff",
                                  font=("Segoe UI", 10))
        self.view_btn.pack(side="right", padx=(8, 0))
        self._update_view_button()

        refresh_btn = tk.Button(topbar, text="⟳", command=self.refresh_async,
                                bg=CARD, fg=FG, relief="flat", padx=12, pady=5,
                                cursor="hand2", activebackground=ACCENT,
                                activeforeground="#fff",
                                font=("Segoe UI", 10))
        refresh_btn.pack(side="right", padx=(8, 0))

        search_box = tk.Frame(topbar, bg=CARD)
        search_box.pack(side="right")
        tk.Label(search_box, text="🔍", bg=CARD, fg=FG_DIM,
                 font=("Segoe UI", 10)).pack(side="left", padx=(10, 2))
        self.search_entry = tk.Entry(search_box, textvariable=self.filter_text,
                                     bg=CARD, fg=FG, insertbackground=FG,
                                     relief="flat", font=("Segoe UI", 10),
                                     width=22)
        self.search_entry.pack(side="left", ipady=6, padx=(0, 10))
        self.filter_text.trace_add("write", self._on_search_changed)

        # ----- Inhaltsbereich (eine scrollbare Fläche) -----
        holder = tk.Frame(right, bg=BG)
        holder.pack(fill="both", expand=True, padx=12, pady=(0, 4))
        self._canvas = tk.Canvas(holder, bg=BG, highlightthickness=0)
        vsb = ttk.Scrollbar(holder, orient="vertical",
                            command=self._canvas.yview)
        self.content = tk.Frame(self._canvas, bg=BG)
        inner_id = self._canvas.create_window((0, 0), window=self.content,
                                              anchor="nw")

        def on_conf(event):
            self._canvas.configure(scrollregion=self._canvas.bbox("all"))
            self._canvas.itemconfigure(inner_id,
                                       width=self._canvas.winfo_width())
        self.content.bind("<Configure>", on_conf)
        self._canvas.bind("<Configure>", on_conf)
        self._canvas.configure(yscrollcommand=vsb.set)
        self._canvas.bind("<Enter>", lambda e: self.bind_all(
            "<MouseWheel>", lambda ev: self._canvas.yview_scroll(
                int(-1 * (ev.delta / 120)), "units")))
        self._canvas.bind("<Leave>",
                          lambda e: self.unbind_all("<MouseWheel>"))
        self._canvas.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")

        # ----- Statusleiste -----
        status = tk.Frame(right, bg=SIDEBAR)
        status.pack(fill="x", side="bottom")
        tk.Label(status, textvariable=self.status_var, bg=SIDEBAR, fg=FG_DIM,
                 anchor="w", font=("Segoe UI", 9)).pack(fill="x", padx=14,
                                                        pady=5)

        ttk.Style(self).theme_use("clam")

    def _bind_keys(self):
        self.bind("<F5>", lambda e: self.refresh_async())
        self.bind("<Control-f>", lambda e: self.search_entry.focus_set())

    def _on_search_changed(self, *args):
        if self._search_job:
            try:
                self.after_cancel(self._search_job)
            except tk.TclError:
                pass
        self._search_job = self.after(250, lambda: self.render(force=True))

    def _update_view_button(self):
        mode = self.view_var.get()
        self.view_btn.config(text="🏠 Räume" if mode == "Räume"
                             else "🗂 Kategorien")

    def toggle_view(self):
        new = "Räume" if self.view_var.get() == "Kategorien" else "Kategorien"
        self.view_var.set(new)
        self.cfg["view_mode"] = new
        save_config(self.cfg)
        self._update_view_button()
        if new == "Räume" and not self.areas:
            self._set_status("Lade Räume aus Home Assistant …")
            threading.Thread(target=self._fetch_areas_then_render,
                             daemon=True).start()
        else:
            self.render(force=True)

    def _fetch_areas_then_render(self):
        try:
            self.areas = self.client.fetch_areas()
        except Exception:
            self.areas = {}
        try:
            self.after(0, lambda: self.render(force=True))
        except tk.TclError:
            pass

    # ---------------- Seitenleisten-Navigation ----------------
    def _rebuild_sidebar(self, visible, counts):
        for child in self.nav_container.winfo_children():
            child.destroy()
        self._nav_items.clear()

        for name in visible:
            item = tk.Frame(self.nav_container, bg=SIDEBAR, cursor="hand2")
            item.pack(fill="x")
            indicator = tk.Frame(item, bg=SIDEBAR, width=3)
            indicator.pack(side="left", fill="y")
            icon = TAB_ICONS.get(name, DEFAULT_ICON)
            lbl = tk.Label(item, text=f"{icon}  {name}", bg=SIDEBAR, fg=FG_DIM,
                           anchor="w", padx=12, pady=7,
                           font=("Segoe UI", 10))
            lbl.pack(side="left", fill="x", expand=True)
            cnt_text = str(counts.get(name, "") or "")
            cnt = tk.Label(item, text=cnt_text, bg=SIDEBAR, fg=FG_OFF,
                           padx=12, font=("Segoe UI", 9))
            cnt.pack(side="right")

            for w in (item, lbl, cnt):
                w.bind("<Button-1>", lambda e, n=name: self._select_tab(n))
                w.bind("<Enter>", lambda e, n=name: self._nav_hover(n, True))
                w.bind("<Leave>", lambda e, n=name: self._nav_hover(n, False))
            self._nav_items[name] = (item, indicator, lbl, cnt)

        self._style_nav()

    def _nav_hover(self, name, entering):
        if name == self.current_tab:
            return
        item, indicator, lbl, cnt = self._nav_items.get(name, (None,) * 4)
        if not item:
            return
        bg = NAV_SEL if entering else SIDEBAR
        for w in (item, indicator, lbl, cnt):
            w.config(bg=bg)
        lbl.config(fg=FG if entering else FG_DIM)

    def _style_nav(self):
        for name, (item, indicator, lbl, cnt) in self._nav_items.items():
            sel = name == self.current_tab
            bg = NAV_SEL if sel else SIDEBAR
            for w in (item, lbl, cnt):
                w.config(bg=bg)
            indicator.config(bg=ACCENT if sel else bg)
            lbl.config(fg=FG if sel else FG_DIM)

    def _select_tab(self, name):
        self.current_tab = name
        self._style_nav()
        icon = TAB_ICONS.get(name, DEFAULT_ICON)
        self.section_title.config(text=f"{icon} {name}")
        self._fill_content(name)

    # ---------------- Verbindung & Aktualisierung ----------------
    def connect(self):
        self.client = HAClient(self.cfg["ha_url"], self.cfg["token"])
        self._snapshot = None
        self.refresh_async()
        threading.Thread(target=self._fetch_areas_then_render,
                         daemon=True).start()

    def refresh_async(self):
        threading.Thread(target=self._refresh, daemon=True).start()

    def _refresh(self):
        if not self.client or not self.cfg.get("token"):
            self._set_status("Kein Zugangstoken hinterlegt - bitte "
                             "Einstellungen öffnen.")
            return
        try:
            states = self.client.states()
        except error.HTTPError as e:
            if e.code == 401:
                self._set_status("Zugangstoken ungültig (401). Bitte in den "
                                 "Einstellungen prüfen.")
            else:
                self._set_status(f"Home Assistant Fehler: HTTP {e.code}")
            return
        except Exception as e:
            self._set_status(f"Keine Verbindung zu {self.cfg['ha_url']} - "
                             f"{e.__class__.__name__}")
            return

        snapshot = hash(tuple(sorted(entity_sig(e) for e in states)))
        unchanged = snapshot == self._snapshot
        self._snapshot = snapshot

        if not unchanged:
            with self._lock:
                self.entities = states
                self.entity_by_id = {e["entity_id"]: e for e in states}
            self._detect_changes(states)
            self._status_extra = self._compute_status_extra(states)

        self._check_open_warnings()

        self._set_status(
            f"Verbunden  •  {len(states)} Entitäten"
            f"{self._status_extra}  •  {time.strftime('%H:%M:%S')}")

        if not unchanged:
            try:
                self.after(0, self.render)
            except tk.TclError:
                pass

    def _compute_status_extra(self, states):
        open_doors = 0
        offline = 0
        for e in states:
            if e["entity_id"].startswith("binary_sensor.") and \
               is_door_or_window(e) and e["state"] == "on":
                open_doors += 1
            elif e["state"] == "unavailable" and \
                    categorize(e, self.cfg) != HIDDEN:
                offline += 1
        parts = ""
        if open_doors:
            parts += f"  •  🚪 {open_doors} offen"
        if offline:
            parts += f"  •  ⚠ {offline} offline"
        return parts

    def _set_status(self, text):
        try:
            self.after(0, lambda: self.status_var.set(text))
        except tk.TclError:
            pass

    def _poll_loop(self):
        while not self._stop.is_set():
            interval = max(3, int(self.cfg.get("refresh_seconds", 10)))
            for _ in range(interval * 2):
                if self._stop.is_set():
                    return
                time.sleep(0.5)
            self._refresh()

    # ---------------- Ereignisse, Protokoll & Benachrichtigungen ----------
    def _load_event_log(self):
        try:
            with open(EVENTLOG_PATH, "r", encoding="utf-8") as f:
                lines = f.readlines()[-300:]
            for line in lines:
                line = line.rstrip("\n")
                if line:
                    self.event_log.append(line)
        except Exception:
            pass

    def _log_event(self, text):
        stamp = time.strftime("%d.%m. %H:%M:%S")
        line = f"{stamp}  {text}"
        self.event_log.append(line)
        try:
            with open(EVENTLOG_PATH, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass

    def clear_event_log(self):
        if not messagebox.askyesno("Protokoll", "Ereignisprotokoll leeren?"):
            return
        self.event_log.clear()
        try:
            open(EVENTLOG_PATH, "w", encoding="utf-8").close()
        except Exception:
            pass
        self.render(force=True)

    def _detect_changes(self, states):
        prev = self._prev_states
        first_run = not prev
        today = time.strftime("%Y-%m-%d")

        for e in states:
            eid = e["entity_id"]
            domain = eid.split(".", 1)[0]
            new = e["state"]
            old = prev.get(eid)
            name = friendly_name(e)

            # Offen-seit-Verfolgung für "lange offen"-Warnung
            if domain == "binary_sensor" and is_door_or_window(e):
                if new == "on" and eid not in self._open_since:
                    self._open_since[eid] = time.time()
                elif new != "on":
                    self._open_since.pop(eid, None)
                    self._open_alerted.discard(eid)

            if not first_run and old is not None and old != new:
                if domain == "lock":
                    self._log_event(f"🔒 {name}: {state_text(e)}")
                    if new == "unlocked" and self.cfg.get("notify_locks"):
                        self.toast("Schloss entriegelt", name, WARN)
                elif domain == "binary_sensor" and is_door_or_window(e):
                    opened = new == "on"
                    self._log_event(f"🚪 {name}: "
                                    f"{'geöffnet' if opened else 'geschlossen'}")
                    if opened and self.cfg.get("notify_doors"):
                        self.toast("Geöffnet", name, WARN)
                elif domain in ("light", "switch") and new in ("on", "off"):
                    self._log_event(
                        f"{'💡' if domain == 'light' else '🔌'} {name}: "
                        f"{'An' if new == 'on' else 'Aus'}")
                elif domain == "cover":
                    self._log_event(f"🪟 {name}: {state_text(e)}")
                elif old == "unavailable" and new != "unavailable":
                    self._log_event(f"✅ {name} ist wieder erreichbar")
                elif new == "unavailable":
                    self._log_event(f"⚠️ {name} ist nicht mehr erreichbar")

            if self.cfg.get("notify_battery") and \
               e.get("attributes", {}).get("device_class") == "battery":
                val = numeric_state(e)
                if val is not None and val <= 15 and \
                   self._alerted.get("bat_" + eid) != today:
                    self._alerted["bat_" + eid] = today
                    self.toast("Batterie schwach", f"{name}: {val:.0f} %", ERR)
                    self._log_event(f"🪫 {name}: Batterie {val:.0f} %")

            if self.cfg.get("fuel_alert_enabled") and is_fuel(e):
                val = numeric_state(e)
                try:
                    limit = float(self.cfg.get("fuel_alert_price", 0))
                except (TypeError, ValueError):
                    limit = 0
                if val is not None and limit > 0 and val <= limit and \
                   self._alerted.get("fuel_" + eid) != today:
                    self._alerted["fuel_" + eid] = today
                    self.toast("⛽ Spritpreis-Alarm",
                               f"{name}: {val:.3f} €".replace(".", ","), OK)
                    self._log_event(f"⛽ {name}: {val:.3f} € "
                                    f"(unter {limit:.2f} €)".replace(".", ","))

        self._prev_states = {e["entity_id"]: e["state"] for e in states}

    def _check_open_warnings(self):
        """Warnt, wenn Tür/Fenster länger als eingestellt offen steht."""
        try:
            minutes = int(self.cfg.get("window_open_minutes", 0))
        except (TypeError, ValueError):
            minutes = 0
        if minutes <= 0:
            return
        now = time.time()
        for eid, since in list(self._open_since.items()):
            if eid in self._open_alerted:
                continue
            if now - since >= minutes * 60:
                self._open_alerted.add(eid)
                ent = self.entity_by_id.get(eid)
                name = friendly_name(ent) if ent else eid
                self.toast("🚪 Lange offen",
                           f"{name} ist seit über {minutes} Minuten offen!",
                           ERR)
                self._log_event(f"🚪 {name}: seit über {minutes} min offen")

    # ---------------- Toast-Benachrichtigungen ----------------
    def toast(self, title, message, color=ACCENT):
        try:
            self.after(0, lambda: self._show_toast(title, message, color))
        except tk.TclError:
            pass

    def _show_toast(self, title, message, color):
        win = tk.Toplevel(self)
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        win.configure(bg=color)

        inner = tk.Frame(win, bg=CARD)
        inner.pack(fill="both", expand=True, padx=2, pady=2)
        tk.Label(inner, text=title, bg=CARD, fg=color, anchor="w",
                 font=("Segoe UI Semibold", 11)).pack(fill="x", padx=12,
                                                      pady=(8, 0))
        tk.Label(inner, text=message, bg=CARD, fg=FG, anchor="w",
                 wraplength=280, justify="left",
                 font=("Segoe UI", 10)).pack(fill="x", padx=12, pady=(2, 8))

        w, h = 320, 70
        sw = self.winfo_screenwidth()
        sh = self.winfo_screenheight()
        self._toasts = [t for t in self._toasts if t.winfo_exists()]
        offset = sum(t.winfo_height() + 10 for t in self._toasts)
        win.geometry(f"{w}x{h}+{sw - w - 16}+{sh - h - 70 - offset}")
        self._toasts.append(win)

        def close(_=None):
            try:
                win.destroy()
            except tk.TclError:
                pass
        win.bind("<Button-1>", close)
        inner.bind("<Button-1>", close)
        for child in inner.winfo_children():
            child.bind("<Button-1>", close)
        win.after(8000, close)

        if SOUND_AVAILABLE and self.cfg.get("notify_sound"):
            try:
                winsound.MessageBeep(winsound.MB_ICONASTERISK)
            except Exception:
                pass

    # ---------------- Kategorien & Favoriten ----------------
    def all_categories(self):
        cats = list(STANDARD_TAB_ORDER)
        for c in self.cfg.get("custom_categories", []):
            if c not in cats:
                cats.append(c)
        return cats

    def move_entity(self, eid, category):
        ov = self.cfg["entity_overrides"].setdefault(eid, {})
        ov["category"] = category
        ov.pop("hidden", None)
        if category not in STANDARD_TAB_ORDER and \
           category not in self.cfg["custom_categories"]:
            self.cfg["custom_categories"].append(category)
        save_config(self.cfg)
        self.render(force=True)

    def reset_entity(self, eid):
        self.cfg["entity_overrides"].pop(eid, None)
        save_config(self.cfg)
        self.render(force=True)

    def hide_entity(self, eid):
        ov = self.cfg["entity_overrides"].setdefault(eid, {})
        ov["hidden"] = True
        ov.pop("category", None)
        if eid in self.cfg["favorites"]:
            self.cfg["favorites"].remove(eid)
        save_config(self.cfg)
        self.render(force=True)

    def unhide_entity(self, eid, entity):
        ov = self.cfg["entity_overrides"].setdefault(eid, {})
        ov.pop("hidden", None)
        if auto_category(entity) == HIDDEN:
            ov["category"] = "Sensoren"
        if not ov:
            self.cfg["entity_overrides"].pop(eid, None)
        save_config(self.cfg)
        self.render(force=True)

    def toggle_favorite(self, eid):
        if eid in self.cfg["favorites"]:
            self.cfg["favorites"].remove(eid)
        else:
            self.cfg["favorites"].append(eid)
        save_config(self.cfg)
        self.render(force=True)

    def new_category_dialog(self, eid=None):
        name = simpledialog.askstring("Neue Kategorie",
                                      "Name der neuen Kategorie:", parent=self)
        if not name:
            return
        name = name.strip()
        reserved = (HIDDEN, TAB_HIDDEN, TAB_FAVORITES, TAB_LOG, TAB_NO_ROOM)
        if not name or name in reserved:
            return
        if name not in self.cfg["custom_categories"] and \
           name not in STANDARD_TAB_ORDER:
            self.cfg["custom_categories"].append(name)
        if eid:
            self.move_entity(eid, name)
        else:
            save_config(self.cfg)
            self.render(force=True)

    def delete_category(self, name):
        if name in STANDARD_TAB_ORDER:
            return
        if not messagebox.askyesno("Kategorie löschen",
                                   f"Kategorie „{name}“ löschen?\n"
                                   "Die Geräte darin werden wieder "
                                   "automatisch einsortiert."):
            return
        self.cfg["custom_categories"] = [c for c in
                                         self.cfg["custom_categories"]
                                         if c != name]
        for eid, ov in list(self.cfg["entity_overrides"].items()):
            if ov.get("category") == name:
                ov.pop("category", None)
                if not ov:
                    self.cfg["entity_overrides"].pop(eid, None)
        if self.current_tab == name:
            self.current_tab = TAB_FAVORITES
        save_config(self.cfg)
        self.render(force=True)

    # ---------------- Schnellaktionen ----------------
    def action_all_lights_off(self):
        self._service("light", "turn_off", {"entity_id": "all"})
        self._log_event("⭐ Schnellaktion: Alle Lichter aus")

    def action_lock_all(self):
        locks = [e["entity_id"] for e in self.entities
                 if e["entity_id"].startswith("lock.")]
        if locks:
            self._service("lock", "lock", {"entity_id": locks})
            self._log_event("⭐ Schnellaktion: Alles abschließen")

    def action_good_night(self):
        if not messagebox.askyesno("Gute Nacht",
                                   "Alle Lichter aus und alle Schlösser "
                                   "verriegeln?"):
            return
        self.action_all_lights_off()
        self.action_lock_all()
        self.toast("Gute Nacht", "Lichter aus, Schlösser verriegelt.", OK)

    # ---------------- Ausschalt-Timer ----------------
    def set_off_timer(self, eid):
        minutes = simpledialog.askinteger(
            "Ausschalt-Timer", "In wie vielen Minuten ausschalten?",
            minvalue=1, maxvalue=1440, initialvalue=30, parent=self)
        if not minutes:
            return
        self.cancel_off_timer(eid, silent=True)
        end = time.time() + minutes * 60
        t = threading.Timer(minutes * 60, lambda: self._timer_fire(eid))
        t.daemon = True
        t.start()
        self._timers[eid] = (t, end)
        name = friendly_name(self.entity_by_id.get(eid, {"entity_id": eid,
                                                         "attributes": {}}))
        endtxt = time.strftime("%H:%M", time.localtime(end))
        self.toast("⏱ Timer gestellt", f"{name} geht um {endtxt} Uhr aus.")
        self._log_event(f"⏱ Timer: {name} aus um {endtxt}")
        self.render(force=True)

    def cancel_off_timer(self, eid, silent=False):
        entry = self._timers.pop(eid, None)
        if entry:
            entry[0].cancel()
            if not silent:
                name = friendly_name(self.entity_by_id.get(
                    eid, {"entity_id": eid, "attributes": {}}))
                self.toast("⏱ Timer abgebrochen", name)
                self._log_event(f"⏱ Timer abgebrochen: {name}")
                self.render(force=True)

    def _timer_fire(self, eid):
        self._timers.pop(eid, None)
        domain = eid.split(".", 1)[0]
        self._service(domain, "turn_off", {"entity_id": eid})
        name = friendly_name(self.entity_by_id.get(eid, {"entity_id": eid,
                                                         "attributes": {}}))
        self.toast("⏱ Timer abgelaufen", f"{name} wurde ausgeschaltet.", OK)
        self._log_event(f"⏱ Timer abgelaufen: {name} ausgeschaltet")
        try:
            self.after(0, lambda: self.render(force=True))
        except tk.TclError:
            pass

    # ---------------- Farbwahl ----------------
    def pick_color(self, eid):
        rgb, _ = colorchooser.askcolor(parent=self, title="Lampenfarbe wählen")
        if rgb:
            self._service("light", "turn_on",
                          {"entity_id": eid,
                           "rgb_color": [int(rgb[0]), int(rgb[1]),
                                         int(rgb[2])]})

    # ---------------- Rendering ----------------
    def render(self, force=False):
        if force:
            self._tab_sigs.clear()
            self._content_sig = None
            self._sidebar_sig = None
        with self._lock:
            entities = list(self.entities)

        flt = self.filter_text.get().strip().lower()

        def passes_filter(e):
            return not flt or flt in friendly_name(e).lower() \
                   or flt in e["entity_id"].lower()

        rooms_mode = self.view_var.get() == "Räume"
        buckets = {}
        scenes = []
        hidden_items = []

        for e in entities:
            cat = categorize(e, self.cfg)
            if not passes_filter(e):
                continue
            if cat == HIDDEN:
                hidden_items.append(e)
                continue
            if cat == TAB_SCENES:
                scenes.append(e)
                continue
            if rooms_mode:
                room = self.areas.get(e["entity_id"], "") or TAB_NO_ROOM
                buckets.setdefault(room, []).append(e)
            else:
                buckets.setdefault(cat, []).append(e)

        favorites = [self.entity_by_id[f] for f in self.cfg.get("favorites", [])
                     if f in self.entity_by_id and
                     passes_filter(self.entity_by_id[f])]

        visible = [TAB_FAVORITES]
        if rooms_mode:
            rooms = sorted([r for r in buckets if r != TAB_NO_ROOM],
                           key=str.lower)
            visible += rooms
            if buckets.get(TAB_NO_ROOM):
                visible.append(TAB_NO_ROOM)
            if scenes:
                visible.append(TAB_SCENES)
        else:
            for c in self.all_categories():
                if c == TAB_SCENES:
                    if scenes:
                        visible.append(TAB_SCENES)
                elif buckets.get(c):
                    visible.append(c)
            for c in self.cfg.get("custom_categories", []):
                if c not in visible:
                    visible.append(c)
        visible.append(TAB_LOG)
        if self.cfg.get("show_hidden") and hidden_items:
            visible.append(TAB_HIDDEN)

        # Render-Daten je Bereich
        tab_data = {TAB_FAVORITES: ("favorites", favorites, None),
                    TAB_LOG: ("log", None, None)}
        if TAB_SCENES in visible:
            tab_data[TAB_SCENES] = ("scenes", sorted(scenes, key=sort_key),
                                    None)
        if TAB_HIDDEN in visible:
            tab_data[TAB_HIDDEN] = ("hidden",
                                    sorted(hidden_items, key=sort_key), None)
        for cat in visible:
            if cat in tab_data:
                continue
            items = buckets.get(cat, [])
            cheapest = None
            if not rooms_mode and cat == "Tankstellen":
                items = sorted(items, key=lambda x: (numeric_state(x) is None,
                                                     numeric_state(x) or 0,
                                                     friendly_name(x).lower()))
                cheapest = next((numeric_state(i) for i in items
                                 if numeric_state(i) is not None), None)
            else:
                items = sorted(items, key=sort_key)
            tab_data[cat] = ("normal", items, cheapest)
        self._tab_data = tab_data
        self._visible_tabs = visible

        # Signaturen berechnen
        timers_sig = tuple(sorted(self._timers.keys()))
        counts = {}
        for cat in visible:
            kind, items, cheapest = tab_data[cat]
            if kind == "log":
                sig = (len(self.event_log),
                       self.event_log[-1] if self.event_log else "")
                counts[cat] = ""
            else:
                sig = (tuple(entity_sig(e) for e in items), timers_sig,
                       tuple(self.cfg.get("favorites", [])))
                counts[cat] = len(items)
            self._tab_sigs[cat] = sig

        # Seitenleiste nur bei Änderung neu aufbauen
        sidebar_sig = tuple((n, counts.get(n)) for n in visible)
        if sidebar_sig != self._sidebar_sig:
            self._sidebar_sig = sidebar_sig
            self._rebuild_sidebar(visible, counts)

        if self.current_tab not in visible:
            self.current_tab = TAB_FAVORITES
            self._style_nav()

        icon = TAB_ICONS.get(self.current_tab, DEFAULT_ICON)
        self.section_title.config(text=f"{icon} {self.current_tab}")

        # Inhalt nur neu zeichnen, wenn sich der aktuelle Bereich geändert hat
        if self._content_tab != self.current_tab or \
           self._content_sig != self._tab_sigs.get(self.current_tab):
            self._fill_content(self.current_tab)

        # Mini-Fenster nur bei Änderung aktualisieren
        if self._mini and self._mini.winfo_exists():
            favs = [self.entity_by_id[f] for f in self.cfg.get("favorites", [])
                    if f in self.entity_by_id]
            mini_sig = tuple(entity_sig(e) for e in favs)
            if mini_sig != self._mini_sig:
                self._mini_sig = mini_sig
                self._render_mini()

    def _fill_content(self, name):
        data = self._tab_data.get(name)
        if data is None:
            return
        self._content_tab = name
        self._content_sig = self._tab_sigs.get(name)
        for child in self.content.winfo_children():
            child.destroy()
        self._canvas.yview_moveto(0)

        kind, items, cheapest = data
        if kind == "favorites":
            self._render_favorites(self.content, items)
            return
        if kind == "log":
            self._render_log(self.content)
            return
        if kind == "scenes":
            for e in items:
                self._render_card(self.content, e, TAB_SCENES)
            return
        if kind == "hidden":
            for e in items:
                self._render_card(self.content, e, TAB_HIDDEN)
            return

        rooms_mode = self.view_var.get() == "Räume"

        if not rooms_mode and name == "Energie" and items:
            total = sum(w for w in (power_watts(e) for e in items)
                        if w is not None)
            txt = f"⚡ Aktuelle Gesamtleistung: {total:,.0f} W".replace(",", ".")
            try:
                price = float(self.cfg.get("strompreis", 0))
            except (TypeError, ValueError):
                price = 0
            if price > 0:
                per_day = total / 1000.0 * 24 * price
                txt += (f"   ≈ {per_day:.2f} € pro Tag  "
                        f"({price:.2f} €/kWh)").replace(".", ",")
            tk.Label(self.content, text=txt, bg=CARD, fg=FG, anchor="w",
                     font=("Segoe UI Semibold", 11)
                     ).pack(fill="x", padx=10, pady=(10, 4), ipady=8)

        if not items:
            tk.Label(self.content, text="Dieser Bereich ist noch leer. "
                     "Geräte per Rechtsklick hierher verschieben.",
                     bg=BG, fg=FG_DIM, font=("Segoe UI", 10)).pack(pady=24)
        for e in items:
            self._render_card(self.content, e, name, cheapest)

        if not rooms_mode and name not in STANDARD_TAB_ORDER and \
           name not in (TAB_FAVORITES, TAB_LOG, TAB_HIDDEN, TAB_NO_ROOM):
            tk.Button(self.content, text=f"Kategorie „{name}“ löschen",
                      command=lambda n=name: self.delete_category(n),
                      bg=CARD, fg=FG_DIM, relief="flat", padx=10, pady=4,
                      font=("Segoe UI", 9)).pack(pady=10)

    def _render_favorites(self, frame, favorites):
        bar = tk.Frame(frame, bg=BG)
        bar.pack(fill="x", padx=10, pady=(10, 4))
        for text, cmd in (("💡 Alle Lichter aus", self.action_all_lights_off),
                          ("🔒 Alles abschließen", self.action_lock_all),
                          ("🌙 Gute Nacht", self.action_good_night)):
            b = tk.Button(bar, text=text, command=cmd, bg=CARD, fg=FG,
                          relief="flat", padx=16, pady=9, cursor="hand2",
                          activebackground=ACCENT, activeforeground="#fff",
                          font=("Segoe UI Semibold", 10))
            b.pack(side="left", padx=(0, 8))
            b.bind("<Enter>", lambda e, w=b: w.config(bg=CARD_HOVER))
            b.bind("<Leave>", lambda e, w=b: w.config(bg=CARD))
        tk.Frame(frame, bg=CARD, height=1).pack(fill="x", padx=10, pady=6)

        if not favorites:
            tk.Label(frame, text="Noch keine Favoriten.\nRechtsklick auf ein "
                                 "Gerät → „⭐ Zu Favoriten“, dann erscheint es "
                                 "hier und im Mini-Modus.",
                     bg=BG, fg=FG_DIM, justify="center",
                     font=("Segoe UI", 11)).pack(pady=24)
            return
        for e in favorites:
            self._render_card(frame, e, TAB_FAVORITES)

    def _log_stats_today(self):
        today = time.strftime("%d.%m.")
        switches = doors = 0
        for line in self.event_log:
            if not line.startswith(today):
                continue
            if "💡" in line or "🔌" in line:
                switches += 1
            if "geöffnet" in line:
                doors += 1
        return switches, doors

    def _render_log(self, frame):
        switches, doors = self._log_stats_today()
        stats = tk.Frame(frame, bg=CARD)
        stats.pack(fill="x", padx=10, pady=(10, 4), ipady=6)
        tk.Label(stats, text=f"Heute:  💡 {switches} Schaltvorgänge   "
                             f"🚪 {doors} Türöffnungen",
                 bg=CARD, fg=FG, font=("Segoe UI Semibold", 10)
                 ).pack(side="left", padx=12)
        tk.Button(stats, text="Protokoll leeren", command=self.clear_event_log,
                  bg=CARD, fg=FG_DIM, relief="flat", padx=10, pady=3,
                  font=("Segoe UI", 9)).pack(side="right", padx=8)

        entries = list(self.event_log)[-150:]
        if not entries:
            tk.Label(frame, text="Noch keine Ereignisse aufgezeichnet.",
                     bg=BG, fg=FG_DIM, font=("Segoe UI", 10)).pack(pady=24)
            return
        box = tk.Frame(frame, bg=CARD)
        box.pack(fill="both", expand=True, padx=10, pady=4)
        for line in reversed(entries):
            tk.Label(box, text=line, bg=CARD, fg=FG, anchor="w",
                     font=("Consolas", 10)).pack(fill="x", padx=10, pady=1)

    # ---------------- Gerätekarten ----------------
    def _hoverize(self, card, widgets, base, hover):
        def enter(_):
            for w in widgets:
                try:
                    w.config(bg=hover)
                except tk.TclError:
                    pass
        def leave(_):
            for w in widgets:
                try:
                    w.config(bg=base)
                except tk.TclError:
                    pass
        for w in widgets:
            w.bind("<Enter>", enter, add="+")
            w.bind("<Leave>", leave, add="+")

    def _render_card(self, parent, entity, tab, cheapest=None):
        eid = entity["entity_id"]
        domain = eid.split(".", 1)[0]
        state = entity["state"]
        attrs = entity.get("attributes", {})
        unavailable = state in ("unavailable", "unknown")
        active = state in ("on", "unlocked", "open", "heat")
        compact = bool(self.cfg.get("compact"))

        base = CARD_ON if (active and not unavailable) else CARD
        hover = CARD_ON_HOVER if base == CARD_ON else CARD_HOVER

        pady = 2 if compact else 4
        ipady = 3 if compact else 7
        name_font = ("Segoe UI Semibold", 10 if compact else 11)
        sub_font = ("Segoe UI", 8 if compact else 9)

        card = tk.Frame(parent, bg=base)
        card.pack(fill="x", padx=10, pady=pady, ipady=ipady)

        name_fg = FG_OFF if unavailable else FG
        sub_fg = FG_OFF if unavailable else FG_DIM

        star = "⭐ " if eid in self.cfg.get("favorites", []) and \
                        tab != TAB_FAVORITES else ""

        left = tk.Frame(card, bg=base)
        left.pack(side="left", fill="x", expand=True, padx=14)
        lbl_name = tk.Label(left, text=star + friendly_name(entity), bg=base,
                            fg=name_fg, anchor="w", font=name_font)
        lbl_name.pack(fill="x")

        sub = state_text(entity)
        extra = []
        if domain == "light" and state == "on" and \
           attrs.get("brightness") is not None:
            extra.append(f"Helligkeit {round(attrs['brightness'] / 255 * 100)} %")
        if domain == "climate":
            if attrs.get("current_temperature") is not None:
                extra.append(f"Ist {attrs['current_temperature']} °C")
            if attrs.get("temperature") is not None:
                extra.append(f"Soll {attrs['temperature']} °C")
        if domain == "switch" and attrs.get("current_power_w") is not None:
            extra.append(f"{attrs['current_power_w']} W")
        if domain == "weather":
            if attrs.get("temperature") is not None:
                extra.append(f"{attrs['temperature']} °C")
            if attrs.get("humidity") is not None:
                extra.append(f"{attrs['humidity']} % Luftfeuchte")
        if domain == "automation":
            extra.append("Automation")
        elif domain == "script":
            extra.append("Skript")
        elif domain == "scene":
            extra.append("Szene")
        if eid in self._timers:
            endtxt = time.strftime("%H:%M",
                                   time.localtime(self._timers[eid][1]))
            extra.append(f"⏱ aus um {endtxt}")
        if extra:
            sub += "  •  " + "  •  ".join(str(x) for x in extra)
        lbl_sub = tk.Label(left, text=sub, bg=base, fg=sub_fg, anchor="w",
                           font=sub_font)
        lbl_sub.pack(fill="x")

        right = tk.Frame(card, bg=base)
        right.pack(side="right", padx=14)

        hover_widgets = [card, left, right, lbl_name, lbl_sub]

        fuel = is_fuel(entity)
        if tab == TAB_HIDDEN:
            self._button(right, "Wieder anzeigen",
                         lambda: self.unhide_entity(eid, entity))
        elif fuel:
            val = numeric_state(entity)
            best = cheapest is not None and val is not None and \
                abs(val - cheapest) < 1e-9
            color = OK if best else FG
            unit = attrs.get("unit_of_measurement", "€")
            txt = f"{val:.3f} {unit}".replace(".", ",") if val is not None \
                else state_text(entity)
            price_lbl = tk.Label(right, text=txt, bg=base, fg=color,
                                 font=("Segoe UI Semibold", 13))
            price_lbl.pack(side="right")
            hover_widgets.append(price_lbl)
            if best:
                b_lbl = tk.Label(right, text="günstigste ", bg=base, fg=OK,
                                 font=("Segoe UI", 9))
                b_lbl.pack(side="right")
                hover_widgets.append(b_lbl)
        elif domain == "scene":
            self._button(right, "▶ Aktivieren",
                         lambda: self._service("scene", "turn_on",
                                               {"entity_id": eid}))
        elif domain == "script":
            self._button(right, "▶ Ausführen",
                         lambda: self._service("script", "turn_on",
                                               {"entity_id": eid}))
        elif domain == "automation":
            sw = ToggleSwitch(right, state == "on",
                              lambda: self.toggle(eid, "automation"), base)
            sw.pack(side="right", padx=(6, 0))
            hover_widgets.append(sw)
            self._button(right, "▶", lambda: self._service(
                "automation", "trigger", {"entity_id": eid}))
        elif domain in ("light", "switch") and not unavailable:
            sw = ToggleSwitch(right, state == "on",
                              lambda: self.toggle(eid, domain), base)
            sw.pack(side="right", padx=(8, 0))
            hover_widgets.append(sw)
            if domain == "light":
                modes = attrs.get("supported_color_modes") or []
                if any(m in COLOR_MODES for m in modes):
                    self._button(right, "🎨", lambda: self.pick_color(eid))
                if any(m != "onoff" for m in modes):
                    cur = attrs.get("brightness") or 0
                    scale = tk.Scale(right, from_=0, to=100, orient="horizontal",
                                     bg=base, fg=FG, highlightthickness=0,
                                     troughcolor=BG, length=100, showvalue=False)
                    scale.set(round(cur / 255 * 100))
                    scale.bind("<ButtonRelease-1>",
                               lambda ev, s=scale, i=eid:
                               self.set_brightness(i, s.get()))
                    scale.pack(side="right", padx=(8, 4))
                    hover_widgets.append(scale)
        elif domain == "lock" and not unavailable:
            if state == "locked":
                self._button(right, "🔓 Aufsperren",
                             lambda: self.lock_action(eid, "unlock"))
            else:
                self._button(right, "🔒 Zusperren",
                             lambda: self.lock_action(eid, "lock"))
        elif domain == "climate" and not unavailable:
            target = attrs.get("temperature")
            self._button(right, "＋",
                         lambda: self.adjust_temperature(eid, attrs, +0.5))
            if target is not None:
                t_lbl = tk.Label(right, text=f"{target:g} °C", bg=base, fg=FG,
                                 font=("Segoe UI Semibold", 12))
                t_lbl.pack(side="right", padx=6)
                hover_widgets.append(t_lbl)
            self._button(right, "−",
                         lambda: self.adjust_temperature(eid, attrs, -0.5))
        elif domain == "cover" and not unavailable:
            self._button(right, "▲", lambda: self.cover(eid, "open_cover"))
            self._button(right, "■", lambda: self.cover(eid, "stop_cover"))
            self._button(right, "▼", lambda: self.cover(eid, "close_cover"))
        elif domain == "binary_sensor":
            if is_door_or_window(entity) or tab == "Tür & Fenster":
                color = ERR if state == "on" else OK
                txt = "● Offen" if state == "on" else "● Geschlossen"
            else:
                color = WARN if state == "on" else FG_DIM
                txt = "● Aktiv" if state == "on" else "● Inaktiv"
            if unavailable:
                color, txt = FG_OFF, "● Nicht erreichbar"
            s_lbl = tk.Label(right, text=txt, bg=base, fg=color,
                             font=("Segoe UI Semibold", 10))
            s_lbl.pack(side="right")
            hover_widgets.append(s_lbl)
        elif domain == "sensor":
            v_lbl = tk.Label(right, text=state_text(entity), bg=base,
                             fg=FG_OFF if unavailable else FG,
                             font=("Segoe UI Semibold", 12))
            v_lbl.pack(side="right")
            hover_widgets.append(v_lbl)

        self._hoverize(card, hover_widgets, base, hover)
        for w in hover_widgets:
            w.bind("<Button-3>",
                   lambda ev, e=entity, t=tab: self._context_menu(ev, e, t))

    def _button(self, parent, text, cmd):
        b = tk.Button(parent, text=text, command=cmd, bg=ACCENT, fg="#ffffff",
                      relief="flat", padx=12, pady=4, cursor="hand2",
                      activebackground=ACCENT_DARK, activeforeground="#ffffff",
                      font=("Segoe UI", 9, "bold"))
        b.pack(side="right", padx=3)
        return b

    def adjust_temperature(self, eid, attrs, delta):
        cur = attrs.get("temperature")
        if cur is None:
            cur = attrs.get("current_temperature") or 21
        new = round((float(cur) + delta) * 2) / 2
        new = max(5.0, min(30.0, new))
        self._service("climate", "set_temperature",
                      {"entity_id": eid, "temperature": new})

    # ---------------- Rechtsklick-Menü ----------------
    def _context_menu(self, event, entity, tab):
        eid = entity["entity_id"]
        domain = eid.split(".", 1)[0]
        menu = tk.Menu(self, tearoff=0, bg=CARD, fg=FG,
                       activebackground=ACCENT, activeforeground="#fff")

        if eid in self.cfg.get("favorites", []):
            menu.add_command(label="⭐ Favorit entfernen",
                             command=lambda: self.toggle_favorite(eid))
        else:
            menu.add_command(label="⭐ Zu Favoriten",
                             command=lambda: self.toggle_favorite(eid))

        if domain in ("light", "switch"):
            if eid in self._timers:
                endtxt = time.strftime("%H:%M",
                                       time.localtime(self._timers[eid][1]))
                menu.add_command(label=f"⏱ Timer abbrechen (aus um {endtxt})",
                                 command=lambda: self.cancel_off_timer(eid))
            else:
                menu.add_command(label="⏱ Ausschalt-Timer…",
                                 command=lambda: self.set_off_timer(eid))
        if domain == "light":
            modes = entity.get("attributes", {}).get("supported_color_modes") \
                or []
            if any(m in COLOR_MODES for m in modes):
                menu.add_command(label="🎨 Farbe wählen…",
                                 command=lambda: self.pick_color(eid))

        if numeric_state(entity) is not None and domain == "sensor":
            menu.add_command(label="📈 Verlauf anzeigen",
                             command=lambda: self.open_history(entity))

        move = tk.Menu(menu, tearoff=0, bg=CARD, fg=FG,
                       activebackground=ACCENT, activeforeground="#fff")
        for cat in self.all_categories():
            if cat != tab and cat != TAB_SCENES:
                move.add_command(label=f"{TAB_ICONS.get(cat, DEFAULT_ICON)} {cat}",
                                 command=lambda c=cat: self.move_entity(eid, c))
        move.add_separator()
        move.add_command(label="＋ Neue Kategorie…",
                         command=lambda: self.new_category_dialog(eid))
        menu.add_cascade(label="In Kategorie verschieben", menu=move)

        ov = self.cfg.get("entity_overrides", {}).get(eid)
        if ov:
            menu.add_command(label="Automatisch einsortieren (zurücksetzen)",
                             command=lambda: self.reset_entity(eid))
        menu.add_separator()
        if tab == TAB_HIDDEN:
            menu.add_command(label="Wieder anzeigen",
                             command=lambda: self.unhide_entity(eid, entity))
        else:
            menu.add_command(label="🙈 Ausblenden",
                             command=lambda: self.hide_entity(eid))
        menu.add_separator()
        menu.add_command(label="📋 Entity-ID kopieren",
                         command=lambda: self._copy_to_clipboard(eid))
        menu.add_command(label=eid, state="disabled")
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _copy_to_clipboard(self, text):
        try:
            self.clipboard_clear()
            self.clipboard_append(text)
            self.toast("📋 Kopiert", text)
        except tk.TclError:
            pass

    # ---------------- Verlaufs-Diagramm ----------------
    def open_history(self, entity):
        eid = entity["entity_id"]
        win = tk.Toplevel(self)
        win.title(f"Verlauf: {friendly_name(entity)}")
        win.configure(bg=BG)
        win.geometry("740x460")
        win.transient(self)

        head = tk.Frame(win, bg=BG)
        head.pack(fill="x", padx=14, pady=(12, 4))
        tk.Label(head, text=f"📈 {friendly_name(entity)}",
                 bg=BG, fg=FG, font=("Segoe UI Semibold", 12)
                 ).pack(side="left")

        canvas = tk.Canvas(win, bg=CARD, highlightthickness=0)
        canvas.pack(fill="both", expand=True, padx=14, pady=(4, 4))
        info = tk.Label(win, text="Lade Verlauf …", bg=BG, fg=FG_DIM,
                        font=("Segoe UI", 9))
        info.pack(fill="x", padx=14, pady=(0, 10))

        def fetch(hours, label):
            info.config(text=f"Lade Verlauf ({label}) …")

            def worker():
                try:
                    data = self.client.history(eid, hours=hours)
                except Exception as e:
                    self._safe(win, lambda: info.config(
                        text=f"Verlauf konnte nicht geladen werden: "
                             f"{e.__class__.__name__}"))
                    return
                points = []
                if data and isinstance(data, list) and data and \
                   isinstance(data[0], list):
                    for item in data[0]:
                        try:
                            val = float(item.get("state"))
                        except (TypeError, ValueError):
                            continue
                        ts = item.get("last_changed") or item.get("last_updated")
                        if not ts:
                            continue
                        try:
                            t = datetime.fromisoformat(
                                ts.replace("Z", "+00:00"))
                        except ValueError:
                            continue
                        points.append((t, val))
                if len(points) < 2:
                    self._safe(win, lambda: info.config(
                        text="Nicht genug Daten für ein Diagramm vorhanden."))
                    return
                self._safe(win, lambda: self._draw_chart(canvas, info, entity,
                                                         points, hours))
            threading.Thread(target=worker, daemon=True).start()

        for hours, label in ((6, "6 h"), (24, "24 h"), (168, "7 Tage")):
            tk.Button(head, text=label,
                      command=lambda h=hours, l=label: fetch(h, l),
                      bg=CARD, fg=FG, relief="flat", padx=10, pady=3,
                      activebackground=ACCENT, activeforeground="#fff",
                      font=("Segoe UI", 9)).pack(side="right", padx=3)

        fetch(24, "24 h")

    def _safe(self, win, fn):
        try:
            if win.winfo_exists():
                win.after(0, fn)
        except tk.TclError:
            pass

    def _draw_chart(self, canvas, info, entity, points, hours):
        canvas.update_idletasks()
        W = max(canvas.winfo_width(), 400)
        H = max(canvas.winfo_height(), 220)
        pad_l, pad_r, pad_t, pad_b = 64, 20, 20, 32
        canvas.delete("all")

        times = [p[0] for p in points]
        vals = [p[1] for p in points]
        vmin, vmax = min(vals), max(vals)
        if abs(vmax - vmin) < 1e-9:
            vmax = vmin + 1
        t0, t1 = min(times), max(times)
        span = (t1 - t0).total_seconds() or 1

        def x(t):
            return pad_l + (t - t0).total_seconds() / span * (W - pad_l - pad_r)

        def y(v):
            return H - pad_b - (v - vmin) / (vmax - vmin) * (H - pad_t - pad_b)

        for i in range(5):
            v = vmin + (vmax - vmin) * i / 4
            yy = y(v)
            canvas.create_line(pad_l, yy, W - pad_r, yy, fill="#2b2e3c")
            canvas.create_text(pad_l - 8, yy, text=f"{v:.2f}".replace(".", ","),
                               fill=FG_DIM, anchor="e", font=("Segoe UI", 8))

        fmt = "%H:%M" if hours <= 24 else "%d.%m."
        for i in range(5):
            t = t0 + (t1 - t0) * i / 4
            canvas.create_text(x(t), H - pad_b + 14,
                               text=t.astimezone().strftime(fmt),
                               fill=FG_DIM, font=("Segoe UI", 8))

        coords = []
        for t, v in points:
            coords += [x(t), y(v)]
        canvas.create_line(*coords, fill=ACCENT, width=2)
        canvas.create_oval(coords[-2] - 3, coords[-1] - 3,
                           coords[-2] + 3, coords[-1] + 3,
                           fill=OK, outline="")

        unit = entity.get("attributes", {}).get("unit_of_measurement", "")
        info.config(text=f"Min {vmin:.2f}  •  Max {vmax:.2f}  •  "
                         f"Aktuell {vals[-1]:.2f} {unit}  •  "
                         f"{len(points)} Messpunkte".replace(".", ","))

    # ---------------- Mini-Modus ----------------
    def open_mini(self):
        if self._mini and self._mini.winfo_exists():
            self._mini.lift()
            return
        self._mini = tk.Toplevel(self)
        self._mini.title(APP_NAME + " - Mini")
        self._mini.configure(bg=BG)
        self._mini.geometry("300x420")
        self._mini.attributes("-topmost", True)
        self._mini_sig = None
        self._render_mini()
        self.withdraw()

        def on_mini_close():
            self._mini.destroy()
            self._mini = None
            self.deiconify()
        self._mini.protocol("WM_DELETE_WINDOW", on_mini_close)

    def _render_mini(self):
        mini = self._mini
        for child in mini.winfo_children():
            child.destroy()

        top = tk.Frame(mini, bg=BG)
        top.pack(fill="x", padx=8, pady=(8, 4))
        tk.Label(top, text="⭐ Favoriten", bg=BG, fg=FG,
                 font=("Segoe UI Semibold", 11)).pack(side="left")
        tk.Button(top, text="⛶ Vollansicht",
                  command=lambda: (mini.destroy(),
                                   setattr(self, "_mini", None),
                                   self.deiconify()),
                  bg=CARD, fg=FG, relief="flat", padx=8, pady=2,
                  font=("Segoe UI", 9)).pack(side="right")

        bar = tk.Frame(mini, bg=BG)
        bar.pack(fill="x", padx=8, pady=(0, 6))
        for text, cmd in (("💡 Aus", self.action_all_lights_off),
                          ("🔒 Zu", self.action_lock_all),
                          ("🌙", self.action_good_night)):
            tk.Button(bar, text=text, command=cmd, bg=CARD, fg=FG,
                      relief="flat", padx=8, pady=4,
                      font=("Segoe UI", 9)).pack(side="left", padx=(0, 6))

        favs = [self.entity_by_id[f] for f in self.cfg.get("favorites", [])
                if f in self.entity_by_id]
        if not favs:
            tk.Label(mini, text="Keine Favoriten.\nIn der Vollansicht per\n"
                                "Rechtsklick hinzufügen.",
                     bg=BG, fg=FG_DIM, font=("Segoe UI", 10)).pack(pady=20)
            return

        for e in favs:
            eid = e["entity_id"]
            domain = eid.split(".", 1)[0]
            state = e["state"]
            active = state in ("on", "unlocked", "open", "heat")
            base = CARD_ON if active else CARD
            row = tk.Frame(mini, bg=base)
            row.pack(fill="x", padx=8, pady=3, ipady=4)
            tk.Label(row, text=friendly_name(e), bg=base, fg=FG, anchor="w",
                     font=("Segoe UI", 10)).pack(side="left", padx=8,
                                                 fill="x", expand=True)
            if domain in ("light", "switch"):
                sw = ToggleSwitch(row, state == "on",
                                  lambda i=eid, d=domain: self.toggle(i, d),
                                  base)
                sw.pack(side="right", padx=6)
            elif domain == "lock":
                if state == "locked":
                    tk.Button(row, text="🔓",
                              command=lambda i=eid:
                              self.lock_action(i, "unlock"),
                              bg=ACCENT, fg="#fff", relief="flat", padx=8,
                              pady=2).pack(side="right", padx=6)
                else:
                    tk.Button(row, text="🔒",
                              command=lambda i=eid: self.lock_action(i, "lock"),
                              bg=ACCENT, fg="#fff", relief="flat", padx=8,
                              pady=2).pack(side="right", padx=6)
            elif domain == "scene":
                tk.Button(row, text="▶",
                          command=lambda i=eid:
                          self._service("scene", "turn_on", {"entity_id": i}),
                          bg=ACCENT, fg="#fff", relief="flat", padx=8,
                          pady=2).pack(side="right", padx=6)
            else:
                tk.Label(row, text=state_text(e), bg=base, fg=FG_DIM,
                         font=("Segoe UI", 9)).pack(side="right", padx=8)

    # ---------------- Tray-Symbol ----------------
    def _setup_tray(self):
        if not TRAY_AVAILABLE or self._tray:
            return
        img = Image.new("RGB", (64, 64), (19, 20, 26))
        d = ImageDraw.Draw(img)
        d.polygon([(32, 8), (56, 30), (48, 30), (48, 54), (16, 54),
                   (16, 30), (8, 30)], fill=(109, 141, 255))
        d.rectangle([26, 38, 38, 54], fill=(19, 20, 26))

        def on_open(icon, item):
            self.after(0, self._tray_show)

        def on_mini(icon, item):
            self.after(0, self.open_mini)

        def on_lights(icon, item):
            self.after(0, self.action_all_lights_off)

        def on_quit(icon, item):
            self.after(0, self.quit_app)

        menu = pystray.Menu(
            pystray.MenuItem("Öffnen", on_open, default=True),
            pystray.MenuItem("Mini-Modus", on_mini),
            pystray.MenuItem("Alle Lichter aus", on_lights),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Beenden", on_quit),
        )
        self._tray = pystray.Icon("smart_home_zentrale", img, APP_NAME, menu)
        threading.Thread(target=self._tray.run, daemon=True).start()

    def _tray_show(self):
        if self._mini and self._mini.winfo_exists():
            self._mini.destroy()
            self._mini = None
        self.deiconify()
        self.lift()

    # ---------------- Updater ----------------
    def check_updates(self, silent=False):
        url = self.cfg.get("update_url", "").strip()
        if not url:
            if not silent:
                messagebox.showinfo(
                    "Updater",
                    "Keine Update-Adresse hinterlegt.\n\nTrage in den "
                    "Einstellungen die Adresse deiner version.json ein "
                    "(z. B. auf GitHub) - Details stehen in der ANLEITUNG.")
            return

        def worker():
            try:
                info = json.loads(http_get_bytes(url, timeout=15)
                                  .decode("utf-8"))
            except Exception as e:
                if not silent:
                    self.toast("Updater", "Update-Prüfung fehlgeschlagen: "
                               f"{e.__class__.__name__}", ERR)
                return
            latest = str(info.get("version", "0"))
            if version_tuple(latest) <= version_tuple(APP_VERSION):
                if not silent:
                    self.toast("Updater", f"Du hast bereits die neueste "
                               f"Version ({APP_VERSION}).", OK)
                return
            notes = str(info.get("notes", ""))[:500]
            exe_url = info.get("exe_url")
            script_url = info.get("script_url")
            try:
                self.after(0, lambda: self._offer_update(latest, notes,
                                                         exe_url, script_url))
            except tk.TclError:
                pass

        threading.Thread(target=worker, daemon=True).start()

    def _offer_update(self, latest, notes, exe_url, script_url):
        msg = (f"Version {latest} ist verfügbar "
               f"(installiert: {APP_VERSION}).\n")
        if notes:
            msg += f"\n{notes}\n"
        msg += "\nJetzt herunterladen und installieren?"
        if not messagebox.askyesno("🔄 Update verfügbar", msg):
            return
        frozen = bool(getattr(sys, "frozen", False))
        url = exe_url if frozen else (script_url or exe_url)
        if not url:
            messagebox.showerror("Updater", "In der version.json ist keine "
                                 "Download-Adresse hinterlegt.")
            return
        self._set_status("Lade Update herunter …")
        threading.Thread(target=lambda: self._do_update(url, frozen),
                         daemon=True).start()

    def _do_update(self, url, frozen):
        try:
            data = http_get_bytes(url, timeout=120)
        except Exception as e:
            self.toast("Updater", f"Download fehlgeschlagen: "
                       f"{e.__class__.__name__}", ERR)
            return
        try:
            if frozen:
                exe = sys.executable
                new_path = exe + ".neu"
                with open(new_path, "wb") as f:
                    f.write(data)
                bat_path = os.path.join(os.path.dirname(exe),
                                        "update_smart_home_zentrale.bat")
                with open(bat_path, "w", encoding="cp850",
                          errors="replace") as f:
                    f.write("@echo off\n"
                            "timeout /t 2 /nobreak >nul\n"
                            f"move /y \"{new_path}\" \"{exe}\" >nul\n"
                            f"start \"\" \"{exe}\"\n"
                            "del \"%~f0\"\n")
                subprocess.Popen(["cmd", "/c", bat_path],
                                 creationflags=0x08000000
                                 if sys.platform.startswith("win") else 0)
                self._log_event("🔄 Update wird installiert - Neustart")
                try:
                    self.after(0, self.quit_app)
                except tk.TclError:
                    pass
            else:
                path = os.path.abspath(sys.argv[0])
                with open(path, "wb") as f:
                    f.write(data)
                self._log_event("🔄 Update installiert - Neustart")
                try:
                    self.after(0, self._restart_script)
                except tk.TclError:
                    pass
        except Exception as e:
            self.toast("Updater", f"Update fehlgeschlagen: {e}", ERR)

    def _restart_script(self):
        self._stop.set()
        if self._tray:
            try:
                self._tray.stop()
            except Exception:
                pass
        try:
            self.destroy()
        except tk.TclError:
            pass
        os.execl(sys.executable, sys.executable,
                 os.path.abspath(sys.argv[0]))

    # ---------------- Aktionen ----------------
    def _service(self, domain, service, data):
        def run():
            try:
                self.client.call_service(domain, service, data)
                time.sleep(0.8)
                self._refresh()
            except Exception as e:
                self._set_status(f"Aktion fehlgeschlagen: {e}")
        threading.Thread(target=run, daemon=True).start()

    def toggle(self, eid, domain):
        self._service(domain, "toggle", {"entity_id": eid})

    def set_brightness(self, eid, percent):
        if percent <= 0:
            self._service("light", "turn_off", {"entity_id": eid})
        else:
            self._service("light", "turn_on",
                          {"entity_id": eid, "brightness_pct": int(percent)})

    def lock_action(self, eid, action):
        if action == "unlock":
            if not messagebox.askyesno("Türschloss",
                                       "Türschloss wirklich AUFSPERREN?"):
                return
        self._service("lock", action, {"entity_id": eid})

    def cover(self, eid, service):
        self._service("cover", service, {"entity_id": eid})

    # ---------------- Einstellungen ----------------
    def open_settings(self):
        win = tk.Toplevel(self)
        win.title("Einstellungen")
        win.configure(bg=BG)
        win.geometry("640x760")
        win.transient(self)
        win.grab_set()

        # Scrollbarer Inhalt
        outer = tk.Canvas(win, bg=BG, highlightthickness=0)
        vsb = ttk.Scrollbar(win, orient="vertical", command=outer.yview)
        body = tk.Frame(outer, bg=BG)
        bid = outer.create_window((0, 0), window=body, anchor="nw")

        def on_conf(event):
            outer.configure(scrollregion=outer.bbox("all"))
            outer.itemconfigure(bid, width=outer.winfo_width())
        body.bind("<Configure>", on_conf)
        outer.bind("<Configure>", on_conf)
        outer.configure(yscrollcommand=vsb.set)
        outer.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")

        def row(label, value, show=None):
            tk.Label(body, text=label, bg=BG, fg=FG, anchor="w",
                     font=("Segoe UI", 10)).pack(fill="x", padx=16, pady=(8, 2))
            var = tk.StringVar(value=value)
            e = tk.Entry(body, textvariable=var, bg=CARD, fg=FG, relief="flat",
                         insertbackground=FG, font=("Segoe UI", 10), show=show)
            e.pack(fill="x", padx=16, ipady=5)
            return var

        def check(label, key):
            var = tk.BooleanVar(value=bool(self.cfg.get(key)))
            tk.Checkbutton(body, text=label, variable=var, bg=BG, fg=FG,
                           selectcolor=CARD, activebackground=BG,
                           activeforeground=FG, anchor="w",
                           font=("Segoe UI", 10)).pack(fill="x", padx=16,
                                                       pady=(3, 0))
            return var

        def section(text):
            tk.Label(body, text=text, bg=BG, fg=ACCENT, anchor="w",
                     font=("Segoe UI Semibold", 11)).pack(fill="x", padx=16,
                                                          pady=(16, 0))

        section("Verbindung")
        url_var = row("Home Assistant Adresse (z. B. http://192.168.1.50:8123):",
                      self.cfg.get("ha_url", ""))
        tok_var = row("Langlebiges Zugangstoken (Long-Lived Access Token):",
                      self.cfg.get("token", ""), show="•")
        ref_var = row("Aktualisierungsintervall in Sekunden:",
                      str(self.cfg.get("refresh_seconds", 10)))

        section("Darstellung")
        compact_var = check("Kompakte Ansicht (mehr Geräte auf einen Blick)",
                            "compact")
        hidden_var = check("Ausgeblendete Geräte im Menü anzeigen",
                           "show_hidden")

        section("Energie")
        price_kwh_var = row("Strompreis in € pro kWh "
                            "(für Kostenschätzung, 0 = aus):",
                            str(self.cfg.get("strompreis", 0.0)))

        section("Benachrichtigungen")
        doors_var = check("Tür/Fenster geöffnet melden", "notify_doors")
        locks_var = check("Schloss entriegelt melden", "notify_locks")
        bat_var = check("Schwache Batterien melden (≤ 15 %)", "notify_battery")
        snd_var = check("Hinweiston abspielen", "notify_sound")
        openmin_var = row("Warnen, wenn Tür/Fenster länger offen als "
                          "(Minuten, 0 = aus):",
                          str(self.cfg.get("window_open_minutes", 0)))
        fuel_var = check("Spritpreis-Alarm aktivieren", "fuel_alert_enabled")
        price_var = row("Spritpreis-Alarm auslösen bei Preis ≤ (€):",
                        str(self.cfg.get("fuel_alert_price", 1.65)))

        section("Updates")
        upd_var = row("Update-Adresse (URL zu einer version.json):",
                      self.cfg.get("update_url", ""))
        autoupd_var = check("Beim Start automatisch nach Updates suchen",
                            "auto_update_check")

        tray_var = None
        startmin_var = None
        section("System")
        if TRAY_AVAILABLE:
            tray_var = check("Beim Schließen ins Tray-Symbol minimieren",
                             "minimize_to_tray")
            startmin_var = check("Minimiert starten (im Tray)",
                                 "start_minimized")
        else:
            tk.Label(body, text="Tray-Symbol nicht verfügbar - dafür einmalig "
                                "installieren:  pip install pystray pillow",
                     bg=BG, fg=FG_OFF, anchor="w", font=("Segoe UI", 9)
                     ).pack(fill="x", padx=16, pady=(6, 0))

        def export_cfg():
            path = filedialog.asksaveasfilename(
                parent=win, title="Einstellungen exportieren",
                defaultextension=".json",
                initialfile="smart-home-zentrale-backup.json",
                filetypes=[("JSON", "*.json")])
            if path:
                try:
                    with open(path, "w", encoding="utf-8") as f:
                        json.dump(self.cfg, f, indent=2, ensure_ascii=False)
                    messagebox.showinfo("Export", "Einstellungen gespeichert.",
                                        parent=win)
                except Exception as e:
                    messagebox.showerror("Export", str(e), parent=win)

        def import_cfg():
            path = filedialog.askopenfilename(
                parent=win, title="Einstellungen importieren",
                filetypes=[("JSON", "*.json")])
            if path:
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    merged = dict(DEFAULT_CONFIG)
                    merged.update(data)
                    self.cfg = merged
                    save_config(self.cfg)
                    win.destroy()
                    self.connect()
                    self.render(force=True)
                    self.toast("Import", "Einstellungen übernommen.", OK)
                except Exception as e:
                    messagebox.showerror("Import", str(e), parent=win)

        def open_cfg_folder():
            try:
                if sys.platform.startswith("win"):
                    os.startfile(config_dir())  # noqa
                else:
                    os.system(f'xdg-open "{config_dir()}"')
            except Exception:
                messagebox.showinfo("Konfiguration", config_dir())

        tools = tk.Frame(body, bg=BG)
        tools.pack(fill="x", padx=16, pady=(10, 0))
        for text, cmd in (("Exportieren…", export_cfg),
                          ("Importieren…", import_cfg),
                          ("Konfig-Ordner öffnen", open_cfg_folder)):
            tk.Button(tools, text=text, command=cmd, bg=CARD, fg=FG_DIM,
                      relief="flat", padx=10, pady=4,
                      font=("Segoe UI", 9)).pack(side="left", padx=(0, 8))

        def save():
            self.cfg["ha_url"] = url_var.get().strip()
            self.cfg["token"] = tok_var.get().strip()
            try:
                self.cfg["refresh_seconds"] = max(3, int(ref_var.get()))
            except ValueError:
                self.cfg["refresh_seconds"] = 10
            try:
                self.cfg["strompreis"] = max(0.0, float(
                    price_kwh_var.get().replace(",", ".")))
            except ValueError:
                pass
            try:
                self.cfg["window_open_minutes"] = max(0, int(
                    openmin_var.get()))
            except ValueError:
                pass
            self.cfg["compact"] = bool(compact_var.get())
            self.cfg["show_hidden"] = bool(hidden_var.get())
            self.cfg["notify_doors"] = bool(doors_var.get())
            self.cfg["notify_locks"] = bool(locks_var.get())
            self.cfg["notify_battery"] = bool(bat_var.get())
            self.cfg["notify_sound"] = bool(snd_var.get())
            self.cfg["fuel_alert_enabled"] = bool(fuel_var.get())
            try:
                self.cfg["fuel_alert_price"] = float(
                    price_var.get().replace(",", "."))
            except ValueError:
                pass
            self.cfg["update_url"] = upd_var.get().strip()
            self.cfg["auto_update_check"] = bool(autoupd_var.get())
            if tray_var is not None:
                self.cfg["minimize_to_tray"] = bool(tray_var.get())
                self.cfg["start_minimized"] = bool(startmin_var.get())
                if self.cfg["minimize_to_tray"]:
                    self._setup_tray()
            save_config(self.cfg)
            win.destroy()
            self.connect()
            self.render(force=True)

        btns = tk.Frame(body, bg=BG)
        btns.pack(fill="x", padx=16, pady=16)
        tk.Button(btns, text="Speichern & Verbinden", command=save,
                  bg=ACCENT, fg="#fff", relief="flat", padx=14, pady=6,
                  font=("Segoe UI", 10, "bold")).pack(side="right")
        tk.Button(btns, text="Abbrechen", command=win.destroy,
                  bg=CARD, fg=FG, relief="flat", padx=14, pady=6,
                  font=("Segoe UI", 10)).pack(side="right", padx=(0, 8))

    # ---------------- Beenden ----------------
    def on_close(self):
        if TRAY_AVAILABLE and self.cfg.get("minimize_to_tray") and self._tray:
            self.withdraw()
            self.toast(APP_NAME, "Läuft im Hintergrund weiter "
                                 "(Symbol neben der Uhr).", ACCENT)
            return
        self.quit_app()

    def quit_app(self):
        self._stop.set()
        for eid, (timer, _) in list(self._timers.items()):
            timer.cancel()
        self._timers.clear()
        if self._tray:
            try:
                self._tray.stop()
            except Exception:
                pass
        try:
            self.destroy()
        except tk.TclError:
            pass


def main():
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()
