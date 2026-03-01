#!/usr/bin/env python3
"""
Aura Music Player — v2
Reads audio from music/ folder next to this file.
Requires: pip install Pillow
Run:      python3 server.py  then open http://localhost:8765
"""

import json, os, re, io, base64, struct, mimetypes, threading
from pathlib import Path
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn

class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True

from urllib.parse import urlparse, unquote
from PIL import Image

SCRIPT_DIR = Path(__file__).resolve().parent
MUSIC_DIR  = Path(os.environ.get("MUSIC_DIR", SCRIPT_DIR / "music"))
MUSIC_DIR.mkdir(exist_ok=True)
AUDIO_EXTS = {".mp3", ".flac", ".m4a", ".ogg", ".oga", ".wav", ".aac"}

# ─── ID3 (MP3) ────────────────────────────────────────────────────────────────

def _decode_text(data):
    if not data: return ""
    enc, payload = data[0], data[1:]
    try:
        if enc == 0: return payload.rstrip(b"\x00").decode("latin-1", errors="replace")
        if enc == 1: return payload.rstrip(b"\x00").decode("utf-16", errors="replace")
        if enc == 2: return payload.rstrip(b"\x00").decode("utf-16-be", errors="replace")
        if enc == 3: return payload.rstrip(b"\x00").decode("utf-8", errors="replace")
    except: pass
    return payload.decode("utf-8", errors="replace").strip("\x00")

def _parse_apic(data):
    try:
        enc = data[0]; rest = data[1:]
        idx = rest.find(b"\x00")
        if idx < 0: return None
        rest = rest[idx+1+1:]
        null = b"\x00\x00" if enc in (1,2) else b"\x00"
        di = rest.find(null)
        return rest[di+len(null):]
    except: return None

def _parse_uslt(data):
    try:
        enc = data[0]; rest = data[4:]
        null = b"\x00\x00" if enc in (1,2) else b"\x00"
        idx = rest.find(null)
        text = rest[idx+len(null):]
        if enc == 0: return text.decode("latin-1", errors="replace")
        if enc == 1: return text.decode("utf-16", errors="replace")
        if enc == 2: return text.decode("utf-16-be", errors="replace")
        return text.decode("utf-8", errors="replace")
    except: return ""

def parse_id3(data):
    r = {}
    if len(data) < 10 or data[:3] != b"ID3": return r
    major = data[3]; flags = data[5]
    size = ((data[6]&0x7f)<<21|(data[7]&0x7f)<<14|(data[8]&0x7f)<<7|(data[9]&0x7f))
    pos = 10
    if flags & 0x40:
        ext = struct.unpack(">I", data[10:14])[0]; pos += ext
    while pos < 10 + size - 10:
        if major in (3,4):
            if pos+10 > len(data): break
            fid = data[pos:pos+4].decode("latin-1", errors="ignore").strip("\x00")
            fsz = struct.unpack(">I", data[pos+4:pos+8])[0]; pos += 10
        elif major == 2:
            if pos+6 > len(data): break
            fid = data[pos:pos+3].decode("latin-1", errors="ignore").strip("\x00")
            fsz = struct.unpack(">I", b"\x00"+data[pos+3:pos+6])[0]; pos += 6
        else: break
        if fsz <= 0 or pos+fsz > len(data): break
        fd = data[pos:pos+fsz]; pos += fsz
        if not fid or fid[0] not in "ABCDEFGHIJKLMNOPQRSTUVWXYZ": continue
        if   fid in ("TIT2","TT2"):  r["title"]  = _decode_text(fd)
        elif fid in ("TPE1","TP1"):  r["artist"] = _decode_text(fd)
        elif fid in ("TALB","TAL"):  r["album"]  = _decode_text(fd)
        elif fid == "APIC":          r["art"]    = _parse_apic(fd)
        elif fid in ("USLT","ULT"):  r["lyrics"] = _parse_uslt(fd)
        elif fid == "TXXX":
            txt = _decode_text(fd[1:]) if fd else ""
            if "\x00" in txt:
                desc, val = txt.split("\x00",1)
                if desc.upper() in ("LYRICS","LYRICSALL","UNSYNCED LYRICS"):
                    r["lyrics"] = val
    return r

# ─── FLAC / Vorbis ────────────────────────────────────────────────────────────

def _vorbis_comments(data):
    r = {}
    try:
        vl = struct.unpack("<I", data[:4])[0]; pos = 4+vl
        cnt = struct.unpack("<I", data[pos:pos+4])[0]; pos += 4
        lyr = []
        for _ in range(cnt):
            cl = struct.unpack("<I", data[pos:pos+4])[0]; pos += 4
            c = data[pos:pos+cl].decode("utf-8", errors="replace"); pos += cl
            if "=" in c:
                k, v = c.split("=",1); k = k.upper()
                if k == "TITLE":  r["title"]  = v
                elif k == "ARTIST": r["artist"] = v
                elif k == "ALBUM":  r["album"]  = v
                elif k in ("LYRICS","LYRICSALL","UNSYNCEDLYRICS","UNSYNCED LYRICS"):
                    lyr.append(v)
        if lyr: r["lyrics"] = max(lyr, key=len)
    except: pass
    return r

def _flac_picture(data):
    try:
        ml = struct.unpack(">I", data[4:8])[0]
        dl = struct.unpack(">I", data[8+ml:12+ml])[0]
        off = 12+ml+dl+16
        il = struct.unpack(">I", data[off:off+4])[0]
        return data[off+4:off+4+il]
    except: return None

def parse_flac(data):
    r = {}
    if data[:4] != b"fLaC": return r
    pos = 4
    while pos < len(data):
        if pos+4 > len(data): break
        bt = data[pos]&0x7f; last = bool(data[pos]&0x80)
        bl = struct.unpack(">I", b"\x00"+data[pos+1:pos+4])[0]
        pos += 4; block = data[pos:pos+bl]; pos += bl
        if bt == 4: r.update(_vorbis_comments(block))
        elif bt == 6:
            p = _flac_picture(block)
            if p: r["art"] = p
        if last: break
    return r

def parse_ogg(data):
    r = {}
    try:
        pos = 0
        while pos < len(data)-7:
            if data[pos:pos+4] != b"OggS": pos += 1; continue
            segs = data[pos+26]; tbl = data[pos+27:pos+27+segs]
            dsz = sum(tbl); ds = pos+27+segs
            pd = data[ds:ds+dsz]; pos = ds+dsz
            if pd[:7] == b"\x03vorbis":
                r.update(_vorbis_comments(pd[7:])); break
    except: pass
    return r

def _mp4_find(data, path):
    parts = path.encode().split(b".")
    pos = 0
    for i, part in enumerate(parts):
        found = False
        while pos+8 <= len(data):
            sz = struct.unpack(">I", data[pos:pos+4])[0]
            nm = data[pos+4:pos+8]
            if sz < 8 or pos+sz > len(data): break
            if nm == part:
                if i == len(parts)-1: return data[pos+8:pos+sz]
                data = data[pos+8:pos+sz]; pos = 0; found = True; break
            pos += sz
        if not found: return None
    return None

def _mp4_text(data):
    pos = 0
    while pos+8 <= len(data):
        sz = struct.unpack(">I", data[pos:pos+4])[0]
        if sz<8: break
        if data[pos+4:pos+8]==b"data" and pos+16<=len(data):
            return data[pos+16:pos+sz].decode("utf-8", errors="replace")
        pos += sz
    return ""

def _mp4_art(data):
    pos = 0
    while pos+8 <= len(data):
        sz = struct.unpack(">I", data[pos:pos+4])[0]
        if sz<8: break
        if data[pos+4:pos+8]==b"data" and pos+16<=len(data):
            return data[pos+16:pos+sz]
        pos += sz
    return None

def parse_mp4(data):
    r = {}
    try:
        ilst = _mp4_find(data, b"moov.udta.meta.ilst")
        if not ilst: return r
        pos = 0
        while pos < len(ilst):
            if pos+8 > len(ilst): break
            sz = struct.unpack(">I", ilst[pos:pos+4])[0]
            nm = ilst[pos+4:pos+8]
            if sz<8 or pos+sz>len(ilst): break
            atom = ilst[pos+8:pos+sz]; pos += sz
            if nm == b"\xa9nam": r["title"]  = _mp4_text(atom)
            elif nm == b"\xa9ART": r["artist"] = _mp4_text(atom)
            elif nm == b"\xa9alb": r["album"]  = _mp4_text(atom)
            elif nm == b"covr":    a=_mp4_art(atom); r["art"]=a if a else r.get("art")
            elif nm in (b"\xa9lyr",):
                lyr = _mp4_text(atom)
                if lyr: r["lyrics"] = lyr
    except: pass
    return r

def parse_tags(path: Path) -> dict:
    sx = path.suffix.lower()
    try:
        with open(path, "rb") as f:
            data = f.read(min(12*1024*1024, path.stat().st_size))
    except: return {}
    if sx == ".mp3":            tags = parse_id3(data)
    elif sx == ".flac":         tags = parse_flac(data)
    elif sx in (".ogg",".oga"): tags = parse_ogg(data)
    elif sx in (".m4a",".mp4",".aac"): tags = parse_mp4(data)
    else: tags = {}
    tags.setdefault("title",  path.stem)
    tags.setdefault("artist", "Unknown Artist")
    tags.setdefault("album",  "")
    tags.setdefault("lyrics", "")
    return tags

# ─── Color ────────────────────────────────────────────────────────────────────

def dominant_color(img_bytes):
    try:
        img = Image.open(io.BytesIO(img_bytes)).convert("RGB")
        img.thumbnail((80, 80), Image.LANCZOS)
        from collections import Counter
        raw = img.tobytes()
        pixels = [(raw[i], raw[i+1], raw[i+2]) for i in range(0, len(raw), 3)]
        buckets = Counter((r >> 5, g >> 5, b >> 5) for r, g, b in pixels)
        colors = []
        for (rb, gb, bb), _ in buckets.most_common(60):
            r, g, b = rb*32+16, gb*32+16, bb*32+16
            br = (r*299 + g*587 + b*114) // 1000
            if br < 20 or br > 235: continue
            if not any(abs(r-ec[0])+abs(g-ec[1])+abs(b-ec[2]) < 70 for ec in colors):
                colors.append((r, g, b))
            if len(colors) >= 3: break
        if not colors: return None
        p = colors[0]
        def dk(c, f=0.18): return tuple(int(v*f) for v in c)
        def lt(c, f=1.9):  return tuple(min(255, int(v*f)) for v in c)
        bg  = dk(p)
        acc = lt(p)
        if (acc[0]*299 + acc[1]*587 + acc[2]*114) // 1000 < 90:
            acc = lt(acc, 2.2)
        return {
            "bg":  "#{:02x}{:02x}{:02x}".format(*bg),
            "acc": "#{:02x}{:02x}{:02x}".format(*acc),
            "mid": "#{:02x}{:02x}{:02x}".format(*tuple(int(v*0.35) for v in p)),
        }
    except:
        return None

# ─── Track store ──────────────────────────────────────────────────────────────

_lock   = threading.Lock()
_store  = {}
_order  = []

def _track_id(path: Path) -> str:
    import hashlib
    return hashlib.sha1(str(path).encode()).hexdigest()[:16]

def register(path: Path) -> str:
    tid = _track_id(path)
    with _lock:
        if tid in _store: return tid
    tags   = parse_tags(path)
    colors = dominant_color(tags["art"]) if tags.get("art") else None
    with _lock:
        _store[tid] = {"path": path, "tags": tags, "colors": colors}
        if tid not in _order: _order.append(tid)
    return tid

def track_json(tid) -> dict:
    with _lock:
        t = _store[tid]
    tags = t["tags"]
    return {
        "id":       tid,
        "title":    tags.get("title","Unknown"),
        "artist":   tags.get("artist","Unknown Artist"),
        "album":    tags.get("album",""),
        "fmt":      t["path"].suffix.upper().strip("."),
        "has_lrc":  bool(tags.get("lyrics","").strip()),
        "art":      f"/art/{tid}" if tags.get("art") else None,
        "colors":   t["colors"],
    }

def scan():
    n = 0
    if not MUSIC_DIR.exists():
        MUSIC_DIR.mkdir(parents=True, exist_ok=True)
        return 0
    files = sorted(p for p in MUSIC_DIR.iterdir()
                   if p.is_file() and p.suffix.lower() in AUDIO_EXTS)
    for p in files:
        try:
            register(p)
            n += 1
            print(f"    + {p.name}")
        except Exception as e:
            print(f"    ! skip {p.name}: {e}")
    return n

# ─── HTML ─────────────────────────────────────────────────────────────────────

PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<title>Aura</title>
<style>
@import url('https://fonts.googleapis.com/css2?family=Figtree:wght@400;500;600;700;800;900&family=DM+Mono:wght@400;500&display=swap');
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0;-webkit-tap-highlight-color:transparent}
:root{
  --bg:#080810;
  --s1:rgba(255,255,255,.04);
  --s2:rgba(255,255,255,.08);
  --s3:rgba(255,255,255,.13);
  --br:rgba(255,255,255,.06);
  --br2:rgba(255,255,255,.10);
  --t1:#f0f0f5;
  --t2:rgba(240,240,245,.52);
  --t3:rgba(240,240,245,.20);
  --t4:rgba(240,240,245,.10);
  --acc:#e8445a;
  --acc2:#ff6b80;
  --radius:16px;
  --bar-h:90px;
  --tab-h:50px;
}
html,body{
  height:100%;background:var(--bg);color:var(--t1);
  font-family:'Figtree',sans-serif;overflow:hidden;
  -webkit-font-smoothing:antialiased;-moz-osx-font-smoothing:grayscale;
}

/* ── Backdrop ── */
#backdrop{
  position:fixed;inset:0;z-index:0;overflow:hidden;
  background:var(--bg);
  transition:background 1.6s ease;
}
#backdrop-img{
  position:absolute;inset:-30%;
  background-size:cover;background-position:center;
  filter:blur(90px) saturate(1.6) brightness(0.35);
  opacity:0;transition:opacity 1.8s ease, background-image 0s;
  transform:scale(1.05);
}
#backdrop-img.vis{opacity:1}
#backdrop-scrim{
  position:absolute;inset:0;
  background:linear-gradient(to bottom,
    rgba(0,0,0,.55) 0%,
    rgba(0,0,0,.2) 40%,
    rgba(0,0,0,.5) 100%);
}

/* ── App shell ── */
.app{position:relative;z-index:1;height:100vh;height:100dvh;display:flex;flex-direction:column;overflow:hidden}

/* ── Bottom tab bar ── */
.tabbar{
  position:fixed;bottom:0;left:0;right:0;z-index:200;
  height:calc(var(--tab-h) + env(safe-area-inset-bottom,0px));
  padding-bottom:env(safe-area-inset-bottom,0px);
  background:rgba(8,8,16,.72);
  backdrop-filter:blur(40px) saturate(1.8);
  -webkit-backdrop-filter:blur(40px) saturate(1.8);
  border-top:1px solid var(--br);
  display:flex;align-items:stretch;
}
.tbtn{
  flex:1;display:flex;flex-direction:column;align-items:center;justify-content:center;
  gap:3px;background:none;border:none;cursor:pointer;color:var(--t3);
  font:500 10px 'Figtree',sans-serif;letter-spacing:.06em;text-transform:uppercase;
  padding:8px 4px;transition:color .2s;
}
.tbtn svg{transition:transform .2s, opacity .2s}
.tbtn.on{color:var(--t1)}
.tbtn.on svg{transform:scale(1.1)}
.tbtn-dot{
  width:4px;height:4px;border-radius:50%;background:var(--acc);
  margin-top:1px;opacity:0;transition:opacity .2s;
}
.tbtn.on .tbtn-dot{opacity:1}

/* ── Pages ── */
.pages{flex:1;position:relative;overflow:hidden}
.pg{
  position:absolute;inset:0;overflow-y:auto;overflow-x:hidden;
  -webkit-overflow-scrolling:touch;
  padding-bottom:calc(var(--tab-h) + env(safe-area-inset-bottom,0px) + 12px);
}
.pg.off{display:none}
*::-webkit-scrollbar{display:none}*{scrollbar-width:none}

/* ══════════════════════════════════════════
   LIBRARY PAGE
══════════════════════════════════════════ */
#plib{padding-top:0}
.lib-header{
  padding:max(52px,env(safe-area-inset-top,52px)) 20px 8px;
  position:sticky;top:0;z-index:10;
  background:linear-gradient(to bottom,rgba(8,8,16,.9) 70%,transparent);
  backdrop-filter:blur(20px);-webkit-backdrop-filter:blur(20px);
}
.lib-title{font-size:28px;font-weight:900;letter-spacing:-.03em;margin-bottom:14px}
.lib-search{
  display:flex;align-items:center;gap:10px;
  background:var(--s2);border:1px solid var(--br);border-radius:12px;
  padding:10px 14px;
}
.lib-search svg{color:var(--t3);flex-shrink:0}
.lib-search input{
  flex:1;background:none;border:none;outline:none;color:var(--t1);
  font:500 15px 'Figtree',sans-serif;
}
.lib-search input::placeholder{color:var(--t3)}
.lib-list{padding:8px 0}

/* Track row */
.row{
  display:flex;align-items:center;gap:14px;
  padding:10px 20px;cursor:pointer;
  transition:background .12s;
  position:relative;
}
.row::after{
  content:'';position:absolute;bottom:0;left:86px;right:20px;
  height:1px;background:var(--br);
}
.row:last-child::after{display:none}
.row:active{background:var(--s1)}
.row.now .rtit{color:var(--acc)}

.thumb{
  width:52px;height:52px;border-radius:11px;
  background:var(--s2);flex-shrink:0;
  display:flex;align-items:center;justify-content:center;
  font-size:20px;overflow:hidden;
  box-shadow:0 4px 16px rgba(0,0,0,.4);
  position:relative;
}
.thumb img{width:100%;height:100%;object-fit:cover}
.thumb-play{
  position:absolute;inset:0;background:rgba(0,0,0,.5);
  display:flex;align-items:center;justify-content:center;
  opacity:0;transition:opacity .15s;border-radius:11px;
}
.row:active .thumb-play{opacity:1}

.rinfo{flex:1;min-width:0}
.rtit{
  font-size:15px;font-weight:600;
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis;
  margin-bottom:3px;
}
.rsub{
  font-size:13px;color:var(--t2);
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis;
}
.rright{display:flex;flex-direction:column;align-items:flex-end;gap:5px;flex-shrink:0}
.fmt{
  padding:2px 7px;border-radius:5px;
  font-size:9px;font-weight:800;letter-spacing:.08em;text-transform:uppercase;
  font-family:'DM Mono',monospace;
}
.fmt.flac{background:rgba(96,165,250,.14);color:#60a5fa}
.fmt.mp3 {background:rgba(251,191,36,.12);color:#fbbf24}
.fmt.ogg,.fmt.oga{background:rgba(167,139,250,.12);color:#a78bfa}
.fmt.wav {background:rgba(52,211,153,.12);color:#34d399}
.fmt.aac {background:rgba(251,146,60,.12);color:#fb923c}
.fmt.m4a {background:rgba(244,114,182,.12);color:#f472b6}
.lrcbadge{
  padding:2px 7px;border-radius:5px;
  font-size:9px;font-weight:800;letter-spacing:.08em;text-transform:uppercase;
  background:rgba(74,222,128,.10);color:#4ade80;font-family:'DM Mono',monospace;
}
.bars{display:flex;gap:2px;align-items:flex-end;height:14px}
.bars span{
  display:block;width:3px;background:var(--acc);border-radius:2px;
  animation:ba .65s ease-in-out infinite alternate;
}
.bars span:nth-child(1){height:6px}
.bars span:nth-child(2){height:12px;animation-delay:.12s}
.bars span:nth-child(3){height:8px;animation-delay:.25s}
.bars.paused span{animation-play-state:paused}
@keyframes ba{from{transform:scaleY(.25)}to{transform:scaleY(1)}}

/* Empty state */
.empty{padding:60px 24px;text-align:center;color:var(--t3)}
.empty-icon{font-size:52px;display:block;margin-bottom:16px;opacity:.6}
.empty h3{font-size:18px;font-weight:700;color:var(--t2);margin-bottom:8px}
.empty p{font-size:14px;line-height:1.7;color:var(--t3)}
.empty code{
  display:inline-block;background:var(--s2);border:1px solid var(--br);
  border-radius:6px;padding:2px 8px;font-family:'DM Mono',monospace;font-size:12px;
  color:var(--t2);margin-top:8px;
}

/* ══════════════════════════════════════════
   NOW PLAYING PAGE
══════════════════════════════════════════ */
#pnow{
  padding-top:env(safe-area-inset-top,0px);
}
.np-page{
  min-height:100%;display:flex;flex-direction:column;
}

/* ── Cover view ── */
.np-cover-view{
  flex:1;
  display:flex;flex-direction:column;align-items:center;justify-content:center;
  padding:max(20px,env(safe-area-inset-top,20px)) 24px max(20px,env(safe-area-inset-bottom,20px));
  transition:opacity .35s ease, transform .35s cubic-bezier(.4,0,.2,1);
  overflow:hidden;
}
.np-cover-view.lyr-on{
  opacity:0;
  pointer-events:none;
  max-height:0 !important;
  padding:0 !important;
  transform:scale(.96) translateY(-12px);
}
.np-cover-view.lyr-off{
  opacity:1;
  pointer-events:auto;
  max-height:9999px;
  transform:none;
}
.np-art-wrap{
  position:relative;
  width:min(300px,80vw);height:min(300px,80vw);
  margin-bottom:28px;
}
.np-art{
  width:100%;height:100%;border-radius:20px;
  background:var(--s2);overflow:hidden;
  display:flex;align-items:center;justify-content:center;
  font-size:80px;
  box-shadow:0 28px 80px rgba(0,0,0,.7), 0 8px 24px rgba(0,0,0,.4);
  transition:transform .4s cubic-bezier(.34,1.56,.64,1), box-shadow .4s ease;
}
.np-art.playing{
  animation:artfloat 5s ease-in-out infinite alternate;
}
@keyframes artfloat{
  from{transform:translateY(0) scale(1)}
  to  {transform:translateY(-8px) scale(1.02)}
}
.np-art img{width:100%;height:100%;object-fit:cover}

/* floating lyrics button */
.lyr-fab{
  position:fixed;
  bottom:calc(var(--tab-h) + env(safe-area-inset-bottom,0px) + 16px);
  right:20px;
  z-index:190;
  display:none; /* shown only on now-playing page via JS */
  align-items:center;gap:7px;
  padding:10px 16px;
  border-radius:99px;
  background:rgba(28,28,40,.85);
  border:1px solid var(--br2);
  color:var(--t2);
  font:600 13px 'Figtree',sans-serif;
  cursor:pointer;
  backdrop-filter:blur(30px);-webkit-backdrop-filter:blur(30px);
  box-shadow:0 4px 24px rgba(0,0,0,.45);
  transition:background .2s, color .2s, transform .15s, box-shadow .2s;
}
.lyr-fab:active{transform:scale(.94)}
.lyr-fab.on{
  background:var(--acc);
  border-color:var(--acc);
  color:#fff;
  box-shadow:0 4px 24px rgba(0,0,0,.45), 0 0 0 1px var(--acc);
}
.lyr-fab.show{display:flex}

.np-meta{text-align:center;width:100%;margin-bottom:20px}
.np-title{
  font-size:22px;font-weight:800;letter-spacing:-.03em;
  margin-bottom:4px;
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis;
}
.np-artist{font-size:16px;color:var(--t2);font-weight:500}
.np-album{font-size:12px;color:var(--t3);margin-top:3px;font-weight:500}
.np-fmt-badge{
  display:inline-block;margin-top:8px;
  padding:3px 9px;border-radius:6px;
  font-size:9px;font-weight:800;letter-spacing:.1em;text-transform:uppercase;
  font-family:'DM Mono',monospace;
}

/* ── Progress ── */
.np-progress{width:100%;padding:0 4px;margin-bottom:18px}
.np-track{
  position:relative;height:4px;background:var(--s3);
  border-radius:4px;cursor:pointer;margin-bottom:8px;
  overflow:hidden;
}
.np-track:before{
  content:'';position:absolute;inset:0;
  border-radius:4px;z-index:0;
}
.np-fill{
  position:absolute;left:0;top:0;height:100%;
  background:var(--t1);border-radius:4px;
  transition:width .1s linear;z-index:1;
  pointer-events:none;
}
.np-times{
  display:flex;justify-content:space-between;
  font:400 11px 'DM Mono',monospace;color:var(--t3);
}

/* ── Controls ── */
.np-controls{
  display:flex;align-items:center;justify-content:center;
  gap:4px;margin-bottom:20px;
}
.npc{
  background:none;border:none;color:var(--t1);cursor:pointer;
  padding:10px;border-radius:14px;
  display:flex;align-items:center;justify-content:center;
  transition:opacity .15s,transform .12s,background .15s;
}
.npc:active{opacity:.5;transform:scale(.88)}
.npc.sm{color:var(--t2)}
.npc.sm.on{color:var(--acc)}
.npc-play{
  width:62px;height:62px;border-radius:50%;
  background:var(--t1);color:var(--bg) !important;
  box-shadow:0 6px 28px rgba(0,0,0,.5);
  margin:0 12px;
}
.npc-play:active{transform:scale(.9) !important;opacity:1 !important}

/* ── Lyrics full view (swaps with cover) ── */
#lyr-inline{
  width:100%;
  transition:opacity .35s ease, transform .35s cubic-bezier(.4,0,.2,1);
}
#lyr-inline.hidden{
  opacity:0;pointer-events:none;
  transform:translateY(16px);
  position:absolute;visibility:hidden;height:0;overflow:hidden;
}
#lyr-inline.visible{
  opacity:1;pointer-events:auto;
  transform:none;
  position:relative;visibility:visible;
}

/* ══════════════════════════════════════════
   LYRICS RENDERER
══════════════════════════════════════════ */
.lyc{padding:0 24px}

/* Word-by-word line */
.wline{
  display:block;
  font-size:26px;font-weight:800;line-height:1.3;letter-spacing:-.025em;
  padding:9px 0;cursor:pointer;
  user-select:none;-webkit-user-select:none;
  transition:font-size .2s cubic-bezier(.4,0,.2,1);
}
.wline.r{text-align:right}
.wline.act{font-size:30px}
.wline.past{opacity:.18}
.wline.next{opacity:.32}
.wline .w{
  display:inline;
  position:relative;
  /* gradient fill technique */
  background-clip:text;-webkit-background-clip:text;
  -webkit-text-fill-color:transparent;
  background-image:linear-gradient(90deg,
    var(--t1) var(--wp,0%),
    rgba(240,240,245,.22) var(--wp,0%)
  );
  background-size:100% 100%;
  transition:none;
}
.wline.past .w{
  background-image:none;
  -webkit-text-fill-color:rgba(240,240,245,.18);
}
.wline.next .w{
  background-image:none;
  -webkit-text-fill-color:rgba(240,240,245,.32);
}
.wline .w::after{content:' '}

/* Standard LRC line */
.sline{
  display:block;
  font-size:26px;font-weight:800;line-height:1.3;letter-spacing:-.025em;
  padding:9px 0;cursor:pointer;
  user-select:none;-webkit-user-select:none;
  position:relative;overflow:hidden;
  transition:font-size .2s cubic-bezier(.4,0,.2,1);
}
.sline.r{text-align:right}
.sline.act{font-size:30px}

/* fill overlay for std lines */
.sline .sfill{
  position:absolute;inset:0;
  background:linear-gradient(90deg,
    var(--t1) var(--sp,0%),
    transparent var(--sp,0%)
  );
  -webkit-background-clip:text;background-clip:text;
  -webkit-text-fill-color:transparent;
  font-size:inherit;font-weight:inherit;line-height:inherit;
  letter-spacing:inherit;padding:9px 0;pointer-events:none;
  white-space:pre-wrap;
}
.sline .sbase{
  -webkit-text-fill-color:rgba(240,240,245,.22);
  color:rgba(240,240,245,.22);
}
.sline.past .sbase{-webkit-text-fill-color:rgba(240,240,245,.15);color:rgba(240,240,245,.15)}
.sline.past .sfill{display:none}
.sline.next .sbase{-webkit-text-fill-color:rgba(240,240,245,.32);color:rgba(240,240,245,.32)}

/* Instrumental dots */
.dots{display:flex;align-items:center;padding:12px 0;gap:6px}
.dots.r{justify-content:flex-end}
.dots span{
  display:inline-block;width:6px;height:6px;border-radius:50%;
  background:var(--t3);animation:dotpulse 1.4s ease-in-out infinite;
}
.dots span:nth-child(2){animation-delay:.2s}
.dots span:nth-child(3){animation-delay:.4s}
@keyframes dotpulse{
  0%,80%,100%{transform:scale(.45);opacity:.2}
  40%{transform:scale(1);opacity:.8}
}

.lyr-spacer-top{height:32px}
.lyr-spacer-bot{height:50vh}

/* No lyrics state */
.nolyr{
  padding:48px 0;text-align:center;color:var(--t3);
}
.nolyr-art{
  width:min(160px,50vw);height:min(160px,50vw);border-radius:16px;
  margin:0 auto 20px;overflow:hidden;
  display:flex;align-items:center;justify-content:center;
  font-size:48px;background:var(--s2);
  box-shadow:0 12px 40px rgba(0,0,0,.4);
}
.nolyr-art img{width:100%;height:100%;object-fit:cover}
.nolyr h3{font-size:17px;font-weight:700;color:var(--t2);margin-bottom:6px}
.nolyr p{font-size:13px;line-height:1.6}

/* ── Player mini bar (only on library page) ── */
.minibar{
  position:fixed;
  bottom:calc(var(--tab-h) + env(safe-area-inset-bottom,0px) + 10px);
  left:12px;right:12px;z-index:150;
  background:rgba(22,22,34,.88);
  backdrop-filter:blur(40px) saturate(1.8);
  -webkit-backdrop-filter:blur(40px) saturate(1.8);
  border:1px solid var(--br2);
  border-radius:18px;
  padding:10px 12px 10px 10px;
  display:flex;align-items:center;gap:10px;
  box-shadow:0 8px 40px rgba(0,0,0,.5);
  cursor:pointer;
  transition:transform .2s, opacity .3s;
  transform:translateY(0);
}
.minibar.hide{transform:translateY(120%);opacity:0;pointer-events:none}
.minibar-prog{
  position:absolute;bottom:0;left:0;height:2px;
  background:var(--acc);border-radius:0 0 18px 18px;
  pointer-events:none;transition:width .1s linear;
}
.mini-thumb{
  width:40px;height:40px;border-radius:10px;
  background:var(--s2);flex-shrink:0;overflow:hidden;
  display:flex;align-items:center;justify-content:center;font-size:16px;
}
.mini-thumb img{width:100%;height:100%;object-fit:cover}
.mini-info{flex:1;min-width:0}
.mini-title{font-size:14px;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.mini-artist{font-size:12px;color:var(--t2);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.mini-btns{display:flex;align-items:center;gap:2px}
.mbtn{
  background:none;border:none;color:var(--t1);cursor:pointer;
  padding:7px;border-radius:10px;
  display:flex;align-items:center;justify-content:center;
  transition:opacity .15s,transform .12s;
}
.mbtn:active{opacity:.5;transform:scale(.85)}
.mbtn-play{
  width:36px;height:36px;border-radius:50%;
  background:var(--t1);color:var(--bg) !important;
  box-shadow:0 3px 12px rgba(0,0,0,.4);
}
.mbtn-play:active{transform:scale(.88) !important}

/* Toast */
.toast{
  position:fixed;
  bottom:calc(var(--tab-h) + env(safe-area-inset-bottom,0px) + 72px);
  left:50%;transform:translateX(-50%) translateY(10px);
  background:rgba(20,20,30,.95);border:1px solid var(--br2);
  border-radius:24px;padding:10px 20px;
  font-size:13px;font-weight:600;
  backdrop-filter:blur(30px);
  opacity:0;pointer-events:none;
  transition:all .3s cubic-bezier(.34,1.56,.64,1);
  z-index:300;white-space:nowrap;
}
.toast.on{opacity:1;transform:translateX(-50%) translateY(0)}

/* Desktop tweaks */
@media(min-width:768px){
  .app{flex-direction:row}
  .tabbar{
    position:fixed;left:0;top:0;bottom:0;right:auto;
    width:220px;height:100%;
    flex-direction:column;align-items:stretch;
    border-top:none;border-right:1px solid var(--br);
    padding:max(28px,env(safe-area-inset-top,28px)) 12px 20px;
    gap:4px;
  }
  .tbtn{
    flex:none;flex-direction:row;gap:10px;
    justify-content:flex-start;padding:12px 16px;
    border-radius:12px;font-size:14px;letter-spacing:.01em;text-transform:none;
  }
  .tbtn.on{background:var(--s2)}
  .tbtn-dot{display:none}
  .pages{margin-left:220px}
  .minibar{left:232px;right:12px;bottom:12px}
  .np-art-wrap{width:min(340px,40vw);height:min(340px,40vw)}
  .np-cover-view{padding-top:40px}
  #plib .lib-header{padding-top:28px}
}
@media(min-width:1100px){
  .np-page{flex-direction:row;gap:0;align-items:flex-start}
  .np-left{
    width:380px;min-width:380px;
    position:sticky;top:0;
    display:flex;flex-direction:column;align-items:center;
    padding:max(24px,env(safe-area-inset-top,24px)) 32px 24px;
  }
  .np-right{
    flex:1;overflow-y:auto;
    padding:max(24px,env(safe-area-inset-top,24px)) 32px 24px 0;
  }
  .np-art-wrap{width:min(300px,28vw);height:min(300px,28vw)}
  .lyr-fab{display:none !important}
  .np-cover-view.lyr-on{
    opacity:1 !important;pointer-events:auto !important;
    max-height:9999px !important;padding:max(24px,env(safe-area-inset-top,24px)) 32px 24px !important;
    transform:none !important;
  }
  #lyr-inline.hidden{
    opacity:1 !important;pointer-events:auto !important;
    position:relative !important;visibility:visible !important;
    transform:none !important;height:auto !important;overflow:visible !important;
  }
}
</style>
</head>
<body>
<div id="backdrop">
  <div id="backdrop-img"></div>
  <div id="backdrop-scrim"></div>
</div>

<div class="app">

  <!-- Tab Bar -->
  <nav class="tabbar">
    <button class="tbtn on" id="tb-lib" onclick="showPage('lib')">
      <svg width="20" height="20" viewBox="0 0 24 24" fill="currentColor">
        <path d="M4 6h16v2H4zm0 5h16v2H4zm0 5h16v2H4z"/>
      </svg>
      <span>Library</span>
      <div class="tbtn-dot"></div>
    </button>
    <button class="tbtn" id="tb-now" onclick="showPage('now')">
      <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
        <circle cx="12" cy="12" r="3"/><circle cx="12" cy="12" r="9"/>
      </svg>
      <span>Now Playing</span>
      <div class="tbtn-dot"></div>
    </button>
  </nav>

  <!-- Pages -->
  <div class="pages">

    <!-- Library -->
    <div class="pg" id="plib">
      <div class="lib-header">
        <div class="lib-title">Library</div>
        <div class="lib-search">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5">
            <circle cx="11" cy="11" r="8"/><path d="m21 21-4.35-4.35"/>
          </svg>
          <input id="searchInput" type="text" placeholder="Search songs, artists…" oninput="filterLib()" autocomplete="off" spellcheck="false">
        </div>
      </div>
      <div class="lib-list" id="tlist"></div>
    </div>

    <!-- Now Playing -->
    <div class="pg off" id="pnow">
      <div class="np-page">

        <!-- Left / top: cover + controls -->
        <div class="np-left np-cover-view lyr-off" id="np-left">
          <div class="np-art-wrap">
            <div class="np-art" id="npArt">🎵</div>
          </div>

          <div class="np-meta">
            <div class="np-title" id="npTitle">Nothing playing</div>
            <div class="np-artist" id="npArtist">—</div>
            <div class="np-album" id="npAlbum"></div>
            <div id="npFmtBadge"></div>
          </div>

          <div class="np-progress" id="npProgress">
            <div class="np-track" id="npTrack" onmousedown="startSeek(event)" ontouchstart="startSeek(event)">
              <div class="np-fill" id="npFill" style="width:0%"></div>
            </div>
            <div class="np-times">
              <span id="npCt">0:00</span>
              <span id="npTt">0:00</span>
            </div>
          </div>

          <div class="np-controls">
            <button class="npc sm" id="btnShuffle" onclick="toggleShuffle()" title="Shuffle">
              <svg width="20" height="20" viewBox="0 0 24 24" fill="currentColor">
                <path d="M10.59 9.17L5.41 4 4 5.41l5.17 5.17 1.42-1.41zM14.5 4l2.04 2.04L4 18.59 5.41 20 17.96 7.46 20 9.5V4h-5.5zm.33 9.41l-1.41 1.41 3.13 3.13L14.5 20H20v-5.5l-2.04 2.04-3.13-3.13z"/>
              </svg>
            </button>
            <button class="npc" onclick="prevTrack()" title="Previous">
              <svg width="26" height="26" viewBox="0 0 24 24" fill="currentColor">
                <path d="M6 6h2v12H6zm3.5 6 8.5 6V6z"/>
              </svg>
            </button>
            <button class="npc npc-play" id="npPlayBtn" onclick="togglePlay()">
              <svg id="npPlayIco" width="26" height="26" viewBox="0 0 24 24" fill="currentColor">
                <path d="M8 5v14l11-7z"/>
              </svg>
            </button>
            <button class="npc" onclick="nextTrack()" title="Next">
              <svg width="26" height="26" viewBox="0 0 24 24" fill="currentColor">
                <path d="M18 6h-2v12h2V6zm-3.5 6L6 6v12z"/>
              </svg>
            </button>
            <button class="npc sm" id="btnRepeat" onclick="toggleRepeat()" title="Repeat">
              <svg width="20" height="20" viewBox="0 0 24 24" fill="currentColor">
                <path d="M7 7h10v3l4-4-4-4v3H5v6h2V7zm10 10H7v-3l-4 4 4 4v-3h12v-6h-2v4z"/>
              </svg>
            </button>

          </div>
        </div>

        <!-- Right / bottom: lyrics -->
        <div class="np-right" id="np-right">
          <div id="lyr-inline" class="hidden">
            <div id="lyc" class="lyc"></div>
          </div>
        </div>

      </div>
    </div>

  </div><!-- /pages -->

  <!-- Floating lyrics toggle button (Now Playing only) -->
  <button class="lyr-fab" id="lyrToggle" onclick="toggleLyricsView()" title="Lyrics (L)">
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round">
      <line x1="3" y1="6" x2="21" y2="6"/><line x1="3" y1="12" x2="15" y2="12"/><line x1="3" y1="18" x2="18" y2="18"/>
    </svg>
    <span>Lyrics</span>
  </button>

  <!-- Mini bar (shows on library page) -->
  <div class="minibar hide" id="minibar" onclick="showPage('now')">
    <div class="minibar-prog" id="minibarProg" style="width:0%"></div>
    <div class="mini-thumb" id="miniThumb">🎵</div>
    <div class="mini-info">
      <div class="mini-title" id="miniTitle">No track</div>
      <div class="mini-artist" id="miniArtist">—</div>
    </div>
    <div class="mini-btns" onclick="e=>e.stopPropagation()">
      <button class="mbtn" onclick="event.stopPropagation();prevTrack()">
        <svg width="18" height="18" viewBox="0 0 24 24" fill="currentColor"><path d="M6 6h2v12H6zm3.5 6 8.5 6V6z"/></svg>
      </button>
      <button class="mbtn mbtn-play" id="miniPlayBtn" onclick="event.stopPropagation();togglePlay()">
        <svg id="miniPlayIco" width="18" height="18" viewBox="0 0 24 24" fill="currentColor"><path d="M8 5v14l11-7z"/></svg>
      </button>
      <button class="mbtn" onclick="event.stopPropagation();nextTrack()">
        <svg width="18" height="18" viewBox="0 0 24 24" fill="currentColor"><path d="M18 6h-2v12h2V6zm-3.5 6L6 6v12z"/></svg>
      </button>
    </div>
  </div>

</div><!-- /app -->

<div class="toast" id="toastEl"></div>
<audio id="aud"></audio>

<script>
// ═══════════════════════════════════════════════
// STATE
// ═══════════════════════════════════════════════
const A = document.getElementById('aud');
let tracks = [], ci = -1, isPlaying = false;
let shuffle = false, repeat = 'none'; // 'none' | 'all' | 'one'
let shuffleOrder = [];
let lyrLines = [], activeLi = -1;
let lyricsVisible = false;
let rafId = null;
let currentPage = 'lib';
let isSeeking = false;

// ═══════════════════════════════════════════════
// NAVIGATION
// ═══════════════════════════════════════════════
function showPage(p) {
  currentPage = p;
  document.getElementById('plib').classList.toggle('off', p !== 'lib');
  document.getElementById('pnow').classList.toggle('off', p !== 'now');
  document.getElementById('tb-lib').classList.toggle('on', p === 'lib');
  document.getElementById('tb-now').classList.toggle('on', p === 'now');
  // mini bar only shows on library
  const mb = document.getElementById('minibar');
  if (ci >= 0) mb.classList.toggle('hide', p !== 'lib');
  // lyrics fab only on now playing
  const fab = document.getElementById('lyrToggle');
  fab.classList.toggle('show', p === 'now');
}

// ═══════════════════════════════════════════════
// LIBRARY
// ═══════════════════════════════════════════════
let filterQ = '';
function filterLib() {
  filterQ = document.getElementById('searchInput').value.toLowerCase();
  renderLib();
}

function renderLib() {
  const el = document.getElementById('tlist');
  const list = filterQ
    ? tracks.filter(t =>
        t.title.toLowerCase().includes(filterQ) ||
        t.artist.toLowerCase().includes(filterQ) ||
        (t.album||'').toLowerCase().includes(filterQ))
    : tracks;

  if (!list.length) {
    el.innerHTML = filterQ
      ? `<div class="empty"><span class="empty-icon">🔍</span><h3>No results</h3><p>No tracks match "${filterQ}"</p></div>`
      : `<div class="empty"><span class="empty-icon">🎵</span><h3>No tracks found</h3><p>Put audio files in the <strong>music/</strong> folder next to server.py and restart.</p><code>music/your-song.mp3</code></div>`;
    return;
  }

  el.innerHTML = list.map(t => {
    const i = tracks.indexOf(t);
    const isNow = i === ci;
    return `<div class="row${isNow ? ' now' : ''}" onclick="playTrack(${i})">
      <div class="thumb">
        ${t.art ? `<img src="${t.art}" loading="lazy">` : '🎵'}
        <div class="thumb-play"><svg width="16" height="16" viewBox="0 0 24 24" fill="white"><path d="M8 5v14l11-7z"/></svg></div>
      </div>
      <div class="rinfo">
        <div class="rtit">${esc(t.title)}</div>
        <div class="rsub">${esc(t.artist)}${t.album ? ' · '+esc(t.album) : ''}</div>
      </div>
      <div class="rright">
        <span class="fmt ${t.fmt.toLowerCase()}">${esc(t.fmt)}</span>
        ${t.has_lrc ? '<span class="lrcbadge">Lyrics</span>' : ''}
        ${isNow ? `<div class="bars${isPlaying ? '' : ' paused'}"><span></span><span></span><span></span></div>` : ''}
      </div>
    </div>`;
  }).join('');
}

// ═══════════════════════════════════════════════
// PLAYBACK
// ═══════════════════════════════════════════════
function playTrack(i) {
  if (i < 0 || i >= tracks.length) return;
  ci = i;
  const t = tracks[i];
  A.src = '/stream/' + t.id;
  A.play();
  isPlaying = true;
  renderLib();
  updateNP(t);
  updateMini(t);
  updBtns();
  document.getElementById('minibar').classList.toggle('hide', currentPage !== 'lib');
  loadLyrics(t);
  updateBackdrop(t);
}

function updateNP(t) {
  const art = document.getElementById('npArt');
  art.innerHTML = t.art ? `<img src="${t.art}">` : '🎵';
  art.className = 'np-art' + (isPlaying ? ' playing' : '');
  document.getElementById('npTitle').textContent  = t.title;
  document.getElementById('npArtist').textContent = t.artist;
  document.getElementById('npAlbum').textContent  = t.album || '';
  // Format badge
  const fb = document.getElementById('npFmtBadge');
  if (t.fmt) {
    const fmtColors = {
      FLAC:['rgba(96,165,250,.15)','#60a5fa'],
      MP3: ['rgba(251,191,36,.12)','#fbbf24'],
      OGG: ['rgba(167,139,250,.12)','#a78bfa'],
      WAV: ['rgba(52,211,153,.12)','#34d399'],
      AAC: ['rgba(251,146,60,.12)','#fb923c'],
      M4A: ['rgba(244,114,182,.12)','#f472b6'],
    };
    const [bg, col] = fmtColors[t.fmt.toUpperCase()] || ['rgba(255,255,255,.08)','rgba(255,255,255,.5)'];
    fb.innerHTML = `<span class="np-fmt-badge" style="background:${bg};color:${col}">${esc(t.fmt)}</span>`;
  } else fb.innerHTML = '';
  applyColors(t.colors);
}

function updateMini(t) {
  document.getElementById('miniThumb').innerHTML  = t.art ? `<img src="${t.art}">` : '🎵';
  document.getElementById('miniTitle').textContent  = t.title;
  document.getElementById('miniArtist').textContent = t.artist;
}

function updateBackdrop(t) {
  const el = document.getElementById('backdrop-img');
  if (t.art) {
    el.style.backgroundImage = `url(${t.art})`;
    el.classList.add('vis');
  } else {
    el.classList.remove('vis');
    el.style.backgroundImage = '';
  }
}

function applyColors(c) {
  if (!c) return;
  document.documentElement.style.setProperty('--acc', c.acc);
  document.documentElement.style.setProperty('--acc2', c.acc);
  // tint backdrop bg
  document.getElementById('backdrop').style.background = c.bg;
}

function togglePlay() {
  if (ci < 0 && tracks.length) { playTrack(0); return; }
  if (A.paused) { A.play(); isPlaying = true; }
  else          { A.pause(); isPlaying = false; }
  updBtns();
  const art = document.getElementById('npArt');
  art.className = 'np-art' + (isPlaying ? ' playing' : '');
}

function prevTrack() {
  if (!tracks.length) return;
  if (A.currentTime > 3) { A.currentTime = 0; return; }
  playTrack(prevIdx());
}
function nextTrack() {
  if (tracks.length) playTrack(nextIdx());
}

function prevIdx() {
  if (shuffle && shuffleOrder.length > 1) {
    const pos = shuffleOrder.indexOf(ci);
    return shuffleOrder[(pos - 1 + shuffleOrder.length) % shuffleOrder.length];
  }
  return (ci - 1 + tracks.length) % tracks.length;
}
function nextIdx() {
  if (repeat === 'one') return ci;
  if (shuffle && shuffleOrder.length > 1) {
    const pos = shuffleOrder.indexOf(ci);
    return shuffleOrder[(pos + 1) % shuffleOrder.length];
  }
  if (repeat === 'all') return (ci + 1) % tracks.length;
  return Math.min(ci + 1, tracks.length - 1);
}

function toggleShuffle() {
  shuffle = !shuffle;
  if (shuffle) {
    shuffleOrder = [...Array(tracks.length).keys()];
    for (let i = shuffleOrder.length - 1; i > 0; i--) {
      const j = Math.floor(Math.random() * (i + 1));
      [shuffleOrder[i], shuffleOrder[j]] = [shuffleOrder[j], shuffleOrder[i]];
    }
  }
  document.getElementById('btnShuffle').classList.toggle('on', shuffle);
  toast(shuffle ? 'Shuffle on' : 'Shuffle off');
}

function toggleRepeat() {
  const modes = ['none','all','one'];
  repeat = modes[(modes.indexOf(repeat) + 1) % 3];
  const btn = document.getElementById('btnRepeat');
  btn.classList.toggle('on', repeat !== 'none');
  // one-repeat icon tweak
  const icons = {
    none: '<path d="M7 7h10v3l4-4-4-4v3H5v6h2V7zm10 10H7v-3l-4 4 4 4v-3h12v-6h-2v4z"/>',
    all:  '<path d="M7 7h10v3l4-4-4-4v3H5v6h2V7zm10 10H7v-3l-4 4 4 4v-3h12v-6h-2v4z"/>',
    one:  '<path d="M7 7h10v3l4-4-4-4v3H5v6h2V7zm10 10H7v-3l-4 4 4 4v-3h12v-6h-2v4z"/><text x="12" y="14" text-anchor="middle" font-size="7" font-weight="bold" fill="currentColor">1</text>',
  };
  btn.querySelector('svg').innerHTML = icons[repeat];
  const labels = {none:'Repeat off',all:'Repeat all',one:'Repeat one'};
  toast(labels[repeat]);
}

function updBtns() {
  const ico = playing => playing
    ? '<rect x="6" y="4" width="4" height="16"/><rect x="14" y="4" width="4" height="16"/>'
    : '<path d="M8 5v14l11-7z"/>';
  document.getElementById('npPlayIco').innerHTML   = ico(isPlaying);
  document.getElementById('miniPlayIco').innerHTML = ico(isPlaying);
}

// ─── Seek ────────────────────────────────────────────────────────────────────
function startSeek(e) {
  isSeeking = true;
  doSeek(e);
  const up = ev => { isSeeking = false; document.removeEventListener('mousemove', mv); document.removeEventListener('mouseup', up); document.removeEventListener('touchmove', mv); document.removeEventListener('touchend', up); };
  const mv = ev => { if (isSeeking) doSeek(ev); };
  document.addEventListener('mousemove', mv);
  document.addEventListener('mouseup', up);
  document.addEventListener('touchmove', mv, {passive:true});
  document.addEventListener('touchend', up);
}
function doSeek(e) {
  const r = document.getElementById('npTrack').getBoundingClientRect();
  const x = (e.touches ? e.touches[0].clientX : e.clientX);
  const pct = Math.max(0, Math.min(1, (x - r.left) / r.width));
  if (A.duration) A.currentTime = pct * A.duration;
}

// ─── Audio events ────────────────────────────────────────────────────────────
A.addEventListener('ended', () => {
  const ni = nextIdx();
  if (repeat === 'one' || ni !== ci || repeat === 'all') playTrack(ni);
  else { isPlaying = false; updBtns(); }
});
A.addEventListener('play',  () => { isPlaying = true;  updBtns(); startRAF(); const a = document.getElementById('npArt'); a.className = 'np-art playing'; renderLib(); });
A.addEventListener('pause', () => { isPlaying = false; updBtns(); const a = document.getElementById('npArt'); a.className = 'np-art'; renderLib(); });

// ─── RAF loop for smooth fills ────────────────────────────────────────────────
function startRAF() {
  if (rafId) return;
  function loop() {
    const c = A.currentTime, d = A.duration || 0;
    // progress bars
    const pct = d ? (c / d * 100) : 0;
    document.getElementById('npFill').style.width = pct + '%';
    document.getElementById('minibarProg').style.width = pct + '%';
    document.getElementById('npCt').textContent = fmt(c);
    document.getElementById('npTt').textContent = fmt(d);
    // lyrics sync
    syncLyrics(c);
    rafId = requestAnimationFrame(loop);
  }
  rafId = requestAnimationFrame(loop);
}
A.addEventListener('pause', () => { cancelAnimationFrame(rafId); rafId = null; });
A.addEventListener('play',  () => startRAF());

// ═══════════════════════════════════════════════
// LYRICS TOGGLE
// ═══════════════════════════════════════════════
function toggleLyricsView() {
  lyricsVisible = !lyricsVisible;
  const lyrEl   = document.getElementById('lyr-inline');
  const coverEl = document.getElementById('np-left');
  const fab     = document.getElementById('lyrToggle');

  if (lyricsVisible) {
    lyrEl.className = 'visible';
    coverEl.classList.add('lyr-on');
    coverEl.classList.remove('lyr-off');
    fab.classList.add('on');
    setTimeout(() => scrollToActive(), 350);
  } else {
    lyrEl.className = 'hidden';
    coverEl.classList.remove('lyr-on');
    coverEl.classList.add('lyr-off');
    fab.classList.remove('on');
  }
}

// ═══════════════════════════════════════════════
// LYRICS PARSER (unchanged core)
// ═══════════════════════════════════════════════
function _parseWordBody(body, singer) {
  const words = [];
  const tok = /<(\d{1,2}):([0-5]\d[.,]\d+)>([^<]*)/g;
  let m;
  while ((m = tok.exec(body)) !== null) {
    const secs = parseInt(m[1], 10) * 60 + parseFloat(m[2].replace(',', '.'));
    const text = m[3].replace(/\s+/g, ' ').trim();
    words.push({ w: text, s: secs, e: 0 });
  }
  for (let i = 0; i < words.length - 1; i++) words[i].e = words[i+1].s;
  if (words.length) words[words.length-1].e = words[words.length-1].s + 0.5;
  const visible = words.filter(w => w.w.length > 0);
  if (!visible.length) return null;
  return { type:'word', singer, time:visible[0].s, end:visible[visible.length-1].e, words:visible };
}

function parseLyrics(raw) {
  if (!raw || !raw.trim()) return [];
  const out = [];
  for (const rawLine of raw.split(/\r?\n/)) {
    const line = rawLine.trim();
    if (!line) continue;
    const vMatch = /^v(\d+):(.*)/i.exec(line);
    if (vMatch) {
      const singer = parseInt(vMatch[1],10) === 2 ? 2 : 1;
      const entry = _parseWordBody(vMatch[2], singer);
      if (entry) out.push(entry);
      continue;
    }
    const lrcMatch = /^\[(\d{1,2}):([0-5]\d[.,]\d+)\](.*)$/.exec(line);
    if (lrcMatch) {
      const lineSecs = parseInt(lrcMatch[1],10)*60 + parseFloat(lrcMatch[2].replace(',','.'));
      const body = lrcMatch[3].trim();
      const innerV = /^v(\d+):(.*)/i.exec(body);
      if (innerV) {
        const singer = parseInt(innerV[1],10) === 2 ? 2 : 1;
        const entry = _parseWordBody(innerV[2], singer);
        if (entry) { out.push(entry); continue; }
      }
      if (body) { out.push({ type:'std', singer:1, time:lineSecs, end:lineSecs+2, text:body }); continue; }
      out.push({ type:'inst', singer:1, time:lineSecs, end:lineSecs+2 });
    }
  }
  out.sort((a,b) => a.time - b.time);
  for (let i = 0; i < out.length; i++) {
    const nextTime = i+1 < out.length ? out[i+1].time : out[i].end + 2;
    if (out[i].type === 'word') { out[i].words[out[i].words.length-1].e = nextTime; out[i].end = nextTime; }
    else out[i].end = nextTime;
  }
  return out;
}

// ═══════════════════════════════════════════════
// LYRICS DOM
// ═══════════════════════════════════════════════
function buildLyricsDOM(lines) {
  const container = document.getElementById('lyc');
  container.innerHTML = '';

  if (!lines.length) {
    _showNoLyrics(container, 'No Lyrics', 'No embedded LRC data found.');
    return;
  }

  const topSp = document.createElement('div');
  topSp.className = 'lyr-spacer-top';
  container.appendChild(topSp);

  lines.forEach((line, li) => {
    const isR = line.singer === 2;

    if (line.type === 'inst') {
      const div = document.createElement('div');
      div.className = 'dots' + (isR ? ' r' : '');
      div.dataset.li = li;
      div.innerHTML = '<span></span><span></span><span></span>';
      div.addEventListener('click', () => jumpTo(li));
      container.appendChild(div);
      return;
    }

    if (line.type === 'std') {
      const div = document.createElement('div');
      div.className = 'sline' + (isR ? ' r' : '');
      div.dataset.li = li;
      // base (dim) text
      const base = document.createElement('span');
      base.className = 'sbase';
      base.textContent = line.text;
      // fill overlay
      const fill = document.createElement('span');
      fill.className = 'sfill';
      fill.textContent = line.text;
      fill.style.setProperty('--sp', '0%');
      div.appendChild(base);
      div.appendChild(fill);
      div.addEventListener('click', () => jumpTo(li));
      container.appendChild(div);
      return;
    }

    if (line.type === 'word') {
      const div = document.createElement('div');
      div.className = 'wline' + (isR ? ' r' : '');
      div.dataset.li = li;
      div.addEventListener('click', () => jumpTo(li));
      line.words.forEach((word, wi) => {
        const span = document.createElement('span');
        span.className = 'w';
        span.dataset.li = li;
        span.dataset.wi = wi;
        span.textContent = word.w;
        span.style.setProperty('--wp', '0%');
        div.appendChild(span);
      });
      container.appendChild(div);
    }
  });

  const botSp = document.createElement('div');
  botSp.className = 'lyr-spacer-bot';
  container.appendChild(botSp);
}

function _showNoLyrics(container, title, msg) {
  const d = document.createElement('div');
  d.className = 'nolyr';
  const artDiv = document.createElement('div');
  artDiv.className = 'nolyr-art';
  if (ci >= 0 && tracks[ci] && tracks[ci].art) {
    artDiv.innerHTML = `<img src="${tracks[ci].art}">`;
  } else {
    artDiv.textContent = '🎤';
  }
  const h = document.createElement('h3'); h.textContent = title;
  const p = document.createElement('p');  p.textContent = msg;
  d.appendChild(artDiv); d.appendChild(h); d.appendChild(p);
  container.appendChild(d);
}

// ═══════════════════════════════════════════════
// LYRICS LOAD
// ═══════════════════════════════════════════════
async function loadLyrics(t) {
  lyrLines = []; activeLi = -1;
  const c = document.getElementById('lyc');
  c.innerHTML = '';

  if (!t.has_lrc) {
    _showNoLyrics(c, 'No Lyrics', 'No embedded lyrics in this file.');
    return;
  }

  try {
    const res = await fetch('/lyrics/' + t.id);
    if (!res.ok) throw new Error('HTTP ' + res.status);
    const data = await res.json();
    const raw = (data && typeof data.raw === 'string') ? data.raw : '';
    if (!raw.trim()) { _showNoLyrics(c, 'No Lyrics', 'Empty lyrics data.'); return; }
    lyrLines = parseLyrics(raw);
    if (!lyrLines.length) { _showNoLyrics(c, 'No Lyrics', 'Could not parse lyrics format.'); return; }
    buildLyricsDOM(lyrLines);
  } catch (e) {
    c.innerHTML = '';
    _showNoLyrics(c, 'Error', 'Could not load lyrics: ' + e.message);
  }
}

// ═══════════════════════════════════════════════
// LYRICS SYNC — gradient fill engine
// ═══════════════════════════════════════════════
let prevActiveLi = -1;

function syncLyrics(cur) {
  if (!lyrLines.length) return;

  // Find current line
  let newLi = -1;
  for (let i = lyrLines.length - 1; i >= 0; i--) {
    if (cur >= lyrLines[i].time) { newLi = i; break; }
  }

  // Line changed → update class states
  if (newLi !== activeLi) {
    prevActiveLi = activeLi;
    activeLi = newLi;

    // Update all line elements
    document.querySelectorAll('[data-li]').forEach(el => {
      if (el.dataset.wi !== undefined) return; // skip word spans
      const li = parseInt(el.dataset.li, 10);
      el.classList.remove('act', 'past', 'next');
      if      (li === activeLi)      el.classList.add('act');
      else if (li < activeLi)        el.classList.add('past');
      else if (li <= activeLi + 2)   el.classList.add('next');
    });

    // Reset fill on previous std line to 100%
    if (prevActiveLi >= 0 && lyrLines[prevActiveLi] && lyrLines[prevActiveLi].type === 'std') {
      const prev = document.querySelector(`.sline[data-li="${prevActiveLi}"] .sfill`);
      if (prev) prev.style.setProperty('--sp', '100%');
    }
    // Reset word fills on prev word line to fully lit
    if (prevActiveLi >= 0 && lyrLines[prevActiveLi] && lyrLines[prevActiveLi].type === 'word') {
      document.querySelectorAll(`span.w[data-li="${prevActiveLi}"]`).forEach(s => {
        s.style.setProperty('--wp', '100%');
      });
    }

    // Scroll
    scrollToActive();
  }

  // ── Per-word gradient fill ──
  if (activeLi >= 0 && lyrLines[activeLi] && lyrLines[activeLi].type === 'word') {
    const words = lyrLines[activeLi].words;
    for (let wi = 0; wi < words.length; wi++) {
      const w = words[wi];
      let pct;
      if (cur <= w.s) {
        pct = 0;
      } else if (cur >= w.e) {
        pct = 100;
      } else {
        pct = ((cur - w.s) / (w.e - w.s)) * 100;
      }
      const span = document.querySelector(`span.w[data-li="${activeLi}"][data-wi="${wi}"]`);
      if (span) span.style.setProperty('--wp', pct.toFixed(1) + '%');
    }
  }

  // ── Std line gradient fill (whole line sweeps over duration) ──
  if (activeLi >= 0 && lyrLines[activeLi] && lyrLines[activeLi].type === 'std') {
    const line = lyrLines[activeLi];
    const dur = line.end - line.time;
    let pct;
    if (dur <= 0) {
      pct = 100;
    } else {
      pct = Math.min(100, ((cur - line.time) / dur) * 100);
    }
    const fill = document.querySelector(`.sline[data-li="${activeLi}"] .sfill`);
    if (fill) fill.style.setProperty('--sp', pct.toFixed(1) + '%');
  }
}

function scrollToActive() {
  if (activeLi < 0) return;
  const el = document.querySelector(`[data-li="${activeLi}"]:not([data-wi])`);
  if (!el) return;
  const page = document.getElementById('pnow');
  const pageRect = page.getBoundingClientRect();
  const elRect   = el.getBoundingClientRect();
  const target   = elRect.top - pageRect.top + page.scrollTop - page.clientHeight * 0.38;
  page.scrollTo({ top: target, behavior: 'smooth' });
}

function jumpTo(li) {
  if (li < 0 || li >= lyrLines.length || A.readyState <= 0) return;
  A.currentTime = lyrLines[li].time;
  if (A.paused) { A.play(); isPlaying = true; updBtns(); }
}

// ═══════════════════════════════════════════════
// UTILS
// ═══════════════════════════════════════════════
function fmt(s) {
  if (!s || isNaN(s)) return '0:00';
  return `${Math.floor(s/60)}:${String(Math.floor(s%60)).padStart(2,'0')}`;
}
function esc(s) {
  return (s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

let _toastTimer;
function toast(m) {
  const e = document.getElementById('toastEl');
  e.textContent = m; e.classList.add('on');
  clearTimeout(_toastTimer);
  _toastTimer = setTimeout(() => e.classList.remove('on'), 2400);
}

// ═══════════════════════════════════════════════
// KEYBOARD
// ═══════════════════════════════════════════════
document.addEventListener('keydown', e => {
  if (e.target.tagName === 'INPUT') return;
  if (e.code === 'Space')      { e.preventDefault(); togglePlay(); }
  if (e.code === 'ArrowRight') A.currentTime = Math.min(A.duration||0, A.currentTime+10);
  if (e.code === 'ArrowLeft')  A.currentTime = Math.max(0, A.currentTime-10);
  if (e.code === 'KeyN')       nextTrack();
  if (e.code === 'KeyP')       prevTrack();
  if (e.code === 'KeyL')       toggleLyricsView();
  if (e.code === 'KeyS')       toggleShuffle();
});

window.addEventListener('pageshow', e => { if (e.persisted) window.location.reload(); });

// ═══════════════════════════════════════════════
// INIT
// ═══════════════════════════════════════════════
(async () => {
  try {
    const res  = await fetch('/tracks');
    const data = await res.json();
    tracks = data;
    renderLib();
    if (tracks.length) toast(`${tracks.length} track${tracks.length > 1 ? 's' : ''} loaded`);
  } catch(e) {
    renderLib();
  }
})();
</script>
</body>
</html>
"""

# ─── HTTP Handler ─────────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def _json(self, data, code=200):
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", len(body))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urlparse(self.path).path.rstrip("/") or "/"

        if path == "/":
            body = PAGE.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", len(body))
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
            self.end_headers()
            self.wfile.write(body)

        elif path == "/tracks":
            with _lock:
                snapshot = list(_order)
            result = []
            for tid in snapshot:
                try:
                    result.append(track_json(tid))
                except Exception:
                    pass
            self._json(result)

        elif path.startswith("/art/"):
            tid = path[5:]
            with _lock:
                t = _store.get(tid)
            if not t or not t["tags"].get("art"):
                self.send_response(404); self.end_headers(); return
            art = t["tags"]["art"]
            mime = "image/jpeg"
            if art[:4] == b"\x89PNG": mime = "image/png"
            self.send_response(200)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", len(art))
            self.send_header("Cache-Control", "public,max-age=86400")
            self.end_headers()
            self.wfile.write(art)

        elif path.startswith("/lyrics/"):
            tid = path[8:]
            with _lock:
                t = _store.get(tid)
            if not t:
                self._json({"raw": ""}, 404); return
            self._json({"raw": t["tags"].get("lyrics", "")})

        elif path.startswith("/stream/"):
            tid = path[8:]
            with _lock:
                t = _store.get(tid)
            if not t:
                self.send_response(404); self.end_headers(); return
            fp   = t["path"]
            size = os.path.getsize(fp)
            mime = mimetypes.guess_type(str(fp))[0] or "audio/mpeg"
            rng  = self.headers.get("Range", "")
            if rng:
                m = re.match(r"bytes=(\d+)-(\d*)", rng)
                if m:
                    s = int(m.group(1))
                    e = int(m.group(2)) if m.group(2) else size-1
                    e = min(e, size-1)
                    n = e-s+1
                    self.send_response(206)
                    self.send_header("Content-Type", mime)
                    self.send_header("Content-Range", f"bytes {s}-{e}/{size}")
                    self.send_header("Content-Length", n)
                    self.send_header("Accept-Ranges", "bytes")
                    self.end_headers()
                    with open(fp,"rb") as f:
                        f.seek(s); rem=n
                        while rem:
                            chunk=f.read(min(65536,rem))
                            if not chunk: break
                            self.wfile.write(chunk); rem-=len(chunk)
                    return
            self.send_response(200)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", size)
            self.send_header("Accept-Ranges", "bytes")
            self.end_headers()
            with open(fp,"rb") as f:
                while True:
                    chunk=f.read(65536)
                    if not chunk: break
                    try: self.wfile.write(chunk)
                    except: break
        else:
            self.send_response(404); self.end_headers()

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin","*")
        self.send_header("Access-Control-Allow-Methods","GET,OPTIONS")
        self.end_headers()

# ─── Main ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    PORT = int(os.environ.get("PORT", 8765))
    print(f"\n🎵  Aura Music Player v2  →  http://localhost:{PORT}")
    print(f"    Music folder: {MUSIC_DIR}\n")
    n = scan()
    print(f"    Loaded {n} track{'s' if n!=1 else ''}\n")
    srv = ThreadedHTTPServer(("0.0.0.0", PORT), Handler)
    try:   srv.serve_forever()
    except KeyboardInterrupt: print("\nStopped.")