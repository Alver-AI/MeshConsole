#!/usr/bin/env python3
# Mesh Console — terminal chat client for Meshtastic
# Copyright (C) 2026 Alver-AI
# SPDX-License-Identifier: GPL-3.0-or-later
# https://github.com/Alver-AI/MeshConsole
"""
mesh_console.py — Meshtastic Console Client (BLE + WiFi/TCP) v1.4.9
Textual UI: upper area = messages, lower = input.

Requirements:
    pip install meshtastic pypubsub textual

Usage (WiFi/TCP):
    python mesh_console.py --tcp 192.168.1.100    # connect via WiFi (device IP)
    python mesh_console.py --tcp meshtastic.local # or via mDNS hostname

Usage (BLE):
    python mesh_console.py                        # auto-detect paired device
    python mesh_console.py --scan                 # list available BLE devices
    python mesh_console.py --ble <addr_or_uuid>   # connect to specific device
    python mesh_console.py --ble <addr> --node <ID>  # send to specific node

Telemetry:
    python mesh_console.py --telemetry            # display all raw packets
    python mesh_console.py --ble <addr> --telemetry --log telem.log   # + log to file
    Ctrl+Y or /telem [on|off] toggles telemetry while running.

Node list (Ctrl+N):
    Enter/d = DM   t = traceroute   i = request NODEINFO   m = open map
    / or Ctrl+F = search (name, short name, !id or hardware; Esc clears)
    s = request LocalStats (radio health)   b = request device metrics

Radio health (Ctrl+O):
    channel utilization / TX airtime with 1 h trend, RX/TX counters,
    bad/duplicate packet ratio, relay statistics, heap, noise floor
    (noise floor needs firmware >= 2.7.25) and "who relays to me".

Channels:
    /channels           list the device's channels (index, name, key type)
    /ch NAME | /ch N    write on that channel
    /join NAME [KEY]    add a channel (KEY: AQ==/default = public, base64,
                        0x-hex, random, none) and switch to it
    /leave NAME | N     remove a secondary channel (asks first)

Direct messages (PKI, firmware 2.7.15+ sends no DM without it):
    - entering DM mode exchanges keys (our NODEINFO out, theirs requested)
    - a key known only to mesh_nodes.json is loaded into the device (add_contact)
    - without any key the DM waits for the node's NODEINFO (max 3 min)
    - recipient cannot decrypt (NO_CHANNEL…) → key exchange, resend on its answer
    - nobody relayed it → automatic resend (max 3 transmissions)
    - relayed but not confirmed → never resent automatically; /retry by hand
    ✓ relayed (entered the mesh)   ✓✓ delivered (confirmed by the recipient)

Help:
    /help               all commands, keys and shortcuts in the message log

Input line and message log:
    ↑ / ↓               previous / next line you sent (history, like a shell;
                        kept between runs in mesh_input_history.json)
    mouse wheel / PgUp  scroll back: the log stays put while you read, new
                        lines are counted in the status bar; End (twice in the
                        input), PgDn to the bottom, sending, or scrolling down
                        to the bottom resumes following
"""

import argparse
import json
import math
import os
import platform
import queue
import re
import subprocess
import sys
import threading
import tempfile
import time
import textwrap
import unicodedata
import webbrowser
from collections import deque
from datetime import datetime, timedelta
from pathlib import Path

from rich.text import Text

from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.css.query import NoMatches
from textual.reactive import reactive
from textual.screen import ModalScreen
from textual.message import Message as Message_
from textual.widget import Widget
from textual.widgets import (
    DataTable, Input, Label, ListItem,
    ListView, RichLog, Static,
)

if platform.system() == "Windows":
    import winsound as _winsound
else:
    _winsound = None


def play_sound(name: str):
    """Play notification sound. Cross-platform."""
    if platform.system() == "Windows":
        # winsound.Beep() uses the PC speaker (often absent on modern machines).
        beeps = {"message": (800, 150), "private": (600, 200), "sent": (1000, 100)}
        freq, dur = beeps.get(name, (750, 100))
        threading.Thread(target=_winsound.Beep, args=(freq, dur), daemon=True).start()
    elif platform.system() == "Darwin":
        sounds = {
            "message": "/System/Library/Sounds/Funk.aiff",
            "private":  "/System/Library/Sounds/Purr.aiff",
            "sent":     "/System/Library/Sounds/Tink.aiff",
        }
        path = sounds.get(name)
        if path:
            subprocess.Popen(["afplay", path],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)



def _disp_width(text: str) -> int:
    """Display width of text in terminal (emoji/CJK = 2 columns)."""
    return sum(2 if unicodedata.east_asian_width(c) in ("W", "F") else 1 for c in text)

def _safe_col(text: str, max_cols: int = 30) -> str:
    """Truncate text to max_cols terminal columns, replacing overflow with …
    Also strips zero-width chars (variation selectors etc.).
    Use wherever text goes into a DataTable or ListView."""
    # strip zero-width chars first
    clean = "".join(c for c in text if unicodedata.category(c) not in ("Mn", "Cf"))
    result, w = [], 0
    for c in clean:
        cw = 2 if unicodedata.east_asian_width(c) in ("W", "F") else 1
        if w + cw > max_cols:
            result.append("…")
            break
        result.append(c)
        w += cw
    return "".join(result)

def _pad_emoji(emoji: str) -> str:
    """Normalize emoji to a fixed display width of 2 columns.
    Strips variation selectors (U+FE0F etc.), then pads with a space if the
    emoji is only 1 column wide. Keeps emoji list rows evenly aligned."""
    clean = "".join(c for c in emoji if unicodedata.category(c) not in ("Mn", "Cf"))
    w = _disp_width(clean)
    if w < 2:
        clean += " " * (2 - w)
    return clean

# ══════════════════════════════════════════════════════════════════════════════
# Keycap emoji normalization
# ══════════════════════════════════════════════════════════════════════════════

_KEYCAP_MAP = {
    '0️⃣': '⓪', '1️⃣': '①', '2️⃣': '②', '3️⃣': '③', '4️⃣': '④',
    '5️⃣': '⑤', '6️⃣': '⑥', '7️⃣': '⑦', '8️⃣': '⑧', '9️⃣': '⑨', '🔟': '⑩',
}

def _fix_keycaps(text: str) -> str:
    """Replace keycap sequences (5️⃣) with circled digits (⑤) for clean terminal rendering."""
    for keycap, replacement in _KEYCAP_MAP.items():
        text = text.replace(keycap, replacement)
    return text


# ══════════════════════════════════════════════════════════════════════════════
# Hanging-indent writer for RichLog
# ══════════════════════════════════════════════════════════════════════════════

_TS_WIDTH  = 11   # visible width of "[HH:MM:SS] "

# All user-controlled strings (node names, message texts, DB entries) are
# rendered as rich.text.Text — never interpolated into markup. A "[" or
# "[/x]" in a node name or message used to crash the UI with MarkupError.

def _ts_text(ts: str) -> Text:
    """Dimmed "[HH:MM:SS] " prefix (literal brackets, not markup)."""
    return Text(f"[{ts}] ", style="dim") if ts else Text("")

def _log_width(log) -> int:
    """Real text width of the RichLog. Falls back to the terminal width
    before the first layout pass."""
    width = log.scrollable_content_region.width or log.size.width
    if not width:
        try:
            width = log.app.size.width - 2
        except Exception:
            width = 0
    return width


def _log_write(log, text: Text) -> None:
    """RichLog.write with an explicit width. Without it RichLog measures the
    text against the Rich console width, which can still be the default
    80 columns (e.g. right after start) — lines then wrap far too early."""
    width = _log_width(log)
    log.write(text, width=width or None)


def _char_width(c: str) -> int:
    """Terminal columns of one character: wide/fullwidth (emoji, CJK) = 2,
    combining marks and format characters (variation selectors, ZWJ) = 0."""
    if unicodedata.category(c) in ("Mn", "Me", "Cf"):
        return 0
    return 2 if unicodedata.east_asian_width(c) in ("W", "F") else 1


def _wrap_cols(text: str, first_w: int, cont_w: int) -> list[str]:
    """Word-wrap by DISPLAY width (textwrap counts characters, so a line with
    emoji came out wider than the window and the terminal wrapped it again)."""
    lines: list[str] = []
    cur, cur_w, limit = "", 0, first_w
    for tok in re.split(r"( +)", text.replace("\n", " ")):
        if not tok:
            continue
        w = sum(_char_width(c) for c in tok)
        if tok.startswith(" "):
            if cur or not lines:          # keep leading indent of the message,
                cur, cur_w = cur + tok, cur_w + w   # drop it on wrapped lines
            continue
        if cur.strip() and cur_w + w > limit:
            lines.append(cur.rstrip())
            cur, cur_w, limit = "", 0, cont_w
        if w > limit:                     # a word longer than a line: cut it
            for c in tok:
                cw = _char_width(c)
                if cur_w + cw > limit and cur:
                    lines.append(cur.rstrip())
                    cur, cur_w, limit = "", 0, cont_w
                cur, cur_w = cur + c, cur_w + cw
            continue
        cur, cur_w = cur + tok, cur_w + w
    if cur.strip() or not lines:
        lines.append(cur.rstrip())
    return lines


def _hang_write(log, ts_part: Text, color: str, prefix: str,
                text: str, suffix: Text | None = None) -> None:
    """Write a message to RichLog with hanging indent.
    Continuation lines are indented by _TS_WIDTH spaces so they align
    under the first character after the timestamp.
    prefix / text are PLAIN strings (no markup); suffix is a styled Text."""
    suffix = suffix if suffix is not None else Text("")
    # 1 column of margin: some terminals draw a few "narrow" symbols (⚠ ✓)
    # two columns wide
    avail  = max(60, _log_width(log) or 80) - 1
    first_w = max(20, avail - _TS_WIDTH - sum(_char_width(c) for c in prefix))
    cont_w  = max(20, avail - _TS_WIDTH)
    lines   = _wrap_cols(text, first_w, cont_w)
    indent  = " " * _TS_WIDTH

    # the dim suffix (link quality, quoted reply) goes to its own line if it
    # does not fit after the last text line
    last_w = sum(_char_width(c) for c in lines[-1]) + (
        cont_w - first_w if len(lines) == 1 else 0)     # + prefix on line 1
    suf_w  = sum(_char_width(c) for c in suffix.plain)
    suffix_own_line = bool(suf_w) and last_w + suf_w > cont_w

    for i, chunk in enumerate(lines):
        head  = ts_part if i == 0 else Text(indent)
        body  = (prefix + chunk) if i == 0 else chunk
        tail  = suffix if (i == len(lines) - 1 and not suffix_own_line) else Text("")
        _log_write(log, Text.assemble(head, (body, color), tail))
    if suffix_own_line:
        _log_write(log, Text.assemble(indent, suffix.copy()))


# ══════════════════════════════════════════════════════════════════════════════
# Node database — persistent, managed automatically
# ══════════════════════════════════════════════════════════════════════════════

NODE_DB_FILE   = Path(__file__).parent / "mesh_nodes.json"
NODE_DB_EXPIRY = 30  # days
DM_ARCHIVE_FILE = Path(__file__).parent / "mesh_dm_archive.jsonl"


class NodeDB:
    """
    Persistent node database saved to mesh_nodes.json next to the script.

    Entry structure:
    {
      "!a1b2c3d4": {
        "current_name": "Node Alpha",
        "name_history":["Alpha", "Node Alpha"],
        "hw":           "RAK4631",
        "first_seen":   "2026-03-15T16:00:00",
        "last_seen":    "2026-03-15T22:17:00",
        "seen_count":   247
      }
    }
    """

    def __init__(self):
        self._lock  = threading.Lock()
        self._db    : dict = {}
        self._dirty = False
        self._load()

    def _load(self):
        if NODE_DB_FILE.exists():
            try:
                with open(NODE_DB_FILE, "r", encoding="utf-8") as f:
                    self._db = json.load(f)
            except Exception:
                self._db = {}
        self._purge_old()

    def save(self):
        # Keep updates and writes serialized; replace only a complete JSON file.
        with self._lock:
            if not self._dirty:
                return
            tmp_path = None
            try:
                with tempfile.NamedTemporaryFile(
                    mode="w", encoding="utf-8", dir=NODE_DB_FILE.parent,
                    prefix=NODE_DB_FILE.name + ".", suffix=".tmp", delete=False
                ) as f:
                    tmp_path = Path(f.name)
                    json.dump(self._db, f, ensure_ascii=False, indent=2)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp_path, NODE_DB_FILE)
                self._dirty = False
            finally:
                if tmp_path is not None and tmp_path.exists():
                    tmp_path.unlink()

    def _purge_old(self):
        cutoff = datetime.now() - timedelta(days=NODE_DB_EXPIRY)
        to_del = []
        for node_id, entry in self._db.items():
            try:
                last = datetime.fromisoformat(entry.get("last_seen", ""))
                if last < cutoff:
                    to_del.append(node_id)
            except Exception:
                pass
        for node_id in to_del:
            del self._db[node_id]
        if to_del:
            self._dirty = True

    def update(self, node_id: str, name: str = "", hw: str = "", pub_key: str = "",
               mark_seen: bool = True):
        if not node_id or not node_id.startswith("!"):
            return
        now = datetime.now().isoformat(timespec="seconds")
        with self._lock:
            entry   = self._db.get(node_id, {})
            changed = False
            if "first_seen" not in entry:
                entry["first_seen"] = now
                changed = True
            if mark_seen or "last_seen" not in entry:
                entry["last_seen"] = now
                entry["seen_count"] = entry.get("seen_count", 0) + 1
                changed = True
            if name and name != entry.get("current_name"):
                history = entry.get("name_history", [])
                if name not in history:
                    history.append(name)
                entry["name_history"] = history
                entry["current_name"] = name
                changed = True
            if hw and hw != entry.get("hw"):
                entry["hw"] = hw
                changed = True
            if pub_key and pub_key != entry.get("pub_key"):
                entry["pub_key"] = pub_key
                changed = True
            self._db[node_id] = entry
            if changed:
                self._dirty = True

    def has_pki(self, node_id: str, live_nodes: dict | None = None) -> bool:
        """True if the node has a PKI key.
        Order: live session (iface.nodes) → node_db (previous sessions)."""
        if live_nodes is not None:
            live_key = live_nodes.get(node_id, {}).get("user", {}).get("publicKey")
            if live_nodes.get(node_id):          # node visible in this session
                return bool(live_key)            # live data wins — even if empty
        with self._lock:
            return bool(self._db.get(node_id, {}).get("pub_key"))

    def touch(self, node_id: str):
        if not node_id or not node_id.startswith("!"):
            return
        now = datetime.now().isoformat(timespec="seconds")
        with self._lock:
            if node_id in self._db:
                self._db[node_id]["last_seen"]  = now
                self._db[node_id]["seen_count"] = self._db[node_id].get("seen_count", 0) + 1
                self._dirty = True

    def update_position(self, node_id: str, position: dict):
        """Store the last valid coordinates, including unnamed nodes.

        Repeated device snapshots do not refresh observation timestamps.
        """
        if not node_id or not node_id.startswith("!") or not isinstance(position, dict):
            return
        try:
            lat = position.get("latitude")
            lon = position.get("longitude")
            if lat is None:
                lat = position["latitudeI"] / 1e7
            if lon is None:
                lon = position["longitudeI"] / 1e7
            lat, lon = float(lat), float(lon)
            if not (math.isfinite(lat) and math.isfinite(lon)
                    and -90 <= lat <= 90 and -180 <= lon <= 180):
                return
            # Meshtastic uses 0,0 when no valid fix is available.
            if lat == 0 and lon == 0:
                return
            clean = {"latitude": lat, "longitude": lon}
            for key in ("altitude", "time", "precisionBits"):
                value = position.get(key)
                if isinstance(value, (int, float)) and math.isfinite(value):
                    clean[key] = value
        except (KeyError, TypeError, ValueError, OverflowError):
            return
        with self._lock:
            entry = self._db.get(node_id, {})
            previous = entry.get("position", {})
            if previous.get("time", 0) > clean.get("time", 0) > 0:
                return
            if previous == clean:
                return
            now = datetime.now().isoformat(timespec="seconds")
            entry.setdefault("first_seen", now)
            entry.setdefault("last_seen", now)
            entry.setdefault("seen_count", 0)
            entry["position"] = clean
            entry["position_updated_at"] = now
            self._db[node_id] = entry
            self._dirty = True

    def resolve(self, node_id: str) -> str:
        with self._lock:
            # strip only for display — the DB keeps the original name
            return (self._db.get(node_id, {}).get("current_name") or "").strip() or node_id

    def get_entry(self, node_id: str) -> dict:
        with self._lock:
            return dict(self._db.get(node_id, {}))

    def all_ids(self) -> list:
        with self._lock:
            return list(self._db.keys())

    def size(self) -> int:
        with self._lock:
            return len(self._db)


# ══════════════════════════════════════════════════════════════════════════════
# Data model
# ══════════════════════════════════════════════════════════════════════════════

VERSION      = "1.4.9"
MAX_MESSAGES = 500

TAPBACK_EMOJI = [
    ("1", "👍"), ("2", "💚"), ("3", "❤"), ("4", "🔥"),
    ("5", "👋"), ("6", "😭"), ("7", "🤣"), ("8", "😊"),
    ("9", "🙏"), ("0", "😮"), ("a", "👎"), ("b", "🥺"),
    ("c", "😉"),
]

EMOJI_LIST = [
    # global top
    ("1", "😂"),  ("2", "😉"),  ("3", "😭"),  ("4", "💚"),
    ("5", "❤"),   ("6", "🔥"),  ("7", "👍"),  ("8", "🤣"),
    ("9", "✨"),  ("0", "🥰"),  ("a", "😍"),  ("b", "🎉"),
    ("c", "😁"),  ("d", "😅"),  ("e", "🥺"),  ("f", "😘"),
    ("g", "👋"),  ("h", "🙏"),
    # radio / outdoors / status
    ("i", "🆘"),  ("j", "⛔"),  ("k", "📍"),  ("l", "📡"),
    ("m", "🔋"),  ("n", "📻"),  ("o", "✅"),  ("p", "❌"),
    # weather / navigation
    ("r", "🌞"),  ("s", "🌧"),  ("t", "🌙"),  ("u", "🏔"),
    ("v", "🏠"),  ("w", "🚗"),  ("x", "🚶"),  ("y", "👎"),
    ("z", "😴"),
]


class Message:
    """Single message in the buffer."""
    __slots__ = ("ts", "kind", "sender", "sender_id", "text", "msg_id", "reply_id", "hops",
                 "to_id", "snr", "rssi", "relay", "uid", "status", "channel")

    def __init__(self, kind: str, text: str, sender: str = "", sender_id: str = "",
                 ts: str = "", msg_id: int = 0, reply_id: int = 0, hops: int = -1,
                 to_id: str = "^all", snr: float | None = None, rssi: int | None = None,
                 relay: str = "", channel: int = 0):
        self.ts        = ts or datetime.now().strftime("%H:%M:%S")
        self.kind      = kind
        self.sender    = sender
        self.sender_id = sender_id   # node ID e.g. "!a1b2c3d4"; "" when unknown
        self.text      = text
        self.msg_id    = msg_id
        self.reply_id  = reply_id
        self.hops      = hops
        self.to_id     = to_id
        self.snr       = snr     # SNR of the last hop (dB), None = unknown
        self.rssi      = rssi    # RSSI of the last hop (dBm), None = unknown
        self.relay     = relay   # display label of the node that relayed it to us
        # stable local id — links the DM archive line to later status records
        self.uid       = os.urandom(6).hex()
        # own messages: queued | sent | relayed | delivered | noack | failed
        self.status    = ""
        self.channel   = channel  # channel index (0 = primary)


class MeshState:
    """Shared application state (BLE thread <-> UI thread)."""

    def __init__(self, logfile: str = "", telemetry: bool = False):
        self.lock              = threading.Lock()
        self.messages          : list[Message]    = []
        self.msg_by_id         : dict[int, Message] = {}
        self.nodes             : dict             = {}
        self.my_id             : str              = "???"
        self.my_name           : str              = "???"
        self.connected         : bool             = False
        self.device_hw         : str              = ""
        self.device_fw         : str              = ""
        self.interface                            = None
        self.current_channel   : int              = 0
        self.reply_dm_to       : str              = ""
        self._dirty            = threading.Event()
        self.tcp_mode          : bool             = False
        self.packet_queue      : queue.Queue      = queue.Queue()
        self._logfile          = open(logfile, "a", encoding="utf-8") if logfile else None
        self.node_db           : NodeDB           = NodeDB()
        self.traceroute_pending: dict             = {}
        # traceroute request packet id -> target node id (to explain failures)
        self.tr_by_id          : dict             = {}
        self.tr_reached        : set              = set()   # targets that ACKed the request
        self.pending_ack       : dict             = {}
        self.telemetry         : bool             = telemetry
        self.dm_unread_count   : int              = 0
        # ── radio diagnostics (v1.3.0) ──
        # node_id -> (localStats dict, unix time received)
        self.local_stats       : dict             = {}
        # node_id -> (name, sent_time, telemetry_type, quiet)
        self.telem_pending     : dict             = {}
        # own channel utilization / TX airtime samples: (unix_time, cu, air_tx)
        self.health_hist       : deque            = deque(maxlen=120)
        # relay byte -> {"count", "snr_sum", "snr_n", "last"}
        self.relay_stats       : dict             = {}
        self.relay_total       : int              = 0
        # low byte -> {node_id: {"t": unix time, "snr": dB}} of nodes heard DIRECTLY (0 hops) —
        # the only moment the full ID behind a relay byte is known for sure
        self.direct_seen       : dict             = {}
        self.neighbours_saved  : float            = time.time()
        # dest node_id -> [queued DM entries] waiting for the device to learn the node
        self.dm_queue          : dict             = {}
        # node_id -> unix time we loaded its key into the device (add_contact)
        self.contact_pushed    : dict             = {}
        # nodes whose pushed key the device refused (PKI fail after a push)
        self.contact_push_failed : set            = set()
        # node_id -> unix time we last sent it our NODEINFO (key exchange)
        self.handshake_at      : dict             = {}
        # node_id -> unix time its last NODEINFO arrived
        self.nodeinfo_seen     : dict             = {}
        # node_id -> (unix time, hops) of the last packet received from it
        self.hops_seen         : dict             = {}
        # callback called by _process_queue when a new message arrives
        self.ui_notify         = None  # set by MeshApp

    def _log(self, ts: str, text: str):
        if self._logfile:
            try:
                self._logfile.write(f"[{ts}] {text}\n")
                self._logfile.flush()
            except Exception:
                pass

    def _archive_dm(self, msg: Message):
        """Append DM to persistent JSONL archive (never auto-deleted)."""
        try:
            entry = {
                "ts":       datetime.now().isoformat(timespec="seconds"),
                "kind":     msg.kind,
                "sender":   msg.sender,
                "my_id":    self.my_id,
                "to_id":    msg.to_id,
                "msg_id":   msg.msg_id,
                "reply_id": msg.reply_id,
                "text":     msg.text,
                "uid":      msg.uid,
            }
            with open(DM_ARCHIVE_FILE, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception:
            pass  # the archive never blocks the app

    def set_status(self, msg: "Message | None", status: str) -> None:
        """Update the delivery status of one of OUR messages. For DMs the
        change is also appended to the archive (Ctrl+H shows the last one)."""
        if msg is None or msg.status == status:
            return
        msg.status = status
        if msg.to_id and msg.to_id != "^all":
            try:
                rec = {"type": "status", "uid": msg.uid, "status": status,
                       "ts": datetime.now().isoformat(timespec="seconds")}
                with open(DM_ARCHIVE_FILE, "a", encoding="utf-8") as f:
                    f.write(json.dumps(rec) + "\n")
            except Exception:
                pass
        self._dirty.set()

    def add(self, kind: str, text: str, sender: str = "", sender_id: str = "",
            ts: str = "", msg_id: int = 0, reply_id: int = 0, hops: int = -1,
            to_id: str = "^all", snr: float | None = None, rssi: int | None = None,
            relay: str = "", channel: int = 0):
        msg = Message(kind, text, sender, sender_id=sender_id, ts=ts,
                      msg_id=msg_id, reply_id=reply_id, hops=hops, to_id=to_id,
                      snr=snr, rssi=rssi, relay=relay, channel=channel)
        with self.lock:
            self.messages.append(msg)
            if len(self.messages) > MAX_MESSAGES:
                removed = self.messages.pop(0)
                if removed.msg_id and removed.msg_id in self.msg_by_id:
                    del self.msg_by_id[removed.msg_id]
            if msg_id:
                self.msg_by_id[msg_id] = msg
        if kind in ("theirs", "private", "dm", "mine"):
            ch_tag    = "[PRIVATE] " if kind == "private" else ("[DM] " if kind == "dm" else "")
            reply_tag = f" replyId={reply_id}" if reply_id else ""
            id_tag    = f" id={msg_id}" if msg_id else ""
            to_tag    = f" to={to_id}" if to_id and to_id != "^all" else ""
            prefix    = "Me" if kind == "mine" else sender
            self._log(msg.ts, f"{ch_tag}{prefix}: {text}")
            from_tag  = self.my_id if kind == "mine" else ""
            if msg_id and from_tag:
                self._log(msg.ts, f"#MSG {from_tag}{id_tag}{to_tag}{reply_tag} {text}")
            elif msg_id:
                node_id = f"!{msg_id & 0xffffffff:08x}" if not sender.startswith("!") else sender
                self._log(msg.ts, f"#MSG {node_id}{id_tag}{to_tag}{reply_tag} {text}")
        elif kind == "system":
            self._log(msg.ts, text)
        # persist DM to archive + count unread
        if kind in ("dm", "mine") and to_id and to_id != "^all":
            self._archive_dm(msg)
            if kind == "dm":   # incoming only
                self.dm_unread_count += 1
        self._dirty.set()
        if self.ui_notify:
            self.ui_notify(msg)
        return msg

    def system(self, text: str, ts: str = ""):
        self.add("system", text, ts=ts)

    def raw(self, text: str, ts: str = ""):
        """Telemetry line: rendered live but NOT stored in self.messages,
        so a packet flood cannot push chat messages out of the 500-entry
        buffer (Reply / Tapback / quotes depend on it)."""
        msg = Message("system_raw", text, ts=ts)
        if self.ui_notify:
            self.ui_notify(msg)

    def error(self, text: str):
        self.add("error", text)

    def close_log(self):
        if self._logfile:
            self._logfile.close()
            self._logfile = None

    def wait_dirty(self, timeout=0.2) -> bool:
        fired = self._dirty.wait(timeout)
        if fired:
            self._dirty.clear()
        return fired


# ══════════════════════════════════════════════════════════════════════════════
# Meshtastic layer
# ══════════════════════════════════════════════════════════════════════════════

def _clear_pubsub():
    try:
        from pubsub import pub
        for topic in ("meshtastic.receive",
                      "meshtastic.connection.established",
                      "meshtastic.connection.lost"):
            try:
                pub.unsubAll(topicName=topic)
            except Exception:
                pass
    except ImportError:
        pass


def connect_ble(state: MeshState, address: str | None):
    try:
        import meshtastic.ble_interface
        from pubsub import pub
    except ImportError as e:
        state.error(f"Missing library: {e}")
        state.error("Install with: pip install meshtastic pypubsub")
        return

    _clear_pubsub()
    _connection_fired = threading.Event()

    def on_receive(packet, interface):
        state.packet_queue.put(("packet", packet, interface))

    def on_connection(interface, topic=None):
        _connection_fired.set()
        state.packet_queue.put(("connected", interface, None))

    def on_connection_lost(interface, topic=None):
        state.packet_queue.put(("disconnected", None, None))

    state._pubsub_callbacks = (on_receive, on_connection, on_connection_lost)
    pub.subscribe(on_receive,         "meshtastic.receive")
    pub.subscribe(on_connection,      "meshtastic.connection.established")
    pub.subscribe(on_connection_lost, "meshtastic.connection.lost")

    state.system("⏳ Connecting via BLE" + (f" → {address}" if address else " (auto-detect)..."))
    try:
        iface = meshtastic.ble_interface.BLEInterface(address)
        if not _connection_fired.is_set():
            state.packet_queue.put(("connected_fallback", iface, None))
    except Exception as e:
        state.packet_queue.put(("error", str(e), None))


def connect_tcp(state: MeshState, host: str):
    try:
        import meshtastic.tcp_interface
        from pubsub import pub
    except ImportError as e:
        state.error(f"Missing library: {e}")
        state.error("Install with: pip install meshtastic pypubsub")
        return

    _clear_pubsub()
    _connection_fired = threading.Event()

    def on_receive(packet, interface):
        state.packet_queue.put(("packet", packet, interface))

    def on_connection(interface, topic=None):
        _connection_fired.set()
        state.packet_queue.put(("connected", interface, None))

    def on_connection_lost(interface, topic=None):
        state.packet_queue.put(("disconnected", None, None))

    state._pubsub_callbacks = (on_receive, on_connection, on_connection_lost)
    pub.subscribe(on_receive,         "meshtastic.receive")
    pub.subscribe(on_connection,      "meshtastic.connection.established")
    pub.subscribe(on_connection_lost, "meshtastic.connection.lost")

    state.system(f"⏳ Connecting via WiFi/TCP → {host}...")
    try:
        iface = meshtastic.tcp_interface.TCPInterface(host)
        if not _connection_fired.is_set():
            state.packet_queue.put(("connected_fallback", iface, None))
    except Exception as e:
        state.packet_queue.put(("error", str(e), None))


def _resolve_name(state: MeshState, iface, from_id: str) -> str:
    with state.lock:
        node = state.nodes.get(from_id, {})
    if node:
        name = node.get("user", {}).get("longName", "").strip()
        if name:
            return name
    try:
        node2 = iface.nodes.get(from_id, {}) if iface and iface.nodes else {}
        if node2:
            name = node2.get("user", {}).get("longName", "").strip()
            if name:
                return name
    except Exception:
        pass
    db_name = state.node_db.resolve(from_id)
    if db_name != from_id:
        return db_name
    return from_id


def _normalize_pub_key(raw_key) -> str:
    """Convert publicKey (bytes, bytearray or base64 string) to a hex string.
    Returns an empty string if the key is empty or invalid."""
    import base64 as _b64
    if isinstance(raw_key, (bytes, bytearray)):
        return raw_key.hex() if raw_key else ""
    if isinstance(raw_key, str) and raw_key:
        # may be base64 (44 chars = 32 B) or already hex (64 chars = 32 B)
        if len(raw_key) == 64 and all(c in "0123456789abcdefABCDEF" for c in raw_key):
            return raw_key.lower()  # already hex
        try:
            decoded = _b64.b64decode(raw_key)
            return decoded.hex()
        except Exception:
            pass
    return ""


def _remember_user(state: MeshState, node_id: str, user: dict,
                   mark_seen: bool = True):
    # A short-only announcement must not overwrite a known long name.
    name = user.get("longName") or ""
    if not name:
        name = state.node_db.get_entry(node_id).get("current_name") or user.get("shortName") or ""
    if node_id and name:
        state.node_db.update(node_id, name=name, hw=user.get("hwModel") or "",
                             pub_key=_normalize_pub_key(user.get("publicKey", b"")),
                             mark_seen=mark_seen)


def _populate_node_db(state: MeshState, nodes: dict, mark_seen: bool = True):
    for node_id, info in list(nodes.items()):
        _remember_user(state, node_id, info.get("user", {}), mark_seen=mark_seen)
        state.node_db.update_position(node_id, info.get("position", {}))


def request_nodeinfo(state: MeshState, dest: str) -> bool:
    """Ask a node for its NODEINFO (wantResponse=True).

    The payload is OUR OWN User protobuf — exactly what the firmware itself
    sends. An empty payload would be decoded on the other side as an empty
    User and could blank our name / public key in that node's NodeDB.
    The reply (their NODEINFO incl. public key) lets the firmware use PKI."""
    iface = state.interface
    if not iface:
        state.error("Not connected — cannot request NODEINFO.")
        return False
    try:
        from google.protobuf.json_format import ParseDict
        from meshtastic.protobuf import mesh_pb2
        my_user = iface.getMyUser()
        if not my_user or not my_user.get("id"):
            raise ValueError("own user info not available yet")
        payload = ParseDict(dict(my_user), mesh_pb2.User(),
                            ignore_unknown_fields=True).SerializeToString()
    except Exception as e:
        state.error(f"NODEINFO request not sent: {e}")
        return False

    def _send():
        try:
            from meshtastic.protobuf import portnums_pb2 as _pn
            iface.sendData(
                payload,
                destinationId=dest,
                portNum=_pn.PortNum.NODEINFO_APP,
                wantAck=False,
                wantResponse=True,
                # far nodes: the device default hop limit would never reach them
                hopLimit=dm_hop_limit(state, dest),
            )
        except Exception as e:
            state.error(f"NODEINFO request error: {e}")
    threading.Thread(target=_send, daemon=True, name="NodeInfoReq").start()
    return True


def send_message(state: MeshState, text: str, dest: str = "^all", reply_id: int = 0,
                 literal: bool = False):
    # literal=True: text is sent as-is even if it starts with "/" (// escape)
    cmd = "" if literal or not text.startswith("/") else text.split()[0].lower()
    if cmd == "/ch":
        parts = text.split(maxsplit=1)
        if len(parts) == 1:
            state.system(f"Current channel: {channel_label(state, state.current_channel)} "
                         f"(index {state.current_channel})   — /channels lists all")
            return
        idx = find_channel(state, parts[1])
        if idx is None:
            state.error(f"No such channel: {parts[1]}   (/channels lists them, "
                        f"/join NAME [KEY] adds one)")
            return
        state.current_channel = idx
        state.reply_dm_to = ""
        state.system(f"✓ Now writing on {channel_label(state, idx)} (channel {idx})")
        return

    if cmd == "/tr":
        parts = text.split(maxsplit=1)
        if len(parts) < 2:
            state.error("Usage: /tr !xxxxxxxx   or   /tr NodeName")
            return
        target_arg = parts[1].strip()
        if not target_arg.startswith("!"):
            resolved = None
            with state.lock:
                for nid, info in state.nodes.items():
                    long_name  = info.get("user", {}).get("longName",  "").strip()
                    short_name = info.get("user", {}).get("shortName", "").strip()
                    if target_arg.lower() in (long_name.lower(), short_name.lower()):
                        resolved = nid
                        break
            if not resolved:
                for nid in state.node_db.all_ids():
                    if state.node_db.resolve(nid).lower() == target_arg.lower():
                        resolved = nid
                        break
            if not resolved:
                state.error(f"Node not found: {target_arg}")
                return
            target_id = resolved
        else:
            target_id = target_arg

        if not state.interface:
            state.error("Not connected — cannot send traceroute.")
            return
        display_name = _resolve_name(state, state.interface, target_id)
        state.system(f"🔍 Sending traceroute → {display_name} ({target_id})...")
        with state.lock:
            state.traceroute_pending[target_id] = (display_name, time.time())

        def _send_tr():
            try:
                # sendData instead of iface.sendTraceRoute: that one blocks and
                # hides the packet id. With the id (and wantAck) the ROUTING
                # answers tell us WHY a traceroute fails.
                from meshtastic.protobuf import mesh_pb2 as _mp, portnums_pb2 as _pn
                pkt = state.interface.sendData(
                    _mp.RouteDiscovery(), destinationId=target_id,
                    portNum=_pn.PortNum.TRACEROUTE_APP, wantResponse=True,
                    wantAck=True, channelIndex=0, hopLimit=7)
                pid = pkt.get("id", 0) if isinstance(pkt, dict) else getattr(pkt, "id", 0)
                if pid:
                    with state.lock:
                        state.tr_by_id[pid] = target_id
            except Exception as e:
                state.error(f"Traceroute error: {e}")
                with state.lock:
                    state.traceroute_pending.pop(target_id, None)
        threading.Thread(target=_send_tr, daemon=True, name="TraceRoute").start()
        return

    if not state.interface:
        state.error("Not connected — cannot send.")
        return
    try:
        effective_dest = state.reply_dm_to if state.reply_dm_to else dest
        is_dm = bool(state.reply_dm_to)
        kwargs = dict(destinationId=effective_dest,
                      channelIndex=0 if is_dm else state.current_channel,
                      wantAck=True)
        if reply_id:
            kwargs["replyId"] = reply_id

        if is_dm and effective_dest != "^all":
            start_dm(state, effective_dest, text, reply_id)
            return

        sent_packet = state.interface.sendText(text, **kwargs)
        sent_id = 0
        if sent_packet:
            sent_id = sent_packet.get("id", 0) if isinstance(sent_packet, dict) \
                      else getattr(sent_packet, "id", 0)
        kind = "private" if state.current_channel != 0 else "mine"
        msg = state.add(kind, text, sender=state.my_name, reply_id=reply_id, msg_id=sent_id,
                        channel=state.current_channel)
        if sent_id:
            label = channel_label(state, state.current_channel)
            state.set_status(msg, "sent")
            with state.lock:
                state.pending_ack[sent_id] = ack_record(_snippet(text), label, effective_dest,
                                                        msg=msg)
        play_sound("sent")
    except Exception as e:
        state.error(f"Send error: {e}")


def _snippet(text: str, n: int = 30) -> str:
    flat = text.replace("\n", " ")
    return flat[:n] + "..." if len(flat) > n else flat


def pki_state(state: "MeshState", nid: str, live: dict | None = None) -> str:
    """'device'  — the device has the node's public key (DM will be E2E)
       'program' — only mesh_nodes.json knows the key; the device must learn it
       'none'    — no key anywhere."""
    if live is None:
        with state.lock:
            live = dict(state.nodes)
    if live.get(nid, {}).get("user", {}).get("publicKey"):
        return "device"
    if state.node_db.get_entry(nid).get("pub_key"):
        with state.lock:
            loaded = nid in state.contact_pushed and nid not in state.contact_push_failed
        return "loaded" if loaded else "program"
    return "none"


# 'loaded' = we pushed the key from mesh_nodes.json into the device ourselves
PKI_ICON = {"device": "🔐", "loaded": "🔐", "program": "🔑", "none": "🔓"}

# delivery status of our own messages → (label, style)
MSG_STATUS = {
    "queued":    ("⏳ queued",                      "yellow"),
    "sent":      ("→ sent",                         "dim"),
    "relayed":   ("✓ relayed (no recipient ACK)",   "cyan"),
    "delivered": ("✓✓ delivered",                   "bold green"),
    "noack":     ("⚠ no confirmation",              "yellow"),
    "failed":    ("✗ not delivered",                "bold red"),
}


def status_text(status: str, short: bool = False) -> tuple[str, str]:
    label, style = MSG_STATUS.get(status, ("", ""))
    if short and label:
        label = label.split(" (")[0]
    return (label, style)


CONTACT_PUSH_EVERY = 600   # s — don't push the same contact more often


def push_contact(state: "MeshState", nid: str) -> bool:
    """Load one node (with its public key from mesh_nodes.json) into the
    device NodeDB via AdminMessage.add_contact — the same message the phone
    apps use for QR contact sharing. Lets the device encrypt a DM at once
    instead of waiting for the node's NODEINFO broadcast."""
    iface = state.interface
    entry = state.node_db.get_entry(nid)
    key_hex = entry.get("pub_key", "")
    num = _node_num(nid)
    if not iface or not key_hex or num is None:
        return False
    with state.lock:
        last = state.contact_pushed.get(nid, 0)
        if nid in state.contact_push_failed:
            return False
    if time.time() - last < CONTACT_PUSH_EVERY:
        return True          # already loaded recently
    try:
        from meshtastic.protobuf import admin_pb2, mesh_pb2
        key = bytes.fromhex(key_hex)
        if len(key) != 32:
            return False
        contact = admin_pb2.SharedContact()
        contact.node_num = num
        contact.user.id = nid
        name = (entry.get("current_name") or "").strip() or nid
        contact.user.long_name = name.encode("utf-8")[:39].decode("utf-8", "ignore")
        # Meshtastic's default short name = last 4 hex digits of the node ID;
        # the real one replaces it with the node's next NODEINFO
        contact.user.short_name = nid[-4:]
        hw = entry.get("hw")
        if hw:
            try:
                contact.user.hw_model = mesh_pb2.HardwareModel.Value(hw)
            except ValueError:
                pass
        contact.user.public_key = key
        msg = admin_pb2.AdminMessage()
        msg.add_contact.CopyFrom(contact)
        # local admin over the API needs no session key and no response
        iface.localNode._sendAdmin(msg, wantResponse=False)
    except Exception as e:
        state.error(f"Could not load contact {nid} into the device: {e}")
        return False
    with state.lock:
        state.contact_pushed[nid] = time.time()
    state.system(f"🔑 Key of {state.node_db.resolve(nid)} loaded into the device "
                 f"from the program database")
    return True



# ══════════════════════════════════════════════════════════════════════════════
# Direct messages — delivery pipeline (v1.3.3)
# ══════════════════════════════════════════════════════════════════════════════
#
# A PKI direct message only gets through when BOTH sides hold each other's
# CURRENT public key, and firmware >= 2.7.15 refuses DMs without PKI at all.
# Every DM is therefore a small job that walks through these steps:
#
#   1. key exchange  — once per peer (every 10 min at most) we send OUR NODEINFO
#                      with wantResponse: it delivers our current key to the
#                      peer and asks for theirs.
#   2. our side      — the device must know the peer's key. If only
#                      mesh_nodes.json has it, it is loaded via add_contact;
#                      otherwise the DM waits for the peer's NODEINFO.
#   3. send          — with wantAck; hop limit raised for far nodes.
#   4. outcome       — ✓✓ recipient ACK → done
#                      ✓ relay ACK      → wait for the recipient's ACK
#                      PKI_SEND_FAIL    → device lacks the key → back to step 2
#                      NO_CHANNEL / PKI_FAILED / PKI_UNKNOWN_PUBKEY from the peer
#                                       → peer could not decrypt (it has our OLD
#                                         key or none) → fresh key exchange,
#                                         resend when the peer answers
#                      no ACK at all    → nothing left our neighbourhood →
#                                         automatic resend after 20 s
#                      relay ACK but no recipient ACK → NOT resent automatically
#                                         (it may have arrived) — /retry by hand
#
# Every resend is safe from duplicates: either nothing was transmitted, or the
# recipient demonstrably could not read it, or nobody relayed it at all.
# At most DM_MAX_SENDS transmissions per message.

DM_KEY_WAIT       = 180   # s — max wait for a NODEINFO (our key side / peer side)
NODEINFO_RETRY_AT = 90    # s — ask for NODEINFO a second time while waiting
DM_E2E_WAIT       = 120   # s — after a relay ACK, wait for the recipient's own ACK
ACK_WAIT          = 60    # s — no ACK of any kind after this
DM_RETRY_DELAY    = 20    # s — pause before an automatic resend
DM_MAX_SENDS      = 3     # transmissions per message (first send included)
HANDSHAKE_EVERY   = 600   # s — key exchange with the same peer at most this often

DECRYPT_ERRORS = ("NO_CHANNEL", "PKI_FAILED", "PKI_UNKNOWN_PUBKEY")
NETWORK_ERRORS = ("MAX_RETRANSMIT", "TIMEOUT", "NO_RESPONSE", "NO_ROUTE", "GOT_NAK",
                  "DUTY_CYCLE_LIMIT", "RATE_LIMIT_EXCEEDED", "NO_INTERFACE")


def ack_record(snippet: str, label: str, dest: str, msg: "Message | None" = None,
               job: dict | None = None, tapback: bool = False) -> dict:
    """One entry of state.pending_ack (keyed by packet id). job is set for DMs;
    tapback marks a reaction (feedback only, never resent)."""
    return {"snippet": snippet, "label": label, "dest": dest, "sent": time.time(),
            "msg": msg, "job": job, "relayed_at": None, "tapback": tapback}


def legacy_dm_allowed(state: "MeshState") -> bool:
    """Firmware < 2.7.15 still sends DMs with channel encryption when no key
    is known; 2.7.15+ refuses (GHSA-377p-prwp-4hwf). Unknown version → modern."""
    fw = _fw_tuple(state.device_fw)
    return bool(fw) and fw < (2, 7, 15)


def device_key(state: "MeshState", dest: str) -> tuple[bool, bool]:
    """(node is in the device NodeDB, device has its public key)."""
    with state.lock:
        node = state.nodes.get(dest)
    return node is not None, bool(node and node.get("user", {}).get("publicKey"))


HOPS_SEEN_TTL = 6 * 3600   # s — how long an observed hop count stays valid


def device_hop_limit(state: "MeshState") -> int:
    """LoRa hop limit configured in the device (Meshtastic default: 3)."""
    try:
        return int(state.interface.localNode.localConfig.lora.hop_limit) or 3
    except Exception:
        return 3


def peer_distance(state: "MeshState", dest: str) -> tuple[int | None, str]:
    """Best known hop distance to dest and where it comes from: the device
    NodeDB (hopsAway) or hop counts of packets we received from it recently.
    The larger value wins — routes change and the device field may be stale."""
    with state.lock:
        dev = state.nodes.get(dest, {}).get("hopsAway")
        seen = state.hops_seen.get(dest)
    cands = []
    if isinstance(dev, int):
        cands.append((dev, "device NodeDB"))
    if seen and time.time() - seen[0] < HOPS_SEEN_TTL:
        cands.append((seen[1], "its recent packets"))
    if not cands:
        return None, ""
    return max(cands, key=lambda c: c[0])


def dm_hop_limit(state: "MeshState", dest: str) -> int | None:
    """Hop limit for packets addressed to one far node. The device default
    (often 3) silently drops anything farther away. None = device default
    is enough. Unknown distance → 7: DMs are rare, so the extra flooding
    costs little compared to a message that never arrives."""
    cfg = device_hop_limit(state)
    hops, _ = peer_distance(state, dest)
    need = 7 if hops is None else min(7, hops + 1)     # +1 margin: routes vary
    return need if need > cfg else None


def key_exchange(state: "MeshState", dest: str, force: bool = False) -> bool:
    """Send our NODEINFO (current public key) to dest and ask for theirs."""
    now = time.time()
    with state.lock:
        last = state.handshake_at.get(dest, 0)
        if not force and now - last < HANDSHAKE_EVERY:
            return False
        state.handshake_at[dest] = now
    return request_nodeinfo(state, dest)


def new_dm_job(dest: str, text: str, reply_id: int, msg: "Message | None") -> dict:
    return {"dest": dest, "text": text, "reply_id": reply_id, "msg": msg,
            "created": time.time(), "sends": 0}


def start_dm(state: "MeshState", dest: str, text: str, reply_id: int = 0) -> None:
    """Entry point for every DM typed by the user."""
    msg = state.add("mine", text, sender=state.my_name, reply_id=reply_id, to_id=dest)
    key_exchange(state, dest)
    try_send_dm(state, new_dm_job(dest, text, reply_id, msg))


def try_send_dm(state: "MeshState", job: dict) -> None:
    """Send now if the device can encrypt for dest, otherwise wait for the key."""
    dest = job["dest"]
    in_dev, has_key = device_key(state, dest)
    if not has_key and push_contact(state, dest):
        has_key = True        # loaded from mesh_nodes.json just now
    if has_key or (in_dev and legacy_dm_allowed(state)):
        if not has_key:
            state.system(f"⚠ {state.node_db.resolve(dest)} has no PKI key — "
                         f"DM goes out with channel encryption only")
        send_dm_now(state, job)
    else:
        dm_wait(state, job, "key")


def dm_wait(state: "MeshState", job: dict, reason: str, delay: float = 0,
            note: str = "") -> None:
    """Park a DM job. reason: key (device lacks the peer's key) | peer (peer
    could not decrypt — waiting for its NODEINFO) | retry (timed resend)."""
    now   = time.time()
    dest  = job["dest"]
    name  = state.node_db.resolve(dest)
    entry = {"job": job, "reason": reason, "since": now, "ready_at": now + delay,
             "asked_again": False}
    with state.lock:
        state.dm_queue.setdefault(dest, []).append(entry)
    state.set_status(job["msg"], "queued")
    snip = _snippet(job["text"])
    if reason == "key":
        key_exchange(state, dest)
        state.system(note or f'⏳ The device has no key for {name} yet — "{snip}" waits '
                             f'for its NODEINFO (max {DM_KEY_WAIT // 60} min)')
    elif reason == "peer":
        key_exchange(state, dest, force=True)
        state.system(f'🔄 {name} could not decrypt "{snip}" — keys do not match. Keys '
                     f'exchanged again; one more attempt when {name} answers '
                     f'(max {DM_KEY_WAIT // 60} min)')
    else:
        state.system(note or f'↻ No ACK from anyone for "{snip}" — sending again in '
                             f'{int(delay)} s (attempt {job["sends"] + 1}/{DM_MAX_SENDS})')


def send_dm_now(state: "MeshState", job: dict) -> bool:
    """Hand the DM to the device and register the ACK watch."""
    dest, text, msg = job["dest"], job["text"], job["msg"]
    kwargs = dict(destinationId=dest, channelIndex=0, wantAck=True)
    if job["reply_id"]:
        kwargs["replyId"] = job["reply_id"]
    hl = dm_hop_limit(state, dest)
    if hl:
        kwargs["hopLimit"] = hl
    job["sends"] += 1
    try:
        sent_packet = state.interface.sendText(text, **kwargs)
    except Exception as e:
        state.error(f"Send error: {e}")
        state.set_status(msg, "failed")
        return False
    sent_id = 0
    if sent_packet:
        sent_id = sent_packet.get("id", 0) if isinstance(sent_packet, dict) \
                  else getattr(sent_packet, "id", 0)
    if sent_id:
        with state.lock:
            if msg is not None:
                msg.msg_id = sent_id
                state.msg_by_id[sent_id] = msg
            state.pending_ack[sent_id] = ack_record(_snippet(text), state.node_db.resolve(dest),
                                                    dest, msg=msg, job=job)
    state.set_status(msg, "sent")
    play_sound("sent")
    return True


def process_dm_queue(state: "MeshState") -> None:
    """Every UI tick: release parked DMs whose condition is met, re-ask for
    NODEINFO halfway, report failures on timeout."""
    if not state.dm_queue or not state.connected:
        return
    now = time.time()
    with state.lock:
        items = [(d, list(q)) for d, q in state.dm_queue.items()]
        state.dm_queue.clear()
    keep: dict = {}
    for dest, entries in items:
        name = state.node_db.resolve(dest)
        in_dev, has_key = device_key(state, dest)
        with state.lock:
            seen = state.nodeinfo_seen.get(dest, 0)
        for e in entries:
            job, reason, age = e["job"], e["reason"], now - e["since"]
            snip = _snippet(job["text"])
            ready = (
                (reason == "key"   and (has_key or (in_dev and legacy_dm_allowed(state))))
                or (reason == "peer" and seen > e["since"])
                or (reason == "retry" and now >= e["ready_at"])
            )
            if ready:
                if reason == "key":
                    state.system(f'↻ {name} is now known to the device — sending "{snip}"')
                elif reason == "peer":
                    state.system(f'↻ {name} answered with its NODEINFO — sending "{snip}" '
                                 f'again (attempt {job["sends"] + 1}/{DM_MAX_SENDS})')
                else:
                    state.system(f'↻ Sending again → {name}: "{snip}" '
                                 f'(attempt {job["sends"] + 1}/{DM_MAX_SENDS})')
                try_send_dm(state, job)
                continue
            if reason in ("key", "peer") and age > DM_KEY_WAIT:
                state.set_status(job["msg"], "failed")
                if reason == "key":
                    state.error(f'✗ Not delivered → {name}: "{snip}" — no NODEINFO within '
                                f'{DM_KEY_WAIT // 60} min (node offline or out of range)')
                else:
                    state.error(f'✗ Not delivered → {name}: "{snip}" — {name} did not '
                                f'answer the key exchange within {DM_KEY_WAIT // 60} min')
                    _explain_old_key(state, name)
                continue
            if reason in ("key", "peer") and age > NODEINFO_RETRY_AT and not e["asked_again"]:
                e["asked_again"] = True
                state.system(f"🔍 Still no NODEINFO from {name} — asking again")
                key_exchange(state, dest, force=True)
            keep.setdefault(dest, []).append(e)
    if keep:
        with state.lock:
            for dest, q in keep.items():
                state.dm_queue.setdefault(dest, []).extend(q)


def retry_last_dm(state: "MeshState") -> None:
    """/retry — resend the latest undelivered DM in the current conversation."""
    dest = state.reply_dm_to
    if not dest:
        state.error("/retry works in DM mode (Ctrl+D) — it resends your last "
                    "undelivered message to that node.")
        return
    with state.lock:
        cands = [m for m in state.messages
                 if m.kind == "mine" and m.to_id == dest
                 and m.status in ("relayed", "noack", "failed")]
        queued = any(e["job"]["msg"] is not None and e["job"]["msg"].to_id == dest
                     for e in state.dm_queue.get(dest, []))
    if not cands:
        state.system("Nothing to retry" + (" — a message is still waiting in the queue."
                                           if queued else "."))
        return
    msg = cands[-1]
    state.system(f'↻ Retrying → {state.node_db.resolve(dest)}: "{_snippet(msg.text)}"')
    key_exchange(state, dest)
    try_send_dm(state, new_dm_job(dest, msg.text, msg.reply_id, msg))


TR_ERROR_HINT = {
    "NO_CHANNEL":         "it could not decrypt the request — most likely it still has "
                          "your key from before your device was reset",
    "PKI_FAILED":         "it could not decrypt the request (PKI) — keys do not match",
    "PKI_UNKNOWN_PUBKEY": "it does not know your public key yet",
    "MAX_RETRANSMIT":     "nobody acknowledged the request — no route in that direction",
    "NO_RESPONSE":        "it received the request but sent no route back "
                          "(REPEATER role or traceroute answers rate-limited)",
    "NO_ROUTE":           "no known route to it",
    "TIMEOUT":            "the request timed out",
    "RATE_LIMIT_EXCEEDED": "rate limit — wait a while before the next traceroute",
    "DUTY_CYCLE_LIMIT":   "your device hit the duty-cycle limit — try again later",
}


def handle_traceroute_routing(state: "MeshState", request_id: int, target: str,
                              error, from_id: str, ts: str = "") -> None:
    """ROUTING answer to our traceroute request (sent with wantAck)."""
    name = state.node_db.resolve(target)
    if error in ("NONE", "", None):
        if from_id == target:
            with state.lock:
                state.tr_reached.add(target)
            state.system(f"✓ Traceroute request reached {name} — waiting for the route data",
                         ts=ts)
        return                      # relay ACK — not informative
    with state.lock:
        state.tr_by_id.pop(request_id, None)
        state.traceroute_pending.pop(target, None)
    who = "" if from_id in (target, state.my_id) else \
        f" (reported by {state.node_db.resolve(from_id)})"
    state.error(f"✗ Traceroute → {name} failed: {error}{who}")
    hint = TR_ERROR_HINT.get(error)
    if hint:
        state.system(f"  ↳ {hint}", ts=ts)


def _explain_old_key(state: "MeshState", label: str, ts: str = "") -> None:
    state.system(f"  ↳ {label}'s device most likely still keeps your key from before your "
                 f"device was reset. Meshtastic firmware (2.6.3+) never replaces a stored "
                 f"key by itself — a protection against impersonation.", ts=ts)
    state.system(f"  ↳ the owner of {label} has to delete your node "
                 f'"{state.my_name}" from their node list; it learns your new key from '
                 f"your next NODEINFO. Then type /retry here.", ts=ts)


def handle_ack(state: "MeshState", request_id: int, ack: dict, error, from_id: str,
               ts: str = "") -> None:
    """Interpret a ROUTING_APP answer to one of our packets.

    Positive ACKs come in two kinds with the same request id:
      • from the RECIPIENT (from == destination) → received and decrypted
      • implicit, made by OUR device after hearing a neighbour rebroadcast the
        packet → it only proves the packet entered the mesh.
    Broadcasts can only ever get the implicit kind."""
    dest, label, snippet = ack["dest"], ack["label"], ack["snippet"]
    is_dm = bool(dest) and dest != "^all"
    msg, job = ack.get("msg"), ack.get("job")
    rtt = time.time() - ack["sent"]

    def _pop():
        with state.lock:
            state.pending_ack.pop(request_id, None)

    if error in ("NONE", "", None):
        if is_dm and from_id == dest:
            _pop()
            state.set_status(msg, "delivered")
            state.system(f'✓✓ Delivered → {label}: "{snippet}"  '
                         f'(confirmed by recipient, {rtt:.0f}s)', ts=ts)
        elif is_dm:
            if ack["relayed_at"] is None:      # keep waiting for the recipient
                ack["relayed_at"] = time.time()
                state.set_status(msg, "relayed")
                state.system(f'✓ Relayed → {label}: "{snippet}" — in the mesh, '
                             f'waiting for confirmation from the recipient', ts=ts)
        else:
            _pop()
            state.set_status(msg, "relayed")
            state.system(f'✓ Heard by the mesh → {label}: "{snippet}"  '
                         f'(a neighbour rebroadcast it)', ts=ts)
        return

    _pop()
    can_resend = job is not None and job["sends"] < DM_MAX_SENDS

    if job is not None and error == "PKI_SEND_FAIL_PUBLIC_KEY":
        # local refusal — nothing was transmitted
        with state.lock:
            if dest in state.contact_pushed:
                state.contact_push_failed.add(dest)
        job["sends"] -= 1                       # didn't go on air — doesn't count
        dm_wait(state, job, "key")
        return

    if job is not None and error in DECRYPT_ERRORS:
        job["decrypt_fails"] = job.get("decrypt_fails", 0) + 1
        if job["decrypt_fails"] == 1 and can_resend:
            # one more try after a fresh key exchange — enough when only OUR
            # copy of their key was stale, or when they had no key of ours
            dm_wait(state, job, "peer")
        else:
            state.set_status(msg, "failed")
            state.error(f'✗ Not delivered → {label}: "{snippet}" — {label} still cannot '
                        f'decrypt your messages ({error})')
            _explain_old_key(state, label, ts)
        return

    if job is not None and error in NETWORK_ERRORS and ack["relayed_at"] is None \
            and can_resend:
        delay = 60 if error in ("DUTY_CYCLE_LIMIT", "RATE_LIMIT_EXCEEDED") else DM_RETRY_DELAY
        dm_wait(state, job, "retry", delay=delay,
                note=f'⚠ "{snippet}" → {label}: {error}, nobody relayed it — '
                     f'next attempt in {delay} s')
        return

    state.set_status(msg, "failed")
    who = "" if from_id in (state.my_id, dest) else \
        f", reported by {state.node_db.resolve(from_id)}"
    state.error(f'✗ Not delivered → {label}: "{snippet}" ({error}{who})')
    if job is not None:
        state.system("  ↳ type /retry to send it again", ts=ts)


def expire_acks(state: "MeshState") -> None:
    """Every tick: feedback for packets whose confirmation never came."""
    now = time.time()
    with state.lock:
        items = list(state.pending_ack.items())
    for pid, ack in items:
        relayed = ack["relayed_at"] is not None
        job, msg = ack.get("job"), ack.get("msg")
        if relayed and now - ack["sent"] > DM_E2E_WAIT:
            with state.lock:
                state.pending_ack.pop(pid, None)
            hint = "" if ack.get("tapback") else " (/retry sends it again)"
            state.system(f'· {ack["label"]} has not confirmed "{ack["snippet"]}" — it reached '
                         f'the mesh, delivery is unknown{hint}')
        elif not relayed and now - ack["sent"] > ACK_WAIT:
            with state.lock:
                state.pending_ack.pop(pid, None)
            if msg is None:
                continue        # location etc. — keep the old silent behaviour
            if job is not None and job["sends"] < DM_MAX_SENDS:
                dm_wait(state, job, "retry", delay=0,
                        note=f'⚠ No ACK from anyone for "{ack["snippet"]}" within '
                             f'{ACK_WAIT} s')
                continue
            state.set_status(msg, "noack")
            if ack.get("tapback") and ack["dest"] not in ("", "^all"):
                state.system(f'⚠ No confirmation → {ack["label"]}: "{ack["snippet"]}" — '
                             f'no ACK from the recipient or any relay')
            elif job is not None:
                state.error(f'⚠ No confirmation → {ack["label"]}: "{ack["snippet"]}" — '
                            f'no ACK from the recipient or any relay (/retry)')
            else:
                state.system(f'⚠ No rebroadcast heard for "{ack["snippet"]}" → '
                             f'{ack["label"]} — possibly nobody received it')


# ══════════════════════════════════════════════════════════════════════════════
# Channels — list / join / leave from the console (v1.4.0)
# ══════════════════════════════════════════════════════════════════════════════
# The device holds 8 channel slots: 0 = PRIMARY, others SECONDARY or DISABLED.
# A channel is identified on air by name + key (PSK). "AQ==" (one byte 0x01)
# is Meshtastic's shorthand for the well-known DEFAULT key — such a channel is
# public: anyone who knows its name can read it.

DEFAULT_PSK = b"\x01"
CHANNEL_NAME_MAX = 11          # bytes (protobuf limit 12 incl. terminator)


def _channels(state: "MeshState") -> list:
    try:
        return list(state.interface.localNode.channels or [])
    except Exception:
        return []


def _role_name(ch) -> str:
    try:
        from meshtastic.protobuf import channel_pb2
        return channel_pb2.Channel.Role.Name(ch.role)
    except Exception:
        return "?"


def _preset_name(state: "MeshState") -> str:
    """Name the PRIMARY channel shows when it has no name: the modem preset,
    e.g. LONG_FAST → LongFast (what the phone apps display)."""
    try:
        from meshtastic.protobuf import config_pb2
        lora = state.interface.localNode.localConfig.lora
        name = config_pb2.Config.LoRaConfig.ModemPreset.Name(lora.modem_preset)
        return "".join(w.capitalize() for w in name.split("_"))
    except Exception:
        return "Primary"


def channel_name(state: "MeshState", idx: int) -> str:
    for ch in _channels(state):
        if ch.index == idx:
            n = ch.settings.name
            return n if n else (_preset_name(state) if idx == 0 else f"Ch{idx}")
    return "Primary" if idx == 0 else f"Ch{idx}"


def channel_is_public(state: "MeshState", idx: int) -> bool:
    """Default key (or no key) → anyone knowing the name can read it."""
    for ch in _channels(state):
        if ch.index == idx:
            return ch.settings.psk in (b"", DEFAULT_PSK)
    return idx == 0


def channel_label(state: "MeshState", idx: int) -> str:
    """'[TEST]' for public channels, '🔒[Prywatny]' for ones with own key."""
    name = channel_name(state, idx)
    return f"[{name}]" if channel_is_public(state, idx) else f"🔒[{name}]"


def find_channel(state: "MeshState", ref: str) -> int | None:
    """Channel index from a number or a (case-insensitive) name; enabled only."""
    ref = ref.strip()
    chans = [c for c in _channels(state) if _role_name(c) != "DISABLED"]
    if ref.isdigit():
        i = int(ref)
        if not chans:                     # channel list not loaded — trust the number
            return i if 0 <= i <= 7 else None
        return i if any(c.index == i for c in chans) else None
    for c in chans:
        if channel_name(state, c.index).lower() == ref.lower():
            return c.index
    return None


def parse_psk(text: str | None) -> bytes:
    """Key in any common notation: default | none | random | base64 (AQ==,
    16/32-byte keys) | 0x-hex. Raises ValueError."""
    if not text or text.lower() in ("default", "aq==", "1"):
        return DEFAULT_PSK
    t = text.strip()
    if t.lower() == "none":
        return b""
    if t.lower() == "random":
        return os.urandom(32)
    if t.lower().startswith("0x"):
        key = bytes.fromhex(t[2:])
    else:
        import base64 as _b64
        try:
            key = _b64.b64decode(t + "=" * (-len(t) % 4), validate=True)
        except Exception:
            raise ValueError("key is neither base64 nor 0x-hex")
    if len(key) not in (0, 1, 16, 32):
        raise ValueError(f"key must be 1, 16 or 32 bytes (got {len(key)})")
    return key


def describe_psk(psk: bytes) -> str:
    import base64 as _b64
    if psk == b"":
        return "no encryption"
    if psk == DEFAULT_PSK:
        return "default key (AQ==) — public"
    if len(psk) == 1:
        return f"simple key #{psk[0]} ({_b64.b64encode(psk).decode()})"
    return f"own {len(psk) * 8}-bit key {_b64.b64encode(psk).decode()}"


def list_channels(state: "MeshState") -> None:
    chans = [c for c in _channels(state) if _role_name(c) != "DISABLED"]
    if not chans:
        state.error("Channel list not available (not connected yet?)")
        return
    state.system("Channels in the device:")
    for c in chans:
        cur = "   ◀ writing here" if c.index == state.current_channel and not state.reply_dm_to else ""
        role = " (primary)" if _role_name(c) == "PRIMARY" else ""
        lbl = channel_label(state, c.index)
        lbl += " " * max(0, 16 - sum(_char_width(ch) for ch in lbl))
        state.system(f"  {c.index}  {lbl}{role}  "
                     f"{describe_psk(c.settings.psk)}{cur}")
    free = 8 - len(chans)
    state.system(f"  {free} free slot{'s' if free != 1 else ''}   — /ch NAME switches, "
                 f"/join NAME [KEY] adds, /leave NAME removes")


def join_channel(state: "MeshState", name: str, key_text: str | None) -> int | None:
    """Add a SECONDARY channel (or switch to it if it already exists).
    Returns its index."""
    iface = state.interface
    if not iface:
        state.error("Not connected — cannot add a channel.")
        return None
    name = name.strip()
    if not name or len(name.encode("utf-8")) > CHANNEL_NAME_MAX:
        state.error(f"Channel name must be 1–{CHANNEL_NAME_MAX} bytes long")
        return None
    try:
        psk = parse_psk(key_text)
    except ValueError as e:
        state.error(f"/join: {e}")
        return None
    node = iface.localNode
    existing = next((c for c in _channels(state)
                     if _role_name(c) != "DISABLED" and c.settings.name.lower() == name.lower()),
                    None)
    if existing is not None:
        if existing.settings.psk != psk and key_text:
            state.error(f"Channel {name} already exists (index {existing.index}) with a "
                        f"different key — /leave it first to change the key")
            return None
        state.current_channel, state.reply_dm_to = existing.index, ""
        state.system(f"✓ {channel_label(state, existing.index)} already exists — "
                     f"now writing there (channel {existing.index})")
        return existing.index
    try:
        from meshtastic.protobuf import channel_pb2
        ch = node.getDisabledChannel()
        if ch is None:
            state.error("No free channel slot (8 in use) — /leave one first")
            return None
        settings = channel_pb2.ChannelSettings()
        settings.name = name
        settings.psk = psk
        ch.settings.CopyFrom(settings)
        ch.role = channel_pb2.Channel.Role.SECONDARY
        node.writeChannel(ch.index)
    except Exception as e:
        state.error(f"Could not add channel: {e}")
        return None
    state.current_channel, state.reply_dm_to = ch.index, ""
    state.system(f"✓ Joined {channel_label(state, ch.index)} as channel {ch.index} "
                 f"({describe_psk(psk)}) — now writing there, /ch 0 returns to "
                 f"{channel_name(state, 0)}")
    return ch.index


def leave_channel(state: "MeshState", idx: int) -> bool:
    """Remove a SECONDARY channel. Later channels move one slot up (that is
    how the firmware stores them), so indexes after it change."""
    iface = state.interface
    if not iface:
        state.error("Not connected — cannot remove a channel.")
        return False
    if idx == 0:
        state.error("The primary channel cannot be removed.")
        return False
    label = channel_label(state, idx)
    try:
        ch = iface.localNode.channels[idx]
        if _role_name(ch) != "SECONDARY":
            state.error(f"Channel {idx} is not a secondary channel.")
            return False
        iface.localNode.deleteChannel(idx)
    except Exception as e:
        state.error(f"Could not remove channel: {e}")
        return False
    if state.current_channel == idx:
        state.current_channel = 0
    elif state.current_channel > idx:
        state.current_channel -= 1          # slots after it shifted up
    state.system(f"✓ Left {label} — writing on {channel_label(state, state.current_channel)}")
    return True


# ══════════════════════════════════════════════════════════════════════════════
# Location sending — history of recently used coordinates
# ══════════════════════════════════════════════════════════════════════════════

LOC_HISTORY_FILE = Path(__file__).parent / "mesh_locations.json"
LOC_HISTORY_MAX  = 5

# number with "." or "," as decimal separator, optional N/S/E/W suffix
_COORD_RE = re.compile(r"([-+]?\d+(?:[.,]\d+)?)\s*([NSEWnsew])?")


def _coords_ok(lat: float, lon: float) -> bool:
    return (math.isfinite(lat) and math.isfinite(lon)
            and -90 <= lat <= 90 and -180 <= lon <= 180
            and not (lat == 0 and lon == 0))


def parse_coords(text: str) -> tuple[float, float, int | None]:
    """Parse "lat, lon [, alt]". Accepts e.g.:
         52.22970, 21.01220        (pasted from Google Maps)
         50,81234 18,98765         (Polish decimal comma)
         52.22970N 21.01220E 110   (hemisphere letters + altitude in m)
    Raises ValueError with a user-readable message."""
    found = _COORD_RE.findall(text or "")
    if len(found) < 2:
        raise ValueError("Enter latitude and longitude, e.g. 52.22970, 21.01220")
    if len(found) > 3:
        raise ValueError("Too many numbers — expected: lat, lon [, alt]")
    vals = []
    for num, hemi in found:
        v = float(num.replace(",", "."))
        if hemi.upper() in ("S", "W"):
            v = -abs(v)
        vals.append(v)
    lat, lon = vals[0], vals[1]
    if not (-90 <= lat <= 90):
        raise ValueError(f"Latitude out of range (-90..90): {lat}")
    if not (-180 <= lon <= 180):
        raise ValueError(f"Longitude out of range (-180..180): {lon}")
    if lat == 0 and lon == 0:
        raise ValueError("0, 0 means 'no fix' in Meshtastic — not sent")
    alt = None
    if len(vals) == 3:
        alt = int(round(vals[2]))
        if not (-500 <= alt <= 9000):
            raise ValueError(f"Altitude out of range (-500..9000 m): {alt}")
    return lat, lon, alt


def _loc_key(entry: dict) -> tuple:
    return (round(entry["lat"], 5), round(entry["lon"], 5))


def load_location_history() -> list[dict]:
    """Return up to LOC_HISTORY_MAX recent locations, newest first."""
    try:
        with open(LOC_HISTORY_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return []
    result = []
    if not isinstance(data, list):
        return result
    for e in data:
        try:
            lat, lon = float(e["lat"]), float(e["lon"])
        except (KeyError, TypeError, ValueError):
            continue
        if not _coords_ok(lat, lon):
            continue
        alt = e.get("alt")
        alt = int(alt) if isinstance(alt, (int, float)) and math.isfinite(alt) else None
        result.append({"lat": lat, "lon": lon, "alt": alt,
                       "name": str(e.get("name") or "")[:40]})
    return result[:LOC_HISTORY_MAX]


def save_location_history(entry: dict) -> None:
    """Move entry to the top of the history (dedup by ~1 m), keep last 5."""
    history = load_location_history()
    key = _loc_key(entry)
    same = [h for h in history if _loc_key(h) == key]
    if not entry.get("name") and same:
        entry = {**entry, "name": same[0].get("name", "")}
    history = [entry] + [h for h in history if _loc_key(h) != key]
    history = history[:LOC_HISTORY_MAX]
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=LOC_HISTORY_FILE.parent,
            prefix=LOC_HISTORY_FILE.name + ".", suffix=".tmp", delete=False
        ) as f:
            tmp_path = Path(f.name)
            json.dump(history, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, LOC_HISTORY_FILE)
    finally:
        if tmp_path is not None and tmp_path.exists():
            tmp_path.unlink()


def extract_latlon(position) -> tuple[float, float] | None:
    """(lat, lon) from a Meshtastic position dict, or None if missing / no fix."""
    if not isinstance(position, dict):
        return None
    try:
        lat = position.get("latitude")
        lon = position.get("longitude")
        if lat is None and "latitudeI" in position:
            lat = position["latitudeI"] / 1e7
        if lon is None and "longitudeI" in position:
            lon = position["longitudeI"] / 1e7
        if lat is None or lon is None:
            return None
        lat, lon = float(lat), float(lon)
    except (TypeError, ValueError, OverflowError):
        return None
    return (lat, lon) if _coords_ok(lat, lon) else None


def my_position(state: "MeshState") -> tuple[float, float] | None:
    """Own station position: live device NodeDB first (GPS or fixed position,
    full precision), then the last one saved in mesh_nodes.json."""
    if not state.my_id.startswith("!"):
        return None
    with state.lock:
        live = state.nodes.get(state.my_id, {}).get("position")
    return (extract_latlon(live)
            or extract_latlon(state.node_db.get_entry(state.my_id).get("position")))


def dist_bearing(lat1: float, lon1: float, lat2: float, lon2: float) -> tuple[float, float]:
    """Great-circle distance in km (haversine) and initial bearing in degrees."""
    R = 6371.0088
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    km = 2 * R * math.asin(min(1.0, math.sqrt(a)))
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return km, (math.degrees(math.atan2(y, x)) + 360) % 360


_COMPASS = [("↑", "N"), ("↗", "NE"), ("→", "E"), ("↘", "SE"),
            ("↓", "S"), ("↙", "SW"), ("←", "W"), ("↖", "NW")]


def compass(bearing: float) -> tuple[str, str]:
    return _COMPASS[int((bearing + 22.5) // 45) % 8]


def position_accuracy_km(precision_bits) -> float:
    """Approximate error radius from Meshtastic precisionBits
    (half of the lat grid cell; 13 bits ≈ 2.9 km). 0 = full/unknown."""
    try:
        p = int(precision_bits)
    except (TypeError, ValueError):
        return 0.0
    if p <= 0 or p >= 32:
        return 0.0
    acc = (2 ** (32 - p)) * 1e-7 * 111.32 / 2
    return acc if acc >= 0.1 else 0.0


def relative_position(state: "MeshState", lat: float, lon: float,
                      precision_bits=None, compact: bool = False) -> str:
    """"14.2 km ↗ NE" relative to own station; "" if own position unknown.
    Reduced-precision positions are shown as approximate."""
    me = my_position(state)
    if not me:
        return ""
    km, brg = dist_bearing(me[0], me[1], lat, lon)
    acc = position_accuracy_km(precision_bits)
    if acc and km <= acc:
        return f"<{acc:.1f} km" if compact else f"within ~{acc:.1f} km"
    arrow, name = compass(brg)
    if acc:
        d = f"~{km:.0f} km" if km >= 10 else f"~{km:.1f} km"
    elif km < 1:
        d = f"{km * 1000:.0f} m"
    else:
        d = f"{km:.1f} km" if km < 100 else f"{km:.0f} km"
    if compact:
        return f"{d} {arrow}"
    s = f"{d} {arrow} {name}"
    if acc:
        s += f" (±{acc:.1f} km)"
    return s


def _fmt_loc(entry: dict) -> str:
    s = f"{entry['lat']:.5f}, {entry['lon']:.5f}"
    if entry.get("alt") is not None:
        s += f", {entry['alt']} m"
    return s


def send_position(state: MeshState, entry: dict) -> bool:
    """Send POSITION_APP packet — DM target if in DM mode, else current channel."""
    if not state.interface:
        state.error("Not connected — cannot send location.")
        return False
    dest  = state.reply_dm_to or "^all"
    is_dm = bool(state.reply_dm_to)
    try:
        sent = state.interface.sendPosition(
            latitude=entry["lat"], longitude=entry["lon"],
            altitude=int(entry.get("alt") or 0),
            destinationId=dest, wantAck=True,
            channelIndex=0 if is_dm else state.current_channel,
        )
    except Exception as e:
        state.error(f"Location send error: {e}")
        return False
    sent_id = 0
    if sent:
        sent_id = sent.get("id", 0) if isinstance(sent, dict) else getattr(sent, "id", 0)
    dest_label = state.node_db.resolve(dest) if is_dm else (
        channel_label(state, state.current_channel))
    name    = f"{entry['name']}: " if entry.get("name") else ""
    snippet = f"📍 {name}{_fmt_loc(entry)}"
    state.add("tapback_mine", f"{snippet} → {dest_label}")
    if sent_id:
        with state.lock:
            state.pending_ack[sent_id] = ack_record(snippet, dest_label, dest)
    play_sound("sent")
    return True


def send_tapback(state: MeshState, emoji: str, reply_id: int,
                 dest: str = "^all", orig_text: str = "", channel: int | None = None):
    if not state.interface:
        state.error("Not connected — cannot send tapback.")
        return
    try:
        from meshtastic.protobuf import mesh_pb2, portnums_pb2
        from meshtastic import BROADCAST_NUM
        iface = state.interface
        if dest == "^all":
            dest_num = BROADCAST_NUM
        elif isinstance(dest, str) and dest.startswith("!"):
            dest_num = int(dest[1:], 16)
        else:
            dest_num = int(dest)
        mesh_packet = mesh_pb2.MeshPacket()
        # a reaction belongs on the channel of the message it reacts to
        mesh_packet.channel              = 0 if dest != "^all" else (
            state.current_channel if channel is None else channel)
        mesh_packet.decoded.portnum      = portnums_pb2.PortNum.TEXT_MESSAGE_APP
        mesh_packet.decoded.payload      = emoji.encode("utf-8")
        mesh_packet.decoded.reply_id     = reply_id
        mesh_packet.decoded.emoji        = 1
        mesh_packet.id                   = iface.currentPacketId = (iface.currentPacketId + 1) & 0xFFFFFFFF
        # want_ack must go through the argument: _sendPacket() overwrites the
        # packet's field with it (default False) — before 1.4.5 tapbacks went
        # out without ACK request, so they got no confirmation and no retries
        iface._sendPacket(mesh_packet, destinationId=dest, wantAck=True)
        label = orig_text if orig_text else (
            _resolve_name(state, iface, dest) if dest != "^all" else "channel"
        )
        msg = state.add("tapback_mine", f"⚡ Tapback {emoji} → {label}",
                        channel=mesh_packet.channel if dest == "^all" else 0)
        # track the ROUTING answer like for text messages (v1.4.5): channel →
        # "heard by the mesh", DM → relayed / delivered / not delivered
        ack_label = (_resolve_name(state, iface, dest) if dest != "^all"
                     else channel_label(state, mesh_packet.channel))
        quoted = orig_text.strip('"')          # caller already wraps it in quotes
        snippet = f"Tapback {emoji}" + (f" ↩ {quoted}" if quoted else "")
        with state.lock:
            state.pending_ack[mesh_packet.id] = ack_record(snippet, ack_label, dest,
                                                           msg=msg, tapback=True)
        play_sound("sent")
    except Exception as e:
        state.error(f"Tapback error: {e}")


# ══════════════════════════════════════════════════════════════════════════════
# Radio diagnostics — LocalStats, telemetry requests, relay statistics (v1.3.0)
# ══════════════════════════════════════════════════════════════════════════════

TELEM_TIMEOUT      = 60     # s — no answer → "no response" in the log
OWN_STATS_REFRESH  = 300    # s — own LocalStats older than this get re-requested
SNR_UNKNOWN        = -128   # INT8_MIN — firmware marker for "SNR not measured"
DIRECT_SEEN_TTL    = 3 * 3600   # s — how long a "heard directly" observation counts

_SPARK = "▁▂▃▄▅▆▇█"


def sparkline(values: list[float], vmax: float) -> str:
    """Unicode sparkline on a FIXED scale 0..vmax (stable meaning over time)."""
    if not values:
        return ""
    out = []
    for v in values:
        r = max(0.0, min(1.0, (v or 0) / vmax)) if vmax > 0 else 0.0
        out.append(_SPARK[min(len(_SPARK) - 1, int(r * len(_SPARK)))])
    return "".join(out)


def sparkline_auto(values: list[float], min_span: float) -> tuple[str, float, float]:
    """Sparkline scaled to the data range (lo..hi). min_span keeps tiny
    fluctuations (e.g. 3.2 → 3.3 %) from being blown up to full height.
    Returns (bars, lo, hi)."""
    vals = [v or 0.0 for v in values]
    if not vals:
        return "", 0.0, 0.0
    lo, hi = min(vals), max(vals)
    span = hi - lo
    if span < min_span:
        # centre the data in a window of min_span (never below zero)
        mid = (lo + hi) / 2
        lo = max(0.0, mid - min_span / 2)
        hi = lo + min_span
        span = min_span
    out = []
    for v in vals:
        r = (v - lo) / span if span else 0.0
        out.append(_SPARK[max(0, min(len(_SPARK) - 1, int(r * len(_SPARK))))])
    return "".join(out), min(vals), max(vals)


def _fmt_uptime(sec) -> str:
    try:
        sec = int(sec)
    except (TypeError, ValueError):
        return "?"
    d, rem = divmod(sec, 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    if d:
        return f"{d}d {h:02d}h"
    if h:
        return f"{h}h {m:02d}m"
    return f"{m}m"


def _pct(part, whole) -> float | None:
    try:
        return 100.0 * part / whole if whole else None
    except (TypeError, ZeroDivisionError):
        return None


def cu_style(cu) -> str:
    """Channel utilization: firmware starts throttling at 25 %, hard limit ~40 %."""
    if cu is None:
        return "dim"
    return "bold red" if cu >= 40 else ("yellow" if cu >= 25 else "green")


def air_style(air) -> str:
    """TX airtime per hour: EU868 duty cycle limit is 10 %."""
    if air is None:
        return "dim"
    return "bold red" if air >= 10 else ("yellow" if air >= 7 else "green")


def fmt_local_stats(ls: dict) -> str:
    """One-line LocalStats summary (fields missing in protobuf dict = 0)."""
    rx      = ls.get("numPacketsRx", 0)
    bad     = ls.get("numPacketsRxBad", 0)
    dupe    = ls.get("numRxDupe", 0)
    relay   = ls.get("numTxRelay", 0)
    cancel  = ls.get("numTxRelayCanceled", 0)
    parts   = [f"CU {ls.get('channelUtilization', 0):.1f}%",
               f"airTx {ls.get('airUtilTx', 0):.2f}%",
               f"rx {rx}"]
    p_bad, p_dupe = _pct(bad, rx), _pct(dupe, rx)
    if p_bad is not None:
        parts.append(f"bad {p_bad:.1f}%")
    if p_dupe is not None:
        parts.append(f"dupe {p_dupe:.0f}%")
    parts.append(f"tx {ls.get('numPacketsTx', 0)}")
    if relay or cancel:
        parts.append(f"relay {relay}/cancel {cancel}")
    parts.append(f"nodes {ls.get('numOnlineNodes', 0)}/{ls.get('numTotalNodes', 0)}")
    if ls.get("noiseFloor"):
        parts.append(f"noise {ls['noiseFloor']} dBm")
    if ls.get("uptimeSeconds"):
        parts.append(f"up {_fmt_uptime(ls['uptimeSeconds'])}")
    return "  ".join(parts)


def _lib_has_noise_floor() -> bool:
    """Older meshtastic Python packages don't know LocalStats.noise_floor and
    silently drop it while decoding — the value never reaches us."""
    try:
        from meshtastic.protobuf import telemetry_pb2
        return "noise_floor" in telemetry_pb2.LocalStats.DESCRIPTOR.fields_by_name
    except Exception:
        return False


def _fw_tuple(fw: str) -> tuple:
    try:
        return tuple(int(x) for x in fw.split(".")[:3])
    except (ValueError, AttributeError):
        return ()


def noise_floor_hint(state: "MeshState") -> str:
    """Why there is no noise floor reading."""
    if not _lib_has_noise_floor():
        return "update library: pip install -U meshtastic"
    fw = _fw_tuple(state.device_fw)
    if fw and fw < (2, 7, 25):
        return "needs firmware ≥ 2.7.25"
    return "no reading yet"


def fmt_device_metrics(dm: dict) -> str:
    parts = []
    if dm.get("batteryLevel") is not None:
        bat = dm["batteryLevel"]
        parts.append("ext.power" if bat > 100 else f"bat {bat}%")
    if dm.get("voltage"):
        parts.append(f"{dm['voltage']:.2f}V")
    if dm.get("channelUtilization") is not None:
        parts.append(f"CU {dm['channelUtilization']:.1f}%")
    if dm.get("airUtilTx") is not None:
        parts.append(f"airTx {dm['airUtilTx']:.2f}%")
    if dm.get("uptimeSeconds"):
        parts.append(f"up {_fmt_uptime(dm['uptimeSeconds'])}")
    return "  ".join(parts) or "(empty)"


def own_air(state: "MeshState") -> tuple[float | None, float | None]:
    """Own channel utilization / TX airtime: fresh LocalStats first,
    then deviceMetrics from the device NodeDB."""
    now = time.time()
    with state.lock:
        ls_entry = state.local_stats.get(state.my_id)
        dm = state.nodes.get(state.my_id, {}).get("deviceMetrics", {})
    if ls_entry and now - ls_entry[1] < 2 * OWN_STATS_REFRESH:
        ls = ls_entry[0]
        return ls.get("channelUtilization", 0.0), ls.get("airUtilTx", 0.0)
    if dm:
        return dm.get("channelUtilization"), dm.get("airUtilTx")
    return None, None


def request_telemetry(state: "MeshState", dest: str, ttype: str = "local_stats",
                      quiet: bool = False) -> bool:
    """Ask a node for telemetry (wantResponse). ttype: local_stats | device_metrics.
    The library call blocks until the answer (or its own timeout), so it runs
    in a daemon thread; the answer itself is handled in _process_queue."""
    iface = state.interface
    if not iface:
        if not quiet:
            state.error("Not connected — cannot request telemetry.")
        return False
    name = _resolve_name(state, iface, dest)
    with state.lock:
        state.telem_pending[dest] = (name, time.time(), ttype, quiet)
    def _send():
        try:
            # direct request to one node → primary channel (PKI if keys known)
            iface.sendTelemetry(destinationId=dest, wantResponse=True,
                                channelIndex=0, telemetryType=ttype)
        except Exception as e:
            # library timeout ("Timed out waiting for telemetry") is expected —
            # our own TELEM_TIMEOUT in _tick reports a missing answer
            if "Timed out" not in str(e) and not quiet:
                state.error(f"Telemetry request error: {e}")
    threading.Thread(target=_send, daemon=True, name="TelemReq").start()
    return True


def _node_num(nid: str) -> int | None:
    try:
        return int(nid[1:], 16) if nid and nid.startswith("!") else None
    except ValueError:
        return None


RELAY_MAX_KM = 50   # a guessed relay farther than this (by stored position) is implausible


def _node_pos(state: "MeshState", nid: str) -> tuple[float, float] | None:
    with state.lock:
        live = state.nodes.get(nid, {}).get("position")
    return extract_latlon(live) or extract_latlon(state.node_db.get_entry(nid).get("position"))


def heard_directly(state: "MeshState", nid: str) -> bool:
    """True if a packet the node transmitted itself (0 hops) reached us
    within DIRECT_SEEN_TTL — i.e. it is a confirmed radio neighbour."""
    num = _node_num(nid)
    if num is None:
        return False
    with state.lock:
        d = state.direct_seen.get(num & 0xFF, {}).get(nid)
    return bool(d) and time.time() - d["t"] < DIRECT_SEEN_TTL


NEIGHBOURS_FILE  = Path(__file__).parent / "mesh_neighbours.json"
NEIGHBOUR_KEEP   = 7 * 86400    # s — a learned neighbour survives restarts this long
NEIGHBOUR_SAVE_EVERY = 300      # s — periodic save (crash safety); also saved on exit


def load_neighbours(state: "MeshState") -> int:
    """Restore neighbours learned in earlier sessions (heard directly or seen
    as the last hop of a traceroute) — fixed repeaters don't change, so the
    1-byte relay_node stays resolved after a restart."""
    try:
        data = json.loads(NEIGHBOURS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return 0
    now, n = time.time(), 0
    with state.lock:
        for nid, d in (data or {}).items():
            num = _node_num(nid)
            t = d.get("t", 0) if isinstance(d, dict) else 0
            if num is None or not isinstance(t, (int, float)) or now - t > NEIGHBOUR_KEEP:
                continue
            slot = state.direct_seen.setdefault(num & 0xFF, {})
            if nid not in slot or slot[nid]["t"] < t:
                slot[nid] = {"t": t, "snr": d.get("snr")}
                n += 1
    return n


def save_neighbours(state: "MeshState") -> None:
    now = time.time()
    with state.lock:
        out = {nid: {"t": round(d["t"]), "snr": d.get("snr")}
               for slot in state.direct_seen.values() for nid, d in slot.items()
               if now - d["t"] <= NEIGHBOUR_KEEP}
    try:
        NEIGHBOURS_FILE.write_text(json.dumps(out, ensure_ascii=False, indent=0),
                                   encoding="utf-8")
    except Exception:
        pass
    state.neighbours_saved = now


def learn_neighbour(state: "MeshState", node_num: int, snr_raw=None) -> None:
    """A traceroute showed node_num as the last hop before us (full ID) →
    remember it like a node heard directly. snr_raw is traceroute SNR (dB*4)."""
    if not node_num or node_num & 0xFFFFFFFF == 0xFFFFFFFF:
        return                                   # unknown hop placeholder
    nid = f"!{node_num & 0xFFFFFFFF:08x}"
    if nid == state.my_id:
        return
    snr = None
    if isinstance(snr_raw, (int, float)) and snr_raw != SNR_UNKNOWN:
        snr = snr_raw / 4.0
    with state.lock:
        state.direct_seen.setdefault(node_num & 0xFF, {})[nid] = {"t": time.time(),
                                                                  "snr": snr}


def relay_label(state: "MeshState", relay_byte: int, snr: float | None = None) -> str:
    """Resolve the 1-byte relay_node to a node name.

    Firmware sends only the LAST byte of the relaying node's number, so with
    hundreds of known nodes several share it. Only one source is certain: a
    packet a node transmitted itself (0 hops) ties its full ID to the byte.
      • learned from such packets   → name without '?'
        (several learned → the one whose direct SNR is closest to `snr`, '?')
      • anything else is a guess    → name WITH '?': device-direct
        neighbours (hopsAway 0) first, then the closest by stored position
        (farther than RELAY_MAX_KM skipped), then ones without a position
      • nothing plausible           → '?…xx' (just the byte)"""
    if not relay_byte:
        return ""
    now = time.time()
    with state.lock:
        known = list(state.direct_seen.get(relay_byte, {}).items())
    # confirmed in the last DIRECT_SEEN_TTL first; otherwise one confirmed in
    # an earlier session (mesh_neighbours.json, up to NEIGHBOUR_KEEP)
    learned = [x for x in known if now - x[1]["t"] < DIRECT_SEEN_TTL] \
        or [x for x in known if now - x[1]["t"] < NEIGHBOUR_KEEP]
    if learned:
        if len(learned) > 1 and snr is not None:
            with_snr = [x for x in learned if x[1].get("snr") is not None]
            if with_snr:
                with_snr.sort(key=lambda x: abs(x[1]["snr"] - snr))
                learned = with_snr + [x for x in learned if x not in with_snr]
        else:
            learned.sort(key=lambda x: x[1]["t"], reverse=True)
        return _resolve_name(state, state.interface, learned[0][0]) \
            + ("?" if len(learned) > 1 else "")

    # guess (never certain) — candidates with this last byte from the device
    # NodeDB and our database, at most RELAY_MAX_KM away by stored position.
    # Order (1.4.6): nodes the DEVICE has heard directly (hopsAway 0) first —
    # a relay must be a radio neighbour, and a closer node with no direct link
    # (e.g. behind a hill) can't be it; then by distance (known position
    # before unknown); then the most recently heard.
    me = my_position(state)
    with state.lock:
        session = dict(state.nodes)
    ids = {nid for nid in session} | set(state.node_db.all_ids())
    ranked = []
    for nid in ids:
        if nid == state.my_id or (_node_num(nid) or -1) & 0xFF != relay_byte:
            continue
        info = session.get(nid, {})
        pos = _node_pos(state, nid)
        dist = dist_bearing(me[0], me[1], pos[0], pos[1])[0] if (me and pos) else None
        if dist is not None and dist > RELAY_MAX_KM:
            continue
        heard = info.get("lastHeard") or 0
        if not heard:
            ls = state.node_db.get_entry(nid).get("last_seen", "")
            try:
                heard = datetime.fromisoformat(ls).timestamp() if ls else 0
            except (ValueError, TypeError):
                heard = 0
        key = (0 if info.get("hopsAway") == 0 else 1,
               0 if dist is not None else 1, dist or 0.0, -heard)
        ranked.append((key, nid, info))
    if ranked:
        ranked.sort(key=lambda r: r[0])
        _, nid, info = ranked[0]
        name = (info.get("user", {}).get("longName") or "").strip() \
            or state.node_db.resolve(nid)
        return name + "?"
    return f"?…{relay_byte:02x}"


def record_relay(state: "MeshState", packet: dict, from_id: str) -> str:
    """Count the relaying neighbour of every packet received over LoRa.
    Returns the relay label (or "")."""
    if from_id == state.my_id or packet.get("viaMqtt"):
        return ""
    hop_limit = packet.get("hopLimit", -1)
    hop_start = packet.get("hopStart", -1)
    snr = packet.get("rxSnr")
    if snr is None and packet.get("rxRssi") is not None:
        snr = 0.0
    num = _node_num(from_id)
    if num is not None and hop_limit >= 0 and hop_start >= 0 and hop_start == hop_limit:
        # 0 hops: the sender transmitted it itself → its low byte is now
        # unambiguously tied to this full node ID (with the SNR we hear it at)
        with state.lock:
            state.direct_seen.setdefault(num & 0xFF, {})[from_id] = {"t": time.time(),
                                                                     "snr": snr}
    relay_byte = packet.get("relayNode", 0) or 0
    if not relay_byte:
        return ""
    with state.lock:
        st = state.relay_stats.setdefault(
            relay_byte, {"count": 0, "snr_sum": 0.0, "snr_n": 0, "last": 0.0})
        st["count"] += 1
        st["last"] = time.time()
        if snr is not None:
            st["snr_sum"] += snr
            st["snr_n"] += 1
        state.relay_total += 1
    return relay_label(state, relay_byte, snr)


def top_relays(state: "MeshState", n: int = 5) -> list[tuple[str, int, float, float | None]]:
    """[(label, count, share %, avg SNR)] — most frequent relays first."""
    with state.lock:
        items = [(b, dict(s)) for b, s in state.relay_stats.items()]
        total = state.relay_total
    items.sort(key=lambda x: x[1]["count"], reverse=True)
    out = []
    for b, s in items[:n]:
        avg = s["snr_sum"] / s["snr_n"] if s["snr_n"] else None
        out.append((relay_label(state, b, avg), s["count"],
                    100.0 * s["count"] / total if total else 0.0, avg))
    return out


def _fmt_tr_snr(snr_val) -> str:
    """Traceroute SNR is stored as dB*4; -128 means 'not measured'."""
    if snr_val is None:
        return ""
    if snr_val == SNR_UNKNOWN:
        return "(?dB)"
    return f"({snr_val / 4:.1f}dB)"


def _format_telemetry_packet(packet: dict, state: MeshState, iface) -> list[str]:
    decoded   = packet.get("decoded", {})
    ptype     = decoded.get("portnum", "UNKNOWN")
    from_id   = packet.get("fromId") or f"!{packet.get('from', 0):08x}"
    to_id     = packet.get("toId", "?")
    pkt_id    = packet.get("id", 0)
    channel   = packet.get("channel", 0)
    hop_limit = packet.get("hopLimit", -1)
    hop_start = packet.get("hopStart", -1)
    rx_snr    = packet.get("rxSnr", None)
    rx_rssi   = packet.get("rxRssi", None)
    want_ack  = packet.get("wantAck", False)
    via_mqtt  = packet.get("viaMqtt", False)
    priority  = packet.get("priority", "")
    encrypted = packet.get("encrypted", None)

    from_name = _resolve_name(state, iface, from_id)
    to_name   = _resolve_name(state, iface, to_id) if to_id and to_id != "^all" else "broadcast"

    lines = []
    hdr   = f"[{ptype}] {from_name}→{to_name}"
    meta  = []
    if pkt_id:    meta.append(f"id={pkt_id:#010x}")
    if channel:   meta.append(f"ch={channel}")
    if hop_limit >= 0:
        hops_used = (hop_start - hop_limit) if hop_start >= 0 else -1
        meta.append(f"hops={hops_used}/{hop_start}")
    if rx_snr  is not None: meta.append(f"SNR={rx_snr:.1f}")
    if rx_rssi is not None: meta.append(f"RSSI={rx_rssi}")
    if want_ack:  meta.append("wantAck")
    if via_mqtt:  meta.append("viaMQTT")
    if priority:  meta.append(f"pri={priority}")
    if meta:
        hdr += "  " + " ".join(meta)
    lines.append(hdr)

    if ptype == "TEXT_MESSAGE_APP":
        text     = decoded.get("text", "")
        emoji    = decoded.get("emoji", 0)
        reply_id = decoded.get("replyId", 0)
        detail   = f'   💬 "{text}"'
        if emoji:    detail += " [tapback]"
        if reply_id: detail += f" replyTo={reply_id:#010x}"
        lines.append(detail)
    elif ptype == "POSITION_APP":
        pos  = decoded.get("position", {})
        lat  = pos.get("latitude",  pos.get("latitudeI",  0) / 1e7)
        lon  = pos.get("longitude", pos.get("longitudeI", 0) / 1e7)
        alt  = pos.get("altitude", 0)
        sats = pos.get("satsInView", pos.get("numSatellites", "?"))
        pdop = pos.get("PDOP", pos.get("pdop", "?"))
        fix  = pos.get("fixType", pos.get("gpsFixType", "?"))
        ts_p = pos.get("time", 0)
        detail = f"   📍 lat={lat:.6f} lon={lon:.6f} alt={alt}m sats={sats} pdop={pdop} fix={fix}"
        if ts_p: detail += f" time={ts_p}"
        lines.append(detail)
    elif ptype == "NODEINFO_APP":
        user = decoded.get("user", {})
        lines.append(f'   👤 id={user.get("id","?")} long="{user.get("longName","?")}" '
                     f'short="{user.get("shortName","?")}" hw={user.get("hwModel","?")} '
                     f'role={user.get("role","?")}')
    elif ptype == "TELEMETRY_APP":
        telem = decoded.get("telemetry", {})
        dm    = telem.get("deviceMetrics", {})
        em    = telem.get("environmentMetrics", {})
        pm    = telem.get("powerMetrics", {})
        am    = telem.get("airQualityMetrics", {})
        ls    = telem.get("localStats", {})
        if ls:
            lines.append(f"   📶 {fmt_local_stats(ls)}")
        if dm:
            lines.append(f'   🔋 batt={dm.get("batteryLevel","?")}% '
                         f'volt={dm.get("voltage","?")}V '
                         f'chUtil={dm.get("channelUtilization","?")}% '
                         f'airTx={dm.get("airUtilTx","?")}% '
                         f'uptime={dm.get("uptimeSeconds","?")}s')
        if em:
            parts = [f'temp={em.get("temperature","?")}°C']
            if em.get("relativeHumidity"):   parts.append(f'hum={em["relativeHumidity"]}%')
            if em.get("barometricPressure"): parts.append(f'press={em["barometricPressure"]}hPa')
            if em.get("iaq"):               parts.append(f'IAQ={em["iaq"]}')
            lines.append("   🌡  " + " ".join(parts))
        if pm:
            items = [f"{k}={v}" for k, v in pm.items() if k != "time"]
            if items: lines.append("   ⚡ " + " ".join(items))
        if am:
            items = [f"{k}={v}" for k, v in am.items() if k != "time"]
            if items: lines.append("   💨 " + " ".join(items))
        if not (dm or em or pm or am or ls):
            lines.append(f"   📊 raw={telem}")
    elif ptype == "TRACEROUTE_APP":
        tr  = decoded.get("traceroute", {})
        fwd = [f"!{n & 0xffffffff:08x}" for n in tr.get("route", [])]
        bck = [f"!{n & 0xffffffff:08x}" for n in tr.get("routeBack", [])]
        lines.append(f"   🔍 route→ {fwd} snr={tr.get('snrTowards',[])}")
        if bck: lines.append(f"   🔍 route← {bck} snr={tr.get('snrBack',[])}")
    elif ptype == "ROUTING_APP":
        routing = decoded.get("routing", decoded.get("requestId", ""))
        if isinstance(routing, dict):
            err = routing.get("errorReason", routing.get("raw", ""))
            lines.append(f"   🔀 routing: {err if err else routing}")
        else:
            lines.append(f"   🔀 routing: {routing}")
    elif ptype == "NEIGHBORINFO_APP":
        ni = decoded.get("neighborinfo", decoded.get("neighbors", ""))
        if isinstance(ni, dict):
            neighbors = ni.get("neighbors", [])
            nb_list   = [f'!{nb.get("nodeId",0)&0xffffffff:08x}(snr={nb.get("snr","?")})' for nb in neighbors]
            lines.append(f'   🏘  node={ni.get("nodeId","?")} neighbors: {", ".join(nb_list) or "(brak)"}')
        else:
            lines.append(f"   🏘  neighborinfo: {ni}")
    else:
        raw_keys = [k for k in decoded.keys() if k != "portnum"]
        if raw_keys:
            for k in raw_keys:
                lines.append(f"   📋 {k}: {decoded[k]}")
        elif encrypted:
            lines.append(f"   🔒 encrypted ({len(encrypted)} bytes)")

    return lines


def _process_queue(state: MeshState):
    """Drain the packet queue and update state. Call from UI thread (Textual timer).
    Processes max 10 packets per call to avoid blocking the UI."""
    processed = 0
    try:
        while processed < 10:
            kind, a, b = state.packet_queue.get_nowait()
            processed += 1

            if kind == "packet":
                packet, iface = a, b
                decoded = packet.get("decoded", {})
                ptype   = decoded.get("portnum", "")
                from_id = packet.get("fromId") or f"!{packet.get('from', 0):08x}"
                rx_time = packet.get("rxTime")
                ts      = datetime.fromtimestamp(rx_time).strftime("%H:%M:%S") if rx_time else ""

                if state.telemetry:
                    try:
                        for tl in _format_telemetry_packet(packet, state, iface):
                            state.raw(f"[T] {tl.lstrip()}", ts=ts)
                    except Exception as _te:
                        state.raw(f"[T] [err] {_te}", ts=ts)

                # link quality of the last hop + who relayed it (every packet)
                rx_snr  = packet.get("rxSnr")
                rx_rssi = packet.get("rxRssi")
                if rx_rssi is not None and rx_snr is None:
                    rx_snr = 0.0    # protobuf → dict drops a 0.0 SNR
                try:
                    relay = record_relay(state, packet, from_id)
                except Exception:
                    relay = ""
                _hl, _hs = packet.get("hopLimit", -1), packet.get("hopStart", -1)
                if (from_id != state.my_id and not packet.get("viaMqtt")
                        and _hl >= 0 and _hs >= _hl):
                    with state.lock:
                        state.hops_seen[from_id] = (time.time(), _hs - _hl)

                if ptype == "TEXT_MESSAGE_APP":
                    text       = decoded.get("text", "")
                    name       = _resolve_name(state, iface, from_id)
                    channel    = packet.get("channel", 0)
                    to_id      = packet.get("toId", "^all")
                    is_dm      = (to_id == state.my_id and to_id != "^all")
                    is_tapback = bool(decoded.get("emoji", 0))
                    msg_kind   = "dm" if is_dm else ("private" if channel != 0 else "theirs")
                    msg_id     = packet.get("id", 0)
                    reply_id   = decoded.get("replyId", 0)
                    hop_limit  = packet.get("hopLimit", -1)
                    hop_start  = packet.get("hopStart", hop_limit)
                    hops       = (hop_start - hop_limit) if hop_limit >= 0 else -1
                    if is_tapback and reply_id:
                        orig_text = orig_sender = ""
                        with state.lock:
                            orig = state.msg_by_id.get(reply_id)
                            if orig:
                                orig_flat   = orig.text.replace("\n", " ")
                                orig_text   = orig_flat[:30] + ("..." if len(orig_flat) > 30 else "")
                                orig_sender = orig.sender if orig.sender else "Me"
                        if orig_text:
                            state.add("tapback", f"⚡ {name} reacts {text} to [{orig_sender}]: {orig_text}" if orig_sender.strip() else f"⚡ {name} reacts {text} to: {orig_text}", ts=ts, channel=channel)
                        else:
                            state.add("tapback", f"⚡ {name} reacts {text}", ts=ts, channel=channel)
                        play_sound("message")
                    else:
                        state.add(msg_kind, text, sender=name, sender_id=from_id,
                                  ts=ts, msg_id=msg_id, reply_id=reply_id,
                                  hops=hops, to_id=to_id, snr=rx_snr, rssi=rx_rssi,
                                  channel=channel,
                                  relay=relay if hops > 0 else "")
                        play_sound("private" if msg_kind in ("private", "dm") else "message")

                elif ptype == "POSITION_APP":
                    pos  = decoded.get("position", {})
                    state.node_db.update_position(from_id, pos)
                    name = _resolve_name(state, iface, from_id)
                    ll   = extract_latlon(pos)
                    if ll is None:
                        state.system(f"📍 {name}: (no valid fix)", ts=ts)
                    else:
                        lat, lon = ll
                        alt  = pos.get("altitude", 0)
                        line = f"📍 {name}: {lat:.5f}, {lon:.5f}"
                        if alt:
                            line += f" (alt={alt}m)"
                        if from_id != state.my_id:
                            rel = relative_position(state, lat, lon,
                                                    pos.get("precisionBits"))
                            if rel:
                                line += f" • {rel}"
                        state.system(line, ts=ts)
                        if state._logfile and from_id.startswith("!"):
                            state._log(ts or datetime.now().strftime("%H:%M:%S"),
                                       f"#POS {from_id} {lat:.5f} {lon:.5f} {alt}")
                    state.node_db.touch(from_id)

                elif ptype == "NODEINFO_APP":
                    user      = decoded.get("user", {})
                    node_id   = user.get("id") or from_id
                    long_name = user.get("longName", "")
                    hw        = user.get("hwModel", "")
                    raw_key   = user.get("publicKey", b"")
                    pub_key   = _normalize_pub_key(raw_key)
                    old_key = state.node_db.get_entry(node_id).get("pub_key", "") if node_id else ""
                    _remember_user(state, node_id, user)
                    if node_id and pub_key and old_key and pub_key != old_key:
                        # the node got new keys (e.g. it was reset) — our device
                        # keeps the old one, so load the new one explicitly
                        state.system(f"🔑 {state.node_db.resolve(node_id)} has a NEW key "
                                     f"(changed since last seen)", ts=ts)
                        with state.lock:
                            state.contact_pushed.pop(node_id, None)
                            state.contact_push_failed.discard(node_id)
                        push_contact(state, node_id)
                    if node_id:
                        with state.lock:
                            state.nodeinfo_seen[node_id] = time.time()
                    long_name = long_name or user.get("shortName", "")
                    if node_id and long_name:
                        if state._logfile:
                            state._log(ts or datetime.now().strftime("%H:%M:%S"),
                                       f"#NODE {node_id} {long_name}"
                                       + (" [PKI]" if pub_key else ""))

                elif ptype == "TRACEROUTE_APP":
                    tr          = decoded.get("traceroute", {})
                    route       = tr.get("route",      [])
                    route_back  = tr.get("routeBack",  [])
                    snr_towards = tr.get("snrTowards", [])
                    snr_back    = tr.get("snrBack",    [])

                    def _fmt_hop(nid_int: int, snr_val=None) -> str:
                        nid_str  = f"!{nid_int & 0xffffffff:08x}"
                        nm       = _resolve_name(state, iface, nid_str)
                        label    = nm if nm != nid_str else nid_str
                        return f"{label}{_fmt_tr_snr(snr_val)}"

                    sender_name = _resolve_name(state, iface, from_id)
                    with state.lock:
                        pending_tr   = state.traceroute_pending.get(from_id)
                        we_initiated = pending_tr is not None
                    if we_initiated:
                        origin_name, dest_name = state.my_name, sender_name
                    else:
                        origin_name, dest_name = sender_name, state.my_name

                    # snrTowards/snrBack have one more entry than route: the
                    # SNR measured by the final receiver of each direction
                    def _end_snr(snrs: list, route_len: int) -> str:
                        return _fmt_tr_snr(snrs[route_len]) if len(snrs) > route_len else ""

                    hops_fwd = [origin_name] + [_fmt_hop(n, snr_towards[i] if i < len(snr_towards) else None)
                                                for i, n in enumerate(route)] \
                               + [dest_name + _end_snr(snr_towards, len(route))]
                    path_fwd = " → ".join(hops_fwd)

                    if route_back:
                        hops_rev = [dest_name] + [_fmt_hop(n, snr_back[i] if i < len(snr_back) else None)
                                                  for i, n in enumerate(route_back)] \
                                   + [origin_name + _end_snr(snr_back, len(route_back))]
                        state.system(f"TR ↗  {path_fwd}", ts=ts)
                        state.system(f"TR ↙  {' → '.join(hops_rev)}", ts=ts)
                    else:
                        state.system(f"TR ↗  {path_fwd}", ts=ts)
                    # the last hop of the path that ENDS at us carries full node
                    # IDs → a certain radio neighbour that delivers to us; it
                    # resolves the 1-byte relay_node in "Last-hop neighbours"
                    path_to_me, snrs_to_me = (route_back, snr_back) if we_initiated \
                        else (route, snr_towards)
                    if path_to_me:
                        learn_neighbour(state, path_to_me[-1],
                                        snrs_to_me[len(path_to_me)]
                                        if len(snrs_to_me) > len(path_to_me) else None)
                    if we_initiated:
                        rtt = time.time() - pending_tr[1]
                        summary = f"TR ⏱  RTT {rtt:.1f}s   hops ↗{len(route)}"
                        if route_back:
                            summary += f" ↙{len(route_back)}"
                        state.system(summary, ts=ts)
                        if state._logfile:
                            state._log(ts or datetime.now().strftime("%H:%M:%S"),
                                       f"#TR {from_id} rtt={rtt:.1f} fwd={len(route)} "
                                       f"back={len(route_back)} {path_fwd}")
                    with state.lock:
                        state.traceroute_pending.pop(from_id, None)
                        for _pid in [k for k, v in state.tr_by_id.items() if v == from_id]:
                            state.tr_by_id.pop(_pid, None)

                elif ptype == "TELEMETRY_APP":
                    telem = decoded.get("telemetry", {})
                    ls    = telem.get("localStats")
                    dm    = telem.get("deviceMetrics")
                    if ls:
                        with state.lock:
                            state.local_stats[from_id] = (ls, time.time())
                        if state._logfile:
                            state._log(ts or datetime.now().strftime("%H:%M:%S"),
                                       f"#LSTATS {from_id} {fmt_local_stats(ls)}")
                    with state.lock:
                        pend = state.telem_pending.get(from_id)
                    # an answer = the requested variant, or anything when the
                    # node doesn't support local_stats and answers with metrics
                    if pend and (ls or dm):
                        p_name, p_sent, p_type, p_quiet = pend
                        with state.lock:
                            state.telem_pending.pop(from_id, None)
                        if not p_quiet:
                            name = _resolve_name(state, iface, from_id)
                            rtt  = time.time() - p_sent
                            if ls:
                                state.system(f"📶 {name}: {fmt_local_stats(ls)}  ({rtt:.1f}s)", ts=ts)
                            if dm:
                                state.system(f"🔋 {name}: {fmt_device_metrics(dm)}  ({rtt:.1f}s)", ts=ts)
                            if p_type == "local_stats" and not ls:
                                state.system(f"  ↳ {name} answered without LocalStats "
                                             f"(firmware does not report them remotely)", ts=ts)

                elif ptype == "ROUTING_APP":
                    routing    = decoded.get("routing", {})
                    error      = routing.get("errorReason", "NONE")
                    request_id = packet.get("requestId", 0) or decoded.get("requestId", 0)
                    if not request_id:
                        request_id = routing.get("requestId", 0)
                    with state.lock:
                        ack = state.pending_ack.get(request_id)
                        tr_target = state.tr_by_id.get(request_id)
                    if ack:
                        handle_ack(state, request_id, ack, error, from_id, ts)
                    elif tr_target:
                        handle_traceroute_routing(state, request_id, tr_target, error,
                                                  from_id, ts)

                if iface and iface.nodes:
                    with state.lock:
                        state.nodes = dict(iface.nodes)

            elif kind in ("connected", "connected_fallback"):
                if state.connected:
                    continue
                iface = a
                with state.lock:
                    state.interface = iface
                state.connected = True
                try:
                    def _silent_traceroute(p, _iface=iface):
                        _iface._acknowledgment.receivedTraceRoute = True
                    iface.onResponseTraceRoute = _silent_traceroute
                except Exception:
                    pass
                try:
                    # the library prints telemetry answers to stdout (breaks the TUI);
                    # we only need the ack flag — the packet itself comes via pubsub
                    def _silent_telemetry(p, _iface=iface):
                        _iface._acknowledgment.receivedTelemetry = True
                    iface.onResponseTelemetry = _silent_telemetry
                except Exception:
                    pass
                my_node = iface.getMyNodeInfo()
                if my_node:
                    user          = my_node.get("user", {})
                    state.my_id   = user.get("id", "???")
                    state.my_name = user.get("longName", "???")
                meta = iface.metadata
                if meta:
                    state.device_hw = getattr(meta, "hw_model_name", "") or ""
                    state.device_fw = getattr(meta, "firmware_version", "") or ""
                if not state.device_hw and iface.nodes:
                    my_node_info    = iface.nodes.get(state.my_id, {})
                    hw_fallback     = my_node_info.get("user", {}).get("hwModel", "")
                    if hw_fallback:
                        state.device_hw = hw_fallback
                if iface.nodes:
                    with state.lock:
                        state.nodes = dict(iface.nodes)
                    _populate_node_db(state, iface.nodes)
                state.system(
                    f"✓ Connected as {state.my_name} ({state.my_id})"
                    + (f" • {state.device_hw}" if state.device_hw else "")
                )
                try:
                    iface.localNode.setTime()
                    import datetime as _dt
                    state.system(f"🕐 Device clock synchronized ({_dt.datetime.now().strftime('%H:%M:%S')})")
                except Exception as _e:
                    state.system(f"⚠ Failed to synchronize device clock: {_e}")

                def _refresh_nodes(iface=iface, state=state):
                    for _ in range(10):
                        time.sleep(1)
                        if iface and iface.nodes:
                            with state.lock:
                                state.nodes = dict(iface.nodes)
                            _populate_node_db(state, iface.nodes)
                            state._dirty.set()
                threading.Thread(target=_refresh_nodes, daemon=True, name="NodeRefresh").start()

            elif kind == "disconnected":
                state.connected = False
                state.error("✗ Connection to device lost")

            elif kind == "error":
                state.error(f"BLE error: {a}")

    except queue.Empty:
        pass


# ══════════════════════════════════════════════════════════════════════════════
# Textual — modal screens (nodes, device, tapback, traceroute, emoji)
# ══════════════════════════════════════════════════════════════════════════════

class NodesScreen(ModalScreen):
    """Ctrl+N — node list with actions on the highlighted row.
    Dismisses with ("dm", nid) / ("tr", nid); NODEINFO and map stay in place."""
    BINDINGS = [
        Binding("escape",   "back",       "Close"),
        Binding("q",        "dismiss",    "Close"),
        # priority: otherwise the "/" that opens the search is typed into it
        Binding("slash,ctrl+f", "search", "Search", priority=True),
        Binding("d",        "dm",         "DM"),
        Binding("t",        "traceroute", "Traceroute"),
        Binding("i",        "nodeinfo",   "NodeInfo"),
        Binding("m",        "map",        "Map"),
        Binding("s",        "stats",      "LocalStats"),
        Binding("b",        "metrics",    "Device metrics"),
    ]

    DEFAULT_CSS = """
    NodesScreen {
        align: center middle;
        background: $background 60%;
    }
    #nodes-container {
        width: 90%;
        height: 80%;
        border: round $accent;
        background: $surface;
        padding: 0 1;
    }
    #nodes-title {
        text-align: center;
        background: $accent;
        color: $text;
        width: 100%;
    }
    #nodes-hint {
        text-align: center;
        color: $text-muted;
        width: 100%;
        height: 1;
    }
    #nodes-filter {
        display: none;
        width: 100%;
    }
    #nodes-filter.shown {
        display: block;
    }
    DataTable {
        height: 1fr;
    }
    """

    def __init__(self, state: MeshState):
        super().__init__()
        self._state  = state
        self._filter = ""          # folded search text; "" = show all nodes

    def compose(self) -> ComposeResult:
        with Vertical(id="nodes-container"):
            yield Label("  Nodes in network  ", id="nodes-title")
            yield Input(placeholder="Search: name, short name, !id or hardware   "
                                    "(Enter/↓ = list, Esc = clear)", id="nodes-filter")
            yield DataTable(id="nodes-table", cursor_type="row")
            yield Label("Enter/d DM  t Trace  i NodeInfo  m Map  s Stats  b Battery  / Search  Esc close"
                        "   │   Hops 0 ● = heard directly", 
                        id="nodes-hint")

    def on_mount(self):
        table = self.query_one(DataTable)
        table.add_columns("ID", "Name", "Hops", "Dist", "SNR", "Battery", "Last seen")
        # the hidden search Input comes first and would otherwise take focus,
        # swallowing the d/t/i/m/s/b/q keys as search text
        table.focus()
        # refresh after first render — DataTable must be fully mounted
        self.call_after_refresh(self._refresh_table)

    def _node_position(self, nid: str) -> dict | None:
        """Live position from the device, else the one saved in node_db."""
        with self._state.lock:
            live = self._state.nodes.get(nid, {}).get("position")
        if extract_latlon(live):
            return live
        saved = self._state.node_db.get_entry(nid).get("position")
        return saved if extract_latlon(saved) else None

    def _hops_cell(self, nid: str, info: dict) -> Text:
        """Hop distance; '0 ●' = we heard the node's own packets directly in
        this session — a confirmed neighbour."""
        if nid == self._state.my_id:
            return Text("me")
        if heard_directly(self._state, nid):
            return Text("0 ●", style="bold green")
        hops = info.get("hopsAway")
        if not isinstance(hops, int):
            with self._state.lock:
                seen = self._state.hops_seen.get(nid)
            hops = seen[1] if seen else None
        return Text("—" if hops is None else str(hops))

    def _dist_cell(self, nid: str) -> str:
        if nid == self._state.my_id:
            return "me"
        pos = self._node_position(nid)
        if not pos:
            return "—"
        lat, lon = extract_latlon(pos)
        return relative_position(self._state, lat, lon,
                                 pos.get("precisionBits"), compact=True) or "?"

    # ── search ───────────────────────────────────────────────────────────────

    def _matches(self, nid: str, *names: str) -> bool:
        """Every word of the search text must appear in the node's ID, names
        (live, saved and earlier ones) or hardware model."""
        if not self._filter:
            return True
        entry = self._state.node_db.get_entry(nid)
        with self._state.lock:
            user = dict(self._state.nodes.get(nid, {}).get("user", {}))
        hay = " ".join([nid, *names,
                        user.get("longName") or "", user.get("shortName") or "",
                        str(user.get("hwModel") or ""),
                        entry.get("current_name") or "", str(entry.get("hw") or ""),
                        *[str(n) for n in entry.get("name_history") or []]])
        hay = _fold(hay)
        return all(w in hay for w in self._filter.split())

    def _update_title(self, shown: int, total: int) -> None:
        title = "  Nodes in network  "
        if self._filter:
            title = f"  Nodes in network — {shown} of {total} match “{self._filter}”  "
        try:
            self.query_one("#nodes-title", Label).update(title)
        except NoMatches:
            pass

    def action_search(self) -> None:
        inp = self.query_one("#nodes-filter", Input)
        inp.add_class("shown")
        inp.focus()

    @on(Input.Changed, "#nodes-filter")
    def _on_filter_changed(self, event: Input.Changed) -> None:
        event.stop()
        self._filter = _fold(event.value.strip())
        self._refresh_table()

    @on(Input.Submitted, "#nodes-filter")
    def _on_filter_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        self.query_one(DataTable).focus()

    def on_key(self, event) -> None:
        if event.key == "down" and isinstance(self.focused, Input):
            self.query_one(DataTable).focus()
            event.stop()

    def action_back(self) -> None:
        """Esc: clear the search first (or leave the search field), close only
        when there is nothing to clear."""
        inp = self.query_one("#nodes-filter", Input)
        if inp.value:
            inp.value = ""                  # Input.Changed refreshes the table
            inp.focus()
        elif "shown" in inp.classes:
            inp.remove_class("shown")
            self.query_one(DataTable).focus()
        else:
            self.dismiss()

    def _refresh_table(self):
        table = self.query_one(DataTable)
        table.clear()
        total = shown = 0

        # ── 1. nodes active in the current session ──────────────────────────
        with self._state.lock:
            nodes = list(self._state.nodes.items())
        nodes.sort(key=lambda x: x[1].get("lastHeard") or 0, reverse=True)
        session_ids = set()
        for nid, info in nodes:
            session_ids.add(nid)
            user    = info.get("user", {})
            name    = (user.get("longName") or "").strip() or self._state.node_db.resolve(nid)
            metrics = info.get("deviceMetrics", {})
            snr     = str(info.get("snr", ""))
            bat     = metrics.get("batteryLevel")
            battery = f"{bat}%" if bat is not None else "—"
            last_t  = info.get("lastHeard", 0)
            if last_t:
                delta  = int(time.time()) - last_t
                last_s = (f"{delta}s" if delta < 60 else
                          f"{delta//60}min" if delta < 3600 else f"{delta//3600}h")
                last_s += " ago"
            else:
                last_s = "???"
            total += 1
            if not self._matches(nid, name):
                continue
            shown += 1
            table.add_row(nid, Text(_safe_col(name, 28)), self._hops_cell(nid, info),
                          self._dist_cell(nid), snr, battery, last_s, key=nid)

        # ── 2. nodes known only from the database (not seen this session) ──
        db_candidates = []
        for nid in self._state.node_db.all_ids():
            if nid in session_ids or nid == self._state.my_id:
                continue
            entry     = self._state.node_db.get_entry(nid)
            name      = (entry.get("current_name") or "").strip() or nid
            try:
                dt = datetime.fromisoformat(entry.get("last_seen", ""))
            except Exception:
                dt = None
            if dt is not None:
                delta = datetime.now() - dt
                if delta.days >= 1:
                    age = f"{delta.days}d ago"
                elif delta.seconds >= 3600:
                    age = f"{delta.seconds // 3600}h ago"
                else:
                    age = f"{delta.seconds // 60}min ago"
            else:
                age = "?"
            db_candidates.append((nid, name, age, dt or datetime.min))
        # newest first (sorting by the age string put "10d" before "2d")
        db_candidates.sort(key=lambda x: x[3], reverse=True)
        for nid, name, age, _dt in db_candidates:
            total += 1
            if not self._matches(nid, name):
                continue
            shown += 1
            table.add_row(nid, Text(_safe_col(name, 28), style="dim"),
                          Text(self._hops_cell(nid, {}).plain, style="dim"),
                          Text(self._dist_cell(nid), style="dim"), "—", "—",
                          f"db·{age}", key=nid)
        self._update_title(shown, total)

    # ── actions on the highlighted row ───────────────────────────────────────

    def _current_nid(self) -> str | None:
        table = self.query_one(DataTable)
        if table.row_count == 0:
            return None
        try:
            return table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value
        except Exception:
            return None

    def _other_node(self) -> str | None:
        """Highlighted node, refusing our own (DM/traceroute/NODEINFO to self)."""
        nid = self._current_nid()
        if nid and nid == self._state.my_id:
            self.app.notify("That's your own node.", severity="warning", timeout=2)
            return None
        return nid

    @on(DataTable.RowSelected)
    def _on_row_selected(self, event: DataTable.RowSelected) -> None:
        event.stop()
        self.action_dm()

    def action_dm(self) -> None:
        nid = self._other_node()
        if nid:
            self.dismiss(("dm", nid))

    def action_traceroute(self) -> None:
        nid = self._other_node()
        if nid:
            self.dismiss(("tr", nid))

    def action_nodeinfo(self) -> None:
        nid = self._other_node()
        if not nid:
            return
        name = self._state.node_db.resolve(nid)
        if request_nodeinfo(self._state, nid):
            self._state.system(f"🔍 NODEINFO request → {name} ({nid})")
            self.app.notify(f"NODEINFO request sent → {name}", timeout=2)
        else:
            self.app.notify("NODEINFO request not sent — see log", severity="error",
                            timeout=3)

    def _request_telem(self, ttype: str, label: str) -> None:
        nid = self._current_nid()   # own node allowed — local, no airtime
        if not nid:
            return
        name = _resolve_name(self._state, self._state.interface, nid)
        if request_telemetry(self._state, nid, ttype):
            self._state.system(f"📊 {label} request → {name} ({nid})")
            self.app.notify(f"{label} request sent → {name}", timeout=2)
        else:
            self.app.notify(f"{label} request not sent — see log", severity="error",
                            timeout=3)

    def action_stats(self) -> None:
        self._request_telem("local_stats", "LocalStats")

    def action_metrics(self) -> None:
        self._request_telem("device_metrics", "Device metrics")

    def action_map(self) -> None:
        nid = self._current_nid()
        if not nid:
            return
        name = self._state.node_db.resolve(nid)
        pos  = self._node_position(nid)
        if not pos:
            self.app.notify(f"No position known for {name}", severity="warning", timeout=2)
            return
        lat, lon = extract_latlon(pos)
        url = (f"https://www.openstreetmap.org/?mlat={lat:.5f}&mlon={lon:.5f}"
               f"#map=14/{lat:.5f}/{lon:.5f}")
        # link also goes to the log — useful over SSH where no browser opens
        self._state.system(f"🗺 {name}: {url}")
        threading.Thread(target=webbrowser.open, args=(url,),
                         daemon=True, name="OpenMap").start()
        self.app.notify(f"Opening map: {name}", timeout=2)


class DeviceInfoScreen(ModalScreen):
    """Ctrl+O — device information."""
    BINDINGS = [Binding("escape", "dismiss", "Close")]

    DEFAULT_CSS = """
    DeviceInfoScreen {
        align: center middle;
        background: $background 60%;
    }
    #info-box {
        width: 80;
        height: auto;
        max-height: 95%;
        border: round $accent;
        background: $surface;
        padding: 1 2;
        overflow-y: auto;
    }
    #health-title {
        color: $accent;
        text-style: bold;
        margin-top: 1;
    }
    #health { height: auto; }
    #info-title {
        text-align: center;
        background: $accent;
        color: $text;
        width: 100%;
        margin-bottom: 1;
    }
    .info-row { height: 1; }
    .info-label { color: $accent; width: 16; }
    .info-value { color: $text; }
    #info-hint { text-align: center; color: $text-muted; margin-top: 1; }
    """

    def __init__(self, state: MeshState):
        super().__init__()
        self._state = state

    def compose(self) -> ComposeResult:
        s = self._state
        with s.lock:
            my_node_info = s.nodes.get(s.my_id, {})
        metrics = my_node_info.get("deviceMetrics", {})
        bat     = metrics.get("batteryLevel")
        voltage = metrics.get("voltage")
        if bat is not None and bat > 100:
            # firmware reports 101 when running from USB / external power
            battery_str = "⚡ external power"
            if voltage:
                battery_str += f"  {voltage:.2f}V"
        elif bat is not None:
            filled  = round(bat / 100 * 10)
            bar     = "█" * filled + "░" * (10 - filled)
            bat_icon = "🔋" if bat > 20 else "🪫"
            battery_str = f"{bat_icon} {bat}%  [{bar}]"
            if voltage:
                battery_str += f"  {voltage:.2f}V"
        else:
            battery_str = "(no data)"

        rows = [
            ("Name:",     s.my_name),
            ("ID:",       s.my_id),
            ("Hardware:",  s.device_hw or "(unknown)"),
            ("Firmware:",  s.device_fw or "(unknown)"),
            ("Hop limit:", f"{device_hop_limit(s)}  (DMs to farther nodes use more)"),
            ("Battery:",  battery_str),
            ("Status:",    "Connected ●" if s.connected else "Disconnected ○"),
            ("Nodes (session):", str(len(s.nodes))),
            ("Node database:", str(s.node_db.size())),
            ("Messages:",      str(len(s.messages))),
        ]
        with Vertical(id="info-box"):
            yield Label("  Device information  ", id="info-title")
            for label, value in rows:
                with Horizontal(classes="info-row"):
                    yield Label(label, classes="info-label")
                    yield Label(Text(value), classes="info-value")
            yield Label("── Radio health ──", id="health-title")
            yield Static(id="health")
            yield Label("Press any key…", id="info-hint")

    def on_mount(self):
        s = self._state
        if s.connected and s.my_id.startswith("!"):
            with s.lock:
                own = s.local_stats.get(s.my_id)
                pending = s.my_id in s.telem_pending
            if not pending and (own is None or time.time() - own[1] > 60):
                request_telemetry(s, s.my_id, "local_stats", quiet=True)
        self._update_health()
        self.set_interval(1.0, self._update_health)

    def _update_health(self) -> None:
        try:
            self.query_one("#health", Static).update(self._health_text())
        except NoMatches:
            pass

    def _health_text(self) -> Text:
        s   = self._state
        now = time.time()
        L   = 16    # label column width
        with s.lock:
            own  = s.local_stats.get(s.my_id)
            hist = list(s.health_hist)[-40:]
        ls, age = (own[0], now - own[1]) if own else ({}, None)
        cu, air = own_air(s)
        t = Text()

        def row(label: str, *parts):
            t.append(label.ljust(L), style="bold")
            for p in parts:
                t.append(*p) if isinstance(p, tuple) else t.append(p)
            t.append("\n")

        cu_hist  = [h[1] for h in hist]
        air_hist = [h[2] for h in hist]
        mins = len(hist) * 30 // 60

        def trend(values: list, min_span: float, fmt: str) -> tuple[str, str]:
            if len(values) < 2:
                return "", "  (trend after ~1 min)"
            bars, lo, hi = sparkline_auto(values, min_span)
            return bars, f"  {fmt.format(lo)}–{fmt.format(hi)}%  ({mins} min)"

        cu_bars, cu_rng   = trend(cu_hist, 2.0, "{:.1f}")
        air_bars, air_rng = trend(air_hist, 0.5, "{:.2f}")
        row("Channel util:",
            (f"{cu:5.1f}%" if cu is not None else "    —", cu_style(cu)),
            "  ", (cu_bars, cu_style(cu)), (cu_rng, "dim"))
        row("TX airtime 1h:",
            (f"{air:5.2f}%" if air is not None else "    —", air_style(air)),
            "  ", (air_bars, air_style(air)), (air_rng + "   limit EU868: 10%", "dim"))

        if not ls:
            row("LocalStats:", ("waiting for device…" if s.connected else "not connected", "dim"))
        else:
            rx, bad, dupe = (ls.get("numPacketsRx", 0), ls.get("numPacketsRxBad", 0),
                             ls.get("numRxDupe", 0))
            p_bad, p_dupe = _pct(bad, rx), _pct(dupe, rx)
            bad_style  = "dim" if p_bad is None else (
                "bold red" if p_bad >= 20 else "yellow" if p_bad >= 10 else "green")
            dupe_style = "dim" if p_dupe is None else (
                "yellow" if p_dupe >= 50 else "green")
            row("Packets RX:", f"{rx:<7}",
                "bad ", (f"{p_bad:.1f}%" if p_bad is not None else "—", bad_style),
                f" ({bad})   dupe ", (f"{p_dupe:.0f}%" if p_dupe is not None else "—", dupe_style),
                f" ({dupe})")
            relay, cancel = ls.get("numTxRelay", 0), ls.get("numTxRelayCanceled", 0)
            p_cancel = _pct(cancel, relay + cancel)
            tx_line = [f"{ls.get('numPacketsTx', 0):<7}", f"relayed {relay}",
                       f"  canceled {cancel}"]
            if p_cancel is not None:
                tx_line.append((f" ({p_cancel:.0f}%)", "dim"))
            if ls.get("numTxDropped"):
                tx_line.append((f"  dropped {ls['numTxDropped']}", "yellow"))
            row("Packets TX:", *tx_line)
            row("Nodes online:", f"{ls.get('numOnlineNodes', 0)} / {ls.get('numTotalNodes', 0)}",
                ("   (heard in last 2 h / NodeDB)", "dim"))
            ht, hf = ls.get("heapTotalBytes", 0), ls.get("heapFreeBytes", 0)
            if ht:
                pf = _pct(hf, ht) or 0
                row("Heap free:", (f"{hf/1024:.1f} / {ht/1024:.1f} kB ({pf:.0f}%)",
                                   "yellow" if pf < 15 else ""))
            nf = ls.get("noiseFloor")
            if nf:
                nf_style = "yellow" if nf > -100 else ""   # high floor = local interference
                row("Noise floor:", (f"{nf} dBm", nf_style))
            else:
                row("Noise floor:", ("—", "dim"), (f"   ({noise_floor_hint(s)})", "dim"))
            if ls.get("uptimeSeconds"):
                row("Uptime:", _fmt_uptime(ls["uptimeSeconds"]))
            row("Updated:", (f"{age:.0f} s ago" if age is not None else "—", "dim"))

        relays = top_relays(s, 5)
        t.append("Last-hop neighbours (who delivers to me)", style="bold")
        with s.lock:
            total = s.relay_total
        t.append(f"  ({total} packets this session)\n", style="dim")
        if not relays:
            t.append("  (no relayed packets yet)\n", style="dim")
        for label, cnt, share, avg in relays:
            t.append(f"  {_safe_col(label, 24):<24} ")
            t.append(f"{share:4.0f}%", style="bold")
            t.append(f"  {cnt:>5} pkt")
            t.append(f"   SNR {avg:5.1f} dB" if avg is not None else "   SNR    —",
                     style="dim")
            t.append("\n")
        if any("?" in r[0] for r in relays):
            t.append("  ? = guessed from the last ID byte — this node has not yet been "
                     "heard directly\n", style="dim")
        t.rstrip()
        return t

    def on_key(self, event):
        if event.key == "escape":
            return  # handled by BINDINGS → dismiss
        event.stop()
        self.dismiss()


class SelectMessageScreen(ModalScreen):
    """Base message selection modal (for Reply and Tapback)."""
    BINDINGS = [Binding("escape", "dismiss", "Cancel")]

    DEFAULT_CSS = """
    SelectMessageScreen {
        align: center middle;
        background: $background 60%;
    }
    #msg-box {
        width: 80%;
        height: 70%;
        border: round $accent;
        background: $surface;
        padding: 0 1;
    }
    #msg-title {
        text-align: center;
        background: $accent;
        color: $text;
        width: 100%;
    }
    ListView { height: 1fr; }
    """

    def __init__(self, state: MeshState, title: str):
        super().__init__()
        self._state = state
        self._title = title
        self._msg_ids: list[int] = []

    def _get_items(self) -> list[Message]:
        with self._state.lock:
            msgs = [m for m in self._state.messages
                    if m.kind in ("theirs", "mine", "private", "dm") and m.text and m.msg_id]
        return list(reversed(msgs[-20:]))

    def compose(self) -> ComposeResult:
        items = self._get_items()
        with Vertical(id="msg-box"):
            yield Label(self._title, id="msg-title")
            if not items:
                yield Label("  ↩ No messages to select  ", id="msg-empty")
            else:
                self._msg_ids = [m.msg_id for m in items]
                list_items = []
                for msg in items:
                    icon = "📩 " if msg.kind == "dm" else ("🔒 " if msg.kind == "private" else "")
                    name = _safe_col(msg.sender if msg.sender else "Me", 20)
                    text = _safe_col(msg.text, 46)
                    list_items.append(ListItem(Label(Text.assemble(
                        f"{msg.ts}  {icon}{name}: {text}  ",
                        status_text(msg.status, short=True)))))
                yield ListView(*list_items, id="msg-list")

    def on_mount(self):
        # no messages — dismiss immediately
        if not self._get_items():
            self.dismiss(None)

    @on(ListView.Selected)
    def on_selected(self, event: ListView.Selected):
        try:
            idx = list(event.item.parent.children).index(event.item)
            msg_id = self._msg_ids[idx]
        except (ValueError, IndexError):
            self.dismiss(None)
            return
        with self._state.lock:
            msg = self._state.msg_by_id.get(msg_id)
        result = msg if msg else None
        self.dismiss(result)


class TapbackEmojiScreen(ModalScreen):
    """Tapback step 2 — emoji selection."""
    BINDINGS = [Binding("escape", "dismiss", "Cancel")]

    DEFAULT_CSS = """
    TapbackEmojiScreen {
        align: center middle;
        background: $background 60%;
    }
    #emoji-box {
        width: 56;
        height: auto;
        border: round $accent;
        background: $surface;
        padding: 1 2;
    }
    #emoji-title {
        text-align: center;
        background: $accent;
        color: $text;
        width: 100%;
        margin-bottom: 1;
    }
    #emoji-grid { height: auto; }
    """

    def __init__(self, msg: Message):
        super().__init__()
        self._msg = msg

    def compose(self) -> ComposeResult:
        with Vertical(id="emoji-box"):
            yield Label("  Choose reaction  ", id="emoji-title")
            yield ListView(
                *[ListItem(Label(f"  {key}  {_pad_emoji(emoji)}"), id=f"tap-{key}") for key, emoji in TAPBACK_EMOJI],
                id="emoji-grid"
            )

    @on(ListView.Selected)
    def on_selected(self, event: ListView.Selected):
        key = event.item.id.split("-", 1)[1]
        emoji = dict(TAPBACK_EMOJI).get(key, "")
        if emoji:
            result = (emoji, self._msg)
            self.dismiss(result)

    def on_key(self, event):
        key = event.character or ""
        emoji = dict(TAPBACK_EMOJI).get(key, "")
        if emoji:
            result = (emoji, self._msg)
            self.dismiss(result)
            event.stop()


class TracerouteScreen(ModalScreen):
    """Ctrl+T — node selection for traceroute with filtering."""
    BINDINGS = [Binding("escape", "dismiss", "Cancel")]

    DEFAULT_CSS = """
    TracerouteScreen {
        align: center middle;
        background: $background 60%;
    }
    #tr-box {
        width: 60%;
        height: 70%;
        border: round $accent;
        background: $surface;
        padding: 0 1;
    }
    #tr-title {
        text-align: center;
        background: $accent;
        color: $text;
        width: 100%;
    }
    #tr-filter { margin: 0 0 1 0; }
    ListView { height: 1fr; }
    """

    def __init__(self, state: MeshState, title: str = "  Traceroute — select node  ", show_pki: bool = False):
        super().__init__()
        self._state    = state
        self._title    = title
        self._show_pki = show_pki
        self._items: list[str] = []   # nids in display order

    def compose(self) -> ComposeResult:
        with Vertical(id="tr-box"):
            yield Label(self._title, id="tr-title")
            yield Input(placeholder="Filter...", id="tr-filter")
            yield ListView(id="tr-list")

    def on_mount(self):
        self._rebuild("")
        self.query_one("#tr-filter", Input).focus()

    def _build_items(self, filt: str) -> list[tuple[str, str, bool]]:
        """Return list of (nid, label, is_active) matching the filter.
        Active nodes (seen this session) come first, sorted by lastHeard.
        DB-only nodes follow, dimmed with last-seen date."""
        with self._state.lock:
            session_nodes = list(self._state.nodes.items())
        session_nodes.sort(key=lambda x: x[1].get("lastHeard") or 0, reverse=True)
        session_nodes_dict = dict(session_nodes)
        session_ids = set()
        result = []

        # 1. Active nodes from current session
        for nid, info in session_nodes:
            if nid == self._state.my_id:
                continue
            name = (info.get("user", {}).get("longName") or "").strip() or self._state.node_db.resolve(nid)
            if filt and filt.lower() not in name.lower() and filt.lower() not in nid.lower():
                continue
            session_ids.add(nid)
            if self._show_pki:
                pki  = PKI_ICON[pki_state(self._state, nid, session_nodes_dict)]
                name = f"{pki} {name}"
            result.append((nid, name, True, ""))

        # 2. DB-only nodes not seen this session — sorted by last_seen descending
        db_candidates = []
        for nid in self._state.node_db.all_ids():
            if nid in session_ids or nid == self._state.my_id:
                continue
            entry = self._state.node_db.get_entry(nid)
            name  = (entry.get("current_name") or "").strip() or nid
            if filt and filt.lower() not in name.lower() and filt.lower() not in nid.lower():
                continue
            last_seen = entry.get("last_seen", "")
            try:
                dt = datetime.fromisoformat(last_seen)
            except Exception:
                dt = datetime.min
            db_candidates.append((nid, name, last_seen, dt))

        db_candidates.sort(key=lambda x: x[3], reverse=True)

        for nid, name, last_seen, dt in db_candidates:
            try:
                delta = datetime.now() - dt
                if delta.days >= 1:
                    age = f"{delta.days}d ago"
                elif delta.seconds >= 3600:
                    age = f"{delta.seconds // 3600}h ago"
                else:
                    age = f"{delta.seconds // 60}min ago"
                pki_prefix = (PKI_ICON[pki_state(self._state, nid, session_nodes_dict)] + " ") if self._show_pki else ""
                label = f"{pki_prefix}{name}"
                suffix = f"(db · {age})"
            except Exception:
                label  = name
                suffix = "(db)"
            result.append((nid, label, False, suffix))

        return result

    def _rebuild(self, filt: str) -> None:
        """Rebuild ListView from scratch — avoids async clear() + DuplicateIds issue."""
        pairs = self._build_items(filt)
        self._items = [nid for nid, _, _, _ in pairs]
        lv = self.query_one("#tr-list", ListView)
        # remove all children synchronously via _nodes directly
        lv._nodes._clear()
        # mount new items without ID — index = position in _items
        new_items = []
        for nid, label, is_active, suffix in pairs:
            # Rich Text instead of markup — node names may contain "[" / "]"
            # (e.g. "Stacja [SP9" truncated by _safe_col) which broke the parser
            if is_active:
                content = Text.assemble(f"{nid}  ", _safe_col(label, 28))
            else:
                content = Text.assemble((nid, "dim"), "  ", _safe_col(label, 28),
                                        (f"  {suffix}", "dim"))
            new_items.append(ListItem(Label(content)))
        if new_items:
            lv.mount(*new_items)
            # mount is async — set the highlight once the items are in place
            self.call_after_refresh(self._reset_index)

    def _reset_index(self) -> None:
        lv = self.query_one("#tr-list", ListView)
        if self._items:
            lv.index = None
            lv.index = 0

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "tr-filter":
            event.stop()
            self._rebuild(event.value)

    def on_key(self, event) -> None:
        lv = self.query_one("#tr-list", ListView)
        focused = self.focused
        in_filter = focused is not None and getattr(focused, "id", None) == "tr-filter"
        if event.key in ("down", "up") and in_filter:
            # move focus to the list, highlight stays on the first item;
            # stop the key so the ListView binding doesn't also move it down
            if self._items:
                lv.focus()
                if lv.index is None:
                    lv.index = 0
            event.stop()
            event.prevent_default()
        elif event.key == "enter":
            focused = self.focused
            if focused is not None and getattr(focused, "id", None) == "tr-filter":
                if self._items:
                    self.dismiss(self._items[0])
                    event.stop()

    @on(ListView.Selected)
    def on_selected(self, event: ListView.Selected):
        try:
            idx = list(event.item.parent.children).index(event.item)
            nid = self._items[idx] if idx < len(self._items) else ""
        except (ValueError, IndexError):
            nid = ""
        if nid:
            self.dismiss(nid)


class EmojiInsertScreen(ModalScreen):
    """Ctrl+P — insert emoji into typed text."""
    BINDINGS = [Binding("escape", "dismiss", "Cancel")]

    DEFAULT_CSS = """
    EmojiInsertScreen {
        align: center middle;
        background: $background 60%;
    }
    #emj-box {
        width: 60;
        height: auto;
        border: round $accent;
        background: $surface;
        padding: 1 2;
    }
    #emj-title {
        text-align: center;
        background: $accent;
        color: $text;
        width: 100%;
        margin-bottom: 1;
    }
    """

    def compose(self) -> ComposeResult:
        with Vertical(id="emj-box"):
            yield Label("  Emoji — select or press key  ", id="emj-title")
            yield ListView(
                *[ListItem(Label(f"  {key}  {_pad_emoji(emoji)}"), id=f"emj-{key}") for key, emoji in EMOJI_LIST],
                id="emj-list"
            )

    @on(ListView.Selected)
    def on_selected(self, event: ListView.Selected):
        key   = event.item.id[4:]
        emoji = dict(EMOJI_LIST).get(key, "")
        if emoji:
            self.dismiss(emoji)

    def on_key(self, event):
        key   = event.character or ""
        emoji = dict(EMOJI_LIST).get(key, "")
        if emoji:
            self.dismiss(emoji)
            event.stop()


# ══════════════════════════════════════════════════════════════════════════════
# Emoji picker — grid with categories, recent, search (v1.4.3)
# ══════════════════════════════════════════════════════════════════════════════
# Shared by Ctrl+P (insert into text) and Ctrl+G (tapback). Single-codepoint
# emoji only (plus a few with VS16): no ZWJ sequences, skin tones or flags —
# those render inconsistently in terminals and on device screens.

EMOJI_CATEGORIES: list[tuple[str, str]] = [
    ("Smileys",
     "😀😃😄😁😆😅🤣😂🙂🙃😉😊😇🥰😍🤩😘😋😛😜🤪😝🤑🤗🤭🤫🤔🤐🤨😐😑😶😏😒🙄😬😌😔😪🤤"
     "😴😷🤒🤕🤢🤮🥵🥶🥴😵🤯🤠🥳😎🤓🧐😕😟🙁😮😯😲😳🥺😦😧😨😰😥😢😭😱😖😣😞😓😩😫🥱"
     "😤😡😠🤬😈👿💀💩🤡👻👽🤖😺😸😹😻😼😽🙀😿😾"),
    ("Hands",
     "👍👎👌🤌🤏✌🤞🤟🤘🤙👈👉👆👇☝✋🤚🖐🖖👋👏🙌👐🤲🤝🙏✍💪🦾🦵🦶👂👃🧠👀👁👅👄💋"),
    ("Hearts",
     "❤🧡💛💚💙💜🖤🤍🤎💔❣💕💞💓💗💖💘💝💟💌💯💢💥💫💦💨💬💭💤🔥✨⭐🌟💡"),
    ("Nature",
     "🐶🐱🐭🐹🐰🦊🐻🐼🐨🐯🦁🐮🐷🐸🐵🐔🐧🐦🐤🦆🦅🦉🐺🐗🐴🦄🐝🐛🦋🐌🐞🐜🐢🐍🦎🐙🦀🐟🐬🐳"
     "🌲🌳🌴🌵🌿🍀🍁🍂🍃🌷🌹🌻🌼🌸💐🍄🌾"),
    ("Weather",
     "☀🌞🌤⛅🌥☁🌦🌧⛈🌩🌨❄☃⛄🌬💨🌪🌫🌈☔💧🌊🌡🌙🌛🌜🌚🌝🌕🌑⭐🌠☄🌍🌎🌏🪐"),
    ("Food",
     "🍏🍎🍐🍊🍋🍌🍉🍇🍓🫐🍒🍑🥭🍍🥥🥝🍅🥑🥦🥕🌽🌶🥔🍞🥐🥨🧀🥚🍳🥓🥩🍗🍖🌭🍔🍟🍕🥪🌮🍝"
     "🍜🍲🍣🍰🎂🧁🍩🍪🍫🍬🍭🍯🥛☕🍵🍺🍻🍷🥂🍸🍹🧉"),
    ("Places",
     "🚗🚕🚙🚌🚎🏎🚓🚑🚒🚐🛻🚚🚜🏍🛵🚲🛴🚂🚆🚇🚊✈🚁🛰🚀⛵🚤🛳⚓⛽🚦🗺🧭🏔⛰🌋🏕🏖🏜🏝"
     "🏞🏟🏛🏗🏘🏚🏠🏡🏢🏥🏦🏨🏪🏫🏰⛪🕌🗼🗽⛲⛺🌁🌃🌄🌅🌆🌇🌉🎡🎢"),
    ("Radio",
     "📡📻📱📲☎📞📟📠🔋🪫🔌💻🖥⌨🖱💾💿📀🎙🎚🎛📺📷📹🔦🕯💡🔍🔎🔑🗝🔒🔓🔨🪛🔧⚙🧲⚡🔊"
     "🔇📢📣🔔🔕⏰⏱⏲⌛⏳📍📌🗓📅📆🧾📝✏📎📦🎁🎈🎉🎊🏆🥇🎯🎮🎲🎸🎺🎷🥁🎵🎶"),
    ("Symbols",
     "✅☑✔❌❎➕➖✖➗❓❔❕❗‼⁉⚠🚫⛔🆘🆗🆕🆒🆓🆙🔴🟠🟡🟢🔵🟣⚫⚪🟥🟩🟦⬛⬜🔺🔻🔸🔹"
     "▶⏸⏹⏺⏭⏮⏩⏪🔁🔄↩↪⬆⬇⬅➡↗↘↙↖♻⚛☮☯✝☢☣🔞📵🚭"),
]

# Polish (and a few English) search words → emoji. unicodedata names are
# searched too, so English words work for every emoji.
EMOJI_ALIASES: dict[str, str] = {
    "kciuk": "👍👎", "ok": "👌🆗✅", "tak": "✅👍", "nie": "❌👎🚫",
    "serce": "❤🧡💛💚💙💜🖤🤍💔💕", "milosc": "❤🥰😍😘💕",
    "smiech": "😂🤣😆😄😁😹", "usmiech": "🙂😊😀😃😄",
    "placz": "😭😢😿", "smutek": "😢😞😔🙁", "zlosc": "😡😠🤬😤",
    "szok": "😱😲😮🤯", "wow": "😮😲🤩", "mysl": "🤔💭", "spanie": "😴💤🥱",
    "chory": "🤒🤕🤢😷", "zimno": "🥶❄☃", "goraco": "🥵🔥🌡",
    "fajnie": "😎🤩👍", "brawo": "👏🙌🎉", "dzieki": "🙏💐",
    "czesc": "👋🙂", "pa": "👋", "piwo": "🍺🍻", "kawa": "☕", "herbata": "🍵",
    "ogien": "🔥", "gwiazda": "⭐🌟✨🌠", "slonce": "☀🌞🌤",
    "deszcz": "🌧🌦☔💧", "burza": "⛈🌩⚡", "snieg": "❄🌨☃⛄",
    "chmura": "☁⛅🌥", "wiatr": "🌬💨🌪", "mgla": "🌫", "tecza": "🌈",
    "noc": "🌙🌃🌛", "ksiezyc": "🌙🌛🌜🌕", "morze": "🌊⛵🐳",
    "gory": "🏔⛰🌋", "las": "🌲🌳🍀", "drzewo": "🌲🌳🌴", "kwiat": "🌷🌹🌻🌸💐",
    "pies": "🐶", "kot": "🐱😺", "auto": "🚗🚙", "samochod": "🚗🚙🚕",
    "rower": "🚲", "pociag": "🚂🚆", "samolot": "✈", "dom": "🏠🏡",
    "praca": "🏢💻", "szpital": "🏥🚑", "policja": "🚓", "straz": "🚒",
    "radio": "📡📻🎙", "antena": "📡", "telefon": "📱☎📞",
    "bateria": "🔋🪫", "prad": "⚡🔌", "komputer": "💻🖥⌨",
    "mapa": "🗺📍", "pozycja": "📍📌", "gps": "📍🛰🧭", "satelita": "🛰📡",
    "kompas": "🧭", "klucz": "🔑🗝", "zamek": "🔒🔓", "narzedzia": "🔨🔧🪛⚙",
    "czas": "⏰⏱⌛⏳", "zegar": "⏰⏱", "kalendarz": "📅📆🗓",
    "muzyka": "🎵🎶🎸🎺", "trabka": "🎺", "impreza": "🎉🎊🥳🎈",
    "urodziny": "🎂🎁🎉", "prezent": "🎁", "puchar": "🏆🥇",
    "pomoc": "🆘⚠", "uwaga": "⚠❗", "stop": "⛔🚫🛑", "pytanie": "❓❔🤔",
    "test": "✅🧪", "jedzenie": "🍕🍔🍝🍲", "pizza": "🍕",
}

RECENT_EMOJI_FILE = Path(__file__).parent / "mesh_emoji_recent.json"
RECENT_EMOJI_MAX  = 30


def _fold(s: str) -> str:
    """Lowercase without Polish diacritics (ą→a, ł→l, …) for search."""
    s = s.lower().replace("ł", "l")
    return "".join(c for c in unicodedata.normalize("NFD", s)
                   if unicodedata.category(c) != "Mn")


def _split_emoji(s: str) -> list[str]:
    """Split a run of emoji into single emoji, keeping a trailing VS16."""
    out: list[str] = []
    for c in s:
        if c in ("️", "︎") and out:
            out[-1] += c
        elif not c.isspace():
            out.append(c)
    seen, uniq = set(), []
    for e in out:
        if e not in seen:
            seen.add(e)
            uniq.append(e)
    return uniq


_EMOJI_CATS: list[tuple[str, list[str]]] = [(n, _split_emoji(s)) for n, s in EMOJI_CATEGORIES]
_ALL_EMOJI: list[str] = list(dict.fromkeys(e for _, lst in _EMOJI_CATS for e in lst))


def emoji_name(e: str) -> str:
    try:
        return unicodedata.name(e[0]).lower()
    except (ValueError, IndexError):
        return ""


def search_emoji(query: str) -> list[str]:
    q = _fold(query.strip())
    if not q:
        return []
    hits: list[str] = []
    for word, ems in EMOJI_ALIASES.items():          # Polish words first
        if word.startswith(q):
            hits.extend(_split_emoji(ems))
    for e in _ALL_EMOJI:                              # then English names
        if any(w.startswith(q) for w in emoji_name(e).split()):   # word starts
            hits.append(e)
    return list(dict.fromkeys(hits))


def load_recent_emoji() -> list[str]:
    try:
        data = json.loads(RECENT_EMOJI_FILE.read_text(encoding="utf-8"))
        return [e for e in data if isinstance(e, str)][:RECENT_EMOJI_MAX]
    except Exception:
        return []


def remember_emoji(e: str) -> None:
    rec = [x for x in load_recent_emoji() if x != e]
    rec.insert(0, e)
    try:
        RECENT_EMOJI_FILE.write_text(json.dumps(rec[:RECENT_EMOJI_MAX], ensure_ascii=False),
                                     encoding="utf-8")
    except Exception:
        pass


class EmojiPicker(Widget, can_focus=True):
    """Category bar + emoji grid + status line, drawn as one Text.
    Keys: arrows move, Tab/Shift+Tab (or PgDn/PgUp) change category, Enter picks,
    letters search (Backspace edits, Esc clears search). Mouse: click a
    category or an emoji."""

    COLS   = 10
    ROWS   = 7                      # visible grid rows (scrolls if more)
    CELL_W = 4                      # " 😀 "

    class Picked(Message_):
        def __init__(self, emoji: str) -> None:
            super().__init__()
            self.emoji = emoji

    def __init__(self) -> None:
        super().__init__()
        self._recent = load_recent_emoji()
        self._tabs = ([("Recent", self._recent)] if self._recent else []) + _EMOJI_CATS
        self._tab = 0
        self._cur = 0
        self._top = 0                   # first visible grid row
        self._query = ""
        self._tab_spans: list[tuple[int, int, int]] = []

    # ── data ────────────────────────────────────────────────────────────────
    def _items(self) -> list[str]:
        if self._query:
            return search_emoji(self._query)
        return self._tabs[self._tab][1]

    def _clamp(self) -> None:
        n = len(self._items())
        self._cur = max(0, min(self._cur, n - 1)) if n else 0
        row = self._cur // self.COLS
        if row < self._top:
            self._top = row
        elif row >= self._top + self.ROWS:
            self._top = row - self.ROWS + 1

    # ── drawing ─────────────────────────────────────────────────────────────
    def get_content_width(self, container, viewport) -> int:
        return self.COLS * self.CELL_W

    def _count_bar_lines(self) -> int:
        width, x, lines = self.COLS * self.CELL_W, 0, 1
        for name, _ in self._tabs:
            n = len(name) + 2
            if x + n > width and x:
                x, lines = 0, lines + 1
            x += n
        return lines

    def get_content_height(self, container, viewport, width) -> int:
        # category bar + blank + grid rows + blank + status + key hints
        return self._count_bar_lines() + self.ROWS + 4

    def render(self) -> Text:
        t = Text()
        # category bar (wraps onto a second line if needed)
        self._tab_spans = []
        x, line = 0, 0
        width = self.COLS * self.CELL_W
        for i, (name, _) in enumerate(self._tabs):
            label = f" {name} "
            if x + len(label) > width and x:
                t.append("\n")
                x, line = 0, line + 1
            active = (i == self._tab and not self._query)
            t.append(label, style="bold reverse" if active else "dim")
            self._tab_spans.append((line, x, x + len(label), i))
            x += len(label)
        self._bar_lines = line + 1
        t.append("\n")
        # grid
        items = self._items()
        for r in range(self._top, self._top + self.ROWS):
            t.append("\n")
            for c in range(self.COLS):
                k = r * self.COLS + c
                if k >= len(items):
                    break
                cell = f" {_pad_emoji(items[k])} "
                t.append(cell, style="reverse" if k == self._cur else "")
        # status
        t.append("\n\n")
        # status: one line only — a long emoji name would wrap and push the
        # key hints out of the box, so it is cut to fit with "…"
        total_rows = (len(items) + self.COLS - 1) // self.COLS
        scroll = ""
        if total_rows > self.ROWS:
            scroll = f"   ▲▼ {self._top + 1}-{min(total_rows, self._top + self.ROWS)}/{total_rows}"
        if self._query:
            found = f"   {len(items)} found" if items else "   nothing found"
            q = self._query
            room = width - 4 - len(found) - len(scroll)
            if len(q) > room:
                q = "…" + q[-max(1, room - 1):]
            t.append(" 🔍 ", style="bold")
            t.append(q, style="bold yellow")
            t.append(found, style="dim")
        elif items:
            name = emoji_name(items[self._cur])
            room = width - 1 - len(scroll)
            if len(name) > room:
                name = name[:max(1, room - 1)] + "…"
            t.append(f" {name}", style="dim")
        t.append(scroll, style="dim")
        t.append("\n")
        t.append(" Tab category · Enter pick · type=search", style="dim")
        return t

    # ── input ───────────────────────────────────────────────────────────────
    def _move(self, d: int) -> None:
        n = len(self._items())
        if n:
            self._cur = max(0, min(n - 1, self._cur + d))
            self._clamp()
            self.refresh()

    def _switch(self, d: int) -> None:
        self._query = ""
        self._tab = (self._tab + d) % len(self._tabs)
        self._cur = self._top = 0
        self.refresh()

    def _pick(self) -> None:
        items = self._items()
        if items:
            e = items[self._cur]
            remember_emoji(e)
            self.post_message(self.Picked(e))

    def on_key(self, event) -> None:
        k = event.key
        handled = True
        if k == "left":
            self._move(-1)
        elif k == "right":
            self._move(1)
        elif k == "up":
            self._move(-self.COLS)
        elif k == "down":
            self._move(self.COLS)
        elif k in ("tab", "pagedown"):
            self._switch(1)
        elif k in ("shift+tab", "pageup"):
            self._switch(-1)
        elif k == "home":
            self._cur = self._top = 0
            self.refresh()
        elif k == "enter":
            self._pick()
        elif k == "backspace":
            self._query = self._query[:-1]
            self._cur = self._top = 0
            self.refresh()
        elif k == "escape" and self._query:
            self._query = ""
            self._cur = self._top = 0
            self.refresh()
        elif event.character and event.character.isprintable() \
                and not event.character.isspace() and len(event.character) == 1:
            self._query += event.character
            self._cur = self._top = 0
            self.refresh()
        else:
            handled = False
        if handled:
            event.stop()
            event.prevent_default()

    def on_click(self, event) -> None:
        y, x = event.y, event.x
        if y < self._bar_lines:
            for line, a, b, i in self._tab_spans:
                if line == y and a <= x < b:
                    self._query = ""
                    self._tab, self._cur, self._top = i, 0, 0
                    self.refresh()
            return
        r = y - self._bar_lines - 1
        if 0 <= r < self.ROWS:
            k = (self._top + r) * self.COLS + x // self.CELL_W
            if k < len(self._items()) and x < self.COLS * self.CELL_W:
                self._cur = k
                self.refresh()
                self._pick()

    def on_mouse_scroll_down(self, event) -> None:
        self._move(self.COLS)
        event.stop()

    def on_mouse_scroll_up(self, event) -> None:
        self._move(-self.COLS)
        event.stop()


class EmojiPickerScreen(ModalScreen):
    """Ctrl+P (insert) and Ctrl+G step 2 (tapback). Dismisses with the emoji
    (or with (emoji, msg) when a message is given) — None on Esc."""
    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    DEFAULT_CSS = """
    EmojiPickerScreen {
        align: center middle;
        background: $background 60%;
    }
    #ep-box {
        width: auto;
        height: auto;
        border: round $accent;
        background: $surface;
        padding: 1 2;
    }
    #ep-title {
        text-align: center;
        background: $accent;
        color: $text;
        width: 100%;
        margin-bottom: 1;
    }
    EmojiPicker { width: auto; height: auto; }
    """

    def __init__(self, title: str, msg: "Message | None" = None):
        super().__init__()
        self._title = title
        self._msg = msg

    def compose(self) -> ComposeResult:
        with Vertical(id="ep-box"):
            yield Label(f"  {self._title}  ", id="ep-title")
            yield EmojiPicker()

    def on_mount(self) -> None:
        self.query_one(EmojiPicker).focus()

    def on_emoji_picker_picked(self, event) -> None:
        self.dismiss((event.emoji, self._msg) if self._msg is not None else event.emoji)

    def action_cancel(self) -> None:
        self.dismiss(None)


class LocationScreen(ModalScreen):
    """Ctrl+L — send location: type coordinates or pick one of 5 recent."""
    BINDINGS = [Binding("escape", "dismiss", "Cancel")]

    DEFAULT_CSS = """
    LocationScreen {
        align: center middle;
        background: $background 60%;
    }
    #loc-box {
        width: 70;
        height: auto;
        max-height: 90%;
        border: round $accent;
        background: $surface;
        padding: 0 1;
    }
    #loc-title {
        text-align: center;
        background: $accent;
        color: $text;
        width: 100%;
    }
    #loc-target { color: $text-muted; margin: 0 0 1 0; }
    #loc-error  { color: $error; height: auto; }
    #loc-recent { margin: 1 0 0 0; color: $text-muted; }
    #loc-list   { height: auto; max-height: 7; }
    .loc-hint   { color: $text-muted; }
    """

    def __init__(self, target_label: str):
        super().__init__()
        self._target  = target_label
        self._history = load_location_history()

    def compose(self) -> ComposeResult:
        with Vertical(id="loc-box"):
            yield Label("  📍 Send location  ", id="loc-title")
            yield Label(Text(f"→ {self._target}"), id="loc-target")
            yield Input(placeholder="lat, lon [, alt m]   e.g. 52.22970, 21.01220",
                        id="loc-coords")
            yield Input(placeholder="Name (optional), e.g. Home", id="loc-name",
                        max_length=40)
            yield Label("", id="loc-error")
            if self._history:
                yield Label("Recent — ↓ to pick, Enter or 1-5 to send:", id="loc-recent")
                items = []
                names = [_safe_col(e["name"], 20) if e.get("name") else "" for e in self._history]
                col   = max((_disp_width(n) for n in names), default=0)
                for i, (e, name) in enumerate(zip(self._history, names), 1):
                    pad = name + " " * (col - _disp_width(name)) + "  " if col else ""
                    items.append(ListItem(Label(Text.assemble(
                        (f" {i} ", "bold"), (pad, "bold"), _fmt_loc(e)))))
                yield ListView(*items, id="loc-list")
            else:
                yield Label("No recent locations yet.", classes="loc-hint")
            yield Label("Enter — send   ↑↓ — move   Esc — cancel", classes="loc-hint")

    def on_mount(self):
        self.query_one("#loc-coords", Input).focus()

    def _list(self) -> ListView | None:
        try:
            return self.query_one("#loc-list", ListView)
        except NoMatches:
            return None

    def _submit_typed(self) -> None:
        coords = self.query_one("#loc-coords", Input).value
        name   = self.query_one("#loc-name", Input).value.strip()
        try:
            lat, lon, alt = parse_coords(coords)
        except ValueError as e:
            self.query_one("#loc-error", Label).update(Text(f"✗ {e}"))
            self.query_one("#loc-coords", Input).focus()
            return
        self.dismiss({"lat": lat, "lon": lon, "alt": alt, "name": name})

    @on(Input.Submitted)
    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        coords = self.query_one("#loc-coords", Input).value.strip()
        if event.input.id == "loc-coords" and coords:
            # coordinates typed — send right away (name is optional)
            self._submit_typed()
        elif event.input.id == "loc-name" and coords:
            self._submit_typed()
        elif self._history:
            # empty coordinates + Enter = send the most recent location
            self.dismiss(dict(self._history[0]))
        else:
            self.query_one("#loc-error", Label).update(Text("✗ Enter coordinates"))

    def on_input_changed(self, event: Input.Changed) -> None:
        event.stop()
        self.query_one("#loc-error", Label).update("")

    @on(ListView.Selected)
    def on_selected(self, event: ListView.Selected) -> None:
        event.stop()
        try:
            idx = list(event.item.parent.children).index(event.item)
        except ValueError:
            return
        if 0 <= idx < len(self._history):
            self.dismiss(dict(self._history[idx]))

    def on_key(self, event) -> None:
        focused_id = getattr(self.focused, "id", None)
        lv = self._list()
        if event.key == "down" and focused_id == "loc-coords":
            self.query_one("#loc-name", Input).focus()
            event.stop()
        elif event.key == "down" and focused_id == "loc-name" and lv is not None:
            lv.index = 0
            lv.focus()
            event.stop()
        elif event.key == "up" and focused_id == "loc-name":
            self.query_one("#loc-coords", Input).focus()
            event.stop()
        elif event.key == "up" and focused_id == "loc-list" and lv is not None \
                and (lv.index or 0) == 0:
            self.query_one("#loc-name", Input).focus()
            event.stop()
        elif focused_id == "loc-list" and event.character in ("1", "2", "3", "4", "5"):
            idx = int(event.character) - 1
            if idx < len(self._history):
                self.dismiss(dict(self._history[idx]))
            event.stop()


# ══════════════════════════════════════════════════════════════════════════════
# Textual — main application
# ══════════════════════════════════════════════════════════════════════════════

# Colors by message kind (Rich markup)
MSG_COLORS = {
    "mine":    "bold yellow",
    "theirs":  "green",
    "private": "magenta",
    "dm":      "bold orange1",
    "system":      "cyan",
    "system_raw":  "cyan",
    "tapback":      "green",
    "tapback_mine": "bold orange1",
    "error":   "bold red",
}


class DmHistoryScreen(ModalScreen):
    """Ctrl+H — DM archive browser (reads mesh_dm_archive.jsonl)."""
    BINDINGS = [
        Binding("escape,q", "dismiss", "Close"),
        Binding("n",        "next_page",  "Next",  show=True),
        Binding("p",        "prev_page",  "Prev",  show=True),
    ]
    PAGE_SIZE = 10

    DEFAULT_CSS = """
    DmHistoryScreen {
        align: center middle;
        background: $background 60%;
    }
    #dmh-container {
        width: 88%;
        height: 85%;
        border: round $warning;
        background: $surface;
        padding: 0 1;
    }
    #dmh-title {
        text-align: center;
        background: $warning;
        color: $background;
        text-style: bold;
        width: 100%;
    }
    #dmh-list { height: 1fr; }
    #dmh-hint {
        text-align: center;
        color: $text-muted;
        height: 1;
    }
    """

    def __init__(self, state: MeshState):
        super().__init__()
        self._state   = state
        self._entries : list[dict] = []   # all loaded entries, newest first
        self._status  : dict       = {}   # uid -> last delivery status
        self._page    : int        = 0    # 0 = newest page

    def compose(self) -> ComposeResult:
        with Vertical(id="dmh-container"):
            yield Label("", id="dmh-title")
            yield ListView(id="dmh-list")
            yield Label("", id="dmh-hint")

    def on_mount(self):
        self._load_archive()
        self._render_page()

    def _load_archive(self):
        """Load all DM entries from JSONL, newest first."""
        entries = []
        if DM_ARCHIVE_FILE.exists():
            try:
                with open(DM_ARCHIVE_FILE, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            rec = json.loads(line)
                        except Exception:
                            continue
                        if rec.get("type") == "status":
                            # later records overwrite earlier ones → last status wins
                            self._status[rec.get("uid", "")] = rec.get("status", "")
                        else:
                            entries.append(rec)
            except Exception:
                pass
        self._entries = list(reversed(entries))  # newest first

    def _render_page(self):
        total  = len(self._entries)
        n_pages = max(1, (total + self.PAGE_SIZE - 1) // self.PAGE_SIZE)
        start  = self._page * self.PAGE_SIZE
        end    = min(start + self.PAGE_SIZE, total)
        page_entries = list(reversed(self._entries[start:end]))

        # update title
        if total == 0:
            title_text = "  📩 DM History — no messages  "
        else:
            showing_from = total - end + 1
            showing_to   = total - start
            title_text = f"  📩 DM History  [{showing_from}–{showing_to} of {total}]  "
        self.query_one("#dmh-title", Label).update(Text(title_text))

        # rebuild list
        lv = self.query_one("#dmh-list", ListView)
        lv._nodes._clear()

        if not page_entries:
            lv.mount(ListItem(Label("  (no DMs saved yet)")))
        else:
            items = []
            for e in page_entries:
                ts      = e.get("ts", "")[:16].replace("T", " ")
                kind    = e.get("kind", "")
                sender  = e.get("sender", "?")
                to_id   = e.get("to_id", "")
                text    = e.get("text", "")
                my_id   = e.get("my_id", "")

                # resolve display names using node_db
                if kind == "mine":
                    to_name = self._state.node_db.resolve(to_id) if to_id else to_id
                    arrow   = (f"You → {_safe_col(to_name, 22)}", "bold yellow")
                    st_code = self._status.get(e.get("uid", ""), "")
                    st_txt  = status_text(st_code)
                else:
                    arrow   = (f"📩 {_safe_col(sender, 22)}", "bold orange1")
                    st_txt  = ("", "")

                # full text, wrapped — no truncation
                flat_text    = text.replace("\n", " ")
                items.append(ListItem(Label(Text.assemble(
                    (ts, "dim"), "  ", arrow, "  ", st_txt, "\n    ", flat_text
                ))))
            lv.mount(*items)

        # hint bar
        parts = []
        if self._page < n_pages - 1:
            parts.append("[bold]N[/bold] older")
        if self._page > 0:
            parts.append("[bold]P[/bold] newer")
        parts.append("[bold]Esc[/bold] close")
        self.query_one("#dmh-hint", Label).update("  " + "   " .join(parts))

    def action_next_page(self):
        total   = len(self._entries)
        n_pages = max(1, (total + self.PAGE_SIZE - 1) // self.PAGE_SIZE)
        if self._page < n_pages - 1:
            self._page += 1
            self._render_page()

    def action_prev_page(self):
        if self._page > 0:
            self._page -= 1
            self._render_page()

    def on_key(self, event) -> None:
        if event.key in ("n",):
            self.action_next_page()
            event.stop()
        elif event.key in ("p",):
            self.action_prev_page()
            event.stop()


INPUT_HISTORY_FILE = Path(__file__).parent / "mesh_input_history.json"
INPUT_HISTORY_MAX  = 200


HELP_TEXT = """\
━━ MESH CONSOLE v{version} — HELP ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
COMMANDS (type in the input line)
  /help  /?              this help
  /ch                    show the channel you are writing on
  /ch NAME | /ch N       write on that channel (by name or index)
  /channels  /chs        list the device's channels (index, name, key type)
  /join NAME [KEY]       add a channel and switch to it; KEY: AQ== / default
                         (public), base64, 0x-hex, random, none
  /leave NAME | N        remove a secondary channel (asks first)
  /tr                    traceroute — pick a node from a list (= Ctrl+T)
  /tr !xxxxxxxx | NAME   traceroute to that node
  /pos                   send location — type it or pick a recent one (= Ctrl+L)
  /pos LAT, LON [, ALT]  send these coordinates (e.g. /pos 52.22970, 21.01220)
  /retry                 resend the last DM by hand
  /telem [on|off]        raw telemetry packets on / off (no arg = toggle)
  /clear  /cls           clear the screen
  //text                 send text that starts with "/" (//shrug → /shrug)

KEYS — main screen
  Enter                  send            ↑ / ↓    previous / next sent line
  Ctrl+N  node list      Ctrl+D  DM (pick node)   Ctrl+B  leave DM / reply
  Ctrl+H  DM archive     Ctrl+R  reply to msg     Ctrl+G  tapback (reaction)
  Ctrl+P  insert emoji   Ctrl+T  traceroute       Ctrl+L  send location
  Ctrl+O  device info / radio health              Ctrl+Y  telemetry on/off
  F1      focus the input line                    Esc     quit (asks first)

MESSAGE LOG
  mouse wheel / PgUp     scroll back — the view stays put, new lines are
                         counted in the status bar (⏸ reading back · ▼ N new)
  PgDn                   scroll forward; at the bottom it follows again
  End  (twice in input)  jump to the newest line; sending a message does too

NODE LIST (Ctrl+N)
  Enter / d  DM     t  traceroute     i  request NODEINFO     m  open map
  s  LocalStats (radio health)        b  device metrics       Esc / q  close
  / or Ctrl+F  search by name, short name, !id or hardware (Esc clears)
DM ARCHIVE (Ctrl+H)   n / p  next / previous page             Esc / q  close
EMOJI PICKER          arrows move, Tab / Shift+Tab (PgDn/PgUp) category,
                      letters search, Enter picks, Esc clears search / closes
OTHER WINDOWS         Esc closes / cancels

DIRECT MESSAGES
  ✓ relayed (entered the mesh)        ✓✓ delivered (confirmed by recipient)
  Entering DM mode exchanges keys (PKI). Without the node's key the DM waits
  for its NODEINFO (max 3 min). Not relayed → automatic resend (max 3 times);
  relayed but unconfirmed → never resent automatically, use /retry.

FILES (next to the script)
  mesh_nodes.json  mesh_dm_archive.jsonl  mesh_locations.json
  mesh_neighbours.json  mesh_emoji_recent.json  mesh_input_history.json
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"""


class ChatLog(RichLog):
    """Message log that stops following new lines while the user reads back.
    follow=False keeps the view where it is: new messages are only counted
    (unseen) and lines trimmed from the top (max_lines) don't shift the text
    under the reader. Scrolling back to the bottom resumes following."""
    HOLD_AFTER_SCROLL = 1.0      # s: don't auto-resume right after scrolling up

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.follow = True
        self.unseen = 0
        self._held_at = 0.0

    def at_bottom(self) -> bool:
        return self.scroll_y >= self.max_scroll_y - 1

    def hold(self) -> None:
        self.follow = False
        self._held_at = time.monotonic()

    def resume(self) -> None:
        self.follow = True
        self.unseen = 0
        self.scroll_end(animate=False)

    def maybe_resume(self) -> None:
        if (not self.follow and self.at_bottom()
                and time.monotonic() - self._held_at > self.HOLD_AFTER_SCROLL):
            self.follow = True
            self.unseen = 0

    def write(self, *args, **kwargs):
        start = getattr(self, "_start_line", 0)
        res = super().write(*args, **kwargs)
        trimmed = getattr(self, "_start_line", 0) - start
        if trimmed > 0 and not self.follow:
            # the oldest lines were dropped: move up by as much so the text
            # being read stays in place
            self.scroll_to(y=max(0, self.scroll_y - trimmed), animate=False)
        return res

    def _on_mouse_scroll_up(self, event) -> None:
        super()._on_mouse_scroll_up(event)
        if not self.at_bottom():
            self.hold()

    def _on_mouse_scroll_down(self, event) -> None:
        super()._on_mouse_scroll_down(event)
        if not self.follow and self.at_bottom():
            self._held_at = 0.0
            self.maybe_resume()


class HistoryInput(Input):
    """Input line with shell-style history: ↑ older, ↓ newer. A half-typed
    line is kept as a draft and comes back after ↓ past the newest entry."""
    BINDINGS = [
        Binding("up",   "history(-1)", "Previous", show=False),
        Binding("down", "history(1)",  "Next",     show=False),
    ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._hist = self._load()
        self._pos = len(self._hist)        # == len → editing a new line
        self._draft = ""

    @staticmethod
    def _load() -> list:
        try:
            data = json.loads(INPUT_HISTORY_FILE.read_text(encoding="utf-8"))
            return [x for x in data if isinstance(x, str) and x][-INPUT_HISTORY_MAX:]
        except Exception:
            return []

    def remember(self, line: str) -> None:
        line = line.strip()
        if line and (not self._hist or self._hist[-1] != line):
            self._hist.append(line)
            del self._hist[:-INPUT_HISTORY_MAX]
            try:
                INPUT_HISTORY_FILE.write_text(json.dumps(self._hist, ensure_ascii=False),
                                              encoding="utf-8")
            except Exception:
                pass
        self._pos = len(self._hist)
        self._draft = ""

    def action_end(self, select: bool = False) -> None:
        """End in the input: cursor to end of line; pressed again (cursor
        already there) it jumps the message log back to the newest line."""
        if not select and self.cursor_position >= len(self.value):
            try:
                self.app.query_one("#msg-log", ChatLog).resume()
            except Exception:
                pass
            return
        super().action_end(select)

    def action_history(self, step: int) -> None:
        if not self._hist:
            return
        if self._pos == len(self._hist):
            if step > 0:
                return
            self._draft = self.value       # leaving the new line: keep what was typed
        new = max(0, min(len(self._hist), self._pos + step))
        if new == self._pos:
            return
        self._pos = new
        self.value = self._hist[new] if new < len(self._hist) else self._draft
        self.cursor_position = len(self.value)


class MeshApp(App):
    """Main Textual application — Mesh Console."""

    TITLE                  = f"Mesh Console v{VERSION}"
    ENABLE_COMMAND_PALETTE = False   # disables Ctrl+P
    CSS = """
    Screen {
        layout: vertical;
        background: $surface;
    }
    #header-bar {
        height: 1;
        background: $success;
        color: $text;
        text-style: bold;
    }
    #header-bar.disconnected {
        background: $error;
        color: $text;
    }
    #hdr-left {
        width: 1fr;
        content-align: left middle;
        padding: 0 1;
    }
    #hdr-center {
        width: auto;
        content-align: center middle;
        padding: 0 2;
    }
    #hdr-right {
        width: 1fr;
        content-align: right middle;
        padding: 0 1;
    }
    #msg-log {
        height: 1fr;
        border: none;
        scrollbar-size: 0 0;
        background: $surface;
        padding: 0 1;
    }
    #status-bar {
        height: 1;
        background: $primary-darken-2;
        color: $text;
        padding: 0 1;
        content-align: left middle;
    }
    #footer-bar {
        height: 1;
        background: $primary-darken-3;
        color: $text-muted;
        padding: 0 1;
        content-align: left middle;
    }
    #msg-input {
        height: 3;
        background: $panel;
        color: $text;
        border: round $accent;
        padding: 0 1;
    }
    #msg-input:focus {
        border: round $success;
    }
    """

    BINDINGS = [
        Binding("ctrl+o",     "device_info",  "^O Device",  show=True,  priority=True),
        Binding("ctrl+t",     "traceroute",   "^T Traceroute",  show=True,  priority=True),
        Binding("ctrl+r",     "reply",        "^R Reply",   show=True,  priority=True),
        Binding("ctrl+g",     "tapback",      "^G Tapback",     show=True,  priority=True),
        Binding("ctrl+p",     "emoji",        "^P Emoji",       show=True,  priority=True),
        Binding("ctrl+n",     "nodes",        "^N Nodes",       show=True,  priority=True),
        Binding("ctrl+d",     "dm",           "^D DM",          show=True,  priority=True),
        Binding("ctrl+h",     "dm_history",   "^H History",     show=True,  priority=True),
        Binding("ctrl+b",     "cancel_reply", "^B DM Exit",     show=True,  priority=True),
        Binding("ctrl+l",     "location",     "^L Location",    show=True,  priority=True),
        Binding("ctrl+y",     "toggle_telemetry", "^Y Telemetry", show=True, priority=True),
        Binding("f1",         "refocus",      "F1 Focus",       show=False, priority=True),
        Binding("tab",        "noop",         "",               show=False, priority=False),
        Binding("shift+tab",  "noop",         "",               show=False, priority=False),
        Binding("escape",     "quit_confirm", "",               show=False, priority=False),
    ]

    def __init__(self, state: MeshState, target_node: str = "^all"):
        super().__init__()
        self._state       = state
        self._target_node = target_node
        self._reply_to    : Message | None = None
        self._last_own_req = 0.0   # last quiet LocalStats request to our own node
        # attach callback — Textual requires call_from_thread
        state.ui_notify   = self._on_new_message

    # ── UI layout ─────────────────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        mode = "WiFi" if self._state.tcp_mode else "BLE"
        with Horizontal(id="header-bar", classes="disconnected"):
            yield Static(Text(f"MESH CONSOLE v{VERSION} [{mode}]"), id="hdr-left")
            yield Static("○ DISCONNECTED", id="hdr-center")
            yield Static(Text(f"{self._state.my_name} ({self._state.my_id})"), id="hdr-right")
        log = ChatLog(id="msg-log", highlight=False, markup=True, wrap=True,
                       auto_scroll=False, max_lines=MAX_MESSAGES)
        log.can_focus = False
        yield log
        status = Static("", id="status-bar")
        status.can_focus = False
        yield status
        inp = HistoryInput(placeholder="Type message... ↑ history  /help  (/ch 0|1  /tr  /pos  /channels  /join  /retry  /clear   // = literal /)", id="msg-input")
        yield inp
        footer = Static(
            " ▶ [bold white]^O[/bold white] Info  [bold white]^N[/bold white] Nodes  [bold white]^D[/bold white] DM  [bold white]^H[/bold white] History  [bold white]^B[/bold white] DM Exit  [bold white]^T[/bold white] Route  [bold white]^R[/bold white] Reply  [bold white]^G[/bold white] Tapback  [bold white]^P[/bold white] Emoji  [bold white]^L[/bold white] Loc  [bold white]^Y[/bold white] Telem  [bold white]Esc[/bold white] Quit",
            id="footer-bar"
        )
        footer.can_focus = False
        yield footer

    def on_mount(self):
        self.set_interval(0.25, self._tick)
        self.set_interval(10.0, self._sync_node_names)
        self.set_interval(30.0, self._sample_health)
        # display system messages buffered before UI start — only after the
        # first layout pass, when the log knows its real width (otherwise
        # RichLog wraps everything at a default ~80 columns)
        self.call_after_refresh(self._render_initial)

    def _render_initial(self) -> None:
        with self._state.lock:
            initial = list(self._state.messages)
        for msg in initial:
            self._render_message(msg)
        self._refocus()

    def on_screen_resume(self) -> None:
        """Called when main screen returns to top (after overlay close).
        Restores focus to input field."""
        try:
            self.query_one("#msg-input", Input).focus()
        except NoMatches:
            pass

    # ── Tick — BLE packet queue processing ───────────────────────────────────

    def _sync_node_names(self):
        try:
            iface = self._state.interface
            if self._state.connected and iface and iface.nodes:
                nodes = dict(iface.nodes)
                _populate_node_db(self._state, nodes, mark_seen=False)
                with self._state.lock:
                    self._state.nodes = nodes
        except Exception as exc:
            self._state.error(f"Node name synchronization failed: {exc}")
        try:
            self._state.node_db.save()
        except Exception as exc:
            self._state.error(f"Cannot save {NODE_DB_FILE}: {exc}")

    # ── scroll-back state lives in the log widget ────────────────────────────
    @property
    def _log_follow(self) -> bool:
        try:
            return self.query_one("#msg-log", ChatLog).follow
        except Exception:
            return True

    @_log_follow.setter
    def _log_follow(self, on: bool) -> None:
        try:
            log = self.query_one("#msg-log", ChatLog)
        except Exception:
            return
        if on:
            log.follow, log.unseen = True, 0
        else:
            log.hold()

    def _tick(self):
        # user scrolled back to the bottom with the mouse — resume auto-follow
        try:
            self.query_one("#msg-log", ChatLog).maybe_resume()
        except Exception:
            pass
        try:
            _process_queue(self._state)
            # refresh header and status bar
            self._refresh_header()
            self._refresh_status()
        except Exception:
            pass  # tick errors must not crash UI
        # check traceroute timeout (30s)
        now = time.time()
        with self._state.lock:
            expired = [(nid, name) for nid, (name, sent)
                       in self._state.traceroute_pending.items() if now - sent > 30]
        for nid, name in expired:
            self._state.error(f"⏱ No traceroute response ← {name} ({nid})")
            with self._state.lock:
                reached = nid in self._state.tr_reached
                self._state.tr_reached.discard(nid)
            if reached:
                self._state.system("  ↳ the request DID arrive — the route data was lost on the "
                                   "way back, or the node is a REPEATER / rate-limits answers")
            else:
                self._state.system("  ↳ no confirmation and no error came back: the request "
                                   "or the answer was lost, or the node is offline")
            with self._state.lock:
                self._state.traceroute_pending.pop(nid, None)
        # learned neighbours → disk now and then (also saved on exit)
        if time.time() - self._state.neighbours_saved > NEIGHBOUR_SAVE_EVERY:
            save_neighbours(self._state)
        # ACK feedback timeouts (no confirmation / relay only)
        try:
            expire_acks(self._state)
        except Exception as exc:
            self._state.error(f"ACK tracking error: {exc}")
        # DMs waiting for the device to learn the recipient
        try:
            process_dm_queue(self._state)
        except Exception as exc:
            self._state.error(f"DM queue error: {exc}")
        # check telemetry request timeout
        with self._state.lock:
            expired_tel = [(nid, name, quiet) for nid, (name, sent, _t, quiet)
                           in self._state.telem_pending.items()
                           if now - sent > TELEM_TIMEOUT]
            for nid, _n, _q in expired_tel:
                self._state.telem_pending.pop(nid, None)
        for nid, name, quiet in expired_tel:
            if not quiet:
                self._state.error(f"⏱ No telemetry response ← {name} ({nid})")

    def _sample_health(self):
        """Every 30 s: store own CU / airTx sample (1 h trend in Ctrl+O) and
        keep own LocalStats fresh — a request to our own node costs no airtime."""
        st = self._state
        if not st.connected or not st.my_id.startswith("!"):
            return
        with st.lock:
            own = st.local_stats.get(st.my_id)
            pending = st.my_id in st.telem_pending
        now = time.time()
        if not pending and (own is None or now - own[1] > OWN_STATS_REFRESH) \
                and now - self._last_own_req > OWN_STATS_REFRESH:
            self._last_own_req = now
            request_telemetry(st, st.my_id, "local_stats", quiet=True)
        cu, air = own_air(st)
        if cu is not None or air is not None:
            with st.lock:
                st.health_hist.append((now, cu or 0.0, air or 0.0))

    # ── New message — callback from BLE thread ───────────────────────────────

    def _on_new_message(self, msg: Message):
        """Called by state.add() from any thread.
        We use app.call_from_thread() only from external threads.
        From UI thread (via _tick) we render directly.
        We check is_running to avoid calls before startup."""
        try:
            if not self.is_running:
                return
            # Check whether we are in the Textual event loop thread
            # _thread_id is set by Textual to the asyncio thread
            current = threading.get_ident()
            app_thread = getattr(self, '_thread_id', None)
            if app_thread and current == app_thread:
                # UI thread — call directly
                self._render_message(msg)
            else:
                # external thread — via call_from_thread
                self.call_from_thread(self._render_message, msg)
        except Exception:
            pass

    def _render_message(self, msg: Message):
        log = self.query_one("#msg-log", RichLog)
        color = MSG_COLORS.get(msg.kind, "white")
        if msg.kind in ("tapback", "tapback_mine") and msg.channel:
            # reactions on a secondary channel look like that channel's messages
            color = MSG_COLORS["private"]
        ts_part = _ts_text(msg.ts)
        # normalize keycap emoji for clean terminal rendering
        msg_text = _fix_keycaps(msg.text)

        def _quote_suffix(reply_id: int) -> Text:
            """Return dimmed quoted snippet if original message is in buffer."""
            if not reply_id:
                return Text("")
            with self._state.lock:
                orig = self._state.msg_by_id.get(reply_id)
            if not orig:
                return Text("")
            who  = orig.sender if orig.sender else "Me"
            flat = orig.text.replace("\n", " ")
            snip = flat[:35] + ("…" if len(flat) > 35 else "")
            return Text(f" ┆ {who}: {snip}", style="dim")

        def _link_suffix(m: Message) -> Text:
            """Dimmed hop count, e.g. '  2↗' (channel messages only)."""
            if m.kind in ("theirs", "private") and m.hops >= 0:
                return Text(f"  {m.hops}↗", style="dim")
            return Text("")

        if msg.kind == "mine":
            prefix = "↩ Me: " if msg.reply_id else "Me: "
            _hang_write(log, ts_part, color, prefix, msg_text, _quote_suffix(msg.reply_id))
        elif msg.kind in ("theirs", "private"):
            tag    = f"{channel_label(self._state, msg.channel)} " if msg.channel else ""
            reply  = "↩ " if msg.reply_id else ""
            prefix = f"{reply}{tag}{msg.sender}: "
            _hang_write(log, ts_part, color, prefix, msg_text,
                        _link_suffix(msg) + _quote_suffix(msg.reply_id))
        elif msg.kind == "dm":
            reply  = "↩ " if msg.reply_id else ""
            prefix = f"{reply}📩 {msg.sender}: "
            _hang_write(log, ts_part, color, prefix, msg_text,
                        _link_suffix(msg) + _quote_suffix(msg.reply_id))
        elif msg.kind in ("tapback", "tapback_mine"):
            text = msg_text
            if msg.channel:
                # same channel tag as the channel's messages: "⚡ [TEST] …"
                tag  = channel_label(self._state, msg.channel)
                text = (f"⚡ {tag} {text[2:]}" if text.startswith("⚡ ")
                        else f"{tag} {text}")
            _hang_write(log, ts_part, color, "", text)
        elif msg.kind == "system_raw":
            # telemetry lines — write as-is, no hang-indent
            _log_write(log, Text.assemble(ts_part, (msg_text, color)))
        elif msg.kind == "system":
            _hang_write(log, ts_part, color, "", msg_text)
        else:  # error
            _hang_write(log, ts_part, color, "! ", msg_text)
        if log.follow:
            log.scroll_end(animate=False)
        elif msg.kind != "system_raw":
            log.unseen += 1

    # ── Header and status bar ─────────────────────────────────────────────────

    def _refresh_header(self):
        try:
            hdr = self.query_one("#header-bar", Horizontal)
            if self._state.connected:
                hdr.remove_class("disconnected")
                self.query_one("#hdr-center", Static).update("● CONNECTED")
            else:
                hdr.add_class("disconnected")
                self.query_one("#hdr-center", Static).update("○ DISCONNECTED")
            self.query_one("#hdr-right", Static).update(
                Text(f"{self._state.my_name} ({self._state.my_id})")
            )
        except NoMatches:
            pass

    def _refresh_status(self):
        try:
            sta = self.query_one("#status-bar", Static)
            with self._state.lock:
                n_nodes = len(self._state.nodes)
                n_msgs  = len(self._state.messages)
            prompt = self._get_prompt()
            ch_info = ""
            if self._state.current_channel != 0:
                ch_info = ""        # channel name is already part of the prompt
            if self._state.reply_dm_to:
                dm_target = self._state.reply_dm_to
                with self._state.lock:
                    live = dict(self._state.nodes)
                pki_icon  = PKI_ICON[pki_state(self._state, dm_target, live)]
                ch_info   = f"  [DM {pki_icon} → {self._state.node_db.resolve(dm_target)}]"
            unread = self._state.dm_unread_count
            unread_tag = (f"  📩 {unread} new DM  ^H", "bold orange1") if unread else ""
            telem_tag  = ("  [T]", "bold cyan") if self._state.telemetry else ""
            cu, air    = own_air(self._state) if self._state.connected else (None, None)
            radio = []
            if cu is not None:
                radio += ["  CU ", (f"{cu:.0f}%", cu_style(cu))]
            if air is not None:
                radio += [" TX ", (f"{air:.1f}%", air_style(air))]
            back_tag = ""
            try:
                log = self.query_one("#msg-log", ChatLog)
                if not log.follow:
                    back_tag = ("  ⏸ reading back"
                                + (f" · ▼ {log.unseen} new" if log.unseen else "")
                                + "  End/PgDn ↓", "bold black on yellow")
            except NoMatches:
                pass
            sta.update(Text.assemble(
                f" {prompt}{ch_info}   nodes:{n_nodes}  msgs:{n_msgs}", *radio,
                telem_tag, unread_tag, back_tag
            ))
        except NoMatches:
            pass

    # ── Input field prompt ────────────────────────────────────────────────────

    def _get_prompt(self) -> str:
        if self._state.reply_dm_to:
            dm_name = self._state.node_db.resolve(self._state.reply_dm_to)
            base    = f"DM → {dm_name}"
            if self._reply_to:
                base = f"↩ DM → {dm_name}"
            return base
        base = f"→ {self._target_node}" if self._target_node != "^all" else "→ broadcast"
        if self._reply_to:
            name = self._reply_to.sender if self._reply_to.sender else "Me"
            base = f"↩ {name} → broadcast"
        if self._state.current_channel != 0:
            return f"{base}  {channel_label(self._state, self._state.current_channel)}"
        return base

    def _update_prompt(self):
        """Prompt is now in the status bar — refreshed by _refresh_status."""
        try:
            # Change input border color depending on mode
            inp = self.query_one("#msg-input", Input)
            if self._state.reply_dm_to:
                inp.styles.border = ("round", "orange")
            elif self._state.current_channel != 0:
                inp.styles.border = ("round", "magenta")
            else:
                inp.styles.border = ("round", "green")
        except NoMatches:
            pass

    # ── Message sending ───────────────────────────────────────────────────────

    @on(Input.Submitted, "#msg-input")
    def on_submit(self, event: Input.Submitted):
        text = event.value.strip()
        if not text:
            return
        inp = self.query_one("#msg-input", Input)
        inp.value = ""
        if isinstance(inp, HistoryInput):
            inp.remember(text)
        try:                                 # sending jumps back to the newest line
            self.query_one("#msg-log", ChatLog).resume()
        except NoMatches:
            pass

        literal = False
        if text.startswith("//"):
            # escape: "//shrug" sends the literal text "/shrug"
            text, literal = text[1:], True
        elif text.startswith("/"):
            cmd = text.split()[0].lower()
            if cmd in ("/help", "/?", "/h"):
                self._show_help()
                return
            if cmd in ("/clear", "/cls"):
                self._clear_screen()
                return
            if cmd == "/retry":
                retry_last_dm(self._state)
                return
            if cmd in ("/channels", "/chs"):
                list_channels(self._state)
                return
            if cmd == "/join":
                parts = text.split()
                if len(parts) < 2 or len(parts) > 3:
                    self._state.error("Usage: /join NAME [KEY]   — KEY: AQ== / default (public, "
                                      "the default), base64 / 0x-hex, random, none")
                    return
                join_channel(self._state, parts[1], parts[2] if len(parts) == 3 else None)
                self._update_prompt()
                return
            if cmd == "/leave":
                parts = text.split(maxsplit=1)
                if len(parts) < 2:
                    self._state.error("Usage: /leave NAME   or   /leave INDEX")
                    return
                self._ask_leave(parts[1])
                return
            if cmd == "/telem":
                arg = text[len(cmd):].strip().lower()
                if arg in ("", "toggle"):
                    self._set_telemetry(not self._state.telemetry)
                elif arg in ("on", "1"):
                    self._set_telemetry(True)
                elif arg in ("off", "0"):
                    self._set_telemetry(False)
                else:
                    self._state.error("Usage: /telem [on|off]")
                return
            if cmd not in ("/ch", "/tr", "/pos"):
                # never broadcast a mistyped command into the mesh
                self._state.error(f"Unknown command: {cmd}   (/help lists all commands; "
                                  f"start with // to send text beginning with /)")
                return

        if text == "/tr" and not literal:
            self.action_traceroute()
            return

        if not literal and (text == "/pos" or text.startswith("/pos ")):
            arg = text[4:].strip()
            if not arg:
                self.action_location()
                return
            try:
                lat, lon, alt = parse_coords(arg)
            except ValueError as e:
                self._state.error(f"/pos: {e}")
                return
            self._send_location({"lat": lat, "lon": lon, "alt": alt, "name": ""})
            return

        rid = self._reply_to.msg_id if self._reply_to else 0
        send_message(self._state, text, dest=self._target_node, reply_id=rid,
                     literal=literal)
        self._reply_to = None
        self._update_prompt()

    # ── Key actions ───────────────────────────────────────────────────────────

    def _refocus(self) -> None:
        """Restore focus to the input field."""
        try:
            self.query_one("#msg-input", Input).focus()
        except NoMatches:
            pass

    def action_nodes(self):
        def _done(result):
            self._refocus()
            if not result:
                return
            action, nid = result
            if action == "dm":
                self._enter_dm(nid)
            elif action == "tr":
                send_message(self._state, f"/tr {nid}")
        self.push_screen(NodesScreen(self._state), _done)

    def _clear_screen(self) -> None:
        """/clear, /cls — wipe the message log and redraw only the session header.
        The message buffer is kept, so Reply, Tapback, quotes, the DM archive
        and the log file are unaffected."""
        st  = self._state
        log = self.query_one("#msg-log", RichLog)
        log.clear()
        ts    = _ts_text(datetime.now().strftime("%H:%M:%S"))
        color = MSG_COLORS["system"]
        mode  = "WiFi/TCP" if st.tcp_mode else "BLE"
        lines = [f"Mesh Console v{VERSION} — Esc to quit"]
        if st.connected:
            dev = " ".join(x for x in (st.device_hw, st.device_fw) if x) or "Meshtastic"
            with st.lock:
                n_sess = len(st.nodes)
            lines.append(f"✓ Connected: {st.my_name} ({st.my_id})  •  {dev}  •  {mode}")
            lines.append(f"  Nodes: {n_sess} in session  •  {st.node_db.size()} in database")
        else:
            lines.append(f"○ Not connected ({mode})")
        if st.reply_dm_to:
            target = f"DM → {st.node_db.resolve(st.reply_dm_to)}"
        elif st.current_channel != 0:
            target = f"{channel_label(st, st.current_channel)} (channel {st.current_channel})"
        else:
            target = "broadcast, primary channel"
        lines.append(f"  Now writing: {target}"
                     + ("   •   📊 telemetry ON" if st.telemetry else ""))
        for line in lines:
            _hang_write(log, ts, color, "", line)
        _log_write(log, Text.assemble(" " * _TS_WIDTH, ("── screen cleared ──", "dim")))
        self._log_follow = True
        log.scroll_end(animate=False)

    def _show_help(self) -> None:
        """/help — print the command / key reference into the log and scroll so
        that its first line is at the top (the log then holds there)."""
        try:
            log = self.query_one("#msg-log", ChatLog)
        except NoMatches:
            return
        start = len(log.lines) + getattr(log, "_start_line", 0)
        for line in HELP_TEXT.format(version=VERSION).splitlines():
            head = line.startswith("━") or (line[:1].isalpha() and "  " not in line)
            _log_write(log, Text(line, style="bold yellow" if head else "cyan"))

        def _to_top():
            first = start - getattr(log, "_start_line", 0)
            if first + log.size.height < len(log.lines):
                log.hold()
                log.scroll_to(y=max(0, first), animate=False, immediate=True)
            else:
                log.resume()
        # after the pending scroll_end from sending and the layout of the new lines
        self.set_timer(0.05, _to_top)

    def _set_telemetry(self, on: bool) -> None:
        self._state.telemetry = on
        if on:
            self._state.system("📊 Telemetry ON — all packets displayed (Ctrl+Y / /telem off)")
        else:
            self._state.system("📊 Telemetry OFF")

    def action_toggle_telemetry(self) -> None:
        """Ctrl+Y — toggle raw packet display without reconnecting."""
        self._set_telemetry(not self._state.telemetry)

    def action_device_info(self):
        def _done(_):
            self._refocus()
        self.push_screen(DeviceInfoScreen(self._state), _done)

    def action_traceroute(self):
        def _done(nid):
            if nid:
                send_message(self._state, f"/tr {nid}")
            self._refocus()
        self.push_screen(TracerouteScreen(self._state), _done)

    def _enter_dm(self, nid: str) -> None:
        """Switch to DM mode with nid (shared by Ctrl+D and the node list)."""
        st = self._state
        st.reply_dm_to = nid
        st.current_channel = 0
        dm_name = st.node_db.resolve(nid)
        with st.lock:
            live = dict(st.nodes)
        in_session = nid in live
        # key exchange: our current key goes to the peer, theirs comes back
        if key_exchange(st, nid):
            st.system(f"🔄 Key exchange with {dm_name} — your NODEINFO sent, waiting for theirs")
        pki = pki_state(st, nid, live)
        if pki == "program" and push_contact(st, nid):
            pki = "loaded"
        st.system(f"📩 DM mode {PKI_ICON[pki]} → {dm_name}  (Ctrl+B to cancel, /retry to resend)")
        if pki == "program":
            st.system(f"🔑 The device did not take the stored key of {dm_name} — messages "
                      f"wait for its NODEINFO")
        elif pki == "none" and in_session and legacy_dm_allowed(st):
            st.system(f"⛔ No PKI key for {dm_name} — DM will be sent without E2E encryption.")
        elif pki == "none":
            st.system(f"🔓 No key for {dm_name} yet — messages wait for its NODEINFO "
                      f"(max {DM_KEY_WAIT // 60} min), then go out encrypted")
        self._update_prompt()

    def action_dm(self):
        def _done(nid):
            self._refocus()
            if nid:
                self._enter_dm(nid)
        self.push_screen(TracerouteScreen(self._state,
                                          title="  DM — select node  ",
                                          show_pki=True), _done)

    def action_dm_history(self):
        """Ctrl+H — open DM archive browser and reset unread counter."""
        self._state.dm_unread_count = 0
        def _done(_):
            self._refocus()
        self.push_screen(DmHistoryScreen(self._state), _done)

    def action_reply(self):
        with self._state.lock:
            has_msgs = any(m.kind in ("theirs", "mine", "private", "dm") and m.text and m.msg_id
                          for m in self._state.messages)
        if not has_msgs:
            self._state.system("⚠ No messages to reply to.")
            return
        def _done(msg):
            if msg is None:
                return
            self._reply_to = msg
            if msg.kind == "dm":
                # use sender_id directly instead of a fragile name→ID lookup
                if msg.sender_id and msg.sender_id != self._state.my_id:
                    self._state.reply_dm_to = msg.sender_id
                else:
                    # fallback for old messages without sender_id
                    self._state.reply_dm_to = msg.to_id if msg.to_id and msg.to_id != "^all" else ""
                    if not self._state.reply_dm_to or self._state.reply_dm_to == self._state.my_id:
                        for nid in self._state.node_db.all_ids():
                            if self._state.node_db.resolve(nid) == msg.sender:
                                self._state.reply_dm_to = nid
                                break
                self._state.current_channel = 0
                dm_name = self._state.node_db.resolve(self._state.reply_dm_to) if self._state.reply_dm_to else msg.sender
                self._state.system(f"📩 DM mode → {dm_name}  (Ctrl+B to cancel)")
            elif msg.channel != self._state.current_channel or self._state.reply_dm_to:
                # reply goes to the channel the original message came from
                self._state.reply_dm_to = ""
                self._state.current_channel = msg.channel
                self._state.system(f"✓ Switched to {channel_label(self._state, msg.channel)} "
                                   f"for reply")
            else:
                self._state.reply_dm_to = ""
            self._update_prompt()
        def _reply_callback(r):
            _done(r)
            self._refocus()
        self.push_screen(SelectMessageScreen(self._state, "  Reply to — select message  "),
                         _reply_callback)

    def action_tapback(self):
        with self._state.lock:
            has_msgs = any(m.kind in ("theirs", "mine", "private", "dm") and m.text and m.msg_id
                          for m in self._state.messages)
        if not has_msgs:
            self._state.system("⚠ No messages to react to.")
            return
        def _step2(result):
            if result is None:
                self._refocus()
                return
            emoji, msg = result
            dest = "^all"
            if msg.kind == "dm":
                # use sender_id directly instead of looking the node up by name
                if msg.sender_id and msg.sender_id != self._state.my_id:
                    dest = msg.sender_id
                else:
                    # fallback for old messages without sender_id (backward compatibility)
                    for nid in self._state.node_db.all_ids():
                        if self._state.node_db.resolve(nid) == msg.sender:
                            dest = nid
                            break
            flat_text = msg.text.replace("\n", " ")
            snippet = (f'"{flat_text[:28]}..."' if len(flat_text) > 31 else f'"{flat_text}"')
            send_tapback(self._state, emoji, msg.msg_id, dest, snippet, channel=msg.channel)
            self._refocus()

        def _step1(msg):
            if msg is None:
                self._refocus()
                return
            self.push_screen(EmojiPickerScreen("Choose reaction", msg), _step2)

        self.push_screen(
            SelectMessageScreen(self._state, "  Tapback — select message  "), _step1
        )

    def _send_location(self, entry: dict) -> None:
        if send_position(self._state, entry):
            try:
                save_location_history(entry)
            except Exception as e:
                self._state.error(f"Could not save location history: {e}")

    def action_location(self):
        """Ctrl+L — send location (typed or picked from 5 recent)."""
        if self._state.reply_dm_to:
            target = f"DM → {self._state.node_db.resolve(self._state.reply_dm_to)}"
        elif self._state.current_channel != 0:
            target = f"broadcast, {channel_label(self._state, self._state.current_channel)}"
        else:
            target = "broadcast, primary channel"
        def _done(entry):
            self._refocus()
            if entry:
                self._send_location(entry)
        self.push_screen(LocationScreen(target), _done)

    def action_emoji(self):
        def _done(emoji):
            self._refocus()
            if not emoji:
                return
            inp = self.query_one("#msg-input", Input)
            pos = inp.cursor_position          # insert at the cursor, not at the end
            inp.value = inp.value[:pos] + emoji + inp.value[pos:]
            inp.cursor_position = pos + len(emoji)
        self.push_screen(EmojiPickerScreen("Insert emoji"), _done)

    def _ask_leave(self, ref: str) -> None:
        """/leave — confirm, then remove a secondary channel."""
        st  = self._state
        idx = find_channel(st, ref)
        if idx is None:
            st.error(f"No such channel: {ref}   (/channels lists them)")
            return
        if idx == 0:
            st.error("The primary channel cannot be removed.")
            return
        psk = b""
        for c in _channels(st):
            if c.index == idx:
                psk = c.settings.psk
        lines = [f"Leave {channel_label(st, idx)} (channel {idx})?"]
        if psk not in (b"", DEFAULT_PSK):
            lines.append("")
            lines.append("It has its OWN key — note it down if you want to rejoin:")
            lines.append(describe_psk(psk).replace("own ", ""))
        lines.append("")
        lines.append("Channels after it move one number up.")

        def _done(ok):
            self._refocus()
            if ok:
                leave_channel(st, idx)
                self._update_prompt()
        self.push_screen(ConfirmScreen("\n".join(lines)), _done)

    def _ask_quit(self) -> None:
        """Show quit confirmation modal."""
        def _on_result(confirmed: bool):
            if confirmed:
                self.exit()
        self.push_screen(QuitConfirmScreen(), _on_result)

    def action_quit_confirm(self) -> None:
        """Called by ESC (priority binding)."""
        from textual.screen import ModalScreen as _MS
        if isinstance(self.screen, QuitConfirmScreen):
            # ESC in quit modal = cancel
            self.screen.dismiss(False)
        elif isinstance(self.screen, _MS):
            # ESC in any other modal = close it
            self.screen.dismiss(None)
        else:
            self._ask_quit()

    def on_key(self, event) -> None:
        """PageUp/Down scrolls message log; Y/N/Enter handles quit modal."""
        # Windows Terminal sends Ctrl+H as ASCII 0x08 (backspace) — Textual may
        # report it as key="backspace" instead of "ctrl+h", bypassing the binding.
        # Detect it via character value and trigger dm_history manually.
        if event.key == "backspace" and event.character == "\x08":
            self.action_dm_history()
            event.stop()
            return
        if isinstance(self.screen, QuitConfirmScreen):
            if event.key in ("y", "enter"):
                self.screen.dismiss(True)
                event.stop()
            elif event.key == "n":
                self.screen.dismiss(False)
                event.stop()
            return
        if event.key == "pageup":
            try:
                log = self.query_one("#msg-log", RichLog)
                log.scroll_page_up(animate=False)
                self._log_follow = False
            except NoMatches:
                pass
            event.stop()
        elif event.key == "pagedown":
            try:
                log = self.query_one("#msg-log", RichLog)
                log.scroll_page_down(animate=False)
                # check if back at bottom
                at_bottom = log.scroll_y >= log.max_scroll_y - 1
                if at_bottom:
                    self._log_follow = True
            except NoMatches:
                pass
            event.stop()
        elif event.key == "end":
            try:
                self.query_one("#msg-log", ChatLog).resume()
            except NoMatches:
                pass
            event.stop()

    def action_refocus(self) -> None:
        """F1 — force focus on input field."""
        try:
            inp = self.query_one("#msg-input", Input)
            inp.focus()
            self.notify("Focus restored!", timeout=1)
        except NoMatches:
            pass

    def action_noop(self) -> None:
        """Absorb key without action (e.g. Tab)."""
        try:
            self.query_one("#msg-input", Input).focus()
        except NoMatches:
            pass

    def action_cancel_reply(self):
        self._reply_to = None
        self._state.reply_dm_to = ""
        self._update_prompt()

    def action_quit(self):
        self._ask_quit()


# ══════════════════════════════════════════════════════════════════════════════
# Quit confirmation modal
# ══════════════════════════════════════════════════════════════════════════════

class ConfirmScreen(ModalScreen):
    """Generic yes/no question. Dismisses with True / False."""
    BINDINGS = [Binding("escape,n", "no", "No"), Binding("y,enter", "yes", "Yes")]
    DEFAULT_CSS = """
    ConfirmScreen { align: center middle; }
    ConfirmScreen > Static {
        width: 64;
        border: double $warning;
        background: $surface;
        padding: 1 2;
    }
    """

    def __init__(self, question: str):
        super().__init__()
        self._q = question

    def compose(self) -> ComposeResult:
        yield Static(Text.assemble(self._q, "\n\n", ("ENTER / Y", "bold"), " — yes     ",
                                   ("ESC / N", "bold"), " — no"))

    def action_yes(self) -> None:
        self.dismiss(True)

    def action_no(self) -> None:
        self.dismiss(False)


class QuitConfirmScreen(ModalScreen):
    """Quit confirmation modal."""

    DEFAULT_CSS = """
    QuitConfirmScreen {
        align: center middle;
    }
    QuitConfirmScreen > Static {
        width: 40;
        border: double $warning;
        background: $surface;
        padding: 1 2;
        text-align: center;
    }
    """

    def compose(self) -> ComposeResult:
        yield Static(
            "Exit Mesh Console?\n\n"
            "[bold]ENTER / Y[/bold] — quit\n"
            "[bold]ESC / N[/bold]   — cancel"
        )

    def action_confirm(self) -> None:
        self.dismiss(True)

    def action_cancel(self) -> None:
        self.dismiss(False)


# ══════════════════════════════════════════════════════════════════════════════
# BLE scanning (no UI)
# ══════════════════════════════════════════════════════════════════════════════

def do_scan():
    try:
        import meshtastic.ble_interface
    except ImportError:
        print("Missing library meshtastic. Install: pip install meshtastic pypubsub")
        sys.exit(1)
    print("Scanning for Meshtastic BLE devices (may take ~10 seconds)...\n")
    devices = meshtastic.ble_interface.BLEInterface.scan()
    if not devices:
        print("No devices found.")
    else:
        for d in devices:
            print(f"  Name:    {d.name}")
            print(f"  Address: {d.address}")
            print()
    sys.exit(0)


# ══════════════════════════════════════════════════════════════════════════════
# Entry point
# ══════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="Meshtastic Console Client (BLE + WiFi/TCP) — Textual UI",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--tcp",  metavar="HOST",
                        help="IP address or hostname of Meshtastic device (WiFi mode)")
    parser.add_argument("--ble",  metavar="ADDRESS",
                        help="BLE device address/UUID (omit = auto-detect)")
    parser.add_argument("--node", metavar="ID", default="^all",
                        help="Target node ID (default: ^all = broadcast)")
    parser.add_argument("--scan", action="store_true",
                        help="Scan for BLE devices and exit")
    parser.add_argument("--log",  metavar="FILE",
                        help="Write log to file (e.g. mesh.log)")
    parser.add_argument("--telemetry", action="store_true",
                        help="Telemetry mode — display all raw packets")
    args = parser.parse_args()

    if args.scan:
        do_scan()
        return

    if args.tcp and args.ble:
        print("Error: specify --tcp OR --ble, not both at the same time.")
        sys.exit(1)

    state = MeshState(logfile=args.log or "", telemetry=args.telemetry)
    state.system("Mesh Console ready.")
    state.system("Commands: /channels - list   /ch NAME - switch   /join NAME [KEY] - add   "
                 "/leave NAME   /clear")
    if args.telemetry:
        state.system("📊 Telemetry ON — all packets will be displayed (Ctrl+Y or /telem toggles)")

    state.tcp_mode = bool(args.tcp)

    if args.tcp:
        conn_thread = threading.Thread(target=connect_tcp, args=(state, args.tcp),
                                       daemon=True, name="TCP-Connect")
    else:
        conn_thread = threading.Thread(target=connect_ble, args=(state, args.ble),
                                       daemon=True, name="BLE-Connect")
    conn_thread.start()

    load_neighbours(state)
    app = MeshApp(state, target_node=args.node)
    try:
        app.run()
    finally:
        mode_str = "WiFi/TCP" if args.tcp else "BLE"
        print(f"Closing {mode_str} connection...", end="", flush=True)
        _clear_pubsub()
        if state.interface:
            def _close():
                try:
                    state.interface.close()
                except Exception:
                    pass
            t = threading.Thread(target=_close, daemon=True)
            t.start()
            t.join(timeout=3.0)
        state.close_log()
        save_neighbours(state)
        try:
            state.node_db.save()
        except Exception as exc:
            print(f"\nCannot save {NODE_DB_FILE}: {exc}", file=sys.stderr, flush=True)
        print(" Goodbye.", flush=True)
        os._exit(0)


if __name__ == "__main__":
    main()
