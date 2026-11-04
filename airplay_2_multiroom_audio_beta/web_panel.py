#!/usr/bin/env python3
"""
web_panel.py -- the add-on's web panel, shown in Home Assistant's sidebar
through Ingress: sound cards, remap sinks (with their system names) and
whether each zone is running. Managed sinks can be renamed.

  GET  /            the page
  GET  /api/state   the same data as JSON
  POST /api/apply   {"names": {"<option index>": "<new name>"}}: saves the
                    remap_sinks option through the Supervisor (so the
                    Configuration tab matches), then runs remap_sinks.py --load
                    and returns its log lines

Python standard library only. Runs in the host network namespace even when
ap2_ip is set, so Ingress can reach it; it only talks to PulseAudio through
its socket, which works from either namespace.

Only Home Assistant's Ingress proxy (172.30.32.2) and localhost may connect:
the add-on uses host networking, so the port is also open on the LAN.
"""

import html
import json
import logging
import os
import re
import subprocess
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import remap_sinks as rs

PORT = int(os.environ.get("WEB_PANEL_PORT", "8285"))
ALLOWED = set(os.environ.get("WEB_PANEL_ALLOWED", "172.30.32.2,127.0.0.1,::1").split(","))
OPTIONS_FILE = Path(os.environ.get("RS_OPTIONS_FILE", "/data/options.json"))
SUPERVISOR_URL = os.environ.get("SUPERVISOR_URL", "http://supervisor")
REMAP_SINKS = os.environ.get("REMAP_SINKS_SCRIPT", "/usr/sbin/remap_sinks.py")

log = logging.getLogger("web_panel")

PAIR_LABEL = {"front": "Front L/R", "rear": "Rear L/R", "side": "Side L/R",
              "center_lfe": "Center / LFE", "stereo": "Stereo"}
PAIR_NEEDS = {"front": {"front-left", "front-right"}, "rear": {"rear-left", "rear-right"},
              "side": {"side-left", "side-right"}, "center_lfe": {"front-center", "lfe"}}


# -- data ---------------------------------------------------------------
def list_sinks() -> list:
    """[{name, description, driver, channel_map}] for every sink."""
    try:
        res = subprocess.run(["pactl", "list", "sinks"], capture_output=True,
                             text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return []
    sinks, cur = [], None
    for line in res.stdout.splitlines() if res.returncode == 0 else []:
        if line.startswith("Sink #"):
            cur = {}
            sinks.append(cur)
            continue
        s = line.strip()
        if cur is None or ":" not in s:
            continue
        key, val = s.split(":", 1)
        if key in ("Name", "Description", "Driver", "Channel Map"):
            cur[key.lower().replace(" ", "_")] = val.strip()
    return sinks


def pair_of(args: str):
    """The channels choice a remap sink's arguments correspond to, or None."""
    _, chans = rs.claim(args)
    for pair, needs in PAIR_NEEDS.items():
        if chans == needs:
            return pair
    return None


def running_zones() -> set:
    """Sink names that have a shairport-sync running, from /proc."""
    names = set()
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            cmd = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode()
        except OSError:
            continue
        if "shairport-sync" in cmd:
            for m in re.finditer(r"([^\s/]+)-ap2\.conf", cmd):
                names.add(m.group(1))
    return names


def state() -> dict:
    sinks = list_sinks()
    descs = {s["name"]: s.get("description", "") for s in sinks if "name" in s}
    loaded = rs.loaded_remap_modules() or []
    running = running_zones()

    configured = {}
    for index, entry in enumerate(rs.load_options()):
        if not isinstance(entry, dict):
            continue
        master = str(entry.get("master", "")).strip()
        pair = str(entry.get("channels", "")).strip()
        name = str(entry.get("name", "") or "").strip() or rs.system_name(master, pair, descs)
        if name:
            configured[name] = {"master": master, "pair": pair, "index": index}

    remaps, used = [], {}
    for args in loaded:
        name = rs.sink_name_of(args)
        master = rs.arg_of(args, "master") or ""
        pair = pair_of(args)
        used.setdefault(master, set()).update(rs.claim(args)[1])
        remaps.append({
            "name": name,
            "system_name": rs.system_name(master, pair, descs) if pair else None,
            "master": master,
            "master_description": descs.get(master, ""),
            "pair": pair,
            "channels": PAIR_LABEL.get(pair, rs.arg_of(args, "master_channel_map") or "?"),
            "managed": name in configured,
            "index": configured[name]["index"] if name in configured else None,
            "loaded": True,
            "zone_running": name in running,
        })
    for name, c in configured.items():
        if not any(r["name"] == name for r in remaps):
            remaps.append({
                "name": name,
                "system_name": rs.system_name(c["master"], c["pair"], descs),
                "master": c["master"], "master_description": descs.get(c["master"], ""),
                "pair": c["pair"], "channels": PAIR_LABEL.get(c["pair"], c["pair"]),
                "managed": True, "index": c["index"], "loaded": False, "zone_running": False,
            })

    # Option order, then unmanaged sinks, so a renamed sink stays in place.
    remaps.sort(key=lambda r: (r["index"] is None, r["index"] or 0, r["name"] or ""))

    cards = []
    for s in sinks:
        if "alsa" not in s.get("driver", "") or "name" not in s:
            continue
        chans = {c.strip() for c in s.get("channel_map", "").split(",") if c.strip()}
        taken = used.get(s["name"], set())
        pairs = [p for p, needs in PAIR_NEEDS.items() if needs <= chans]
        if not pairs and chans:
            pairs = ["stereo"]
        cards.append({
            "name": s["name"], "description": s.get("description", ""),
            "used": [p for p in pairs if (PAIR_NEEDS.get(p) or chans) & taken],
            "free": [p for p in pairs if not ((PAIR_NEEDS.get(p) or chans) & taken)],
        })
    return {"cards": cards, "remap_sinks": remaps}


# -- apply ----------------------------------------------------------------
class ApplyError(Exception):
    pass


def save_options(options: dict) -> None:
    """Saves the add-on's whole options through the Supervisor (validated
    against the schema there), then writes /data/options.json, which the
    Supervisor otherwise only rewrites when the add-on starts."""
    token = os.environ.get("SUPERVISOR_TOKEN", "")
    if not token:
        raise ApplyError("no SUPERVISOR_TOKEN (hassio_api missing from config.yaml?)")
    req = urllib.request.Request(
        f"{SUPERVISOR_URL}/addons/self/options",
        data=json.dumps({"options": options}).encode(), method="POST",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            body = json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read() or b"{}")
        except ValueError:
            body = {}
        raise ApplyError(f"Supervisor refused the options: {body.get('message') or e}")
    except (OSError, ValueError) as e:
        raise ApplyError(f"cannot reach the Supervisor: {e}")
    if body.get("result") != "ok":
        raise ApplyError(f"Supervisor refused the options: {body.get('message') or body}")
    tmp = OPTIONS_FILE.with_name(OPTIONS_FILE.name + ".tmp")
    tmp.write_text(json.dumps(options))
    os.replace(tmp, OPTIONS_FILE)


def apply_names(names: dict) -> list:
    """Renames option entries ({index: new name}), saves, applies. Returns
    the log lines to show."""
    try:
        options = json.loads(OPTIONS_FILE.read_text())
    except (OSError, ValueError) as e:
        raise ApplyError(f"cannot read {OPTIONS_FILE}: {e}")
    entries = options.get("remap_sinks") or []
    descs = {s["name"]: s.get("description", "") for s in list_sinks() if "name" in s}

    lines = []
    for key, new in names.items():
        try:
            i = int(key)
            entry = entries[i]
        except (ValueError, IndexError, TypeError):
            raise ApplyError(f"no remap_sinks entry #{key}; reload the page")
        new = str(new).strip()
        if not rs.NAME_RE.match(new):
            raise ApplyError(f"'{new}': letters, digits, _ . - only")
        old = entry.get("name") or rs.system_name(entry.get("master", ""),
                                                   entry.get("channels", ""), descs)
        if new != old:
            entry["name"] = new
            lines.append(f"rename {old} -> {new}")
    if not lines:
        return ["nothing changed"]

    resolved = [e.get("name") or rs.system_name(e.get("master", ""), e.get("channels", ""), descs)
                for e in entries]
    dupes = {n for n in resolved if n and resolved.count(n) > 1}
    if dupes:
        raise ApplyError(f"name used twice: {', '.join(sorted(dupes))}")

    save_options(options)
    lines.append("options saved")
    try:
        res = subprocess.run(["python3", "-I", REMAP_SINKS, "--load"], capture_output=True,
                             text=True, timeout=90)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise ApplyError(f"options saved, but applying failed: {e}")
    lines += [l for l in res.stdout.splitlines() + res.stderr.splitlines()
              if not l.startswith(("[remap_sinks] +", "[remap_sinks] -", "[remap_sinks] @",
                                   "[remap_sinks]  "))]
    return lines


# -- page -----------------------------------------------------------------
CSS = """
body{margin:0;background:#111318;color:#e6e8eb;font:14px system-ui,sans-serif}
header{padding:20px 32px;border-bottom:1px solid #2a2f38}
header b{font-size:18px;font-weight:600}
main{max-width:1280px;margin:0 auto;padding:28px 32px;display:flex;flex-direction:column;gap:28px}
h1,h2{margin:0;font-weight:600}h1{font-size:22px}h2{font-size:20px}
p.sub{margin:4px 0 0;color:#9aa3ae}
.cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:16px;margin-top:14px}
.card{border:1px solid #2a2f38;border-radius:12px;background:#1a1d24;padding:16px;display:flex;flex-direction:column;gap:8px}
.mono{font-family:ui-monospace,monospace;font-size:12px;color:#9aa3ae;overflow-wrap:anywhere}
.box{overflow-x:auto;border:1px solid #2a2f38;border-radius:12px;background:#1a1d24;margin-top:14px}
table{width:100%;min-width:900px;border-collapse:collapse}
th{text-align:left;color:#9aa3ae;font-size:12px;text-transform:uppercase;letter-spacing:.04em;padding:12px 16px;font-weight:600}
td{padding:12px 16px;border-top:1px solid #2a2f38;color:#c9d1db}
td.sys{white-space:nowrap}
td.name{font-family:ui-monospace,monospace;font-size:13px;color:#e6e8eb}
button.copy{width:32px;height:32px;border:1px solid #353b46;border-radius:6px;background:transparent;color:#b8c0ca;cursor:pointer;vertical-align:middle;margin-left:6px}
.warn{color:#f0b26a}
input.nm{width:100%;min-height:40px;padding:0 10px;border-radius:8px;border:1px solid #353b46;background:#111318;color:#e6e8eb;font:13px ui-monospace,monospace;box-sizing:border-box}
input.nm.changed{border-color:#4a6a96}
.bar{display:flex;flex-wrap:wrap;align-items:center;justify-content:space-between;gap:12px;margin-top:14px}
.btn{min-height:44px;padding:0 18px;border-radius:8px;font:600 14px system-ui,sans-serif;cursor:pointer}
.btn.pri{border:0;background:#3d8bfd;color:#0b1220}.btn.sec{border:1px solid #353b46;background:transparent;color:#e6e8eb;font-weight:500}
.btn:disabled{opacity:.5;cursor:default}
pre#result{margin:14px 0 0;border:1px solid #2a2f38;border-radius:12px;background:#0d0f13;padding:14px 16px;font:12px/1.7 ui-monospace,monospace;color:#b8c0ca;white-space:pre-wrap}
pre#result.err{color:#f0b26a}
"""

COPY_ICON = ('<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" '
             'stroke-width="2" aria-hidden="true"><rect x="9" y="9" width="12" height="12" rx="2">'
             '</rect><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"></path></svg>')

SCRIPT = """
const inputs = [...document.querySelectorAll('input.nm')];
const applyBtn = document.getElementById('apply'), discardBtn = document.getElementById('discard');
const changed = () => inputs.filter(i => i.value.trim() !== i.defaultValue);
const refresh = () => { inputs.forEach(i => i.classList.toggle('changed', i.value.trim() !== i.defaultValue));
  const n = changed().length; if (applyBtn) { applyBtn.disabled = !n; discardBtn.disabled = !n; } };
inputs.forEach(i => i.addEventListener('input', refresh));
if (discardBtn) discardBtn.addEventListener('click', () => { inputs.forEach(i => i.value = i.defaultValue); refresh(); });
if (applyBtn) applyBtn.addEventListener('click', async () => {
  const names = {}; changed().forEach(i => names[i.dataset.index] = i.value.trim());
  const out = document.getElementById('result');
  applyBtn.disabled = discardBtn.disabled = true; out.hidden = false; out.className = ''; out.textContent = 'Applying...';
  try {
    const r = await fetch('api/apply', {method: 'POST', headers: {'Content-Type': 'application/json'},
                                        body: JSON.stringify({names})});
    const j = await r.json();
    out.textContent = (j.lines || []).join('\\n') + (j.error ? '\\n' + j.error : '');
    if (!j.ok) { out.className = 'err'; refresh(); return; }
    out.textContent += '\\n\\nReloading in 3 s...'; setTimeout(() => location.reload(), 3000);
  } catch (e) { out.className = 'err'; out.textContent = 'Request failed: ' + e; refresh(); }
});
refresh();
document.querySelectorAll('button.copy').forEach(b => b.addEventListener('click', () => {
  const t = b.dataset.text;
  (navigator.clipboard ? navigator.clipboard.writeText(t) : Promise.reject())
    .catch(() => { const r = document.createRange(); r.selectNode(b.previousElementSibling);
                   getSelection().removeAllRanges(); getSelection().addRange(r); });
}));
"""


def render(st: dict) -> str:
    e = html.escape
    cards = "".join(
        f'<div class="card"><div style="font-weight:600">{e(c["description"])}</div>'
        f'<div class="mono">{e(c["name"])}</div>'
        f'<div>In use: {e(", ".join(PAIR_LABEL[p] for p in c["used"]) or "none")}'
        f' — Free: {e(", ".join(PAIR_LABEL[p] for p in c["free"]) or "none")}</div></div>'
        for c in st["cards"])

    rows = []
    for r in st["remap_sinks"]:
        sysn = (f'<span class="mono">{e(r["system_name"])}</span>'
                f'<button class="copy" aria-label="Copy system name" data-text="{e(r["system_name"])}">'
                f'{COPY_ICON}</button>') if r["system_name"] else '<span class="mono">—</span>'
        if not r["loaded"]:
            st_txt = '<span class="warn">Configured, not loaded (see log)</span>'
        else:
            st_txt = ("Zone running" if r["zone_running"] else "No zone running") + \
                     ("" if r["managed"] else " · unmanaged")
        if r["index"] is not None:
            name_cell = (f'<td><input class="nm" data-index="{r["index"]}" value="{e(r["name"])}" '
                         f'aria-label="Sink name for {e(r["system_name"] or r["name"])}"></td>')
        else:
            name_cell = f'<td class="name">{e(r["name"] or "?")}</td>'
        rows.append(f'<tr>{name_cell}<td class="sys">{sysn}</td>'
                    f'<td>{e(r["master_description"] or r["master"])}</td>'
                    f'<td>{e(r["channels"])}</td><td>{st_txt}</td></tr>')

    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>AirPlay 2 Multiroom Audio</title><style>{CSS}</style></head><body>
<header><b>AirPlay 2 Multiroom Audio</b></header>
<main>
<section><h1>Sound cards</h1><p class="sub">Free pairs can become new zones.</p>
<div class="cards">{cards or '<p class="sub">No sound cards found.</p>'}</div></section>
<section><h2>Remap sinks</h2><p class="sub">Rename a managed sink and Apply. The system name stays as a reference.</p>
<div class="box"><table><thead><tr><th>Sink name</th><th>System name</th><th>Card</th>
<th>Channels</th><th>State</th></tr></thead><tbody>{''.join(rows)}</tbody></table></div>
<div class="bar"><p class="sub" style="margin:0">Renaming a sink renames its zone, so Music Assistant sees it as a new player.</p>
<div style="display:flex;gap:10px"><button class="btn sec" id="discard" disabled>Discard changes</button>
<button class="btn pri" id="apply" disabled>Apply</button></div></div>
<pre id="result" hidden></pre></section>
</main><script>{SCRIPT}</script></body></html>"""


# -- server ---------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, body: str, ctype: str):
        data = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        ip = self.client_address[0]
        if ip not in ALLOWED:
            log.warning("refused %s from %s", self.path, ip)
            self._send(403, "Forbidden\n", "text/plain")
            return
        # Ingress forwards the path after its own prefix, so "/" and "" are
        # the page; every link the page uses is relative.
        path = self.path.split("?", 1)[0].rstrip("/")
        try:
            if path in ("", "/index.html"):
                self._send(200, render(state()), "text/html; charset=utf-8")
            elif path == "/api/state":
                self._send(200, json.dumps(state(), indent=2), "application/json")
            else:
                self._send(404, "Not found\n", "text/plain")
        except Exception:  # keep serving; the traceback goes to the log
            log.exception("error serving %s", self.path)
            self._send(500, "Internal error, see web_panel.log\n", "text/plain")

    def do_POST(self):
        ip = self.client_address[0]
        if ip not in ALLOWED:
            log.warning("refused POST %s from %s", self.path, ip)
            self._send(403, "Forbidden\n", "text/plain")
            return
        if self.path.split("?", 1)[0].rstrip("/") != "/api/apply":
            self._send(404, "Not found\n", "text/plain")
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            names = json.loads(self.rfile.read(min(length, 65536)) or b"{}").get("names") or {}
            if not isinstance(names, dict):
                raise ApplyError("bad request")
            log.info("apply requested: %s", names)
            lines = apply_names(names)
            for l in lines:
                log.info("apply: %s", l)
            result = {"ok": True, "lines": lines}
        except ApplyError as e:
            log.warning("apply failed: %s", e)
            result = {"ok": False, "lines": [], "error": str(e)}
        except Exception as e:  # keep serving; the traceback goes to the log
            log.exception("apply crashed")
            result = {"ok": False, "lines": [], "error": f"internal error: {e}"}
        self._send(200, json.dumps(result), "application/json")

    def log_message(self, fmt, *args):
        log.info("%s %s", self.client_address[0], fmt % args)


def main():
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    log.info("listening on port %d, allowing %s", PORT, ", ".join(sorted(ALLOWED)))
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
