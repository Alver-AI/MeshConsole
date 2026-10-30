# Changelog

All notable changes to Meshtastic Console. The newest version is at the top.

When `VERSION` in `mesh_console.py` changes on `main`, the release workflow
creates the `vX.Y.Z` tag and a GitHub release whose notes are the section for
that version below.

## [1.5.1] - 2026-10-30

**Changed**
- Renamed to **Meshtastic Console** (was Mesh Console). The header reads `MESHTASTIC CONSOLE vX.Y.Z`, the same style as MeshCore Console.

## [1.5.0] - 2026-10-27

**New**
- MQTT status in the device information (`Ctrl+O`): on/off, uplink ↑ / downlink ↓ channels, server and topic, and the number of packets received via MQTT this session. A warning is shown when MQTT is enabled but cannot work (no network, no channel with uplink/downlink, or proxy to the phone app).
- `/mqtt` command with the full MQTT details. The firmware does not report the broker connection itself, so packets received via MQTT are shown as the proof that it works.

## [1.4.9] - 2026-10-23

**New**
- Search in the node list (Ctrl+N): press `/` or `Ctrl+F` to filter by name, short name, earlier names, !id or hardware model. Case and Polish diacritics are ignored. `Esc` clears the search.

**Fixes**
- The node list hint bar no longer gets cut off in narrower terminals.

## [1.4.8] - 2026-10-23

First public release.
