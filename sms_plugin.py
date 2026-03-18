#!/usr/bin/env python3
"""
FPP SMS Plugin v2.5 - Twilio Integration
"""

from flask import Flask, request, jsonify, render_template_string, Response
import logging
import json
import requests
from datetime import datetime, timedelta, timezone
import re
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from twilio.rest import Client
from collections import deque
import os
import struct
import io

# PIL/Pillow for pixel-accurate text rendering (optional — falls back to FPP text API if unavailable)
try:
    from PIL import Image, ImageDraw, ImageFont
    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False

# zstandard for FSEQ zstd decompression (optional — install via fpp_install.sh)
try:
    import zstandard as _zstd_mod
    ZSTD_AVAILABLE = True
except ImportError:
    _zstd_mod = None
    ZSTD_AVAILABLE = False

_scroll_thread = None   # background PIL scroll animation thread

# Configuration
PLUGIN_DIR      = os.path.dirname(os.path.abspath(__file__))

# All runtime data lives under one plugin folder
PLUGIN_DATA_DIR = "/home/fpp/media/plugin.fpp-sms-twilio"
CONFIG_FILE     = os.path.join(PLUGIN_DATA_DIR, "plugin.json")
LOG_FILE        = os.path.join(PLUGIN_DATA_DIR, "logs", "sms_plugin.log")
QUEUE_FILE      = os.path.join(PLUGIN_DATA_DIR, "queue_pending.json")
MESSAGES_DIR    = os.path.join(PLUGIN_DATA_DIR, "logs", "messages")
LAST_SID_FILE   = os.path.join(PLUGIN_DATA_DIR, "last_message_sid.txt")
BLOCKLIST_FILE  = os.path.join(PLUGIN_DATA_DIR, "blocked_phones.json")

FSEQ_SEQUENCE_PATH = '/home/fpp/media/sequences'
FPP_VIDEOS_PATH    = '/home/fpp/media/videos'
FPP_IMAGES_PATH    = '/home/fpp/media/images'

# Whitelist/blacklist source files stay in the plugin git repo directory
BLACKLIST_FILE = os.path.join(PLUGIN_DIR, "blacklist.txt")
BLACKLIST_REMOVED_FILE = os.path.join(PLUGIN_DIR, "blacklist_removed.txt")
BLACKLIST_ADDED_FILE = os.path.join(PLUGIN_DIR, "blacklist_added.txt")
WHITELIST_FILE = os.path.join(PLUGIN_DIR, "whitelist.txt")
WHITELIST_REMOVED_FILE = os.path.join(PLUGIN_DIR, "whitelist_removed.txt")
WHITELIST_ADDED_FILE = os.path.join(PLUGIN_DIR, "whitelist_added.txt")

# Create directory structure before logging setup
os.makedirs(os.path.join(PLUGIN_DATA_DIR, "logs", "messages"), exist_ok=True)

# Setup logging — ensure the log directory exists, then write to file + stderr
_log_handlers = [logging.StreamHandler()]  # stderr always available via nohup
try:
    _log_handlers.append(logging.FileHandler(LOG_FILE))
except Exception:
    pass  # directory may not exist on some FPP installs; stderr is the fallback
logging.basicConfig(
    level=logging.ERROR,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=_log_handlers
)
logging.getLogger('werkzeug').setLevel(logging.ERROR)

import flask.cli
flask.cli.show_server_banner = lambda *args: None

app = Flask(__name__)

IFRAME_RESIZE_SCRIPT = """<script>
(function() {
    function reportHeight() {
        window.parent.postMessage({ type: 'iframeHeight', height: document.body.scrollHeight }, '*');
    }
    window.addEventListener('load', reportHeight);
    new MutationObserver(reportHeight).observe(document.body, { subtree: true, childList: true, characterData: true });
})();
</script>"""

@app.after_request
def inject_iframe_resize(response):
    if response.content_type.startswith('text/html'):
        body = response.get_data(as_text=True)
        body = body.replace('</body>', IFRAME_RESIZE_SCRIPT + '</body>')
        response.set_data(body)
    return response

# ============================================================================
# OPTIMIZED LIST CACHING - Module-level cache variables
# ============================================================================
_blacklist_cache = None
_blacklist_mtime = None

_whitelist_cache = None
_whitelist_mtime = None

_blocklist_cache = None
_blocklist_mtime = None

_fpp_data_cache = None
_fpp_data_cache_time = 0
_FPP_DATA_CACHE_TTL = 60  # seconds

# FPP runs locally — always use localhost
FPP_HOST = 'http://127.0.0.1'

# Default configuration
DEFAULT_CONFIG = {
    "enabled": False,
    "twilio_account_sid": "",
    "twilio_auth_token": "",
    "twilio_phone_number": "",
    "poll_interval": 2,
    "display_duration": 10,
    "max_messages_per_phone": 5,
    "max_message_length": 100,
    "max_message_age_mins": 5,
    "one_word_only": False,
    "two_words_max": True,
    "use_whitelist": False,
    "profanity_filter": True,
    "fpp_host": "http://127.0.0.1",
    "default_playlist": "",
    "name_display_playlist": "",
    "overlay_model_name": "",
    "text_color": "#FF0000",
    "text_font": "FreeSans",
    "text_font_size": 48,
    "text_position": "Center",
    "message_template": "Merry Christmas {name}!",  # legacy — migrated to message_lines on load
    "message_lines": ["Merry Christmas", "{name}!", "", ""],
    "line_positions": [{"x": -1, "y": -1}, {"x": -1, "y": -1}, {"x": -1, "y": -1}, {"x": -1, "y": -1}],
    "scroll_speed": 5,
    "overlay_model_width": 0,
    "overlay_model_height": 0,
    "sms_response_show_not_live": False,
    "sms_response_success": False,
    "sms_response_profanity": False,
    "sms_response_rate_limited": False,
    "allow_duplicate_names": False,
    "sms_response_duplicate": False,
    "sms_response_invalid_format": False,
    "sms_response_not_whitelisted": False,
    "sms_response_blocked": False,
    "response_show_not_live": "Ho, Ho, Ho, It looks like our show isn't running now. Try again later.",
    "response_success": "Merry Christmas! Your name will appear on our display soon! 🎄",
    "response_profanity": "Sorry, your message contains inappropriate content and cannot be displayed. Please keep within the Christmas spirit! 🎅",
    "response_blocked": "Sorry, Your phone number has been blocked from sending messages.",
    "response_rate_limited": "You've reached the maximum number of messages allowed. Please try again tomorrow!",
    "response_duplicate": "You've already sent this name today!",
    "response_invalid_format": "Please send only a name (1-2 words, no sentences).",
    "response_not_whitelisted": "Sorry, that name is not on our approved list and cannot be shown.",
}

config = DEFAULT_CONFIG.copy()
twilio_client = None
last_message_sid = None
polling_thread = None
display_thread = None
stop_polling = False
stop_display = False

# Queue system
message_queue = deque()
currently_displaying = None
queue_lock = threading.Lock()

def load_config():
    """Load configuration from file, merging with defaults so new settings survive updates"""
    global config, twilio_client, last_message_sid
    try:
        with open(CONFIG_FILE, 'r') as f:
            loaded = json.load(f)
            config.update(loaded)

        # If the plugin was updated and new default keys were added, save them
        # back so the file stays complete across updates
        new_keys = set(DEFAULT_CONFIG.keys()) - set(loaded.keys())
        if new_keys:
            save_config()
            logging.info(f"Saved {len(new_keys)} new default setting(s) after update: {new_keys}")

        # Migrate old scroll_speed values (pre-v2.6 stored raw px/s, now 1-10 scale)
        if config.get('scroll_speed', 5) > 10:
            config['scroll_speed'] = 5
            save_config()

        # Migrate old message_template to message_lines (introduced in v2.6)
        if 'message_lines' not in loaded and 'message_template' in loaded:
            tmpl = loaded.get('message_template', 'Merry Christmas {name}!')
            config['message_lines'] = [tmpl, '', '', '']
            config['line_positions'] = [{'x': -1, 'y': -1}] * 4
            save_config()
            logging.info(f"Migrated message_template '{tmpl}' to message_lines[0]")

        if config['twilio_account_sid'] and config['twilio_auth_token']:
            twilio_client = Client(
                config['twilio_account_sid'],
                config['twilio_auth_token']
            )

        try:
            with open(LAST_SID_FILE, 'r') as f:
                last_message_sid = f.read().strip()
                logging.info(f"Loaded last message SID: {last_message_sid}")
        except:
            last_message_sid = None

        logging.info("Configuration loaded successfully")
    except FileNotFoundError:
        save_config()
        logging.info("Created default configuration")
    except Exception as e:
        logging.error(f"Error loading config: {e}")

def save_config():
    """Save configuration to file"""
    try:
        with open(CONFIG_FILE, 'w') as f:
            json.dump(config, f, indent=2)
        logging.info("Configuration saved")
    except Exception as e:
        logging.error(f"Error saving config: {e}")

def _find_font(font_name, font_size):
    """Locate a PIL ImageFont matching font_name. Returns ImageFont or None."""
    if not PIL_AVAILABLE:
        return None

    # Use fontconfig (fc-match) — same resolution FPP uses for its font names
    try:
        import subprocess
        result = subprocess.run(
            ['fc-match', '--format=%{file}', font_name],
            capture_output=True, text=True, timeout=2
        )
        if result.returncode == 0 and result.stdout.strip():
            font_path = result.stdout.strip()
            if os.path.exists(font_path):
                return ImageFont.truetype(font_path, font_size)
    except Exception:
        pass

    # Fallback: manual search in common FPP font directories
    search_dirs = [
        '/usr/share/fonts/truetype/freefont',
        '/usr/share/fonts/truetype',
        '/usr/share/fonts/opentype',
        '/usr/share/fonts',
        '/usr/share/fpp/fonts',
        '/home/fpp/media/fonts',
    ]
    font_name_lower = font_name.lower()
    for search_dir in search_dirs:
        if not os.path.isdir(search_dir):
            continue
        for dirpath, _, filenames in os.walk(search_dir):
            for fname in filenames:
                if fname.lower().endswith(('.ttf', '.otf')) and font_name_lower in fname.lower():
                    try:
                        return ImageFont.truetype(os.path.join(dirpath, fname), font_size)
                    except Exception:
                        pass
    try:
        return ImageFont.load_default()
    except Exception:
        return None


def render_to_shm(line_items, model_name, width, height, font_name, font_size, color_hex):
    """Render multiple text lines to FPP shared memory, each at its own (x, y) position.
    line_items: list of (text, x, y) tuples. x/y < 0 means auto-center that line.
    Returns True on success, False on failure."""
    if not PIL_AVAILABLE or width <= 0 or height <= 0:
        return False
    try:
        hex_str = color_hex.lstrip('#')
        color = (int(hex_str[0:2], 16), int(hex_str[2:4], 16), int(hex_str[4:6], 16))

        img = Image.new('RGB', (width, height), (0, 0, 0))
        draw = ImageDraw.Draw(img)
        font = _find_font(font_name, font_size)

        for (text, x, y) in line_items:
            if not text:
                continue
            if font is not None:
                bbox = draw.textbbox((0, 0), text, font=font)
                text_w, text_h = bbox[2] - bbox[0], bbox[3] - bbox[1]
            else:
                text_w = len(text) * 6
                text_h = font_size

            draw_x = max(0, (width - text_w) // 2) if x < 0 else max(0, min(width - text_w, x))
            draw_y = max(0, (height - text_h) // 2) if y < 0 else max(0, min(height - text_h, y))
            draw.text((draw_x, draw_y), text, fill=color, font=font)

        shm_path = f"/dev/shm/FPP-Model-Data-{model_name}"
        raw = img.tobytes()
        expected = width * height * 3
        if len(raw) != expected:
            logging.error(f"render_to_shm: size mismatch ({len(raw)} != {expected})")
            return False

        def _write():
            with open(shm_path, 'r+b') as f:
                f.write(raw)

        try:
            _write()
        except PermissionError:
            # FPP creates shm files as root after postStart.sh runs.
            # Use the sudoers rule added by fpp_install.sh to fix permissions once.
            logging.warning(f"render_to_shm: permission denied on {shm_path} — running sudo chmod")
            import subprocess
            result = subprocess.run(
                ['sudo', '-n', '/usr/bin/chmod', '666', shm_path],
                capture_output=True, timeout=5
            )
            if result.returncode == 0:
                _write()
            else:
                logging.error(f"render_to_shm: sudo chmod failed: {result.stderr.decode().strip()}")
                logging.error("render_to_shm: restart FPPD to apply shm permissions from postStart.sh")
                return False

        logging.info(f"render_to_shm: wrote {len(raw)} bytes to {shm_path} ({len(line_items)} lines)")
        return True
    except Exception as e:
        logging.error(f"render_to_shm failed: {e}")
        return False


def render_image_to_shm(image_path, model_name, width, height,
                        line_items=None, font_name=None, font_size=48, color_hex='#ffffff'):
    """Load an image file, resize to model dimensions, optionally composite text on top,
    then write to FPP shared memory.  Returns True on success.
    line_items: optional list of (text, x, y) to draw over the image (same as render_to_shm).
    State 2 (Opaque) should be used so the image fully covers the background."""
    if not PIL_AVAILABLE or width <= 0 or height <= 0:
        return False
    try:
        img = Image.open(image_path).convert('RGB')
        img = img.resize((width, height), Image.LANCZOS)

        if line_items:
            draw = ImageDraw.Draw(img)
            font = _find_font(font_name, font_size) if font_name else None
            hex_str = color_hex.lstrip('#')
            color = (int(hex_str[0:2], 16), int(hex_str[2:4], 16), int(hex_str[4:6], 16))
            for (text, x, y) in line_items:
                if not text:
                    continue
                if font is not None:
                    bbox = draw.textbbox((0, 0), text, font=font)
                    text_w, text_h = bbox[2] - bbox[0], bbox[3] - bbox[1]
                else:
                    text_w = len(text) * 6
                    text_h = font_size
                draw_x = max(0, (width - text_w) // 2) if x < 0 else max(0, min(width - text_w, x))
                draw_y = max(0, (height - text_h) // 2) if y < 0 else max(0, min(height - text_h, y))
                draw.text((draw_x, draw_y), text, fill=color, font=font)

        shm_path = f"/dev/shm/FPP-Model-Data-{model_name}"
        raw = img.tobytes()
        expected = width * height * 3
        if len(raw) != expected:
            logging.error(f"render_image_to_shm: size mismatch ({len(raw)} != {expected})")
            return False

        def _write():
            with open(shm_path, 'r+b') as f:
                f.write(raw)

        try:
            _write()
        except PermissionError:
            import subprocess
            result = subprocess.run(
                ['sudo', '-n', '/usr/bin/chmod', '666', shm_path],
                capture_output=True, timeout=5
            )
            if result.returncode == 0:
                _write()
            else:
                logging.error(f"render_image_to_shm: sudo chmod failed")
                return False

        logging.info(f"render_image_to_shm: wrote {image_path} → {shm_path}")
        return True
    except Exception as e:
        logging.error(f"render_image_to_shm failed: {e}")
        return False


def scroll_via_shm(line_items, model_name, width, height, scroll_dir,
                   font_name, font_size, color_hex, scroll_speed, duration):
    """Animate per-line scrolling text in FPP shared memory.
    All lines share the same scroll position so they move together.
    Runs in a background thread for `duration` seconds then stops.

    scroll_dir 'L2R'|'R2L': line_items = [(text, y_pos), ...]
        Each line scrolls horizontally at its own fixed y_pos.
        y_pos < 0 means auto-center that line vertically.
    scroll_dir 'T2B'|'B2T': line_items = [(text, x_pos), ...]
        Each line scrolls vertically at its own fixed x_pos.
        x_pos < 0 means auto-center that line horizontally.
    Returns True if the thread started, False on error."""
    global _scroll_thread
    if not PIL_AVAILABLE or width <= 0 or height <= 0:
        return False
    try:
        hex_str = color_hex.lstrip('#')
        color = (int(hex_str[0:2], 16), int(hex_str[2:4], 16), int(hex_str[4:6], 16))
        font = _find_font(font_name, font_size)

        # Pre-render each line to its own image strip
        probe = Image.new('RGB', (1, 1))
        pdraw = ImageDraw.Draw(probe)
        strips = []
        for (text, fixed_pos) in line_items:
            if not text:
                continue
            if font:
                bbox = pdraw.textbbox((0, 0), text, font=font)
                tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
            else:
                tw, th = len(text) * 6, font_size
            tw, th = max(1, tw), max(1, th)
            strip = Image.new('RGB', (tw, th), (0, 0, 0))
            ImageDraw.Draw(strip).text((0, 0), text, fill=color, font=font)
            strips.append((strip, tw, th, fixed_pos))

        if not strips:
            return False

        max_tw = max(s[1] for s in strips)
        max_th = max(s[2] for s in strips)

        shm_path = f"/dev/shm/FPP-Model-Data-{model_name}"
        fps = 30
        pixels_per_sec = max(10, scroll_speed * 20)
        step_px = max(1.0, pixels_per_sec / fps)

        if os.path.exists(shm_path) and not os.access(shm_path, os.W_OK):
            import subprocess
            subprocess.run(['sudo', '-n', '/usr/bin/chmod', '666', shm_path],
                           capture_output=True, timeout=5)

        logging.info(f"🎬 scroll_via_shm: {scroll_dir} model={model_name} "
                     f"size={width}x{height} lines={len(strips)} "
                     f"speed={pixels_per_sec}px/s duration={duration}s")

        def _animate():
            import time as _time
            start = _time.time()

            if scroll_dir in ('L2R', 'R2L'):
                # Resolve y positions (auto-center vertically when < 0)
                resolved = []
                for (strip, tw, th, yp) in strips:
                    dy = max(0, (height - th) // 2) if yp < 0 else max(0, min(height - th, yp))
                    resolved.append((strip, tw, th, dy))
                pos = float(width) if scroll_dir == 'R2L' else float(-max_tw)
                direction = -1.0 if scroll_dir == 'R2L' else 1.0
                loop_start = float(width) if scroll_dir == 'R2L' else float(-max_tw)
                loop_end   = float(-max_tw) if scroll_dir == 'R2L' else float(width)

                while _time.time() - start < duration:
                    frame = Image.new('RGB', (width, height), (0, 0, 0))
                    ix = int(pos)
                    for (strip, tw, th, dy) in resolved:
                        src_x = max(0, -ix);  dst_x = max(0, ix)
                        vis_w = min(tw - src_x, width - dst_x)
                        if vis_w > 0:
                            frame.paste(strip.crop((src_x, 0, src_x + vis_w, th)), (dst_x, dy))
                    try:
                        with open(shm_path, 'r+b') as f: f.write(frame.tobytes())
                    except Exception:
                        pass
                    pos += direction * step_px
                    if (direction < 0 and pos < loop_end) or (direction > 0 and pos > loop_end):
                        pos = loop_start
                    _time.sleep(1.0 / fps)

            elif scroll_dir in ('T2B', 'B2T'):
                # Resolve x positions (auto-center horizontally when < 0)
                resolved = []
                for (strip, tw, th, xp) in strips:
                    dx = max(0, (width - tw) // 2) if xp < 0 else max(0, min(width - tw, xp))
                    resolved.append((strip, tw, th, dx))
                pos = float(height) if scroll_dir == 'B2T' else float(-max_th)
                direction = -1.0 if scroll_dir == 'B2T' else 1.0
                loop_start = float(height) if scroll_dir == 'B2T' else float(-max_th)
                loop_end   = float(-max_th) if scroll_dir == 'B2T' else float(height)

                while _time.time() - start < duration:
                    frame = Image.new('RGB', (width, height), (0, 0, 0))
                    iy = int(pos)
                    for (strip, tw, th, dx) in resolved:
                        src_y = max(0, -iy);  dst_y = max(0, iy)
                        vis_h = min(th - src_y, height - dst_y)
                        if vis_h > 0:
                            frame.paste(strip.crop((0, src_y, tw, src_y + vis_h)), (dx, dst_y))
                    try:
                        with open(shm_path, 'r+b') as f: f.write(frame.tobytes())
                    except Exception:
                        pass
                    pos += direction * step_px
                    if (direction < 0 and pos < loop_end) or (direction > 0 and pos > loop_end):
                        pos = loop_start
                    _time.sleep(1.0 / fps)

        _scroll_thread = threading.Thread(target=_animate, daemon=True)
        _scroll_thread.start()
        return True
    except Exception as e:
        logging.error(f"scroll_via_shm failed: {e}")
        return False


def get_fpp_playlists():
    """Get list of playlists from FPP"""
    try:
        fpp_host = FPP_HOST
        playlists = []
        
        try:
            response = requests.get(f"{fpp_host}/api/playlists", timeout=3)
            if response.status_code == 200:
                data = response.json()
                if isinstance(data, dict):
                    playlists = list(data.keys())
                elif isinstance(data, list):
                    playlists = data
                logging.info(f"Found {len(playlists)} playlists: {playlists}")
        except Exception as e:
            logging.error(f"Could not fetch playlists: {e}")
        
        return sorted(playlists)
        
    except Exception as e:
        logging.error(f"Error fetching FPP playlists: {e}")
        return []

# ---------------------------------------------------------------------------
# FSEQ preview helpers
# ---------------------------------------------------------------------------

def parse_fseq_header(filepath):
    """Parse an FSEQ v2 file header. Returns a metadata dict or raises ValueError."""
    with open(filepath, 'rb') as f:
        raw = f.read(32)
    if len(raw) < 32 or raw[0:4] != b'PSEQ':
        raise ValueError("Not a valid FSEQ file (missing PSEQ magic)")
    major_ver = raw[7]
    if major_ver != 2:
        raise ValueError(f"Unsupported FSEQ version {raw[7]}.{raw[6]}")

    chan_data_offset  = struct.unpack_from('<H', raw, 4)[0]
    channel_count     = struct.unpack_from('<I', raw, 10)[0]
    frame_count       = struct.unpack_from('<I', raw, 14)[0]
    step_time_ms      = raw[18]
    compression_type  = raw[19] & 0x0F   # 0=none, 1=zlib, 2=zstd (per xLights: 1=zstd)
    # Offset 20 and 21 are separate uint8 fields — NOT a single uint16
    num_comp_blocks   = raw[20]           # uint8
    num_sparse_ranges = raw[21]           # uint8

    # ── Auto-detect zstd compression ─────────────────────────────────────────
    # FSEQ v2.2 (minor_version >= 2) sometimes writes compression_type=0 in
    # byte 19 even though the data is zstd-compressed.  Probe the actual data
    # at chan_data_offset for the zstd frame magic (0xFD2FB528 little-endian).
    _ZSTD_MAGIC = b'\x28\xB5\x2F\xFD'
    with open(filepath, 'rb') as _f:
        _f.seek(chan_data_offset)
        _probe = _f.read(4)
    effective_ctype = compression_type
    if _probe == _ZSTD_MAGIC and compression_type == 0:
        effective_ctype = 2   # override: treat as zstd
        logging.info(
            "FSEQ: header says uncompressed (byte 19 = 0) but zstd magic detected "
            "at chan_data_offset — treating as zstd (FSEQ v2.2 quirk)"
        )

    # ── Compression block table ───────────────────────────────────────────────
    # When compression is active (or auto-detected), scan from offset 32 for
    # valid (firstFrame uint32, dataLen uint32) block entries.  FSEQ v2.2 may
    # report num_comp_blocks incorrectly in byte 20; derive actual count by
    # scanning until firstFrame >= frameCount or dataLen == 0.
    comp_blocks = []
    if effective_ctype in (1, 2):
        with open(filepath, 'rb') as _f:
            _f.seek(32)
            _blk_raw = _f.read(chan_data_offset - 32)
        _off = 0
        while _off + 7 < len(_blk_raw):
            ff = struct.unpack_from('<I', _blk_raw, _off)[0]
            ds = struct.unpack_from('<I', _blk_raw, _off + 4)[0]
            if ff >= frame_count or ds == 0:
                break
            comp_blocks.append({'first_frame': ff, 'data_size': ds})
            _off += 8

    # ── Sparse range table ────────────────────────────────────────────────────
    # For standard v2.0 files: sparse ranges follow the comp block table at
    # offset 32 + num_comp_blocks*8, each entry 6 bytes (uint24 + uint24).
    # For auto-detected zstd (v2.2): the block table fills the entire header
    # space; sparse ranges are absent or in a variable-length metadata section
    # we don't parse here — discard to ensure direct channel offset mapping.
    sparse_ranges = []
    if effective_ctype == compression_type and num_sparse_ranges > 0:
        # Standard v2.0: sparse ranges at fixed position after comp block table
        sr_table_offset = 32 + num_comp_blocks * 8
        with open(filepath, 'rb') as f:
            f.seek(sr_table_offset)
            sr_raw = f.read(num_sparse_ranges * 6)
        for i in range(num_sparse_ranges):
            start = sr_raw[i*6] | (sr_raw[i*6+1] << 8) | (sr_raw[i*6+2] << 16)
            count = sr_raw[i*6+3] | (sr_raw[i*6+4] << 8) | (sr_raw[i*6+5] << 16)
            sparse_ranges.append({'start': start, 'count': count})

    fps = 1000.0 / step_time_ms if step_time_ms > 0 else 25.0
    return {
        'filepath':              filepath,
        'chan_data_offset':      chan_data_offset,
        'channel_count':         channel_count,
        'frame_count':           frame_count,
        'step_time_ms':          step_time_ms,
        'fps':                   fps,
        'duration_ms':           frame_count * step_time_ms,
        'compression_type':      effective_ctype,
        'raw_compression_type':  compression_type,
        'num_comp_blocks':       num_comp_blocks,
        'num_sparse_ranges':     num_sparse_ranges,
        'comp_blocks':           comp_blocks,
        'sparse_ranges':         sparse_ranges,
    }


def _sparse_ch_to_frame_byte(sparse_ranges, logical_ch):
    """Map a 0-indexed logical channel number to its byte offset within a packed frame.

    For dense FSEQs (no sparse ranges) the offset equals the logical channel number.
    For sparse FSEQs the frame data only contains channels listed in the sparse range
    table, packed together in range order.  Returns None if the channel falls in a gap.
    """
    if not sparse_ranges:
        return logical_ch   # Dense FSEQ — direct 1:1 mapping

    byte_offset = 0
    for sr in sparse_ranges:
        if logical_ch < sr['start']:
            return None     # Channel is in a gap between ranges
        if logical_ch < sr['start'] + sr['count']:
            return byte_offset + (logical_ch - sr['start'])
        byte_offset += sr['count']
    return None             # Channel is after all ranges


def read_fseq_frame(header, frame_idx, start_ch, ch_count):
    """Return raw channel bytes for one frame's model slice.

    Handles uncompressed (type 0), zlib (type 1), and zstd (type 2) FSEQs.
    Correctly resolves sparse-range FSEQs by mapping the logical start channel
    to its actual byte offset within each packed frame.
    """
    import zlib as _zlib
    filepath      = header['filepath']
    total_ch      = header['channel_count']
    ctype         = header['compression_type']
    sparse_ranges = header.get('sparse_ranges', [])

    # --- Resolve logical channel → byte offset within a frame ---
    frame_byte = _sparse_ch_to_frame_byte(sparse_ranges, start_ch)
    if frame_byte is None:
        # Channel not found in any sparse range.  Possible reasons:
        #   • The FSEQ is model-specific (channels start at 0 in the file).
        #   • The FPP start_channel is the show-level number but the FSEQ only
        #     contains this model's channels.
        # Try offset 0 as a fallback.
        if ch_count <= total_ch:
            frame_byte = 0
            logging.warning(
                f"FSEQ preview: start_ch {start_ch} not in sparse ranges — "
                f"falling back to frame byte 0 (model-specific FSEQ?)"
            )
        else:
            raise ValueError(
                f"Model channel count {ch_count} exceeds FSEQ channel count {total_ch}"
            )

    if ctype == 0:
        # Uncompressed: seek directly to frame + channel byte offset
        offset = header['chan_data_offset'] + frame_idx * total_ch + frame_byte
        with open(filepath, 'rb') as f:
            f.seek(offset)
            return f.read(ch_count)

    elif ctype in (1, 2):
        # zlib (1) or zstd (2) block compression — same block table layout
        if ctype == 2 and not ZSTD_AVAILABLE:
            raise ValueError(
                "FSEQ uses zstd compression — run fpp_install.sh to install the "
                "'zstandard' library, then restart the plugin."
            )

        blocks = header['comp_blocks']
        if not blocks:
            raise ValueError(f"{'zlib' if ctype==1 else 'zstd'} FSEQ has no compression block table")

        # Find the block containing frame_idx
        block_idx = len(blocks) - 1
        for i in range(len(blocks) - 1):
            if blocks[i + 1]['first_frame'] > frame_idx:
                block_idx = i
                break

        block = blocks[block_idx]

        # Byte offset of this block's compressed data in the file
        data_offset = header['chan_data_offset']
        for i in range(block_idx):
            data_offset += blocks[i]['data_size']

        with open(filepath, 'rb') as f:
            f.seek(data_offset)
            compressed = f.read(block['data_size'])

        if ctype == 1:
            decompressed = _zlib.decompress(compressed)
        else:
            dctx = _zstd_mod.ZstdDecompressor()
            try:
                decompressed = dctx.decompress(compressed)
            except Exception:
                # Fallback with an explicit size cap — allow up to 64 frames per
                # block, which is far more than any real FSEQ uses (typically 1-4).
                decompressed = dctx.decompress(
                    compressed, max_output_size=total_ch * 64
                )

        local_frame  = frame_idx - block['first_frame']
        frame_offset = local_frame * total_ch + frame_byte
        return decompressed[frame_offset: frame_offset + ch_count]

    else:
        raise ValueError(f"FSEQ compression type {ctype} is not supported")


def get_model_channel_info(model_name):
    """Return (start_channel_1indexed, channel_count) for a named model from FPP's /api/models.
    channel_count is 3*w*h for RGB, 4*w*h for RGBW, etc. Returns (None, None) on failure."""
    try:
        resp = requests.get(f"{FPP_HOST}/api/models", timeout=3)
        if resp.status_code != 200:
            return None, None
        data = resp.json()
        models = data if isinstance(data, list) else data.get('models', [])
        for m in models:
            name = m.get('Name') or m.get('name') or ''
            if name.lower() == model_name.lower():
                sc = (m.get('StartChannel') or m.get('startChannel')
                      or m.get('start_channel'))
                cc = (m.get('ChannelCount') or m.get('channelCount')
                      or m.get('channel_count'))
                return (int(sc) if sc is not None else None,
                        int(cc) if cc is not None else None)
        return None, None
    except Exception as e:
        logging.warning(f"Could not get channel info for '{model_name}': {e}")
        return None, None

# Keep old name as alias so nothing else breaks
def get_model_start_channel(model_name):
    sc, _ = get_model_channel_info(model_name)
    return sc


def get_fpp_sequences():
    """Get list of sequences from FPP"""
    try:
        fpp_host = FPP_HOST
        response = requests.get(f"{fpp_host}/api/sequence", timeout=3)
        if response.status_code == 200:
            sequences = response.json()
            result = sequences if isinstance(sequences, list) else []
            logging.info(f"FPP sequences raw response: {sequences}")
            logging.info(f"Found {len(result)} sequences: {result}")
            return result
        logging.warning(f"FPP sequences API returned {response.status_code}: {response.text}")
        return []
    except Exception as e:
        logging.error(f"Error fetching FPP sequences: {e}")
        return []

def get_fpp_videos():
    """Get list of video files from FPP media/videos directory."""
    video_exts = {'.mp4', '.mkv', '.avi', '.mpg', '.mpeg', '.mov'}
    try:
        if os.path.isdir(FPP_VIDEOS_PATH):
            return sorted(
                f for f in os.listdir(FPP_VIDEOS_PATH)
                if os.path.splitext(f.lower())[1] in video_exts
            )
    except Exception as e:
        logging.error(f"Error listing FPP videos: {e}")
    return []


def get_fpp_images():
    """Get list of image files from FPP media/images directory."""
    image_exts = {'.jpg', '.jpeg', '.png', '.bmp', '.gif', '.webp'}
    try:
        if os.path.isdir(FPP_IMAGES_PATH):
            return sorted(
                f for f in os.listdir(FPP_IMAGES_PATH)
                if os.path.splitext(f.lower())[1] in image_exts
            )
    except Exception as e:
        logging.error(f"Error listing FPP images: {e}")
    return []


def get_fpp_models():
    """Get list of overlay models from FPP, including pixel dimensions when available.
    Tries /api/overlays/models first (has dimensions), falls back to /api/models."""
    def extract_model(m):
        if not isinstance(m, dict):
            return None
        name = m.get('Name') or m.get('name')
        if not name:
            return None
        # FPP overlay models use rows/cols; channel output models use Width/Height
        w = int(m.get('Width') or m.get('width') or m.get('Cols') or m.get('cols') or
                m.get('Columns') or m.get('columns') or 0)
        h = int(m.get('Height') or m.get('height') or m.get('Rows') or m.get('rows') or 0)
        return {"name": name, "width": w, "height": h}

    def parse_response(data):
        models = []
        if isinstance(data, dict) and 'models' in data:
            for m in data['models']:
                obj = extract_model(m)
                if obj: models.append(obj)
        elif isinstance(data, list):
            for m in data:
                obj = extract_model(m)
                if obj: models.append(obj)
        elif isinstance(data, dict):
            models = [{"name": k, "width": 0, "height": 0} for k in data.keys()]
        return models

    try:
        fpp_host = FPP_HOST
        # /api/overlays/models is the overlay-specific endpoint and includes dimensions
        for endpoint in ['/api/overlays/models', '/api/models']:
            try:
                response = requests.get(f"{fpp_host}{endpoint}", timeout=3)
                if response.status_code == 200:
                    models = parse_response(response.json())
                    if models:
                        has_dims = any(m['width'] > 0 or m['height'] > 0 for m in models)
                        logging.info(f"Got {len(models)} models from {endpoint} (dims: {has_dims})")
                        return models
            except Exception:
                pass

        logging.warning("Could not fetch models from FPP")
        return []
    except Exception as e:
        logging.error(f"Error fetching FPP models: {e}")
        return []

def get_fpp_fonts():
    """Get list of supported fonts from FPP"""
    try:
        fpp_host = FPP_HOST
        response = requests.get(f"{fpp_host}/api/overlays/fonts", timeout=3)
        if response.status_code == 200:
            fonts = response.json()
            logging.info(f"Found {len(fonts)} fonts from FPP")
            return fonts if isinstance(fonts, list) else []
        
        logging.warning(f"FPP fonts API returned status {response.status_code}")
        return []
    except Exception as e:
        logging.error(f"Error fetching FPP fonts: {e}")
        return []

def test_fpp_connection():
    """Test connection to FPP"""
    try:
        fpp_host = FPP_HOST
        response = requests.get(f"{fpp_host}/api/fppd/status", timeout=3)
        if response.status_code == 200:
            status = response.json()
            return True, status.get('fppd', 'Unknown')
        return False, "Unable to connect"
    except Exception as e:
        return False, str(e)

# ============================================================================
# OPTIMIZED WHITELIST LOADING - WITH CACHING
# ============================================================================
def load_removed_names():
    """Load names the user has explicitly deleted (so git pull can't re-add them)"""
    if not os.path.exists(WHITELIST_REMOVED_FILE):
        return set()
    try:
        with open(WHITELIST_REMOVED_FILE, 'r', encoding='latin-1') as f:
            return {line.strip().lower() for line in f if line.strip()}
    except Exception:
        return set()

def load_whitelist():
    """Load and cache the whitelist: global + user-added - user-removed"""
    global _whitelist_cache, _whitelist_mtime

    try:
        mtime_global  = os.path.getmtime(WHITELIST_FILE)          if os.path.exists(WHITELIST_FILE)          else 0
        mtime_added   = os.path.getmtime(WHITELIST_ADDED_FILE)    if os.path.exists(WHITELIST_ADDED_FILE)    else 0
        mtime_removed = os.path.getmtime(WHITELIST_REMOVED_FILE)  if os.path.exists(WHITELIST_REMOVED_FILE)  else 0
        current_mtime = (mtime_global, mtime_added, mtime_removed)

        if _whitelist_cache is None or _whitelist_mtime != current_mtime:
            global_names = set()
            if os.path.exists(WHITELIST_FILE):
                with open(WHITELIST_FILE, 'r', encoding='latin-1') as f:
                    global_names = {line.strip().lower() for line in f if line.strip() and not line.startswith('#')}

            removed = load_removed_names()
            added = load_whitelist_added()

            # If any user-added names are now in the global list, remove from added (global has priority)
            overlap = added & global_names
            if overlap:
                added -= overlap
                with open(WHITELIST_ADDED_FILE, 'w', encoding='utf-8') as f:
                    f.write('\n'.join(sorted(added)) + '\n' if added else '')

            effective = (global_names - removed) | added
            _whitelist_cache = effective  # keep as set for O(1) lookup
            _whitelist_mtime = current_mtime
            logging.info(f"Loaded {len(_whitelist_cache)} names into whitelist cache")

        return _whitelist_cache

    except Exception as e:
        logging.error(f"Error reading whitelist: {e}")
        return []

# ============================================================================
# OPTIMIZED BLOCKLIST LOADING - WITH CACHING
# ============================================================================
def load_blocklist():
    """Load and cache blocked phone numbers, reload if file has changed"""
    global _blocklist_cache, _blocklist_mtime
    
    try:
        current_mtime = os.path.getmtime(BLOCKLIST_FILE)
        
        # Only reload if file changed or not yet loaded
        if _blocklist_cache is None or _blocklist_mtime != current_mtime:
            with open(BLOCKLIST_FILE, 'r') as f:
                blocked = json.load(f)
            
            _blocklist_cache = blocked if isinstance(blocked, list) else []
            _blocklist_mtime = current_mtime
            logging.info(f"Loaded {len(_blocklist_cache)} numbers into blocklist cache")
        
        return _blocklist_cache
        
    except FileNotFoundError:
        return []
    except Exception as e:
        logging.error(f"Error reading blocklist: {e}")
        return []

def save_blocklist(blocklist):
    """Save blocked phone numbers and invalidate cache"""
    global _blocklist_cache, _blocklist_mtime
    
    try:
        with open(BLOCKLIST_FILE, 'w') as f:
            json.dump(blocklist, f, indent=2)
        
        # Update cache immediately
        _blocklist_cache = blocklist
        _blocklist_mtime = os.path.getmtime(BLOCKLIST_FILE)
        
        logging.info(f"Blocklist saved: {len(blocklist)} numbers")
    except Exception as e:
        logging.error(f"Error saving blocklist: {e}")

def is_blocked(phone):
    """Check if phone number is blocked"""
    blocklist = load_blocklist()
    return phone in blocklist

def block_phone(phone):
    """Add phone number to blocklist"""
    blocklist = load_blocklist()
    if phone not in blocklist:
        blocklist.append(phone)
        save_blocklist(blocklist)
        logging.info(f"🚫 Blocked phone number: {phone}")
        return True
    return False

def unblock_phone(phone):
    """Remove phone number from blocklist"""
    blocklist = load_blocklist()
    if phone in blocklist:
        blocklist.remove(phone)
        save_blocklist(blocklist)
        logging.info(f"✅ Unblocked phone number: {phone}")
        return True
    return False

def is_on_whitelist(name):
    """Check if name is on the approved whitelist"""
    if not config.get('use_whitelist', False):
        return True
    
    whitelist = load_whitelist()
    if not whitelist:
        return True
    
    name_lower = name.lower().strip()
    return name_lower in whitelist

def send_sms_response(to_phone, message_type):
    """Send an SMS response to the user based on message type"""
    if not config.get(f'sms_response_{message_type}', False):
        return False
    
    if not twilio_client:
        logging.warning("Cannot send SMS response: Twilio client not initialized")
        return False
    
    # Get the appropriate response message
    response_key = f"response_{message_type}"
    response_message = config.get(response_key, "")
    
    if not response_message:
        logging.warning(f"No response message configured for type: {message_type}")
        return False
    
    try:
        twilio_client.messages.create(
            body=response_message,
            from_=config['twilio_phone_number'],
            to=to_phone
        )
        logging.info(f"📤 Sent SMS response to {to_phone[-4:]}: {message_type}")
        return True
    except Exception as e:
        logging.error(f"Error sending SMS response: {e}")
        return False

def extract_name(message):
    """Extract name from SMS message and convert to proper case"""
    message = message.strip()
    message = re.sub(r'^(hi|hello|hey|merry christmas|happy holidays)[,!.\s]*', '', message, flags=re.IGNORECASE)
    message = re.sub(r'[^a-zA-Z\s-]', '', message)
    message = message.strip()
    
    if message:
        message = message.title()
    
    max_len = config.get('max_message_length', 100)
    return message[:max_len] if message else "Guest"

def is_valid_name(text):
    """Check if text is a valid name"""
    text = ' '.join(text.split())
    words = text.split()
    word_count = len(words)
    
    if config.get('one_word_only', False):
        if word_count != 1:
            return False, "Please send only a first name (one word)"
    elif config.get('two_words_max', True):
        if word_count > 2:
            return False, "Please send only a name (1-2 words, no sentences)"
    
    if len(text) > 50:
        return False, "Message too long - please send only a name"
    
    return True, ""

# ============================================================================
# OPTIMIZED PROFANITY FILTER - WITH CACHING AND PRE-COMPILED REGEX
# ============================================================================
def load_blacklist_removed():
    """Load words the user has explicitly removed from the profanity filter"""
    if not os.path.exists(BLACKLIST_REMOVED_FILE):
        return set()
    try:
        with open(BLACKLIST_REMOVED_FILE, 'r', encoding='latin-1') as f:
            return {line.strip().lower() for line in f if line.strip()}
    except Exception:
        return set()

def load_blacklist_added():
    """Load words the user has added beyond the global list"""
    if not os.path.exists(BLACKLIST_ADDED_FILE):
        return set()
    try:
        with open(BLACKLIST_ADDED_FILE, 'r', encoding='utf-8') as f:
            return {line.strip().lower() for line in f if line.strip()}
    except Exception:
        return set()

def load_whitelist_added():
    """Load names the user has added beyond the global list"""
    if not os.path.exists(WHITELIST_ADDED_FILE):
        return set()
    try:
        with open(WHITELIST_ADDED_FILE, 'r', encoding='utf-8') as f:
            return {line.strip().lower() for line in f if line.strip()}
    except Exception:
        return set()

def load_blacklist_words():
    """Return effective word list: global + user-added - user-removed"""
    try:
        global_words = set()
        if os.path.exists(BLACKLIST_FILE):
            with open(BLACKLIST_FILE, 'r', encoding='latin-1') as f:
                global_words = {line.strip().lower() for line in f if line.strip() and not line.startswith('#')}

        removed = load_blacklist_removed()
        added = load_blacklist_added()

        # If any user-added words are now in the global list, remove from added (global has priority)
        overlap = added & global_words
        if overlap:
            added -= overlap
            with open(BLACKLIST_ADDED_FILE, 'w', encoding='utf-8') as f:
                f.write('\n'.join(sorted(added)) + '\n' if added else '')

        effective = (global_words - removed) | added
        return sorted(effective)
    except Exception as e:
        logging.error(f"Error reading blacklist words: {e}")
        return []

def load_blacklist():
    """Load and cache the profanity blacklist as a single combined regex for fast one-pass matching"""
    global _blacklist_cache, _blacklist_mtime

    try:
        mtime_global  = os.path.getmtime(BLACKLIST_FILE)         if os.path.exists(BLACKLIST_FILE)  else 0
        mtime_added   = os.path.getmtime(BLACKLIST_ADDED_FILE)   if os.path.exists(BLACKLIST_ADDED_FILE)   else 0
        mtime_removed = os.path.getmtime(BLACKLIST_REMOVED_FILE) if os.path.exists(BLACKLIST_REMOVED_FILE) else 0
        current_mtime = (mtime_global, mtime_added, mtime_removed)

        if _blacklist_cache is None or _blacklist_mtime != current_mtime:
            words = load_blacklist_words()
            if words:
                # Single combined pattern — one regex pass instead of N passes
                combined = '|'.join(r'\b' + re.escape(w) + r'\b' for w in words)
                _blacklist_cache = re.compile(combined)
            else:
                _blacklist_cache = None
            _blacklist_mtime = current_mtime
            logging.info(f"Loaded {len(words)} words into profanity filter cache (combined regex)")

        return _blacklist_cache

    except Exception as e:
        logging.error(f"Error reading blacklist: {e}")
        return None

def contains_profanity(text):
    """Check for profanity using a single combined regex pattern"""
    if not config['profanity_filter']:
        return False

    pattern = load_blacklist()
    if not pattern:
        return False

    text_lower = text.lower()

    if pattern.search(text_lower):
        logging.info(f"🚫 Profanity detected in '{text}'")
        return True
    
    return False

def _parse_log_date(log_entry):
    """Safely parse the date from a log entry's timestamp field."""
    try:
        return datetime.fromisoformat(log_entry.get('timestamp', '')).date()
    except (ValueError, TypeError):
        return None

def get_day_log_path(date=None):
    """Return the path to the daily message log file for the given date (default: today)."""
    if date is None:
        date = datetime.now().date()
    return os.path.join(MESSAGES_DIR, f"messages_{date.isoformat()}.json")

def cleanup_old_logs():
    """Delete daily message log files older than 7 days from MESSAGES_DIR."""
    try:
        cutoff = datetime.now().date() - timedelta(days=7)
        for filename in os.listdir(MESSAGES_DIR):
            if not filename.startswith("messages_") or not filename.endswith(".json"):
                continue
            date_str = filename[len("messages_"):-len(".json")]
            try:
                file_date = datetime.strptime(date_str, "%Y-%m-%d").date()
                if file_date < cutoff:
                    os.remove(os.path.join(MESSAGES_DIR, filename))
                    logging.error(f"Deleted old log: {filename}")
            except ValueError:
                pass
    except Exception as e:
        logging.error(f"Error during cleanup_old_logs: {e}")

def save_queue():
    """Persist the current in-memory queue to QUEUE_FILE. Must be called OUTSIDE queue_lock."""
    try:
        snapshot = list(message_queue)
        with open(QUEUE_FILE, 'w') as f:
            json.dump(snapshot, f, indent=2)
    except Exception as e:
        logging.error(f"Error saving queue: {e}")

def load_queue_from_file():
    """Restore queued items from QUEUE_FILE into the deque at startup. Returns count restored."""
    try:
        with open(QUEUE_FILE, 'r') as f:
            items = json.load(f)
        restored = 0
        for item in items:
            if item.get('status') == 'queued':
                message_queue.append(item)
                restored += 1
        if restored:
            logging.error(f"Queue restore: {restored} item(s) loaded from disk")
        return restored
    except FileNotFoundError:
        return 0
    except Exception as e:
        logging.error(f"Error restoring queue: {e}")
        return 0

def get_message_count(phone):
    """Get number of messages from a phone number today"""
    try:
        with open(get_day_log_path(), 'r') as f:
            logs = json.load(f)
        today = datetime.now().date()
        return sum(1 for log in logs
                   if log.get('phone_full') == phone and _parse_log_date(log) == today)
    except (FileNotFoundError, json.JSONDecodeError):
        return 0
    except Exception as e:
        logging.error(f"Error in get_message_count: {e}")
        return 0

def has_sent_name_today(phone, name):
    """Check if this phone has already sent this specific name today"""
    try:
        with open(get_day_log_path(), 'r') as f:
            logs = json.load(f)
        today = datetime.now().date()
        for log in logs:
            if (log.get('phone_full') == phone
                    and log.get('extracted_name', '').lower() == name.lower()
                    and log.get('status', '') in ('displayed', 'displaying', 'queued')
                    and _parse_log_date(log) == today):
                return True
        return False
    except (FileNotFoundError, json.JSONDecodeError):
        return False
    except Exception as e:
        logging.error(f"Error in has_sent_name_today: {e}")
        return False

def save_last_sid(sid):
    """Save the last processed message SID to file"""
    try:
        with open(LAST_SID_FILE, 'w') as f:
            f.write(sid)
    except Exception as e:
        logging.error(f"Error saving last SID: {e}")

def log_message(phone, message, name, status):
    """Log received message to today's daily log file."""
    try:
        log_path = get_day_log_path()
        try:
            with open(log_path, 'r') as f:
                logs = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            logs = []
        logs.append({
            "timestamp": datetime.now().isoformat(),
            "phone": phone,
            "phone_full": phone,
            "message": message,
            "extracted_name": name,
            "status": status
        })
        with open(log_path, 'w') as f:
            json.dump(logs, f, indent=2)
        logging.info(f"✅ Message logged: {phone[-4:]} | {name} | {status}")
    except Exception as e:
        logging.error(f"Error logging message: {e}")

def update_message_status(phone, name, new_status):
    """Update the status of a message — searches today's file, then yesterday's if not found."""
    def _update_in_file(path):
        try:
            with open(path, 'r') as f:
                logs = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return False
        found = False
        for log in reversed(logs):
            if log.get('phone_full') == phone and log.get('extracted_name') == name:
                log['status'] = new_status
                log['status_updated'] = datetime.now().isoformat()
                found = True
                break
        if found:
            try:
                with open(path, 'w') as f:
                    json.dump(logs, f, indent=2)
                logging.info(f"Updated status: {phone[-4:]} | {name} | {new_status}")
            except Exception as e:
                logging.error(f"Error writing status update: {e}")
        return found

    try:
        today = datetime.now().date()
        if not _update_in_file(get_day_log_path(today)):
            _update_in_file(get_day_log_path(today - timedelta(days=1)))
    except Exception as e:
        logging.error(f"Error updating message status: {e}")

def add_to_queue(name, phone, message):
    """Add a message to the display queue"""
    global message_queue
    
    try:
        queue_item = {
            "name": name,
            "phone": phone,
            "phone_last4": phone[-4:],
            "message": message,
            "timestamp": datetime.now().isoformat(),
            "status": "queued"
        }
        
        logging.info(f"📋 Created queue item: {queue_item}")
        
        with queue_lock:
            message_queue.append(queue_item)
            queue_position = len(message_queue)

        save_queue()  # persist OUTSIDE queue_lock

        logging.info(f"📋 Added to queue (position {queue_position}): {name}")
        return True
    except Exception as e:
        logging.error(f"💥 ERROR in add_to_queue: {e}")
        import traceback
        logging.error(traceback.format_exc())
        return False

def send_to_fpp(name):
    """Send name to FPP - Start name sequence and display text overlay"""
    try:
        fpp_host = FPP_HOST
        name_playlist = config.get('name_display_playlist', '')
        overlay_model = config.get('overlay_model_name', 'Texting Matrix')
        
        # Build per-line rendered items from message_lines config
        message_lines = config.get('message_lines', ['Merry Christmas', '{name}!', '', ''])
        line_positions_cfg = config.get('line_positions', [])
        font_size_cfg  = config.get('text_font_size', 48)
        line_h_px      = int(font_size_cfg * 1.2)  # approximate line height in model pixels

        # Collect non-empty rendered lines + their saved positions
        rendered_lines = []  # [(rendered_text, px, py), ...]
        for i, tmpl_line in enumerate(message_lines):
            if not tmpl_line.strip():
                continue
            rendered = tmpl_line.replace('{name}', name)
            pos = line_positions_cfg[i] if i < len(line_positions_cfg) else {'x': -1, 'y': -1}
            rendered_lines.append((rendered, pos.get('x', -1), pos.get('y', -1)))

        # Compute stacked Y defaults (group centered vertically, spaced by line_h_px)
        mh_pre = config.get('overlay_model_height', 0)
        non_empty_count = len(rendered_lines)
        stack_start_y = max(0, (mh_pre - non_empty_count * line_h_px) // 2) if mh_pre > 0 else 0

        # line_items for static PIL: replace y=-1 with stacked default
        line_items = []
        for idx, (rendered, px, py) in enumerate(rendered_lines):
            resolved_y = stack_start_y + idx * line_h_px if py < 0 else py
            line_items.append((rendered, px, resolved_y))

        # scroll_line_items for L2R/R2L: [(text, y_pos), ...]
        scroll_lr_items = [(rendered, stack_start_y + idx * line_h_px if py < 0 else py)
                           for idx, (rendered, px, py) in enumerate(rendered_lines)]
        # scroll_line_items for T2B/B2T: [(text, x_pos), ...] — x=-1 means auto-center
        scroll_tb_items = [(rendered, px)
                           for (rendered, px, py) in rendered_lines]

        # FPP API fallback: join lines with newline
        display_message = '\n'.join(r for r, _, _ in rendered_lines) if rendered_lines else name

        logging.info(f"🎄 ========== STARTING DISPLAY FOR: {name} ==========")
        logging.info(f"📺 FPP Host: {fpp_host}")
        logging.info(f"🎬 Name Display Playlist: {name_playlist}")
        logging.info(f"📝 Overlay Model: {overlay_model}")
        logging.info(f"📝 Message Lines: {message_lines}")
        logging.info(f"📝 Display Message: {display_message}")
        
        # Step 1: Start the name display playlist/sequence/video/image (background)
        if name_playlist:
            try:
                logging.info(f"⏸️  STEP 1: Stopping any running playlist...")
                requests.get(f"{fpp_host}/api/playlists/stop", timeout=3)
                # Note: background FSEQ Effect is NOT stopped here — the names sequence
                # (foreground) will automatically suppress it and it auto-resumes after.
                time.sleep(0.1)

                logging.info(f"▶️  STEP 2: Starting name display content: {name_playlist}")

                import urllib.parse
                if name_playlist.startswith('seq:'):
                    # FSEQ Effect (loop=true, background=true): plays as background so
                    # overlay model renders on top with correct text colors.
                    seq_name = name_playlist[4:].removesuffix('.fseq')
                    effect_url = f"{fpp_host}/api/command/{urllib.parse.quote('FSEQ Effect Start')}/{urllib.parse.quote(seq_name)}/true/true"
                    start_response = requests.get(effect_url, timeout=3)
                    logging.info(f"   FSEQ Effect Start (names): {start_response.status_code} - {start_response.text}")

                elif name_playlist.startswith('vid:'):
                    vid_name = name_playlist[4:]
                    _start_video_looping(fpp_host, vid_name)
                    logging.info(f"   Names video started via playlist: {vid_name}")

                elif name_playlist.startswith('img:'):
                    # Image background — will be composited with text in Step 2 below
                    logging.info(f"   Image background: will render in overlay step")

                else:
                    command = "Start Playlist"
                    encoded_playlist = urllib.parse.quote(name_playlist)
                    command_url = f"{fpp_host}/api/command/{urllib.parse.quote(command)}/{encoded_playlist}/true/false"
                    start_response = requests.get(command_url, timeout=3)
                    logging.info(f"   Start playlist response: {start_response.status_code}")

                time.sleep(0.3)

            except Exception as e:
                logging.error(f"💥 ERROR starting name playlist: {e}")
        
        # Step 2: Display text ON TOP of the sequence
        if overlay_model:
            try:
                logging.info(f"📝 STEP 3: Displaying text on model: {overlay_model}")

                text_position = config.get('text_position', 'Center')
                text_color = config.get('text_color', '#FF0000')
                text_font = config.get('text_font', 'FreeSans')
                font_size = config.get('text_font_size', 48)
                scroll_speed = config.get('scroll_speed', 20)

                import urllib.parse

                if not text_color.startswith('#'):
                    text_color = '#' + text_color

                encoded_model = urllib.parse.quote(overlay_model)

                # Map config abbreviations to FPP API full strings
                position_map = {
                    'Center': 'Center',
                    'L2R': 'Left to Right',
                    'R2L': 'Right to Left',
                    'T2B': 'Top to Bottom',
                    'B2T': 'Bottom to Top',
                }
                fpp_position = position_map.get(text_position, 'Center')

                state_url = f"{fpp_host}/api/overlays/model/{encoded_model}/state"
                text_url  = f"{fpp_host}/api/overlays/model/{encoded_model}/text"

                # Order matters to avoid flash of previous name:
                # 1. Disable overlay (State 0) — hides it
                # 2. Write new frame (PIL shm or FPP text API)
                # 3. Enable overlay (State 3 Transparent RGB) — activates cleanly
                requests.put(state_url, json={"State": 0}, timeout=3)

                mw = config.get('overlay_model_width', 0)
                mh = config.get('overlay_model_height', 0)
                logging.info(f"📐 Overlay: model={overlay_model} overlay_size={mw}x{mh} "
                             f"lines={len(line_items)} mode={text_position} PIL={PIL_AVAILABLE}")

                shm_rendered = False
                scroll_started = False

                # For img: names content, composite text onto the image (State 2 = Opaque)
                img_bg_path = None
                if name_playlist.startswith('img:'):
                    img_bg_path = os.path.join(FPP_IMAGES_PATH, name_playlist[4:])
                    if not os.path.exists(img_bg_path):
                        logging.warning(f"⚠️ Image not found: {img_bg_path}")
                        img_bg_path = None

                if PIL_AVAILABLE and mw > 0 and mh > 0:
                    if text_position == 'Center':
                        if img_bg_path:
                            shm_rendered = render_image_to_shm(
                                img_bg_path, overlay_model, mw, mh,
                                line_items=line_items, font_name=text_font,
                                font_size=font_size, color_hex=text_color
                            )
                        else:
                            shm_rendered = render_to_shm(
                                line_items, overlay_model,
                                mw, mh, text_font, font_size, text_color
                            )
                    elif text_position in ('L2R', 'R2L', 'T2B', 'B2T'):
                        duration = config.get('display_duration', 30)
                        items = scroll_lr_items if text_position in ('L2R', 'R2L') else scroll_tb_items
                        scroll_started = scroll_via_shm(
                            items, overlay_model,
                            mw, mh, text_position,
                            text_font, font_size, text_color,
                            scroll_speed, duration
                        )
                        if scroll_started:
                            time.sleep(0.05)  # let first frame land before enabling overlay
                elif mw == 0 or mh == 0:
                    logging.warning(f"⚠️ PIL skipped: overlay dimensions not saved ({mw}x{mh}). "
                                    f"Re-select the model in config to save dimensions.")
                elif not PIL_AVAILABLE:
                    logging.warning("⚠️ Pillow not installed — using FPP text API (no X/Y positioning). "
                                    "Run plugin install to add Pillow.")

                if not shm_rendered and not scroll_started:
                    text_payload = {
                        "Message": display_message,
                        "Color": text_color,
                        "Font": text_font,
                        "FontSize": font_size,
                        "Position": fpp_position,
                        "PixelsPerSecond": scroll_speed * 20,
                        "AntiAlias": True,
                        "AutoEnable": False
                    }
                    response = requests.put(text_url, json=text_payload, timeout=10)
                    logging.info(f"📡 FPP text API fallback: {response.status_code}")
                else:
                    logging.info(f"✅ PIL {'scroll' if scroll_started else 'static'} render active")

                # State 2 (Opaque) for image background so it covers the display fully.
                # State 3 (Transparent RGB) for normal/FSEQ background (black = transparent).
                overlay_state = 2 if (img_bg_path and shm_rendered) else 3
                state_resp = requests.put(state_url, json={"State": overlay_state}, timeout=3)
                logging.info(f"   Overlay state={overlay_state}: {state_resp.status_code}")

            except Exception as e:
                logging.error(f"💥 ERROR sending text command: {e}")
                import traceback
                logging.error(traceback.format_exc())
        
        logging.info(f"✅ ========== DISPLAY COMMANDS COMPLETED ==========")
        return True
        
    except Exception as e:
        logging.error(f"💥 CRITICAL ERROR in send_to_fpp: {e}")
        import traceback
        logging.error(traceback.format_exc())
        return False
def _start_video_looping(fpp_host, vid_name):
    """Start a video via FPP 'Play Media' command.
    FPP clamps loop count < 1 to 1 (play once); there is no infinite-loop value.
    999 bypasses the UI cap of 100 and tells VLC input-repeat=998 (~8hrs at 30s/loop)."""
    import urllib.parse
    cmd_url = (f"{fpp_host}/api/command/{urllib.parse.quote('Play Media')}/"
               f"{urllib.parse.quote(vid_name)}/999/0")
    r = requests.get(cmd_url, timeout=5)
    logging.info(f"▶️  Play Media ({vid_name}): {r.status_code} - {r.text}")
    return r.status_code == 200


def start_default_playlist():
    """Start the configured default waiting playlist/sequence.
    For sequences (seq:), uses FSEQ Effect (loop=true, background=true) so it loops
    seamlessly as a background effect."""
    import urllib.parse
    fpp_host = FPP_HOST
    default_playlist = config.get('default_playlist', '')

    if not default_playlist:
        logging.info("ℹ️  No default playlist configured — skipping auto-start")
        return False

    try:
        if default_playlist.startswith('seq:'):
            # FSEQ Effect Start uses the display name WITHOUT .fseq extension
            seq_name = default_playlist[4:]
            seq_name = seq_name.removesuffix('.fseq')

            # loop=true, background=true: loops natively, auto-suppressed by foreground
            # sequences, auto-resumes when foreground stops
            effect_url = f"{fpp_host}/api/command/{urllib.parse.quote('FSEQ Effect Start')}/{urllib.parse.quote(seq_name)}/true/true"
            logging.info(f"▶️  Starting FSEQ Effect Start (loop+background): {seq_name}")
            logging.info(f"   URL: {effect_url}")
            response = requests.get(effect_url, timeout=3)
            logging.info(f"   Response: {response.status_code} - {response.text}")

            if response.status_code == 200:
                logging.info(f"✅ FSEQ Effect Start — looping in background")
                return True

            logging.error(f"❌ FSEQ Effect Start failed: {response.status_code} - {response.text}")
            return False

        elif default_playlist.startswith('vid:'):
            vid_name = default_playlist[4:]
            logging.info(f"▶️  Starting video via playlist (loop): {vid_name}")
            return _start_video_looping(fpp_host, vid_name)

        elif default_playlist.startswith('img:'):
            # Static image — render to overlay model shared memory
            img_name = default_playlist[4:]
            img_path = os.path.join(FPP_IMAGES_PATH, img_name)
            overlay_model = config.get('overlay_model_name', '')
            mw = config.get('overlay_model_width', 0)
            mh = config.get('overlay_model_height', 0)
            if PIL_AVAILABLE and overlay_model and mw > 0 and mh > 0 and os.path.exists(img_path):
                ok = render_image_to_shm(img_path, overlay_model, mw, mh)
                if ok:
                    encoded = urllib.parse.quote(overlay_model)
                    state_url = f"{fpp_host}/api/overlays/model/{encoded}/state"
                    requests.put(state_url, json={"State": 2}, timeout=3)  # Opaque
                    logging.info(f"✅ Image background set: {img_name}")
                    return True
            logging.warning(f"⚠️  Image background failed: PIL={PIL_AVAILABLE} model={overlay_model} "
                            f"dims={mw}x{mh} exists={os.path.exists(img_path) if img_path else False}")
            return False

        else:
            command = "Start Playlist"
            command_url = f"{fpp_host}/api/command/{urllib.parse.quote(command)}/{urllib.parse.quote(default_playlist)}/true/true"
            logging.info(f"▶️  Starting playlist: {default_playlist}")
            logging.info(f"   URL: {command_url}")
            response = requests.get(command_url, timeout=3)
            logging.info(f"   Response: {response.status_code} - {response.text}")

            if response.status_code == 200:
                logging.info(f"✅ Playlist started")
                return True
            else:
                logging.error(f"❌ Failed to start playlist: {response.status_code}")
                return False
    except Exception as e:
        logging.error(f"Error starting default playlist: {e}")
        return False


def return_to_default_playlist():
    """Clear text overlay and stop the names sequence/playlist.
    If the default is a seq: (FSEQ Effect background), the background auto-resumes.
    If the default is a playlist, restart it explicitly."""
    try:
        fpp_host = FPP_HOST
        overlay_model = config.get('overlay_model_name', 'Texting Matrix')

        if overlay_model:
            try:
                logging.info(f"🧹 Clearing text from model: {overlay_model}")
                import urllib.parse
                encoded_model = urllib.parse.quote(overlay_model)
                # Disable the overlay model (State 0) to stop rendering text
                state_url = f"{fpp_host}/api/overlays/model/{encoded_model}/state"
                response = requests.put(state_url, json={"State": 0}, timeout=3)
                logging.info(f"   Disable overlay (State 0): {response.status_code} - {response.text}")
                if response.status_code == 200:
                    logging.info(f"✅ Text cleared")
                else:
                    logging.warning(f"⚠️  Could not clear text: {response.status_code}")
            except Exception as e:
                logging.warning(f"Could not clear text: {e}")

        with queue_lock:
            queue_length = len(message_queue)

        if queue_length > 0:
            logging.info(f"📋 Queue has {queue_length} more names — skipping return-to-default")
            return

        import urllib.parse
        name_playlist  = config.get('name_display_playlist', '')
        default_content = config.get('default_playlist', '')

        if name_playlist.startswith('seq:'):
            # Stop the names FSEQ Effect — waiting FSEQ keeps running underneath
            seq_name = name_playlist[4:].removesuffix('.fseq')
            r = requests.get(f"{fpp_host}/api/command/{urllib.parse.quote('FSEQ Effect Stop')}/{urllib.parse.quote(seq_name)}", timeout=3)
            logging.info(f"⏹️  FSEQ Effect Stop (names): {r.status_code} - {r.text}")

        elif name_playlist.startswith('vid:'):
            # Stop the names video playlist
            r = requests.get(f"{fpp_host}/api/command/{urllib.parse.quote('Stop Now')}", timeout=3)
            logging.info(f"⏹️  Stop Now (names video): {r.status_code}")
            # Restart default content if it's also vid: or img: (won't auto-resume)
            if default_content.startswith('vid:') or default_content.startswith('img:'):
                start_default_playlist()

        elif name_playlist.startswith('img:'):
            # Image mode: overlay was used, nothing extra to stop.
            # Re-apply default content (img:/vid: won't auto-resume)
            if default_content.startswith('vid:') or default_content.startswith('img:'):
                start_default_playlist()

        else:
            r = requests.get(f"{fpp_host}/api/command/{urllib.parse.quote('Stop Now')}", timeout=3)
            logging.info(f"⏹️  Stop Now ({r.status_code})")
            # If default is vid:/img:, restart it (it was interrupted)
            if default_content.startswith('vid:') or default_content.startswith('img:'):
                start_default_playlist()

    except Exception as e:
        logging.error(f"Error in return_to_default_playlist: {e}")


def display_worker():
    """Background worker that displays messages from the queue"""
    global currently_displaying, message_queue, stop_display
    
    logging.info("🎬 Display worker thread started")
    
    while not stop_display:
        try:
            with queue_lock:
                if len(message_queue) == 0:
                    currently_displaying = None
                    _next_item = None
                else:
                    _next_item = message_queue.popleft()

            if _next_item is None:
                time.sleep(0.1)
                continue

            save_queue()  # item popped — remove from persistent queue before display starts

            currently_displaying = _next_item
            
            name = currently_displaying['name']
            phone = currently_displaying['phone']
            
            logging.info(f"🎬 NOW DISPLAYING: {name} (from {phone[-4:]})")
            
            try:
                logging.info(f"📝 Updating status to 'displaying'...")
                update_message_status(phone, name, "displaying")
                logging.info(f"✅ Status updated to 'displaying'")
            except Exception as e:
                logging.error(f"💥 Error updating status to displaying: {e}")
            
            try:
                logging.info(f"📺 Sending to FPP display...")
                send_to_fpp(name)
                logging.info(f"✅ Sent to FPP display")
            except Exception as e:
                logging.error(f"💥 Error sending to FPP: {e}")
            
            display_duration = config.get('display_duration', 30)
            logging.info(f"⏱️  Displaying for {display_duration} seconds...")
            
            try:
                time.sleep(display_duration)
                logging.info(f"⏱️  Display duration completed")
            except Exception as e:
                logging.error(f"💥 Error during sleep: {e}")
            
            try:
                logging.info(f"🔄 Returning to default playlist...")
                return_to_default_playlist()
            except Exception as e:
                logging.error(f"💥 Error returning to default: {e}")
            
            logging.info(f"✅ FINISHED DISPLAYING: {name}")
            
            try:
                update_message_status(phone, name, "displayed")
                logging.info(f"✅ Status updated to 'displayed'")
            except Exception as e:
                logging.error(f"💥 Error updating status to displayed: {e}")
            
            currently_displaying = None
            
        except Exception as e:
            logging.error(f"💥 Error in display worker: {e}")
            import traceback
            logging.error(traceback.format_exc())
            currently_displaying = None
            time.sleep(1)
    
    logging.info("🛑 Display worker stopped")

def get_queue_status():
    """Get current queue status for display on web page"""
    try:
        if queue_lock.acquire(timeout=2):
            try:
                queue_list = list(message_queue)
                current = currently_displaying
            finally:
                queue_lock.release()
        else:
            queue_list = []
            current = None
    except Exception as e:
        logging.error(f"Error getting queue status: {e}")
        queue_list = []
        current = None
    
    status = {
        "currently_displaying": current,
        "queue": queue_list,
        "queue_length": len(queue_list),
        "show_live": config.get('enabled', False)
    }
    
    return status

def poll_twilio():
    """Poll Twilio for new messages"""
    global last_message_sid, stop_polling

    logging.info("🚀 Twilio polling started")
    first_run = last_message_sid is None
    thread_start_time = datetime.now(timezone.utc)  # used to skip pre-start messages on first run
    _current_day = datetime.now().date()

    while not stop_polling:
        try:
            # Midnight cleanup — delete daily log files older than 7 days
            today = datetime.now().date()
            if today != _current_day:
                _current_day = today
                try:
                    cleanup_old_logs()
                    logging.error(f"🌙 Midnight: old daily logs cleaned up for {today}")
                except Exception as e:
                    logging.error(f"Error during midnight cleanup: {e}")

            if not twilio_client:
                time.sleep(config.get('poll_interval', 2))
                continue

            logging.debug("📡 Polling Twilio for new messages...")

            messages = twilio_client.messages.list(
                to=config['twilio_phone_number'],
                date_sent_after=datetime.utcnow() - timedelta(minutes=10),
                limit=20
            )

            logging.debug(f"📨 Found {len(messages)} total messages in last 10 minutes")

            new_messages = []
            for msg in messages:
                if last_message_sid and msg.sid == last_message_sid:
                    logging.debug(f"✓ Reached last processed message SID: {last_message_sid[:10]}...")
                    break
                new_messages.append(msg)

            logging.debug(f"🆕 Found {len(new_messages)} NEW messages to process")

            if first_run:
                # Anchor to the newest SID so future polls don't re-process old messages
                if messages:
                    last_message_sid = messages[0].sid
                    save_last_sid(messages[0].sid)
                # Filter new_messages to only those that arrived after this thread started
                # so we don't replay messages that predate the polling session
                new_messages = [
                    m for m in new_messages
                    if m.date_sent and m.date_sent >= thread_start_time
                ]
                logging.info(
                    f"⚙️ First run: baseline SID set, {len(new_messages)} post-start message(s) to process"
                )
                first_run = False
                if not new_messages:
                    time.sleep(config['poll_interval'])
                    continue
                # fall through to process any messages that arrived after thread start
            
            for msg in reversed(new_messages):
                from_number = msg.from_
                body = msg.body

                logging.info(f"📱 SMS from {from_number[-4:]}: '{body[:30]}'")  # keep at INFO — new message is significant

                try:
                    if not config.get('enabled', False):
                        # Show not live — reply if enabled, then discard
                        if not is_blocked(from_number):
                            send_sms_response(from_number, "show_not_live")
                            log_message(from_number, body, "", "show_not_live")
                            logging.info(f"🔴 Show not live reply sent to {from_number[-4:]}")
                        last_message_sid = msg.sid
                        save_last_sid(msg.sid)
                        continue

                    # Exactly one branch fires — only one SMS response is ever sent per message
                    if is_blocked(from_number):
                        logging.info(f"🚫 Blocked: {from_number[-4:]}")
                        log_message(from_number, body, "", "blocked")
                        send_sms_response(from_number, "blocked")

                    else:
                        name = extract_name(body)
                        logging.debug(f"👤 Extracted name: '{name}'")
                        max_msgs = config.get('max_messages_per_phone', 0)
                        msg_count = get_message_count(from_number) if max_msgs > 0 else 0
                        is_valid, _ = is_valid_name(name)

                        if max_msgs > 0 and msg_count >= max_msgs:
                            logging.info(f"⛔ Rate limited: {from_number[-4:]}")
                            log_message(from_number, body, "", "rate_limited")
                            send_sms_response(from_number, "rate_limited")

                        elif not config.get('allow_duplicate_names', False) and has_sent_name_today(from_number, name):
                            logging.info(f"🔄 Duplicate name: {name}")
                            log_message(from_number, body, name, "duplicate_name_today")
                            send_sms_response(from_number, "duplicate")

                        elif not is_valid and not config.get('use_whitelist', False):
                            logging.info(f"❌ Invalid format: '{body[:20]}'")
                            log_message(from_number, body, name, "invalid_format")
                            send_sms_response(from_number, "invalid_format")

                        elif not is_on_whitelist(name):
                            logging.info(f"❌ Not on whitelist: {name}")
                            log_message(from_number, body, name, "not_on_whitelist")
                            send_sms_response(from_number, "not_whitelisted")

                        elif config['profanity_filter'] and contains_profanity(body):
                            logging.info(f"❌ Profanity rejected")
                            log_message(from_number, body, name, "profanity")
                            send_sms_response(from_number, "profanity")

                        else:
                            success = add_to_queue(name, from_number, body)
                            if success:
                                logging.info(f"✅ Queued: {name}")
                                log_message(from_number, body, name, "queued")
                                send_sms_response(from_number, "success")
                            else:
                                logging.warning(f"❌ Queue error: {name}")
                                log_message(from_number, body, name, "error")

                    last_message_sid = msg.sid
                    save_last_sid(msg.sid)
                    logging.debug(f"💾 Saved SID: {msg.sid[:10]}...")

                except Exception as e:
                    logging.error(f"💥 EXCEPTION processing message: {e}")
                    import traceback
                    logging.error(traceback.format_exc())
            
        except Exception as e:
            logging.error(f"💥 Error polling Twilio: {e}")
        
        time.sleep(config['poll_interval'])
    
    logging.info("🛑 Twilio polling stopped")
@app.route('/')
def index():
    """Main configuration page"""
    html = """
    <!DOCTYPE html>
    <html>
    <head>
        <title>FPP SMS Plugin Configuration</title>
        <style>
            body { font-family: Arial, sans-serif; margin: 0; padding: 20px; background: #ffffff; color: #333; }
            h1 { color: #4CAF50; }
            .section { background: #f8f8f8; padding: 20px; margin: 20px 0; border-radius: 5px; border: 1px solid #ddd; }
            label { display: block; margin: 10px 0 5px; font-weight: bold; }
            input, select, textarea { width: 100%; padding: 8px; margin-bottom: 10px; border: 1px solid #ccc; border-radius: 4px; background: #fff; color: #333; box-sizing: border-box; }
            button { background: #4CAF50; color: white; padding: 10px 20px; border: none; border-radius: 4px; cursor: pointer; margin: 5px; }
            button:hover { background: #45a049; }
            .test-btn { background: #2196F3; }
            .test-btn:hover { background: #0b7dda; }
            .view-btn { background: #FF9800; }
            .view-btn:hover { background: #e68900; }
            .refresh-btn { background: #9C27B0; }
            .refresh-btn:hover { background: #7B1FA2; }
            .checkbox-label { display: inline; margin-left: 5px; font-weight: normal; }
            input[type="checkbox"] { width: auto; }
            .success { color: #4CAF50; }
            .error { color: #f44336; }
            .info { background: #e3f2fd; padding: 15px; border-radius: 5px; margin: 20px 0; border: 1px solid #90caf9; color: #333; }
            .queue-info { background: #f3e5f5; padding: 15px; border-radius: 5px; margin: 20px 0; border: 1px solid #ce93d8; color: #333; }
            h3 { color: #4CAF50; margin-top: 20px; margin-bottom: 10px; }
            .help-text { font-size: 12px; color: #666; margin-top: 5px; }
            select#text_font option { padding: 8px; font-size: 14px; }
            .columns { display: flex; gap: 20px; margin: 0; align-items: stretch; }
            .column { flex: 1; min-width: 0; display: flex; flex-direction: column; }
            .column .section { flex: 0 0 auto; }
            .column .section:last-child { flex: 1; }
            .top-actions { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; margin: 15px 0; padding: 15px; background: #f8f8f8; border-radius: 5px; border: 1px solid #ddd; }
            .tabs { display: flex; gap: 0; margin: 20px 0 0 0; border-bottom: 2px solid #4CAF50; }
            .tab-btn { background: #f0f0f0; color: #555; padding: 10px 24px; border: 1px solid #ddd; border-bottom: none; border-radius: 4px 4px 0 0; cursor: pointer; font-size: 14px; font-weight: bold; margin-right: 4px; }
            .tab-btn.active { background: #4CAF50; color: white; border-color: #4CAF50; }
            .tab-btn:hover:not(.active) { background: #e8e8e8; }
            .tab-content { display: none; }
            .tab-content.active { display: block; }
        </style>
    </head>
    <body><script>if('scrollRestoration'in history)history.scrollRestoration='manual';function _toTop(){window.scrollTo(0,0);document.documentElement.scrollTop=0;document.body.scrollTop=0;try{window.parent.postMessage({type:'scrollTop'},'*');}catch(e){}}_toTop();document.addEventListener('DOMContentLoaded',_toTop);window.addEventListener('load',_toTop);</script>

        <!-- Tab navigation -->
        <div class="tabs" style="display:flex; align-items:center; gap:6px;">
            <button class="tab-btn active" onclick="showTab('settings', this)">⚙️ Settings</button>
            <button class="tab-btn" onclick="showTab('sms', this)">📱 SMS Responses</button>
            <button class="tab-btn" onclick="showTab('testing', this)">🧪 Testing</button>
            <button class="view-btn" onclick="viewMessages()" style="margin-left:8px;">📋 View Message Queue</button>
            <span id="autosave_status" style="font-size:13px; margin-left:8px;"></span>
            <div style="margin-left:auto; display:flex; gap:8px; align-items:center;">
                <button id="btn_twilio_start" onclick="twilioStart()" style="background:#2e7d32; color:#fff; border:none; padding:8px 16px; border-radius:4px; font-size:13px; font-weight:bold; cursor:pointer;">▶ TwilioStart</button>
                <button id="btn_twilio_stop" onclick="twilioStop()" style="background:#c62828; color:#fff; border:none; padding:8px 16px; border-radius:4px; font-size:13px; font-weight:bold; cursor:pointer;">■ TwilioStop</button>
            </div>
        </div>

        <!-- Plugin Live Banner -->
        <div id="plugin_live_banner" style="display:none; background:#1b5e20; color:#fff; padding:10px 16px; border-radius:5px; margin-top:10px; font-size:14px; font-weight:bold; align-items:center; gap:10px;">
            <span style="display:inline-block; width:12px; height:12px; background:#69f0ae; border-radius:50%; box-shadow:0 0 6px #69f0ae;"></span>
            Plugin is Live
        </div>

        <!-- Settings Tab -->
        <div id="tab-settings" class="tab-content active">
            <div class="columns">

                <!-- LEFT COLUMN: Twilio + FPP Display + Message Settings -->
                <div class="column">
                    <div class="section">
                        <h2>Twilio Settings</h2>
                        <label>Enable Plugin:</label>
                        <input type="checkbox" id="enabled" {{ 'checked' if config.enabled else '' }}>
                        <label class="checkbox-label">Enable SMS polling</label>

                        <label>Twilio Account SID:</label>
                        <input type="text" id="account_sid" value="{{ config.twilio_account_sid }}" placeholder="Starts with AC...">

                        <label>Twilio Auth Token:</label>
                        <input type="password" id="auth_token" value="{{ config.twilio_auth_token }}">

                        <label>Twilio Phone Number:</label>
                        <input type="text" id="phone_number" value="{{ config.twilio_phone_number }}" placeholder="+1234567890">

                        <label>Poll Interval (seconds):</label>
                        <input type="number" id="poll_interval" value="{{ config.poll_interval }}" min="1" max="60">

                        <button class="test-btn" onclick="testConnection()">🔌 Test Twilio Connection</button>
                        <div id="twilio_test_result" style="margin-top: 8px; font-size: 14px;"></div>

                        <hr style="border: none; border-top: 1px solid #ddd; margin: 15px 0;">
                        <h2 style="margin-top: 0;">FPP Display Settings</h2>

                        <div id="fpp_content_live_warning" style="display:none; background:#b71c1c; color:#fff; border-radius:5px; padding:8px 12px; margin-bottom:10px; font-size:13px;">
                            🔴 <strong>Plugin is Live</strong> — run TwilioStop to edit
                        </div>
                        <div id="fpp_content_inputs">
                            <label>Default "Waiting" Content: <span style="color:#f44336;font-size:12px;">* required</span></label>
                            <select id="default_playlist">
                                <option value="">-- Select content --</option>
                            </select>
                            <p class="help-text">📺 This content loops while waiting for text messages</p>

                            <label>Name Display Content:</label>
                            <select id="name_display_playlist">
                                <option value="">-- None (Same as "Waiting" Content) --</option>
                                {% set _np = config.get('name_display_playlist', '') %}
                                {% if _np %}<option value="{{ _np }}" selected>{{ _np }}</option>{% endif %}
                            </select>
                            <p class="help-text">🎬 This content plays when displaying a name</p>

                            <label>Overlay Model Name: <button type="button" onclick="refreshFPPLists(this)" style="font-size:11px;padding:2px 7px;margin-left:8px;cursor:pointer;">↻ Refresh Lists</button></label>
                            <select id="overlay_model_name">
                                <option value="">-- None --</option>
                            </select>
                            <p class="help-text">📝 The pixel overlay model for text (e.g., "Texting Matrix")</p>
                        </div>

                        <hr style="border: none; border-top: 1px solid #ddd; margin: 15px 0;">
                        <h2 style="margin-top: 0;">Message Settings</h2>

                        <label>Display Duration (seconds):</label>
                        <input type="number" id="display_duration" value="{{ config.display_duration }}" min="5" max="300">
                        <p class="help-text">⏱️ Each message displays for this many seconds before moving to the next</p>

                        <label>Max Messages Per Phone (0 = unlimited):</label>
                        <input type="number" id="max_messages" value="{{ config.max_messages_per_phone }}" min="0" max="100">

                        <div style="margin-top:10px;">
                            <input type="checkbox" id="allow_duplicate_names" {{ 'checked' if config.get('allow_duplicate_names', False) else '' }} onchange="checkDuplicateState(); saveConfig();">
                            <label class="checkbox-label">✓ Allow Duplicate Names — same name can be submitted multiple times per day</label>
                        </div>

                        <div id="max_length_section">
                            <label>Max Message Length:</label>
                            <input type="number" id="max_length" value="{{ config.max_message_length }}" min="10" max="200">
                        </div>
                        <div id="max_length_disabled_warning" style="display:none; background:#fff3cd; border:1px solid #ffc107; color:#856404; border-radius:5px; padding:8px 12px; margin-top:6px; font-size:13px;">
                            ⚠️ <strong>Max Message Length is disabled</strong> — whitelist is enabled. Names are validated against the approved list, not by length.
                        </div>

                    </div>
                </div>

                <!-- RIGHT COLUMN: Text Display -->
                <div class="column">
                    <div class="section">
                        <h2>Text Display Options</h2>

                        <label>Message Lines: <span style="font-size:11px; color:#888; font-weight:normal;">Use {name} in any line. Empty lines are skipped.</span></label>
                        <style>
                            .line-row { display:flex; align-items:center; gap:6px; margin-bottom:6px; }
                            .line-label { width:46px; font-size:12px; color:#aaa; flex-shrink:0; }
                            .pos-badge { font-size:11px; color:#888; white-space:nowrap; min-width:80px; text-align:right; font-family:monospace; }
                            .reset-line-btn { background:#444; border:none; color:#ccc; padding:2px 7px; font-size:12px; border-radius:3px; cursor:pointer; flex-shrink:0; }
                            .reset-line-btn:hover { background:#666; }
                        </style>
                        {% set ml = config.get('message_lines') or ['Merry Christmas', '{name}!', '', ''] %}
                        {% set lp = config.get('line_positions') or [] %}
                        <div id="message_lines_section">
                            <div class="line-row">
                                <span class="line-label">Line 1:</span>
                                <input type="text" id="line_1" value="{{ ml[0] if ml|length > 0 else 'Merry Christmas' }}" placeholder="e.g. Merry Christmas" style="flex:1;" onblur="saveConfig()">
                                <span id="line_1_pos" class="pos-badge">auto</span>
                                <button type="button" class="reset-line-btn" onclick="resetLine(0)" title="Reset to auto-center">✕</button>
                            </div>
                            <div class="line-row">
                                <span class="line-label">Line 2:</span>
                                <input type="text" id="line_2" value="{{ ml[1] if ml|length > 1 else '{name}!' }}" style="flex:1;" onblur="saveConfig()">
                                <span id="line_2_pos" class="pos-badge">auto</span>
                                <button type="button" class="reset-line-btn" onclick="resetLine(1)" title="Reset to auto-center">✕</button>
                            </div>
                            <div class="line-row">
                                <span class="line-label">Line 3:</span>
                                <input type="text" id="line_3" value="{{ ml[2] if ml|length > 2 else '' }}" placeholder="" style="flex:1;" onblur="saveConfig()">
                                <span id="line_3_pos" class="pos-badge">auto</span>
                                <button type="button" class="reset-line-btn" onclick="resetLine(2)" title="Reset to auto-center">✕</button>
                            </div>
                            <div class="line-row">
                                <span class="line-label">Line 4:</span>
                                <input type="text" id="line_4" value="{{ ml[3] if ml|length > 3 else '' }}" placeholder="" style="flex:1;" onblur="saveConfig()">
                                <span id="line_4_pos" class="pos-badge">auto</span>
                                <button type="button" class="reset-line-btn" onclick="resetLine(3)" title="Reset to auto-center">✕</button>
                            </div>
                        </div>

                        <!-- Canvas: per-line drag in static mode; block preview in scroll modes -->
                        <div id="canvas_section">
                            <label>Position Preview: <span id="canvas_hint" style="font-weight:normal; font-size:12px; color:#888;">click a line to select, drag to reposition</span></label>
                            <canvas id="matrix_canvas" style="width:100%; display:block; background:#000; border:2px solid #555; border-radius:4px; cursor:default;"></canvas>
                            <div style="display:flex; gap:8px; margin-top:6px; align-items:center;">
                                <button type="button" onclick="resetAllLines()" style="background:#555; padding:6px 12px; font-size:12px;">Reset All to Center</button>
                                <span id="pos_display" style="font-size:12px; color:#888;"></span>
                            </div>

                            <!-- Canvas background preview (FSEQ / video / image) -->
                            <div style="margin-top:10px; padding:10px; background:#616161; border:1px solid #777; border-radius:4px;">
                                <span style="font-size:13px; font-weight:bold; color:#eee;">Background Preview</span>
                                <span id="fseq_scrub_hint" style="font-weight:normal; font-size:11px; color:#bbb; margin-left:6px;">Use scroll bar to move preview.</span>
                                <div id="fseq_preview_controls" style="margin-top:8px;">
                                    <div style="margin-bottom:6px;">
                                        <span id="fseq_seq_label" style="font-size:12px; color:#ccc;">Sequence: —</span>
                                    </div>
                                    <div id="fseq_scrubber_row" style="display:none;">
                                        <div style="display:flex; align-items:center; gap:8px;">
                                            <span id="fseq_time_display" style="font-size:12px; color:#aaa; min-width:85px; white-space:nowrap;">0:00 / 0:00</span>
                                            <input type="range" id="fseq_scrubber" min="0" max="100" value="0" step="1"
                                                   style="flex:1;" oninput="fseqScrub(this.value)">
                                            <button type="button" onclick="clearFseqPreview()" style="padding:4px 8px; font-size:11px; background:#555; color:#fff; border:none; border-radius:3px; cursor:pointer;">Clear</button>
                                        </div>
                                        <div id="fseq_status" style="font-size:11px; color:#888; margin-top:4px; min-height:16px;"></div>
                                    </div>
                                    <div id="fseq_load_status" style="font-size:11px; color:#888; margin-top:4px; min-height:16px;"></div>
                                </div>
                            </div>
                        </div>

                        <label>Text Movement:</label>
                        <select id="text_position" onchange="updateScrollSpeedVisibility()">
                            <option value="Center" {{ 'selected' if config.get('text_position') == 'Center' else '' }}>Static</option>
                            <option value="L2R" {{ 'selected' if config.get('text_position') == 'L2R' else '' }}>Scroll Left to Right</option>
                            <option value="R2L" {{ 'selected' if config.get('text_position') == 'R2L' else '' }}>Scroll Right to Left</option>
                            <option value="T2B" {{ 'selected' if config.get('text_position') == 'T2B' else '' }}>Scroll Top to Bottom</option>
                            <option value="B2T" {{ 'selected' if config.get('text_position') == 'B2T' else '' }}>Scroll Bottom to Top</option>
                        </select>

                        <div id="scroll_speed_row">
                            <label>Scroll Speed:</label>
                            <input type="number" id="scroll_speed" value="{{ config.get('scroll_speed', 5) }}" min="1" max="10"
                                   oninput="this.value = Math.min(10, Math.max(1, parseInt(this.value)||1))">
                            <p class="help-text">⚡ 1 = slowest, 10 = fastest</p>
                        </div>

                        <label>Text Color:</label>
                        <div style="display: flex; align-items: center; gap: 10px;">
                            <input type="color" id="text_color" value="{{ config.get('text_color', '#FF0000') }}"
                                   style="width: 60px; height: 40px; padding: 2px; cursor: pointer;">
                            <input type="text" id="text_color_hex" value="{{ config.get('text_color', '#FF0000') }}"
                                   placeholder="#FF0000" style="width: 100px;"
                                   onchange="document.getElementById('text_color').value = this.value">
                        </div>
                        <script>
                            document.getElementById('text_color').addEventListener('change', function() {
                                document.getElementById('text_color_hex').value = this.value;
                                document.getElementById('color_preview').style.background = this.value;
                            });
                            document.getElementById('text_color_hex').addEventListener('input', function() {
                                if (/^#[0-9A-F]{6}$/i.test(this.value)) {
                                    document.getElementById('text_color').value = this.value;
                                    document.getElementById('color_preview').style.background = this.value;
                                }
                            });
                        </script>

                        <label>Font:</label>
                        <select id="text_font">
                            <option value="">Loading fonts...</option>
                        </select>

                        <label>Font Size:</label>
                        <input type="number" id="text_font_size" value="{{ config.get('text_font_size', 48) }}" min="12" max="200">

                        <input type="hidden" id="overlay_model_width" value="{{ config.get('overlay_model_width', 0) }}">
                        <input type="hidden" id="overlay_model_height" value="{{ config.get('overlay_model_height', 0) }}">
                        <script>window._linePositionsInit = {{ config.get('line_positions', [{'x':-1,'y':-1},{'x':-1,'y':-1},{'x':-1,'y':-1},{'x':-1,'y':-1}]) | tojson }};</script>
                    </div>
                </div>

            </div>

            <!-- Filters — full width, spans both columns -->
            <div class="section" style="margin-top:12px;">
                <h2>Filters</h2>
                <div style="display:flex; gap:24px; align-items:flex-start; flex-wrap:wrap;">

                    <!-- Sub-col 1: Profanity + Whitelist -->
                    <div style="flex:1; min-width:220px;">
                        <div id="blacklist_section">
                            <input type="checkbox" id="profanity_filter" {{ 'checked' if config.profanity_filter else '' }} onchange="checkFiltersState(); saveConfig();">
                            <label class="checkbox-label">✓ Enable Profanity Filter</label><br>
                            <button class="view-btn" onclick="location.href='/blacklist'" style="margin-top:6px;">🚫 Manage Blacklist</button>
                        </div>
                        <div id="profanity_disabled_warning" style="display:none; background:#f8d7da; border:1px solid #f5c6cb; color:#721c24; border-radius:5px; padding:8px 12px; margin-top:8px; font-size:13px;">
                            ⚠️ <strong>Profanity filter is disabled</strong> — this is not recommended.
                        </div>
                        <div id="blacklist_disabled_warning" style="display:none; background:#fff3cd; border:1px solid #ffc107; color:#856404; border-radius:5px; padding:8px 12px; margin-top:8px; font-size:13px;">
                            ⚠️ <strong>Blacklist inactive</strong> — whitelist is enabled. All names are validated against the whitelist.
                        </div>

                        <hr style="border:none; border-top:1px solid #444; margin:15px 0;">

                        <input type="checkbox" id="use_whitelist" {{ 'checked' if config.get('use_whitelist', False) else '' }} onchange="updateFormatRules(); checkFiltersState(); saveConfig();">
                        <label class="checkbox-label">✓ Enable Name Whitelist — only allow approved names</label><br>
                        <button class="view-btn" onclick="location.href='/whitelist'" style="margin-top:6px;">📋 Manage Whitelist</button>
                    </div>

                    <!-- Sub-col 2: Name Format Rules -->
                    <div style="flex:1; min-width:220px;">
                        <div id="format_rules_section">
                            <h3 style="margin-bottom:6px;">Name Format Rules</h3>
                            <div id="format_rules_disabled_note" style="display:none; background:#fff3cd; border:1px solid #ffc107; color:#856404; border-radius:5px; padding:8px 12px; margin-bottom:8px; font-size:13px;">
                                ⚠️ Name format rules are disabled when the whitelist is active.
                            </div>
                            <div id="format_rules_inputs">
                                <input type="checkbox" id="one_word_only" {{ 'checked' if config.get('one_word_only', False) and not config.get('use_whitelist', False) else '' }}
                                       onchange="if(this.checked) document.getElementById('two_words_max').checked = false; checkFormatWarning(); saveConfig();">
                                <label class="checkbox-label">✓ One Word Only (e.g., "John" ✓, "John Smith" ✗)</label><br>

                                <input type="checkbox" id="two_words_max" {{ 'checked' if config.get('two_words_max', True) and not config.get('use_whitelist', False) else '' }}
                                       onchange="if(this.checked) document.getElementById('one_word_only').checked = false; checkFormatWarning(); saveConfig();">
                                <label class="checkbox-label">✓ Two Words Maximum (e.g., "John Smith" ✓, sentences ✗)</label><br>

                                <div id="format_warning" style="display:none; background:#f8d7da; border:1px solid #f5c6cb; color:#721c24; border-radius:5px; padding:10px 14px; margin:8px 0; font-size:13px;">
                                    ⚠️ <strong>Warning:</strong> With no format rules enabled, viewers can send any message up to your Max Message Length. This is not recommended.
                                </div>
                            </div>
                            <p id="hyphen_note" class="help-text">ℹ️ Hyphenated names like "Jean-Luc" count as one word. All names are converted to Proper Case.</p>
                        </div>
                    </div>

                    <!-- Sub-col 3: Phone Blocklist -->
                    <div style="flex:0 0 180px;">
                        <label style="font-weight:bold; margin-bottom:4px;">Phone Blocklist</label>
                        <button onclick="location.href='/blocklist'" style="background:#f44336; margin-top:4px; display:block;">🚫 View Blocklist</button>
                    </div>

                </div>
            </div>
            <script>
                var _formatRulesInitialized = false;
                function updateFormatRules() {
                    var whitelistOn = document.getElementById('use_whitelist').checked;
                    var inputs = document.getElementById('format_rules_inputs');
                    var note = document.getElementById('format_rules_disabled_note');
                    inputs.style.opacity = whitelistOn ? '0.4' : '1';
                    inputs.style.pointerEvents = whitelistOn ? 'none' : '';
                    note.style.display = whitelistOn ? 'block' : 'none';
                    if (whitelistOn) {
                        document.getElementById('one_word_only').checked = false;
                        document.getElementById('two_words_max').checked = false;
                    } else if (_formatRulesInitialized) {
                        document.getElementById('two_words_max').checked = true;
                    }
                    _formatRulesInitialized = true;
                    checkFormatWarning();
                }
                function checkFormatWarning() {
                    var whitelistOn = document.getElementById('use_whitelist').checked;
                    var oneWord = document.getElementById('one_word_only').checked;
                    var twoWords = document.getElementById('two_words_max').checked;
                    var warn = !whitelistOn && !oneWord && !twoWords;
                    var rulesActive = !whitelistOn && (oneWord || twoWords);
                    document.getElementById('format_warning').style.display = warn ? 'block' : 'none';
                    document.getElementById('hyphen_note').style.opacity = rulesActive ? '1' : '0.4';
                }
                function checkDuplicateState() {
                    var allowDupes = document.getElementById('allow_duplicate_names').checked;
                    var row = document.getElementById('row_duplicate');
                    var cb = document.getElementById('sms_response_duplicate');
                    var warn = document.getElementById('duplicate_disabled_warning');
                    if (!row) return;
                    if (allowDupes) {
                        row.classList.add('locked');
                        row.classList.remove('enabled');
                        if (cb) cb.checked = false;
                    } else {
                        row.classList.remove('locked');
                        toggleResp('duplicate');
                    }
                    if (warn) warn.style.display = allowDupes ? '' : 'none';
                }
                function checkFiltersState() {
                    var whitelistOn = document.getElementById('use_whitelist').checked;
                    var profanityOn = document.getElementById('profanity_filter').checked;
                    var section = document.getElementById('blacklist_section');
                    section.style.opacity = whitelistOn ? '0.4' : '1';
                    section.style.pointerEvents = whitelistOn ? 'none' : '';
                    var maxLenSection = document.getElementById('max_length_section');
                    maxLenSection.style.opacity = whitelistOn ? '0.4' : '1';
                    maxLenSection.style.pointerEvents = whitelistOn ? 'none' : '';
                    document.getElementById('max_length_disabled_warning').style.display = whitelistOn ? 'block' : 'none';
                    document.getElementById('blacklist_disabled_warning').style.display = whitelistOn ? 'block' : 'none';
                    document.getElementById('profanity_disabled_warning').style.display = (!whitelistOn && !profanityOn) ? 'block' : 'none';
                }
                updateFormatRules();
                checkFiltersState();
                checkDuplicateState();
            </script>

        </div>

        <!-- SMS Responses Tab -->
        <div id="tab-sms" class="tab-content">
            <div class="section" style="border: 2px solid #2196F3; margin-top: 20px;">
                <h2>📱 SMS Auto-Response Settings</h2>
                <p class="help-text">💡 Enable a response for each event type individually. Only one response is ever sent per incoming message.</p>
                <div style="background:#fff3cd; border:1px solid #ffc107; color:#856404; border-radius:5px; padding:10px 14px; margin:10px 0; font-size:13px;">
                    ⚠️ <strong>Message &amp; data rates may apply.</strong>
                </div>
                <div style="background:#f8d7da; border:2px solid #f5c6cb; color:#721c24; border-radius:6px; padding:12px 16px; margin:10px 0; font-size:14px; font-weight:bold;">
                    ⛔ SMS responses will NOT be delivered unless your Twilio number is registered:<br>
                    <span style="font-weight:normal; font-size:13px; display:block; margin-top:6px;">
                        • <strong>Local 10-digit number</strong> — requires a valid A2P 10DLC brand &amp; campaign approval<br>
                        • <strong>Toll-free number</strong> — requires a completed toll-free verification (recommended)
                    </span>
                </div>

                <style>
                    .resp-row { border: 1px solid #ddd; border-radius: 6px; padding: 12px 14px; margin-bottom: 10px; background: #fafafa; }
                    .resp-row.enabled { background: #f0f7ff; border-color: #90caf9; }
                    .resp-row.locked { pointer-events: none; background: #f0f0f0; border-color: #ccc; }
                    .resp-row.locked .resp-toggle { opacity: 0.4; }
                    .resp-toggle { display: flex; align-items: center; gap: 8px; margin-bottom: 6px; font-weight: bold; font-size: 14px; }
                    .resp-row textarea { opacity: 0.4; pointer-events: none; transition: opacity .2s; }
                    .resp-row.locked textarea { opacity: 0.4; }
                    .resp-row.enabled textarea { opacity: 1; pointer-events: auto; }
                    .resp-locked-note { font-size: 13px; color: #856404; background: #fff3cd; border: 1px solid #ffc107; border-radius: 4px; padding: 7px 10px; margin: 4px 0 6px; }
                </style>

                <script>
                function toggleResp(id) {
                    var row = document.getElementById('row_' + id);
                    row.classList.toggle('enabled', document.getElementById('sms_response_' + id).checked);
                }
                function initRespRows() {
                    ['show_not_live','blocked','profanity','duplicate','invalid_format','rate_limited','not_whitelisted','success'].forEach(function(id) {
                        toggleResp(id);
                    });
                }
                </script>

                <div id="row_show_not_live" class="resp-row">
                    <div class="resp-toggle">
                        <input type="checkbox" id="sms_response_show_not_live" {{ 'checked' if config.get('sms_response_show_not_live', False) else '' }} onchange="toggleResp('show_not_live')">
                        <label for="sms_response_show_not_live">🔴 Show Not Live — Send Response</label>
                    </div>
                    <p class="help-text" style="margin:4px 0 6px;">Sent to anyone who texts while the show is not active (TwilioStop has been called).</p>
                    <textarea id="response_show_not_live" rows="2">{{ config.get('response_show_not_live', "Ho, Ho, Ho, It looks like our show isn't running now. Try again later.") }}</textarea>
                </div>

                <div id="row_blocked" class="resp-row">
                    <div class="resp-toggle">
                        <input type="checkbox" id="sms_response_blocked" {{ 'checked' if config.get('sms_response_blocked', False) else '' }} onchange="toggleResp('blocked')">
                        <label for="sms_response_blocked">🚫 Blocked Number — Send Response</label>
                    </div>
                    <textarea id="response_blocked" rows="2">{{ config.get('response_blocked', 'You have been blocked from sending messages.') }}</textarea>
                </div>

                <div id="row_profanity" class="resp-row">
                    <div class="resp-toggle">
                        <input type="checkbox" id="sms_response_profanity" {{ 'checked' if config.get('sms_response_profanity', False) else '' }} onchange="toggleResp('profanity')">
                        <label for="sms_response_profanity">🤬 Profanity Detected — Send Response</label>
                    </div>
                    <textarea id="response_profanity" rows="2">{{ config.get('response_profanity', 'Sorry, your message contains inappropriate content and cannot be displayed.') }}</textarea>
                </div>

                <div id="row_duplicate" class="resp-row{% if config.get('allow_duplicate_names', False) %} locked{% endif %}">
                    <div class="resp-toggle">
                        <input type="checkbox" id="sms_response_duplicate"
                               {{ 'checked' if config.get('sms_response_duplicate', False) and not config.get('allow_duplicate_names', False) else '' }}
                               onchange="toggleResp('duplicate')">
                        <label for="sms_response_duplicate">🔄 Duplicate Name — Send Response</label>
                    </div>
                    <p id="duplicate_disabled_warning" class="resp-locked-note" style="{{ '' if config.get('allow_duplicate_names', False) else 'display:none;' }}">⚠️ <strong>Duplicate response is disabled</strong> — Allow Duplicate Names is on, so this response will never send.</p>
                    <textarea id="response_duplicate" rows="2">{{ config.get('response_duplicate', "You've already sent this name today!") }}</textarea>
                </div>

                <div id="row_invalid_format" class="resp-row{% if config.get('use_whitelist', False) %} locked{% endif %}">
                    <div class="resp-toggle">
                        <input type="checkbox" id="sms_response_invalid_format"
                               {{ 'checked' if config.get('sms_response_invalid_format', False) and not config.get('use_whitelist', False) else '' }}
                               {{ 'disabled' if config.get('use_whitelist', False) else '' }}
                               onchange="toggleResp('invalid_format')">
                        <label for="sms_response_invalid_format">❌ Invalid Format — Send Response</label>
                    </div>
                    {% if config.get('use_whitelist', False) %}
                    <p class="resp-locked-note">⚠️ Invalid Format responses are disabled when the whitelist is active — all names are validated against the whitelist instead of format rules.</p>
                    {% endif %}
                    <textarea id="response_invalid_format" rows="2">{{ config.get('response_invalid_format', 'Please send only a name (1-2 words, no sentences).') }}</textarea>
                </div>

                <div id="row_rate_limited" class="resp-row">
                    <div class="resp-toggle">
                        <input type="checkbox" id="sms_response_rate_limited" {{ 'checked' if config.get('sms_response_rate_limited', False) else '' }} onchange="toggleResp('rate_limited')">
                        <label for="sms_response_rate_limited">⛔ Rate Limited — Send Response</label>
                    </div>
                    <textarea id="response_rate_limited" rows="2">{{ config.get('response_rate_limited', "You've reached the maximum number of messages allowed. Please try again tomorrow!") }}</textarea>
                </div>

                <div id="row_not_whitelisted" class="resp-row">
                    <div class="resp-toggle">
                        <input type="checkbox" id="sms_response_not_whitelisted" {{ 'checked' if config.get('sms_response_not_whitelisted', False) else '' }} onchange="toggleResp('not_whitelisted')">
                        <label for="sms_response_not_whitelisted">📋 Not on Whitelist — Send Response</label>
                    </div>
                    <textarea id="response_not_whitelisted" rows="2">{{ config.get('response_not_whitelisted', 'Sorry, that name is not on our approved list.') }}</textarea>
                </div>

                <div id="row_success" class="resp-row">
                    <div class="resp-toggle">
                        <input type="checkbox" id="sms_response_success" {{ 'checked' if config.get('sms_response_success', False) else '' }} onchange="toggleResp('success')">
                        <label for="sms_response_success">✅ Success — Send Response</label>
                    </div>
                    <textarea id="response_success" rows="2">{{ config.get('response_success', 'Thanks! Your name will appear on our display soon! 🎄') }}</textarea>
                </div>

            </div>
        </div>

        <!-- Testing Tab -->
        <div id="tab-testing" class="tab-content">

            <div id="test_message_section" class="section" style="border: 2px solid #FF9800; margin-top: 20px;">
                <h2>🧪 Message Testing</h2>

                <div id="show_not_live_banner" style="display:none; background:#ffecb3; border:1px solid #FF9800; border-radius:6px; padding:10px 14px; margin-bottom:14px; color:#7a4f00; font-size:14px;">
                    🔴 Show is not live — run <strong>TwilioStart</strong> from the FPP scheduler to activate the display before testing.
                </div>

                <div id="test_form_inner">
                    <p style="color: #FF9800; font-size: 14px;">
                        ⚠️ Use this to test messages without sending actual texts. Works without Twilio credentials.
                    </p>

                    <label>Test Name:</label>
                    <input type="text" id="test_name" placeholder="Enter a name to test">

                    <button class="test-btn" onclick="submitTestMessage()">🧪 Submit Test Message</button>

                    <div id="test_result" style="margin-top: 10px;"></div>
                </div>
            </div>

            <div class="section" style="border: 2px solid #FF9800;">
                <h2>🧪 SMS Response Testing</h2>
                <p style="color: #FF9800; font-size: 14px;">
                    ⚠️ Test sending SMS responses to a phone number. Requires Twilio credentials. </p>

                <label>Phone Number:</label>
                <input type="text" id="test_sms_phone" placeholder="Number to Text">

                <label>Message Type:</label>
                <select id="test_sms_type">
                    <option value="success">✅ Success</option>
                    <option value="profanity">🚫 Profanity</option>
                    <option value="rate_limited">⛔ Rate Limited</option>
                    <option value="duplicate">🔄 Duplicate</option>
                    <option value="invalid_format">❌ Invalid Format</option>
                    <option value="not_whitelisted">📋 Not Whitelisted</option>
                    <option value="blocked">🚫 Blocked</option>
                </select>

                <button class="test-btn" onclick="sendTestSMS()">📤 Send Test SMS</button>

                <div id="test_sms_result" style="margin-top: 10px;"></div>
            </div>

        </div>

        <script>
            function twilioStart() {
                var btn = document.getElementById('btn_twilio_start');
                btn.disabled = true; btn.textContent = '...';
                fetch('/api/activate', {method:'POST'})
                .then(r => r.json())
                .then(function(d) {
                    if (d.success === false) { alert('TwilioStart failed: ' + (d.error || 'Unknown error')); }
                    updateLiveStatus();
                })
                .catch(function() { alert('TwilioStart request failed.'); })
                .finally(function() { btn.disabled = false; btn.textContent = '▶ TwilioStart'; });
            }

            function twilioStop() {
                var btn = document.getElementById('btn_twilio_stop');
                btn.disabled = true; btn.textContent = '...';
                fetch('/api/deactivate', {method:'POST'})
                .then(r => r.json())
                .then(function() { updateLiveStatus(); })
                .catch(function() { alert('TwilioStop request failed.'); })
                .finally(function() { btn.disabled = false; btn.textContent = '■ TwilioStop'; });
            }

            function updateLiveStatus() {
                fetch('/api/queue/status').then(r => r.json()).then(data => {
                    const live = data.show_live === true;

                    // Testing tab banner (show is NOT live warning)
                    const notLiveBanner = document.getElementById('show_not_live_banner');
                    const form = document.getElementById('test_form_inner');
                    if (notLiveBanner) notLiveBanner.style.display = live ? 'none' : 'block';
                    if (form) {
                        form.style.opacity = live ? '1' : '0.4';
                        form.style.pointerEvents = live ? '' : 'none';
                    }

                    // Settings tab: "Plugin is Live" banner at top
                    const liveBanner = document.getElementById('plugin_live_banner');
                    if (liveBanner) liveBanner.style.display = live ? 'flex' : 'none';

                    // Lock content dropdowns when live
                    const liveWarning = document.getElementById('fpp_content_live_warning');
                    const contentInputs = document.getElementById('fpp_content_inputs');
                    if (liveWarning) liveWarning.style.display = live ? 'block' : 'none';
                    if (contentInputs) {
                        contentInputs.style.opacity = live ? '0.4' : '';
                        contentInputs.style.pointerEvents = live ? 'none' : '';
                    }
                }).catch(() => {});
            }

            function updateScrollSpeedVisibility() {
                var pos = document.getElementById('text_position').value;
                var isStatic = (pos === 'Center');
                var row = document.getElementById('scroll_speed_row');
                if (row) row.style.display = isStatic ? 'none' : '';

                // Update canvas hint text based on mode
                var hint = document.getElementById('canvas_hint');
                var help = document.getElementById('canvas_help');
                if (pos === 'L2R' || pos === 'R2L') {
                    if (hint) hint.textContent = 'drag each line up/down to set its vertical position';
                    if (help) help.textContent = 'Each line scrolls horizontally at its own Y position. Click a line then drag up/down to reposition it.';
                } else if (pos === 'T2B' || pos === 'B2T') {
                    if (hint) hint.textContent = 'drag each line left/right to set its horizontal position';
                    if (help) help.textContent = 'Each line scrolls vertically at its own X position. Click a line then drag left/right to reposition it.';
                } else {
                    if (hint) hint.textContent = 'click a line to select, drag to reposition';
                }
                if (typeof window.renderCanvasPreview === 'function') window.renderCanvasPreview();
            }

            function updateModelAspect(width, height) {
                if (width > 0 && height > 0) {
                    window._canvasModelW = width;
                    window._canvasModelH = height;
                    var c = document.getElementById('matrix_canvas');
                    if (c) { c.width = 640; c.height = Math.round(640 * height / width); }
                    if (typeof window.renderCanvasPreview === 'function') window.renderCanvasPreview();
                }
            }

            function initValignButtons() {
                // No-op: v_align removed; per-line Y positioning handles vertical placement.
            }

            function initCanvasPreview() {
                var canvas = document.getElementById('matrix_canvas');
                if (!canvas) return;
                var ctx = canvas.getContext('2d');
                if (!ctx) return;

                window._canvasModelW = parseInt(document.getElementById('overlay_model_width').value) || 640;
                window._canvasModelH = parseInt(document.getElementById('overlay_model_height').value) || 360;
                canvas.width  = 640;
                canvas.height = Math.round(640 * window._canvasModelH / window._canvasModelW);

                // Load per-line positions from config (injected as JS by server to avoid HTML attribute quote issues)
                var initLP = (window._linePositionsInit && Array.isArray(window._linePositionsInit))
                    ? window._linePositionsInit
                    : [{x:-1,y:-1},{x:-1,y:-1},{x:-1,y:-1},{x:-1,y:-1}];
                while (initLP.length < 4) initLP.push({x:-1,y:-1});
                window._linePositions = initLP;

                var selectedLine = -1;
                var hoveredLine  = -1;
                var lineRects    = [null, null, null, null]; // canvas-pixel rects, filled by render
                var dragging     = false;
                var dragOffX     = 0, dragOffY = 0;

                function getLineText(i) {
                    var el = document.getElementById('line_' + (i + 1));
                    return el ? el.value.replace('{name}', 'Santa') : '';
                }

                // Returns array of indices (0-3) for non-empty lines
                function getNonEmptyIdx() {
                    var r = [];
                    for (var i = 0; i < 4; i++) { if (getLineText(i)) r.push(i); }
                    return r;
                }

                function updateBadges() {
                    var pos = document.getElementById('text_position').value;
                    for (var i = 0; i < 4; i++) {
                        var badge = document.getElementById('line_' + (i + 1) + '_pos');
                        if (!badge) continue;
                        var lp = window._linePositions[i];
                        var txt;
                        if (pos === 'L2R' || pos === 'R2L') {
                            txt = lp.y < 0 ? 'auto' : ('Y:' + lp.y);
                        } else if (pos === 'T2B' || pos === 'B2T') {
                            txt = lp.x < 0 ? 'auto' : ('X:' + lp.x);
                        } else {
                            txt = (lp.x < 0 && lp.y < 0) ? 'auto' : ('X:' + lp.x + ' Y:' + lp.y);
                        }
                        badge.textContent = txt;
                        badge.style.color = (i === selectedLine) ? '#4CAF50' : '#888';
                    }
                }

                function drawLineDecoration(drawX, drawY, lw2, lh2, i) {
                    if (i === selectedLine) {
                        ctx.save();
                        ctx.strokeStyle = '#fff'; ctx.lineWidth = 1; ctx.setLineDash([]);
                        ctx.strokeRect(drawX - 2, drawY - 2, lw2 + 4, lh2 + 4);
                        ctx.restore();
                    } else if (i === hoveredLine) {
                        ctx.save();
                        ctx.strokeStyle = 'rgba(255,255,255,0.35)'; ctx.lineWidth = 1; ctx.setLineDash([3,3]);
                        ctx.strokeRect(drawX - 2, drawY - 2, lw2 + 4, lh2 + 4);
                        ctx.restore();
                    }
                }

                function renderCanvasPreview() {
                    var mw = window._canvasModelW || 640;
                    var mh = window._canvasModelH || 360;
                    var pos = document.getElementById('text_position').value;
                    ctx.fillStyle = '#000';
                    ctx.fillRect(0, 0, canvas.width, canvas.height);
                    if (window._fseqBgImage) {
                        ctx.imageSmoothingEnabled = false;
                        ctx.drawImage(window._fseqBgImage, 0, 0, canvas.width, canvas.height);
                        ctx.imageSmoothingEnabled = true;
                    }
                    var fontSize   = parseInt(document.getElementById('text_font_size').value) || 48;
                    var color      = document.getElementById('text_color').value || '#ff0000';
                    var fontName   = document.getElementById('text_font').value || 'sans-serif';
                    var scaledFont = Math.round(fontSize * canvas.width / mw);
                    var lineH      = Math.round(scaledFont * 1.2);
                    var posLabel   = '';
                    ctx.font = scaledFont + 'px ' + fontName + ', sans-serif';
                    ctx.textBaseline = 'top';

                    lineRects = [null, null, null, null];

                    var nonEmptyIdx = getNonEmptyIdx();
                    var nLines = nonEmptyIdx.length || 1;
                    // Stacked-group Y: center the group, space lines by lineH
                    var stackStartY = (canvas.height - nLines * lineH) / 2;

                    if (pos === 'L2R' || pos === 'R2L') {
                        // Per-line Y control; text scrolls full width at each line's Y
                        ctx.save();
                        ctx.fillStyle = 'rgba(255,255,255,0.2)';
                        ctx.font = 'bold ' + Math.max(8, Math.round(scaledFont * 0.5)) + 'px sans-serif';
                        ctx.textBaseline = 'top';
                        var arrowTxt = pos === 'L2R' ? '→' : '←';
                        var arrowX = pos === 'L2R' ? 4 : canvas.width - ctx.measureText(arrowTxt).width - 4;
                        ctx.fillText(arrowTxt, arrowX, 4);
                        ctx.restore();
                        ctx.font = scaledFont + 'px ' + fontName + ', sans-serif'; ctx.textBaseline = 'top';

                        var stackIdx = 0;
                        for (var i = 0; i < 4; i++) {
                            var lineText = getLineText(i);
                            if (!lineText) { lineRects[i] = null; continue; }
                            var lp  = window._linePositions[i];
                            var lw2 = ctx.measureText(lineText).width;
                            var lh2 = scaledFont;
                            var drawY = lp.y < 0 ? stackStartY + stackIdx * lineH : lp.y * canvas.height / mh;
                            drawY = Math.max(0, Math.min(canvas.height - lh2, drawY));
                            var drawX = (canvas.width - lw2) / 2;
                            lineRects[i] = {x: drawX, y: drawY, w: lw2, h: lh2};
                            // Dashed guide line at this Y
                            ctx.save();
                            ctx.strokeStyle = 'rgba(255,255,255,0.1)'; ctx.setLineDash([3,5]); ctx.lineWidth = 1;
                            ctx.beginPath(); ctx.moveTo(0, drawY + lh2/2); ctx.lineTo(canvas.width, drawY + lh2/2); ctx.stroke();
                            ctx.restore();
                            drawLineDecoration(drawX, drawY, lw2, lh2, i);
                            ctx.fillStyle = color; ctx.fillText(lineText, drawX, drawY);
                            if (i === selectedLine)
                                posLabel = 'Line ' + (i+1) + ' Y: ' + (lp.y < 0 ? 'auto' : lp.y) + ' — drag up/down';
                            stackIdx++;
                        }

                    } else if (pos === 'T2B' || pos === 'B2T') {
                        // Per-line X control; text scrolls full height at each line's X
                        ctx.save();
                        ctx.fillStyle = 'rgba(255,255,255,0.2)';
                        ctx.font = 'bold ' + Math.max(8, Math.round(scaledFont * 0.5)) + 'px sans-serif';
                        ctx.textBaseline = 'top';
                        var sarrow = pos === 'T2B' ? '↓' : '↑';
                        ctx.fillText(sarrow, canvas.width / 2 - ctx.measureText(sarrow).width / 2, 4);
                        ctx.restore();
                        ctx.font = scaledFont + 'px ' + fontName + ', sans-serif'; ctx.textBaseline = 'top';

                        var stackIdx = 0;
                        for (var i = 0; i < 4; i++) {
                            var lineText = getLineText(i);
                            if (!lineText) { lineRects[i] = null; continue; }
                            var lp  = window._linePositions[i];
                            var lw2 = ctx.measureText(lineText).width;
                            var lh2 = scaledFont;
                            var drawX = lp.x < 0 ? (canvas.width - lw2) / 2 : lp.x * canvas.width / mw;
                            drawX = Math.max(0, Math.min(canvas.width - lw2, drawX));
                            var drawY = (canvas.height - lh2) / 2;
                            lineRects[i] = {x: drawX, y: drawY, w: lw2, h: lh2};
                            // Dashed guide at this X
                            ctx.save();
                            ctx.strokeStyle = 'rgba(255,255,255,0.1)'; ctx.setLineDash([3,5]); ctx.lineWidth = 1;
                            ctx.beginPath(); ctx.moveTo(drawX + lw2/2, 0); ctx.lineTo(drawX + lw2/2, canvas.height); ctx.stroke();
                            ctx.restore();
                            drawLineDecoration(drawX, drawY, lw2, lh2, i);
                            ctx.fillStyle = color; ctx.fillText(lineText, drawX, drawY);
                            if (i === selectedLine)
                                posLabel = 'Line ' + (i+1) + ' X: ' + (lp.x < 0 ? 'auto' : lp.x) + ' — drag left/right';
                            stackIdx++;
                        }

                    } else {
                        // Static mode: each line at its own X,Y with stacked Y default
                        var stackIdx = 0;
                        for (var i = 0; i < 4; i++) {
                            var lineText = getLineText(i);
                            if (!lineText) { lineRects[i] = null; continue; }
                            var lp  = window._linePositions[i];
                            var lw2 = ctx.measureText(lineText).width;
                            var lh2 = scaledFont;
                            var drawX = lp.x < 0 ? (canvas.width - lw2) / 2 : lp.x * canvas.width / mw;
                            var drawY = lp.y < 0 ? stackStartY + stackIdx * lineH : lp.y * canvas.height / mh;
                            drawX = Math.max(0, Math.min(canvas.width - lw2, drawX));
                            drawY = Math.max(0, Math.min(canvas.height - lh2, drawY));
                            lineRects[i] = {x: drawX, y: drawY, w: lw2, h: lh2};
                            drawLineDecoration(drawX, drawY, lw2, lh2, i);
                            ctx.fillStyle = color; ctx.fillText(lineText, drawX, drawY);
                            if (i === selectedLine)
                                posLabel = (lp.x < 0 && lp.y < 0)
                                    ? 'Line ' + (i+1) + ': auto-stacked'
                                    : 'Line ' + (i+1) + ':  X:' + lp.x + '  Y:' + lp.y;
                            stackIdx++;
                        }
                    }

                    var posEl = document.getElementById('pos_display');
                    if (posEl) posEl.textContent = posLabel;
                    updateBadges();
                }
                window.renderCanvasPreview = renderCanvasPreview;

                function hitTestLine(cx, cy) {
                    var PAD = 8;
                    for (var i = lineRects.length - 1; i >= 0; i--) {
                        var r = lineRects[i];
                        if (!r) continue;
                        if (cx >= r.x - PAD && cx <= r.x + r.w + PAD &&
                            cy >= r.y - PAD && cy <= r.y + r.h + PAD) { return i; }
                    }
                    return -1;
                }

                function canvasXY(e) {
                    var rect = canvas.getBoundingClientRect();
                    return { cx: e.clientX - rect.left, cy: e.clientY - rect.top };
                }

                function dragCursor(pos) {
                    return (pos === 'L2R' || pos === 'R2L') ? 'ns-resize' :
                           (pos === 'T2B' || pos === 'B2T') ? 'ew-resize' : 'grabbing';
                }
                function hoverCursor(pos) {
                    return (pos === 'L2R' || pos === 'R2L') ? 'ns-resize' :
                           (pos === 'T2B' || pos === 'B2T') ? 'ew-resize' : 'grab';
                }

                canvas.addEventListener('mousedown', function(e) {
                    var pos = document.getElementById('text_position').value;
                    var c = canvasXY(e);
                    var hit = hitTestLine(c.cx, c.cy);
                    selectedLine = hit;
                    if (hit >= 0) {
                        dragging = true;
                        var r   = lineRects[hit];
                        var lp  = window._linePositions[hit];
                        var mw2 = window._canvasModelW || 640;
                        var mh2 = window._canvasModelH || 360;
                        // For L2R/R2L: anchor Y offset; for T2B/B2T: anchor X; static: both
                        dragOffX = c.cx - (lp.x < 0 ? r.x : lp.x * canvas.width  / mw2);
                        dragOffY = c.cy - (lp.y < 0 ? r.y : lp.y * canvas.height / mh2);
                        canvas.style.cursor = dragCursor(pos);
                    }
                    renderCanvasPreview();
                    e.preventDefault();
                });

                canvas.addEventListener('mousemove', function(e) {
                    var mw2 = window._canvasModelW || 640;
                    var mh2 = window._canvasModelH || 360;
                    var pos = document.getElementById('text_position').value;
                    var c   = canvasXY(e);
                    if (dragging && selectedLine >= 0) {
                        var r  = lineRects[selectedLine] || {w: 20, h: 20};
                        var lp = window._linePositions[selectedLine];
                        var newX = Math.round(Math.max(0, Math.min(canvas.width  - r.w, c.cx - dragOffX)) * mw2 / canvas.width);
                        var newY = Math.round(Math.max(0, Math.min(canvas.height - r.h, c.cy - dragOffY)) * mh2 / canvas.height);
                        if (pos === 'L2R' || pos === 'R2L') {
                            window._linePositions[selectedLine] = {x: lp.x, y: newY};
                        } else if (pos === 'T2B' || pos === 'B2T') {
                            window._linePositions[selectedLine] = {x: newX, y: lp.y};
                        } else {
                            window._linePositions[selectedLine] = {x: newX, y: newY};
                        }
                        renderCanvasPreview();
                    } else if (!dragging) {
                        var prev = hoveredLine;
                        hoveredLine = hitTestLine(c.cx, c.cy);
                        canvas.style.cursor = hoveredLine >= 0 ? hoverCursor(pos) : 'default';
                        if (hoveredLine !== prev) renderCanvasPreview();
                    }
                });

                window.addEventListener('mouseup', function() {
                    if (dragging) {
                        dragging = false;
                        var pos = document.getElementById('text_position').value;
                        canvas.style.cursor = hoveredLine >= 0 ? hoverCursor(pos) : 'default';
                        saveConfig();
                    }
                });
                canvas.addEventListener('mouseleave', function() {
                    if (!dragging) { hoveredLine = -1; canvas.style.cursor = 'default'; renderCanvasPreview(); }
                });

                // Arrow key nudging — moves selected line 1px per press, 10px with Shift
                // saveConfig is debounced so holding a key doesn't spam the server
                var _arrowSaveTimer = null;
                document.addEventListener('keydown', function(e) {
                    if (selectedLine < 0) return;
                    var arrows = {ArrowLeft:1, ArrowRight:1, ArrowUp:1, ArrowDown:1};
                    if (!arrows[e.key]) return;
                    e.preventDefault();
                    var pos  = document.getElementById('text_position').value;
                    var mw2  = window._canvasModelW || 640;
                    var mh2  = window._canvasModelH || 360;
                    var lp   = window._linePositions[selectedLine];
                    var step = e.shiftKey ? 10 : 1;
                    // Resolve auto (-1) positions from the rendered rect so the
                    // first keypress anchors from the visual position, not from 0
                    var curX = lp.x, curY = lp.y;
                    var r = lineRects[selectedLine];
                    if (curX < 0 && r) curX = Math.round(r.x * mw2 / canvas.width);
                    if (curY < 0 && r) curY = Math.round(r.y * mh2 / canvas.height);
                    if (curX < 0) curX = Math.round(mw2 / 2);
                    if (curY < 0) curY = Math.round(mh2 / 2);
                    if (pos === 'L2R' || pos === 'R2L') {
                        if (e.key === 'ArrowUp')    curY = Math.max(0, curY - step);
                        if (e.key === 'ArrowDown')  curY = Math.min(mh2 - 1, curY + step);
                        window._linePositions[selectedLine] = {x: lp.x, y: curY};
                    } else if (pos === 'T2B' || pos === 'B2T') {
                        if (e.key === 'ArrowLeft')  curX = Math.max(0, curX - step);
                        if (e.key === 'ArrowRight') curX = Math.min(mw2 - 1, curX + step);
                        window._linePositions[selectedLine] = {x: curX, y: lp.y};
                    } else {
                        if (e.key === 'ArrowLeft')  curX = Math.max(0, curX - step);
                        if (e.key === 'ArrowRight') curX = Math.min(mw2 - 1, curX + step);
                        if (e.key === 'ArrowUp')    curY = Math.max(0, curY - step);
                        if (e.key === 'ArrowDown')  curY = Math.min(mh2 - 1, curY + step);
                        window._linePositions[selectedLine] = {x: curX, y: curY};
                    }
                    renderCanvasPreview();
                    clearTimeout(_arrowSaveTimer);
                    _arrowSaveTimer = setTimeout(saveConfig, 300);
                });

                window.resetLine = function(i) {
                    window._linePositions[i] = {x: -1, y: -1};
                    renderCanvasPreview(); saveConfig();
                };
                window.resetAllLines = function() {
                    window._linePositions = [{x:-1,y:-1},{x:-1,y:-1},{x:-1,y:-1},{x:-1,y:-1}];
                    selectedLine = -1;
                    renderCanvasPreview(); saveConfig();
                };

                // Re-render on text / color / font-size changes
                for (var li = 1; li <= 4; li++) {
                    (function(el) { if (el) el.addEventListener('input', renderCanvasPreview); })(document.getElementById('line_' + li));
                }
                document.getElementById('text_color').addEventListener('change',     renderCanvasPreview);
                document.getElementById('text_font_size').addEventListener('change', renderCanvasPreview);
                document.getElementById('text_font').addEventListener('change',      renderCanvasPreview);

                updateBadges();
                renderCanvasPreview();
            }

            // ---------------------------------------------------------------------------
            // Canvas background preview — supports FSEQ (.fseq), video (vid:), image (img:)
            // ---------------------------------------------------------------------------
            (function() {
                var _fseqMeta   = null;
                var _fseqSeq    = null;   // clean FSEQ name: no seq: prefix, no .fseq suffix
                var _contentType = null;  // 'seq', 'vid', or 'img'
                var _contentFile = null;  // filename (vid:/img:) or clean seq name (seq:)
                window._fseqBgImage = null;

                function fmtTime(ms) {
                    var s = Math.floor(ms / 1000);
                    var m = Math.floor(s / 60);
                    s = s % 60;
                    return m + ':' + (s < 10 ? '0' : '') + s;
                }

                // Returns {type, file} for the configured Names Display content, or null.
                function getConfiguredContent() {
                    var dp = document.getElementById('name_display_playlist');
                    var defaultDp = document.getElementById('default_playlist');
                    // Fall back to waiting content when names content is "None"
                    var val = (dp && dp.value) ? dp.value : (defaultDp ? defaultDp.value : '');
                    if (!val) return null;
                    if (val.startsWith('seq:')) {
                        return { type: 'seq', file: val.replace(/^seq:/, '').replace(/\.fseq$/, '') };
                    }
                    if (val.startsWith('vid:')) {
                        return { type: 'vid', file: val.replace(/^vid:/, '') };
                    }
                    if (val.startsWith('img:')) {
                        return { type: 'img', file: val.replace(/^img:/, '') };
                    }
                    return null;  // plain playlist — no canvas preview
                }

                window.toggleFseqPreview = function() {
                    var ct = getConfiguredContent();
                    var label = document.getElementById('fseq_seq_label');
                    if (!ct) {
                        label.textContent = '\u26a0 Select a .fseq, video, or image as Waiting or Names content for background preview.';
                        label.style.color = '#ff9800';
                        return;
                    }
                    var icon = ct.type === 'seq' ? '🎬 ' : ct.type === 'vid' ? '🎥 ' : '🖼️ ';
                    label.textContent = icon + ct.file;
                    label.style.color = '#ccc';
                    loadBgPreview();
                };

                function _clearState() {
                    window._fseqBgImage = null;
                    _fseqMeta = null;
                    _fseqSeq  = null;
                    _contentType = null;
                    _contentFile = null;
                    if (typeof window.renderCanvasPreview === 'function') window.renderCanvasPreview();
                }

                function loadBgPreview() {
                    var ct = getConfiguredContent();
                    if (!ct) return;
                    _contentType = ct.type;
                    _contentFile = ct.file;
                    _fseqSeq     = (ct.type === 'seq') ? ct.file : null;

                    var loadEl = document.getElementById('fseq_load_status');
                    var scrubHint = document.getElementById('fseq_scrub_hint');
                    loadEl.textContent = 'Loading\u2026';
                    loadEl.style.color = '#aaa';
                    if (scrubHint) scrubHint.style.display = (ct.type === 'img') ? 'none' : '';

                    if (ct.type === 'seq') {
                        // ---- FSEQ: fetch info then show scrubber ----
                        var model = document.getElementById('overlay_model_name').value || '';
                        fetch('/api/fseq/info?sequence=' + encodeURIComponent(ct.file)
                                              + '&model=' + encodeURIComponent(model))
                            .then(function(r) { return r.json(); })
                            .then(function(data) {
                                if (data.error) {
                                    loadEl.textContent = '\u2717 ' + data.error;
                                    loadEl.style.color = '#f44336';
                                    return;
                                }
                                _fseqMeta = data;
                                if (data.detected_start_channel) {
                                    loadEl.textContent = '';
                                } else {
                                    loadEl.textContent = '\u26a0 Overlay model not found \u2014 verify model name in settings';
                                    loadEl.style.color = '#ff9800';
                                }
                                var totalSec = Math.max(1, Math.floor(data.duration_ms / 1000));
                                var scrubber = document.getElementById('fseq_scrubber');
                                scrubber.max = totalSec;
                                scrubber.value = 0;
                                document.getElementById('fseq_scrubber_row').style.display = '';
                                document.getElementById('fseq_time_display').textContent =
                                    '0:00 / ' + fmtTime(data.duration_ms);
                                doFseqFetch(0);
                            })
                            .catch(function(e) {
                                loadEl.textContent = '\u2717 ' + e;
                                loadEl.style.color = '#f44336';
                            });

                    } else if (ct.type === 'vid') {
                        // ---- Video: show scrubber (time in seconds), fetch frames ----
                        loadEl.textContent = '';
                        var scrubber = document.getElementById('fseq_scrubber');
                        scrubber.max = 300;  // assume up to 5 min; user can scrub
                        scrubber.value = 0;
                        document.getElementById('fseq_scrubber_row').style.display = '';
                        document.getElementById('fseq_time_display').textContent = '0:00';
                        document.getElementById('fseq_status').textContent =
                            'Scrub to preview different parts of the video';
                        doMediaFetch(0);

                    } else {
                        // ---- Image: load once, no scrubber ----
                        loadEl.textContent = '';
                        document.getElementById('fseq_scrubber_row').style.display = 'none';
                        doMediaFetch(0);
                    }
                }

                // Alias so old callers still work
                window.loadFseqPreview = loadBgPreview;

                var _scrubTimer = null;
                var _pendingImg  = null;

                function doFseqFetch(seconds) {
                    if (!_fseqMeta || !_fseqSeq) return;
                    var sec      = parseInt(seconds);
                    var frameIdx = Math.min(
                        Math.round(sec * _fseqMeta.fps),
                        _fseqMeta.frame_count - 1
                    );
                    var mw    = document.getElementById('overlay_model_width').value  || 0;
                    var mh    = document.getElementById('overlay_model_height').value || 0;
                    var model = document.getElementById('overlay_model_name').value   || '';

                    var url = '/api/fseq/frame'
                        + '?sequence=' + encodeURIComponent(_fseqSeq)
                        + '&frame='    + frameIdx
                        + '&model='    + encodeURIComponent(model)
                        + '&width='    + mw
                        + '&height='   + mh;
                    if (_fseqMeta.detected_start_channel) {
                        url += '&start_channel=' + _fseqMeta.detected_start_channel;
                    }
                    if (_fseqMeta.detected_channel_count) {
                        url += '&channel_count=' + _fseqMeta.detected_channel_count;
                    }
                    _loadImageUrl(url);
                }

                function doMediaFetch(seconds) {
                    if (!_contentType || !_contentFile || _contentType === 'seq') return;
                    var mw = document.getElementById('overlay_model_width').value  || 0;
                    var mh = document.getElementById('overlay_model_height').value || 0;
                    var url = '/api/media/preview'
                        + '?type='   + _contentType
                        + '&file='   + encodeURIComponent(_contentFile)
                        + '&time='   + Math.floor(seconds)
                        + '&width='  + mw
                        + '&height=' + mh;
                    _loadImageUrl(url);
                }

                function _loadImageUrl(url) {
                    var statusEl = document.getElementById('fseq_status');
                    if (_pendingImg) { _pendingImg.onload = null; _pendingImg.onerror = null; _pendingImg.src = ''; }
                    var img = new Image();
                    _pendingImg = img;
                    img.onload = function() {
                        if (img !== _pendingImg) return;
                        window._fseqBgImage = img;
                        statusEl.textContent = '';
                        if (typeof window.renderCanvasPreview === 'function') window.renderCanvasPreview();
                    };
                    img.onerror = function() {
                        if (img !== _pendingImg) return;
                        fetch(url).then(function(r) { return r.json(); }).then(function(d) {
                            statusEl.textContent = '\u2717 ' + (d.error || 'Failed to load frame');
                            statusEl.style.color = '#f44336';
                        }).catch(function() {
                            statusEl.textContent = '\u2717 Failed to load preview';
                            statusEl.style.color = '#f44336';
                        });
                    };
                    img.src = url;
                }

                window.fseqScrub = function(seconds) {
                    var loadEl = document.getElementById('fseq_load_status');
                    if (_contentType === 'seq') {
                        if (!_fseqMeta) return;
                        document.getElementById('fseq_time_display').textContent =
                            fmtTime(parseInt(seconds) * 1000) + ' / ' + fmtTime(_fseqMeta.duration_ms);
                        clearTimeout(_scrubTimer);
                        _scrubTimer = setTimeout(function() { doFseqFetch(seconds); }, 150);
                    } else if (_contentType === 'vid') {
                        document.getElementById('fseq_time_display').textContent = fmtTime(parseInt(seconds) * 1000);
                        clearTimeout(_scrubTimer);
                        _scrubTimer = setTimeout(function() { doMediaFetch(seconds); }, 150);
                    }
                    // img: no scrubbing
                };

                window.clearFseqPreview = function() {
                    _clearState();
                    document.getElementById('fseq_scrubber_row').style.display = 'none';
                    document.getElementById('fseq_status').textContent = '';
                    document.getElementById('fseq_load_status').textContent = '';
                };
            })();

            // All DOM elements are above this script block — call init functions directly.
            try { initCanvasPreview(); } catch(e) { console.error('Canvas init error:', e); }
            // Load preview immediately using server-rendered dropdown value, then again after FPP data populates
            if (window.toggleFseqPreview) window.toggleFseqPreview();
            loadFonts();
            loadFPPData();
            initRespRows();
            setupAutoSave();
            updateLiveStatus();
            setInterval(updateLiveStatus, 5000);
            updateScrollSpeedVisibility();
            initValignButtons();
            (function() {
                var w = parseInt(document.getElementById('overlay_model_width').value) || 0;
                var h = parseInt(document.getElementById('overlay_model_height').value) || 0;
                if (w > 0 && h > 0) updateModelAspect(w, h);
            })();

            function showTab(tabName, btn) {
                document.querySelectorAll('.tab-content').forEach(t => t.classList.remove('active'));
                document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
                document.getElementById('tab-' + tabName).classList.add('active');
                btn.classList.add('active');
                // Re-report height after layout settles so iframe resizes correctly
                requestAnimationFrame(function() {
                    window.parent.postMessage({ type: 'iframeHeight', height: document.body.scrollHeight }, '*');
                });
            }

            function refreshFPPLists(btn) {
                if (btn) { btn.disabled = true; btn.textContent = '...'; }
                fetch('/api/fpp/refresh', {method:'POST'})
                    .then(() => loadFPPData())
                    .finally(() => { if (btn) { btn.disabled = false; btn.textContent = '↻ Refresh Lists'; } });
            }

            function loadFonts() {
                const fontSelect = document.getElementById('text_font');
                const currentFont = "{{ config.get('text_font', 'FreeSans') }}";
                fetch('/api/fpp/fonts')
                .then(r => r.json())
                .then(function(fonts) {
                    if (fonts && fonts.length > 0) {
                        fontSelect.innerHTML = '<option value="">-- Select Font --</option>';
                        fonts.forEach(function(font) {
                            const opt = new Option(font, font, false, font === currentFont);
                            fontSelect.add(opt);
                        });
                    } else {
                        fontSelect.innerHTML = '<option value="FreeSans">FreeSans (default)</option>';
                    }
                })
                .catch(function() {
                    fontSelect.innerHTML = '<option value="FreeSans">FreeSans (default)</option>';
                });
            }

            function loadFPPData() {
                fetch('/api/fpp/data')
                .then(r => r.json())
                .then(data => {
                    if (data.error) console.warn('FPP data partial error:', data.error);
                    const defaultSelect = document.getElementById('default_playlist');
                    const nameSelect = document.getElementById('name_display_playlist');
                    const currentDefault = "{{ config.get('default_playlist', '') }}";
                    const currentName = "{{ config.get('name_display_playlist', '') }}";

                    defaultSelect.innerHTML = '<option value="">-- Select a playlist --</option>';
                    nameSelect.innerHTML = '<option value="">-- None (No Playlist Change) --</option>';

                    if (data.playlists && data.playlists.length > 0) {
                        const pg1 = document.createElement('optgroup');
                        pg1.label = '📋 Playlists';
                        const pg2 = document.createElement('optgroup');
                        pg2.label = '📋 Playlists';
                        data.playlists.forEach(playlist => {
                            pg1.appendChild(new Option(playlist, playlist, false, playlist === currentDefault));
                            pg2.appendChild(new Option(playlist, playlist, false, playlist === currentName));
                        });
                        defaultSelect.add(pg1);
                        nameSelect.add(pg2);
                    }

                    if (data.sequences && data.sequences.length > 0) {
                        const sg1 = document.createElement('optgroup');
                        sg1.label = '🎬 Sequences (.fseq)';
                        const sg2 = document.createElement('optgroup');
                        sg2.label = '🎬 Sequences (.fseq)';
                        data.sequences.forEach(seq => {
                            const val = 'seq:' + seq;
                            sg1.appendChild(new Option(seq, val, false, val === currentDefault));
                            sg2.appendChild(new Option(seq, val, false, val === currentName));
                        });
                        defaultSelect.add(sg1);
                        nameSelect.add(sg2);
                    }

                    if (data.videos && data.videos.length > 0) {
                        const vg1 = document.createElement('optgroup');
                        vg1.label = '🎥 Videos';
                        const vg2 = document.createElement('optgroup');
                        vg2.label = '🎥 Videos';
                        data.videos.forEach(vid => {
                            const val = 'vid:' + vid;
                            vg1.appendChild(new Option(vid, val, false, val === currentDefault));
                            vg2.appendChild(new Option(vid, val, false, val === currentName));
                        });
                        defaultSelect.add(vg1);
                        nameSelect.add(vg2);
                    }

                    if (data.images && data.images.length > 0) {
                        const ig1 = document.createElement('optgroup');
                        ig1.label = '🖼️ Images';
                        const ig2 = document.createElement('optgroup');
                        ig2.label = '🖼️ Images';
                        data.images.forEach(img => {
                            const val = 'img:' + img;
                            ig1.appendChild(new Option(img, val, false, val === currentDefault));
                            ig2.appendChild(new Option(img, val, false, val === currentName));
                        });
                        defaultSelect.add(ig1);
                        nameSelect.add(ig2);
                    }

                    window._fppSeqList = data.sequences || [];

                    const modelSelect = document.getElementById('overlay_model_name');
                    const currentModel = "{{ config.get('overlay_model_name', 'Texting Matrix') }}";
                    modelSelect.innerHTML = '<option value="">-- None --</option>';
                    window.fppModels = data.models || [];

                    if (data.models && data.models.length > 0) {
                        data.models.forEach(model => {
                            const name = typeof model === 'object' ? model.name : model;
                            const opt = new Option(name, name, false, name === currentModel);
                            modelSelect.add(opt);
                        });
                        // Set aspect ratio and save dimensions for the currently selected model
                        const cur = data.models.find(m => (typeof m === 'object' ? m.name : m) === currentModel);
                        if (cur && cur.width && cur.height) {
                            updateModelAspect(cur.width, cur.height);
                            document.getElementById('overlay_model_width').value = cur.width;
                            document.getElementById('overlay_model_height').value = cur.height;
                            saveConfig();
                        }
                    }

                    modelSelect.addEventListener('change', function() {
                        const selected = this.value;
                        const m = (window.fppModels || []).find(m => (typeof m === 'object' ? m.name : m) === selected);
                        if (m && m.width && m.height) {
                            updateModelAspect(m.width, m.height);
                            document.getElementById('overlay_model_width').value = m.width;
                            document.getElementById('overlay_model_height').value = m.height;
                        }
                        saveConfig();
                    });

                    // Load background preview now that dropdowns are populated
                    try { if (window.toggleFseqPreview) window.toggleFseqPreview(); } catch(e) { console.error('Preview error:', e); }
                })
                .catch(function(e) {
                    console.error('FPP data load failed:', e);
                    try { if (window.toggleFseqPreview) window.toggleFseqPreview(); } catch(e2) {}
                });
            }


var _saveTimer = null;
            function saveConfig() {
                clearTimeout(_saveTimer);
                _saveTimer = setTimeout(_doSave, 300);
            }
            function _doSave() {
                var status = document.getElementById('autosave_status');
                status.style.color = '#888';
                status.textContent = 'Saving...';

                const data = {
                    twilio_account_sid: document.getElementById('account_sid').value,
                    twilio_auth_token: document.getElementById('auth_token').value,
                    twilio_phone_number: document.getElementById('phone_number').value,
                    poll_interval: parseInt(document.getElementById('poll_interval').value),
                    display_duration: parseInt(document.getElementById('display_duration').value),
                    max_messages_per_phone: parseInt(document.getElementById('max_messages').value),
                    allow_duplicate_names: document.getElementById('allow_duplicate_names').checked,
                    max_message_length: parseInt(document.getElementById('max_length').value),
                    one_word_only: document.getElementById('one_word_only')?.checked ?? false,
                    two_words_max: document.getElementById('two_words_max')?.checked ?? true,
                    profanity_filter: document.getElementById('profanity_filter').checked,
                    use_whitelist: document.getElementById('use_whitelist').checked,
                    default_playlist: document.getElementById('default_playlist').value,
                    name_display_playlist: document.getElementById('name_display_playlist').value,
                    overlay_model_name: document.getElementById('overlay_model_name').value,
                    text_color: document.getElementById('text_color_hex').value,
                    text_font: document.getElementById('text_font').value,
                    text_font_size: parseInt(document.getElementById('text_font_size').value),
                    scroll_speed: parseInt(document.getElementById('scroll_speed').value),
                    text_position: document.getElementById('text_position').value,
                    overlay_model_width: parseInt(document.getElementById('overlay_model_width').value) || 0,
                    overlay_model_height: parseInt(document.getElementById('overlay_model_height').value) || 0,
                    message_lines: [
                        document.getElementById('line_1').value,
                        document.getElementById('line_2').value,
                        document.getElementById('line_3').value,
                        document.getElementById('line_4').value,
                    ],
                    line_positions: window._linePositions || [{x:-1,y:-1},{x:-1,y:-1},{x:-1,y:-1},{x:-1,y:-1}],
                    sms_response_show_not_live: document.getElementById('sms_response_show_not_live').checked,
                    sms_response_success: document.getElementById('sms_response_success').checked,
                    sms_response_profanity: document.getElementById('sms_response_profanity').checked,
                    sms_response_rate_limited: document.getElementById('sms_response_rate_limited').checked,
                    sms_response_duplicate: document.getElementById('sms_response_duplicate').checked,
                    sms_response_invalid_format: !document.getElementById('use_whitelist').checked && document.getElementById('sms_response_invalid_format').checked,
                    sms_response_not_whitelisted: document.getElementById('sms_response_not_whitelisted').checked,
                    sms_response_blocked: document.getElementById('sms_response_blocked').checked,
                    response_success: document.getElementById('response_success').value,
                    response_profanity: document.getElementById('response_profanity').value,
                    response_rate_limited: document.getElementById('response_rate_limited').value,
                    response_duplicate: document.getElementById('response_duplicate').value,
                    response_invalid_format: document.getElementById('response_invalid_format').value,
                    response_not_whitelisted: document.getElementById('response_not_whitelisted').value,
                    response_blocked: document.getElementById('response_blocked').value,
                    response_show_not_live: document.getElementById('response_show_not_live').value
                };

                fetch('/api/config', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify(data)
                })
                .then(r => r.json())
                .then(function() {
                    status.style.color = '#4CAF50';
                    status.textContent = '✓ Saved';
                    setTimeout(function() { status.textContent = ''; }, 3000);
                })
                .catch(function() {
                    status.style.color = '#f44336';
                    status.textContent = '✗ Save failed';
                });
            }

            function setupAutoSave() {
                // enabled has its own handler — saves only itself so it never
                // clobbers the runtime state set by TwilioStart/TwilioStop
                var enabledEl = document.getElementById('enabled');
                if (enabledEl) enabledEl.addEventListener('change', function() {
                    fetch('/api/config', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({enabled: this.checked})
                    });
                });

                // Checkboxes, selects, color picker — save immediately on change
                ['profanity_filter','use_whitelist','allow_duplicate_names','text_color',
                 'default_playlist','name_display_playlist','overlay_model_name',
                 'text_font','text_position','scroll_speed',
                 'one_word_only','two_words_max',
                 'sms_response_success','sms_response_profanity','sms_response_rate_limited',
                 'sms_response_duplicate','sms_response_invalid_format',
                 'sms_response_not_whitelisted','sms_response_blocked'
                ].forEach(function(id) {
                    var el = document.getElementById(id);
                    if (el) el.addEventListener('change', saveConfig);
                });
                // Reload background preview when Names Display content or model changes
                ['name_display_playlist', 'overlay_model_name'].forEach(function(id) {
                    var el = document.getElementById(id);
                    if (el) el.addEventListener('change', function() {
                        if (window.toggleFseqPreview) window.toggleFseqPreview();
                    });
                });

                // Text, number inputs — save when user clicks away
                ['account_sid','auth_token','phone_number',
                 'poll_interval','display_duration','max_messages','max_length',
                 'text_color_hex','text_font_size',
                 'line_1','line_2','line_3','line_4',
                 'response_success','response_profanity','response_rate_limited',
                 'response_duplicate','response_invalid_format',
                 'response_not_whitelisted','response_blocked'
                ].forEach(function(id) {
                    var el = document.getElementById(id);
                    if (el) el.addEventListener('blur', saveConfig);
                });
            }

            function testConnection() {
                const result = document.getElementById('twilio_test_result');
                result.innerHTML = '<span style="color:#555;">Testing...</span>';
                fetch('/api/test')
                .then(r => r.json())
                .then(data => {
                    if (data.success) {
                        result.innerHTML = '<span style="color:#4CAF50;">✅ Twilio connection successful!</span>';
                    } else {
                        result.innerHTML = '<span style="color:#f44336;">❌ Connection failed: ' + data.error + '</span>';
                    }
                });
            }

            function viewMessages() {
                window.location.href = '/messages';
            }

            function submitTestMessage() {
                const testName = document.getElementById('test_name').value.trim();
                const resultDiv = document.getElementById('test_result');

                if (!testName) {
                    resultDiv.innerHTML = '<p class="error">❌ Please enter a name</p>';
                    return;
                }

                resultDiv.innerHTML = '<p>🧪 Submitting test message...</p>';

                fetch('/api/test/message', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({name: testName})
                })
                .then(r => r.json())
                .then(data => {
                    if (data.success) {
                        resultDiv.innerHTML = '<p class="success">✅ ' + data.message + '</p>';
                        document.getElementById('test_name').value = '';
                        setTimeout(() => {
                            resultDiv.innerHTML += '<p><a href="/messages" style="color: #4CAF50;">📋 View Queue Status</a></p>';
                        }, 1000);
                    } else {
                        resultDiv.innerHTML = '<p class="error">❌ ' + data.error + '</p>';
                        if (data.reason) {
                            resultDiv.innerHTML += '<p style="font-size: 12px; color: #666;">Reason: ' + data.reason + '</p>';
                        }
                    }
                });
            }

            function sendTestSMS() {
                const phone = document.getElementById('test_sms_phone').value.trim();
                const messageType = document.getElementById('test_sms_type').value;
                const resultDiv = document.getElementById('test_sms_result');

                if (!phone) {
                    resultDiv.innerHTML = '<p class="error">❌ Please enter a phone number</p>';
                    return;
                }

                resultDiv.innerHTML = '<p>📤 Sending test SMS...</p>';

                fetch('/api/test/sms', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({phone: phone, message_type: messageType})
                })
                .then(r => r.json())
                .then(data => {
                    if (data.success) {
                        resultDiv.innerHTML = '<p class="success">✅ Test SMS sent successfully to ' + phone + '</p>';
                    } else {
                        resultDiv.innerHTML = '<p class="error">❌ Failed to send: ' + data.error + '</p>';
                    }
                });
            }
        </script>
    </body>
    </html>
    """

    return render_template_string(html, config=config)

@app.route('/api/config', methods=['POST'])
def update_config():
    global config, twilio_client, polling_thread, stop_polling
    try:
        new_config = request.json
        config.update(new_config)

        # Normalize phone number to E.164 (strip spaces, dashes, parens — keep + and digits)
        if config.get('twilio_phone_number'):
            config['twilio_phone_number'] = re.sub(r'[^\d+]', '', config['twilio_phone_number'])

        save_config()

        if config['twilio_account_sid'] and config['twilio_auth_token']:
            twilio_client = Client(
                config['twilio_account_sid'],
                config['twilio_auth_token']
            )
            # Start polling thread if not already running (e.g. credentials entered
            # after TwilioStart was called, or updated mid-show)
            if not polling_thread or not polling_thread.is_alive():
                polling_thread = threading.Thread(target=poll_twilio, daemon=True)
                polling_thread.start()
                logging.error("▶️ Polling thread started after credential update")

        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})

@app.route('/api/fpp/fonts')
def fpp_fonts_endpoint():
    try:
        return jsonify(get_fpp_fonts())
    except Exception:
        return jsonify([])

@app.route('/api/fpp/data')
def get_fpp_data():
    global _fpp_data_cache, _fpp_data_cache_time
    try:
        # Return cached result if still fresh
        if _fpp_data_cache and (time.time() - _fpp_data_cache_time) < _FPP_DATA_CACHE_TTL:
            return jsonify(_fpp_data_cache)

        # Fetch all in parallel instead of sequentially
        results = {}
        tasks = {
            'playlists': get_fpp_playlists,
            'sequences': get_fpp_sequences,
            'models':    get_fpp_models,
            'fonts':     get_fpp_fonts,
            'videos':    get_fpp_videos,
            'images':    get_fpp_images,
        }
        with ThreadPoolExecutor(max_workers=6) as executor:
            futures = {executor.submit(fn): key for key, fn in tasks.items()}
            for future in as_completed(futures):
                key = futures[future]
                try:
                    results[key] = future.result()
                except Exception as e:
                    logging.error(f"FPP data task '{key}' failed: {e}")
                    results[key] = []

        _fpp_data_cache = results
        _fpp_data_cache_time = time.time()
        return jsonify(results)
    except Exception as e:
        return jsonify({"error": str(e)})

@app.route('/api/fpp/refresh', methods=['POST'])
def refresh_fpp_data():
    global _fpp_data_cache, _fpp_data_cache_time
    _fpp_data_cache = None
    _fpp_data_cache_time = 0
    return jsonify({"success": True})

@app.route('/api/fpp/test', methods=['POST'])
def test_fpp_api():
    try:
        success, status = test_fpp_connection()
        return jsonify({"success": success, "status": status})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})

@app.route('/api/fseq/debug')
def fseq_debug():
    """Diagnostic endpoint — returns JSON describing exactly how the FSEQ frame would be read.
    ?sequence=name&model=ModelName   (same params as /api/fseq/frame)
    Helps diagnose channel-offset and bpp issues without needing SSH."""
    seq        = request.args.get('sequence', '').strip()
    model_name = request.args.get('model', config.get('overlay_model_name', '')).strip()

    if not seq:
        return jsonify({'error': 'No sequence specified'}), 400

    name     = seq.removeprefix('seq:').removesuffix('.fseq')
    filepath = os.path.join(FSEQ_SEQUENCE_PATH, name + '.fseq')
    if not os.path.exists(filepath):
        return jsonify({'error': f'Sequence not found: {name}.fseq'}), 404

    result = {
        'sequence':      name,
        'model':         model_name,
        'zstd_available': ZSTD_AVAILABLE,
        'pil_available':  PIL_AVAILABLE,
    }

    try:
        hdr = parse_fseq_header(filepath)
        comp_names = {0: 'uncompressed', 1: 'zlib', 2: 'zstd'}
        result['fseq'] = {
            'channel_count':     hdr['channel_count'],
            'frame_count':       hdr['frame_count'],
            'fps':               round(hdr['fps'], 2),
            'step_time_ms':      hdr['step_time_ms'],
            'compression_type':  hdr['compression_type'],
            'compression_name':  comp_names.get(hdr['compression_type'], 'unknown'),
            'chan_data_offset':   hdr['chan_data_offset'],
            'num_comp_blocks':   len(hdr['comp_blocks']),
            'num_sparse_ranges': hdr['num_sparse_ranges'],
            'sparse_ranges':     hdr['sparse_ranges'],
            'sparse_sum':        sum(sr['count'] for sr in hdr['sparse_ranges']),
        }
    except Exception as e:
        result['fseq_error'] = str(e)
        return jsonify(result)

    sc, cc = get_model_channel_info(model_name) if model_name else (None, None)
    mw = int(request.args.get('width',  config.get('overlay_model_width',  0)))
    mh = int(request.args.get('height', config.get('overlay_model_height', 0)))
    num_pixels = mw * mh if mw > 0 and mh > 0 else 0
    ch_count   = cc if cc else (num_pixels * 3 if num_pixels else None)
    bpp        = (ch_count // num_pixels) if (num_pixels and ch_count) else None
    start_ch   = (sc - 1) if sc else 0   # 0-indexed

    resolved_frame_byte = _sparse_ch_to_frame_byte(hdr['sparse_ranges'], start_ch)
    result['model_info'] = {
        'start_channel_1idx':   sc,
        'channel_count':        cc,
        'width':                mw,
        'height':               mh,
        'num_pixels':           num_pixels,
        'effective_ch_count':   ch_count,
        'effective_bpp':        bpp,
        'start_ch_0idx':        start_ch,
        'resolved_frame_byte':  resolved_frame_byte,  # None = not in sparse ranges
    }

    # Try reading frame 0 and show first 5 pixel values
    if ch_count and num_pixels:
        try:
            raw = read_fseq_frame(hdr, 0, start_ch, ch_count)
            sample_pixels = []
            actual_bpp = bpp or 3
            for i in range(min(5, num_pixels)):
                b = i * actual_bpp
                if b + 2 < len(raw):
                    sample_pixels.append([raw[b], raw[b+1], raw[b+2]])
                else:
                    sample_pixels.append(None)
            result['frame0_sample'] = {
                'bytes_read':    len(raw),
                'bytes_expected': ch_count,
                'first_5_pixels_rgb': sample_pixels,
                'all_zero':      all(v == 0 for v in raw),
                'all_same':      len(set(raw)) == 1,
            }
        except Exception as e:
            result['frame0_error'] = str(e)

    return jsonify(result)

@app.route('/api/fseq/info')
def fseq_info():
    """Return FSEQ file metadata for the canvas scrubber (frame count, fps, duration).
    Also attempts to auto-detect the overlay model's start channel from FPP."""
    seq        = request.args.get('sequence', '').strip()
    model_name = request.args.get('model', config.get('overlay_model_name', '')).strip()
    if not seq:
        return jsonify({'error': 'No sequence specified'}), 400
    name = seq.removeprefix('seq:').removesuffix('.fseq')
    filepath = os.path.join(FSEQ_SEQUENCE_PATH, name + '.fseq')
    if not os.path.exists(filepath):
        return jsonify({'error': f'Sequence not found: {name}.fseq'}), 404
    try:
        hdr = parse_fseq_header(filepath)
        detected_sc, detected_cc = (get_model_channel_info(model_name)
                                    if model_name else (None, None))
        return jsonify({
            'frame_count':             hdr['frame_count'],
            'fps':                     round(hdr['fps'], 3),
            'duration_ms':             hdr['duration_ms'],
            'channel_count':           hdr['channel_count'],
            'compression_type':        hdr['compression_type'],
            'step_time_ms':            hdr['step_time_ms'],
            'detected_start_channel':  detected_sc,
            'detected_channel_count':  detected_cc,
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/fseq/frame')
def fseq_frame():
    """Return a single FSEQ frame as a PNG image for the canvas background preview."""
    if not PIL_AVAILABLE:
        return jsonify({'error': 'Pillow not installed — run fpp_install.sh'}), 503

    seq        = request.args.get('sequence', '').strip()
    frame_idx  = max(0, int(request.args.get('frame', 0)))
    model_name = request.args.get('model', config.get('overlay_model_name', ''))
    width      = int(request.args.get('width',  config.get('overlay_model_width',  0)))
    height     = int(request.args.get('height', config.get('overlay_model_height', 0)))
    start_ch_override  = request.args.get('start_channel', '').strip()
    ch_count_override  = request.args.get('channel_count', '').strip()

    if not seq:
        return jsonify({'error': 'No sequence specified'}), 400
    if width <= 0 or height <= 0:
        return jsonify({'error': 'Overlay model dimensions unknown — select a model first'}), 400

    name = seq.removeprefix('seq:').removesuffix('.fseq')
    filepath = os.path.join(FSEQ_SEQUENCE_PATH, name + '.fseq')
    if not os.path.exists(filepath):
        return jsonify({'error': f'Sequence not found: {name}.fseq'}), 404

    # Determine start channel and channel count from FPP model info
    if start_ch_override:
        start_ch_1 = int(start_ch_override)
        ch_count   = int(ch_count_override) if ch_count_override else width * height * 3
    else:
        start_ch_1, ch_count_fpp = (get_model_channel_info(model_name)
                                    if model_name else (None, None))
        ch_count = ch_count_fpp if ch_count_fpp else width * height * 3

    if not start_ch_1:
        return jsonify({
            'error': (
                f'Could not find start channel for model "{model_name}". '
                'Verify the overlay model name matches an FPP channel output model.'
            )
        }), 400

    try:
        hdr          = parse_fseq_header(filepath)
        frame_idx    = min(frame_idx, hdr['frame_count'] - 1)
        start_ch     = start_ch_1 - 1   # convert to 0-indexed
        num_pixels   = width * height
        # bytes_per_pixel: 3 for RGB, 4 for RGBW — derived from actual channel count
        bpp          = max(3, ch_count // num_pixels) if num_pixels > 0 else 3

        raw = read_fseq_frame(hdr, frame_idx, start_ch, ch_count)

        logging.info(
            f"FSEQ preview: model={model_name} start_ch={start_ch_1} "
            f"ch_count={ch_count} bpp={bpp} frame={frame_idx} "
            f"first_px=({raw[0] if raw else '?'},{raw[1] if len(raw)>1 else '?'},"
            f"{raw[2] if len(raw)>2 else '?'})"
        )

        img = Image.new('RGB', (width, height))
        pixels = []
        for i in range(num_pixels):
            b = i * bpp
            if b + 2 < len(raw):
                pixels.append((raw[b], raw[b + 1], raw[b + 2]))
            else:
                pixels.append((0, 0, 0))
        img.putdata(pixels)

        buf = io.BytesIO()
        img.save(buf, format='PNG')
        buf.seek(0)
        return Response(buf.read(), mimetype='image/png',
                        headers={'Cache-Control': 'no-store'})
    except Exception as e:
        logging.error(f"FSEQ frame error: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/media/preview')
def media_preview():
    """Return a canvas-preview PNG for an image or video file.
    ?type=img&file=filename.jpg  — resize image to model dims and return as PNG.
    ?type=vid&file=filename.mp4&time=0 — extract a frame at `time` seconds via ffmpeg,
        resize to model dims, return as PNG.  Falls back to a black frame if ffmpeg
        is unavailable or extraction fails."""
    if not PIL_AVAILABLE:
        return jsonify({'error': 'Pillow not installed — run fpp_install.sh'}), 503

    media_type = request.args.get('type', '').strip()   # 'img' or 'vid'
    filename   = request.args.get('file', '').strip()
    time_sec   = max(0.0, float(request.args.get('time', 0)))
    width  = int(request.args.get('width',  config.get('overlay_model_width',  0)))
    height = int(request.args.get('height', config.get('overlay_model_height', 0)))

    if not filename or media_type not in ('img', 'vid'):
        return jsonify({'error': 'Requires ?type=img|vid&file=filename'}), 400
    if width <= 0 or height <= 0:
        return jsonify({'error': 'Overlay model dimensions unknown — select a model first'}), 400

    # Security: no path traversal — strip all directory components
    filename = os.path.basename(filename)

    try:
        if media_type == 'img':
            img_path = os.path.join(FPP_IMAGES_PATH, filename)
            if not os.path.exists(img_path):
                return jsonify({'error': f'Image not found: {filename}'}), 404
            img = Image.open(img_path).convert('RGB')
            img = img.resize((width, height), Image.LANCZOS)

        else:  # vid
            vid_path = os.path.join(FPP_VIDEOS_PATH, filename)
            if not os.path.exists(vid_path):
                return jsonify({'error': f'Video not found: {filename}'}), 404
            # Try ffmpeg to extract a single frame at the requested timestamp
            import subprocess as _sp
            try:
                result = _sp.run(
                    [
                        'ffmpeg', '-ss', str(time_sec), '-i', vid_path,
                        '-vframes', '1', '-f', 'image2pipe',
                        '-vcodec', 'png', '-'
                    ],
                    capture_output=True, timeout=10
                )
                if result.returncode == 0 and result.stdout:
                    img = Image.open(io.BytesIO(result.stdout)).convert('RGB')
                    img = img.resize((width, height), Image.LANCZOS)
                else:
                    # ffmpeg failed — return a dark grey placeholder
                    img = Image.new('RGB', (width, height), (32, 32, 32))
            except (FileNotFoundError, _sp.TimeoutExpired):
                # ffmpeg not installed on this system — return placeholder
                img = Image.new('RGB', (width, height), (32, 32, 32))

        buf = io.BytesIO()
        img.save(buf, format='PNG')
        buf.seek(0)
        return Response(buf.read(), mimetype='image/png',
                        headers={'Cache-Control': 'no-store'})
    except Exception as e:
        logging.error(f"media_preview error: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/test')
def test_twilio():
    try:
        if not twilio_client:
            return jsonify({"success": False, "error": "Twilio client not initialized"})
        
        account = twilio_client.api.accounts(config['twilio_account_sid']).fetch()
        return jsonify({"success": True, "account": account.friendly_name})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})

@app.route('/api/messages')
def get_messages():
    try:
        with open(get_day_log_path(), 'r') as f:
            messages = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        messages = []
    return jsonify(list(reversed(messages)))

@app.route('/api/messages/clear', methods=['POST'])
def clear_messages():
    try:
        with open(get_day_log_path(), 'w') as f:
            json.dump([], f)
        logging.info("Message history cleared (today's file)")
        return jsonify({"success": True})
    except Exception as e:
        logging.error(f"Error clearing messages: {e}")
        return jsonify({"success": False, "error": str(e)})

@app.route('/api/messages/<date_str>')
def get_messages_by_date(date_str):
    try:
        target_date = datetime.strptime(date_str, "%Y-%m-%d").date()
    except ValueError:
        return jsonify({"error": "Invalid date format. Use YYYY-MM-DD."}), 400
    try:
        with open(get_day_log_path(target_date), 'r') as f:
            messages = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        messages = []
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    return jsonify(list(reversed(messages)))

@app.route('/api/queue/status')
def queue_status():
    try:
        status = get_queue_status()
        return jsonify(status)
    except Exception as e:
        return jsonify({"error": str(e)})

@app.route('/api/test/message', methods=['POST'])
def test_message_submission():
    try:
        data = request.json
        test_name = data.get('name', '').strip()
        test_phone = data.get('phone', 'Local Testing')
        
        if not config.get('enabled', False):
            return jsonify({"success": False, "error": "Show is not live — run TwilioStart first"})

        if not test_name:
            return jsonify({"success": False, "error": "Name is required"})

        test_name = extract_name(test_name)
        is_valid, validation_msg = is_valid_name(test_name)

        if not is_valid:
            return jsonify({"success": False, "error": validation_msg, "reason": "invalid_format"})

        if not is_on_whitelist(test_name):
            return jsonify({"success": False, "error": "Name not on whitelist", "reason": "not_on_whitelist"})

        if config['profanity_filter'] and contains_profanity(test_name):
            return jsonify({"success": False, "error": "Profanity detected", "reason": "profanity"})

        success = add_to_queue(test_name, test_phone, f"TEST: {test_name}")

        if success:
            logging.info(f"🧪 Queued: {test_name}")
            log_message(test_phone, f"TEST: {test_name}", test_name, "queued")
            return jsonify({"success": True, "message": f"Test message '{test_name}' queued successfully!"})
        else:
            logging.error(f"🧪 Queue error: {test_name}")
            return jsonify({"success": False, "error": "Failed to add to queue"})
            
    except Exception as e:
        logging.error(f"🧪 💥 ERROR in test message submission: {e}")
        import traceback
        logging.error(traceback.format_exc())
        return jsonify({"success": False, "error": str(e)})

@app.route('/api/test/sms', methods=['POST'])
def test_sms_response():
    """Test sending an SMS response — bypasses enabled/disabled toggles so any response can be previewed"""
    try:
        data = request.json
        phone = data.get('phone', '').strip()
        message_type = data.get('message_type', 'success')

        if not phone:
            return jsonify({"success": False, "error": "Phone number is required"})

        if not twilio_client:
            return jsonify({"success": False, "error": "Twilio credentials not configured"})

        response_message = config.get(f"response_{message_type}", "")
        if not response_message:
            return jsonify({"success": False, "error": f"No message text configured for '{message_type}'"})

        twilio_client.messages.create(
            body=response_message,
            from_=config['twilio_phone_number'],
            to=phone
        )
        return jsonify({"success": True, "message": f"Test SMS sent to {phone}"})

    except Exception as e:
        logging.error(f"Error in test SMS: {e}")
        return jsonify({"success": False, "error": str(e)})

@app.route('/api/phone/block', methods=['POST'])
def api_block_phone():
    try:
        data = request.json
        phone = data.get('phone')
        if phone:
            success = block_phone(phone)
            return jsonify({"success": success, "phone": phone})
        return jsonify({"success": False, "error": "No phone number provided"})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})

@app.route('/api/phone/unblock', methods=['POST'])
def api_unblock_phone():
    try:
        data = request.json
        phone = data.get('phone')
        if phone:
            success = unblock_phone(phone)
            return jsonify({"success": success, "phone": phone})
        return jsonify({"success": False, "error": "No phone number provided"})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})

@app.route('/api/blocklist')
def api_get_blocklist():
    try:
        blocklist = load_blocklist()
        return jsonify({"blocklist": blocklist})
    except Exception as e:
        return jsonify({"error": str(e)})

@app.route('/api/blacklist')
def api_get_blacklist():
    try:
        words = load_blacklist_words()
        return jsonify({"blacklist": words})
    except Exception as e:
        return jsonify({"error": str(e)})

@app.route('/api/blacklist/add', methods=['POST'])
def api_add_blacklist():
    global _blacklist_cache, _blacklist_mtime
    try:
        data = request.json
        word = data.get('word', '').strip().lower()
        if not word:
            return jsonify({"success": False, "error": "Word is required"})
        # Check if already in global list
        global_words = set()
        if os.path.exists(BLACKLIST_FILE):
            with open(BLACKLIST_FILE, 'r', encoding='latin-1') as f:
                global_words = {line.strip().lower() for line in f if line.strip()}
        if word in global_words:
            return jsonify({"success": False, "error": "Word already in blacklist"})
        # Add to user-added list
        added = load_blacklist_added()
        if word in added:
            return jsonify({"success": False, "error": "Word already in blacklist"})
        added.add(word)
        with open(BLACKLIST_ADDED_FILE, 'w', encoding='utf-8') as f:
            f.write('\n'.join(sorted(added)) + '\n')
        # If word was previously removed from global, un-remove it
        removed = load_blacklist_removed()
        if word in removed:
            removed.discard(word)
            with open(BLACKLIST_REMOVED_FILE, 'w', encoding='utf-8') as f:
                f.write('\n'.join(sorted(removed)) + '\n')
        _blacklist_cache = None
        _blacklist_mtime = None
        logging.info(f"Added '{word}' to user blacklist")
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})

@app.route('/api/blacklist/remove', methods=['POST'])
def api_remove_blacklist():
    global _blacklist_cache, _blacklist_mtime
    try:
        data = request.json
        word = data.get('word', '').strip().lower()
        global_words = set()
        if os.path.exists(BLACKLIST_FILE):
            with open(BLACKLIST_FILE, 'r', encoding='latin-1') as f:
                global_words = {line.strip().lower() for line in f if line.strip()}
        # Remove from user-added if present
        added = load_blacklist_added()
        if word in added:
            added.discard(word)
            with open(BLACKLIST_ADDED_FILE, 'w', encoding='utf-8') as f:
                f.write('\n'.join(sorted(added)) + '\n' if added else '')
        # If in global, track removal so git pull can't re-add it
        if word in global_words:
            removed = load_blacklist_removed()
            removed.add(word)
            with open(BLACKLIST_REMOVED_FILE, 'w', encoding='utf-8') as f:
                f.write('\n'.join(sorted(removed)) + '\n')
        _blacklist_cache = None
        _blacklist_mtime = None
        logging.info(f"Removed '{word}' from blacklist")
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})

@app.route('/api/whitelist')
def api_get_whitelist():
    try:
        names = sorted(load_whitelist())
        return jsonify({"whitelist": names})
    except Exception as e:
        return jsonify({"error": str(e)})

@app.route('/api/whitelist/add', methods=['POST'])
def api_add_whitelist():
    global _whitelist_cache, _whitelist_mtime
    try:
        data = request.json
        name = data.get('name', '').strip().lower()
        if not name:
            return jsonify({"success": False, "error": "Name is required"})
        global_names = set()
        if os.path.exists(WHITELIST_FILE):
            with open(WHITELIST_FILE, 'r', encoding='latin-1') as f:
                global_names = {line.strip().lower() for line in f if line.strip()}
        removed = load_removed_names()

        if name in global_names:
            if name in removed:
                # Name was blocked by user — un-remove it to make it active again
                removed.discard(name)
                with open(WHITELIST_REMOVED_FILE, 'w', encoding='utf-8') as f:
                    f.write('\n'.join(sorted(removed)) + '\n')
                _whitelist_cache = None
                _whitelist_mtime = None
                logging.info(f"Re-enabled '{name}' in whitelist")
                return jsonify({"success": True})
            else:
                return jsonify({"success": False, "error": "Name already in whitelist"})

        # Add to user-added list
        added = load_whitelist_added()
        if name in added:
            return jsonify({"success": False, "error": "Name already in whitelist"})
        added.add(name)
        with open(WHITELIST_ADDED_FILE, 'w', encoding='utf-8') as f:
            f.write('\n'.join(sorted(added)) + '\n')
        # If name was previously removed from global, un-remove it
        if name in removed:
            removed.discard(name)
            with open(WHITELIST_REMOVED_FILE, 'w', encoding='utf-8') as f:
                f.write('\n'.join(sorted(removed)) + '\n')
        _whitelist_cache = None
        _whitelist_mtime = None
        logging.info(f"Added '{name}' to user whitelist")
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})

@app.route('/api/whitelist/remove', methods=['POST'])
def api_remove_whitelist():
    global _whitelist_cache, _whitelist_mtime
    try:
        data = request.json
        name = data.get('name', '').strip().lower()
        global_names = set()
        if os.path.exists(WHITELIST_FILE):
            with open(WHITELIST_FILE, 'r', encoding='latin-1') as f:
                global_names = {line.strip().lower() for line in f if line.strip()}
        # Remove from user-added if present
        added = load_whitelist_added()
        if name in added:
            added.discard(name)
            with open(WHITELIST_ADDED_FILE, 'w', encoding='utf-8') as f:
                f.write('\n'.join(sorted(added)) + '\n' if added else '')
        # If in global, track removal so git pull can't re-add it
        if name in global_names:
            removed = load_removed_names()
            removed.add(name)
            with open(WHITELIST_REMOVED_FILE, 'w', encoding='utf-8') as f:
                f.write('\n'.join(sorted(removed)) + '\n')
        _whitelist_cache = None
        _whitelist_mtime = None
        logging.info(f"Removed '{name}' from whitelist")
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})

@app.route('/whitelist')
def view_whitelist():
    html = """
    <!DOCTYPE html>
    <html>
    <head>
        <title>Name Whitelist</title>
        <style>
            body { font-family: Arial, sans-serif; margin: 0; padding: 20px; background: #ffffff; color: #333; }
            h1 { color: #4CAF50; }
            table { width: 100%; border-collapse: collapse; margin: 10px 0; }
            th, td { border: 1px solid #ddd; padding: 8px 10px; text-align: left; }
            th { background: #4CAF50; color: white; }
            tr:nth-child(even) { background: #f5f5f5; }
            button { background: #4CAF50; color: white; padding: 10px 20px; border: none; border-radius: 4px; cursor: pointer; margin: 5px 5px 5px 0; }
            button:hover { background: #45a049; }
            .remove-btn { background: #f44336; padding: 4px 10px; font-size: 12px; margin: 0; }
            .remove-btn:hover { background: #d32f2f; }
            .add-btn { background: #2196F3; }
            .add-btn:hover { background: #0b7dda; }
            .info { background: #e8f5e9; padding: 10px; border-radius: 5px; margin: 10px 0; font-size: 14px; border: 1px solid #c8e6c9; }
            .add-row { display: flex; gap: 10px; margin: 12px 0; }
            .add-row input { flex: 1; padding: 10px; border: 1px solid #ccc; border-radius: 4px; font-size: 14px; }
            .search-row { display: flex; gap: 10px; margin: 12px 0; }
            .search-row input { flex: 1; padding: 10px; border: 2px solid #4CAF50; border-radius: 4px; font-size: 14px; }
            .hint { color: #888; font-size: 13px; margin: 6px 0; }
            .error { color: #f44336; font-size: 13px; }
            .success { color: #4CAF50; font-size: 13px; }
            .empty { background: #f5f5f5; padding: 30px; text-align: center; border-radius: 5px; margin: 20px 0; }
        </style>
    </head>
    <body><script>if('scrollRestoration'in history)history.scrollRestoration='manual';function _toTop(){window.scrollTo(0,0);document.documentElement.scrollTop=0;document.body.scrollTop=0;try{window.parent.postMessage({type:'scrollTop'},'*');}catch(e){}}_toTop();document.addEventListener('DOMContentLoaded',_toTop);window.addEventListener('load',_toTop);</script>
        <h1>📋 Name Whitelist</h1>
        <div class="info">
            Only names on this list are accepted when the whitelist is enabled. &nbsp;|&nbsp; <strong id="count">Loading...</strong>
        </div>
        {% if not config.get('use_whitelist', False) %}
        <div style="background:#fff3cd; border:1px solid #ffc107; color:#856404; padding:10px 14px; border-radius:5px; margin:10px 0; font-size:14px; display:flex; align-items:center; gap:12px; flex-wrap:wrap;">
            <span>⚠️ <strong>Whitelist is not enabled</strong> — All names will be shown regardless of this list.</span>
            <button onclick="toggleSetting('use_whitelist', true)" style="background:#4CAF50; color:white; border:none; padding:6px 14px; border-radius:4px; cursor:pointer; font-size:13px; white-space:nowrap;">✓ Enable Whitelist</button>
        </div>
        {% else %}
        <div style="background:#e8f5e9; border:1px solid #a5d6a7; color:#2e7d32; padding:10px 14px; border-radius:5px; margin:10px 0; font-size:14px; display:flex; align-items:center; gap:12px; flex-wrap:wrap;">
            <span>✅ <strong>Whitelist is enabled</strong></span>
            <button onclick="toggleSetting('use_whitelist', false)" style="background:#f44336; color:white; border:none; padding:6px 14px; border-radius:4px; cursor:pointer; font-size:13px; white-space:nowrap;">✗ Disable Whitelist</button>
        </div>
        {% endif %}
        <button onclick="location.href='/'">← Back to Config</button>

        <script>
        function toggleSetting(key, value) {
            fetch('/api/config', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({[key]: value})
            }).then(() => location.reload());
        }
        </script>

        <h3>Add a Name</h3>
        <div class="add-row">
            <input type="text" id="add_name" placeholder="Type a name to approve and press Enter..." onkeydown="if(event.key==='Enter') addName()">
            <button class="add-btn" onclick="addName()">+ Add</button>
        </div>
        <div id="add_result"></div>

        <h3>Search / Browse</h3>
        <div class="search-row">
            <input type="text" id="search" placeholder="Search names... (e.g. John)" oninput="renderTable()">
        </div>
        <div id="hint" class="hint"></div>
        <div id="list_area"></div>
        <div id="scroll_sentinel" style="height:1px;"></div>

        <script>
            var allNames = [];

            function loadWhitelist() {
                document.getElementById('count').textContent = 'Loading...';
                fetch('/api/whitelist')
                .then(r => r.json())
                .then(data => {
                    allNames = data.whitelist || [];
                    document.getElementById('count').textContent = allNames.length.toLocaleString() + ' approved names';
                    renderTable();
                    window.scrollTo(0, 0);
                })
                .catch(() => {
                    document.getElementById('count').textContent = 'Error loading';
                });
            }

            var visibleCount = 100;
            var currentFiltered = [];
            var PAGE_SIZE = 100;
            var observer = null;

            function setupSentinel() {
                if (observer) observer.disconnect();
                observer = new IntersectionObserver(function(entries) {
                    if (entries[0].isIntersecting) appendRows();
                }, { rootMargin: '400px' });
                observer.observe(document.getElementById('scroll_sentinel'));
            }

            function renderTable() {
                const query = document.getElementById('search').value.trim().toLowerCase();
                const area = document.getElementById('list_area');
                const hint = document.getElementById('hint');

                visibleCount = PAGE_SIZE;

                if (allNames.length === 0) {
                    hint.textContent = '';
                    area.innerHTML = '<div class="empty"><h3>No names in whitelist yet</h3><p>Add names above to approve them.</p></div>';
                    return;
                }

                currentFiltered = query
                    ? allNames.filter(n => n.toLowerCase().includes(query))
                    : allNames;

                if (currentFiltered.length === 0) {
                    hint.textContent = 'No names match "' + query + '"';
                    area.innerHTML = '<div class="empty"><p>No names match your search.</p></div>';
                    return;
                }

                updateHint();
                const showing = currentFiltered.slice(0, visibleCount);
                area.innerHTML = '<table id="names_table"><tr><th>Name</th><th></th><th>Name</th><th></th></tr>' + buildRows(showing) + '</table>';
                setupSentinel();
            }

            function appendRows() {
                if (visibleCount >= currentFiltered.length) return;
                visibleCount = Math.min(visibleCount + PAGE_SIZE, currentFiltered.length);
                updateHint();
                const showing = currentFiltered.slice(0, visibleCount);
                const area = document.getElementById('list_area');
                area.innerHTML = '<table id="names_table"><tr><th>Name</th><th></th><th>Name</th><th></th></tr>' + buildRows(showing) + '</table>';
            }

            function buildRows(items) {
                let rows = '';
                for (let i = 0; i < items.length; i += 2) {
                    const a = items[i];
                    const b = items[i + 1];
                    const ae = a.replace(/'/g, "&#39;");
                    const be = b ? b.replace(/'/g, "&#39;") : '';
                    rows += `<tr>` +
                        `<td style="text-transform:capitalize">${a}</td>` +
                        `<td><button class="remove-btn" onclick="removeName('${ae}')">✕ Remove</button></td>` +
                        (b
                            ? `<td style="text-transform:capitalize">${b}</td><td><button class="remove-btn" onclick="removeName('${be}')">✕ Remove</button></td>`
                            : `<td></td><td></td>`) +
                        `</tr>`;
                }
                return rows;
            }

            function updateHint() {
                const hint = document.getElementById('hint');
                const query = document.getElementById('search').value.trim();
                const showing = Math.min(visibleCount, currentFiltered.length);
                if (query) {
                    hint.textContent = 'Showing ' + showing + ' of ' + currentFiltered.length + ' matches';
                } else {
                    hint.textContent = 'Showing ' + showing + ' of ' + allNames.length.toLocaleString() + ' names' +
                        (showing < allNames.length ? ' — scroll down to load more' : '');
                }
            }

            function addName() {
                const input = document.getElementById('add_name');
                const name = input.value.trim();
                const result = document.getElementById('add_result');
                if (!name) return;
                fetch('/api/whitelist/add', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({name: name})
                })
                .then(r => r.json())
                .then(data => {
                    if (data.success) {
                        result.innerHTML = '<p class="success">✅ Added: ' + name + '</p>';
                        input.value = '';
                        loadWhitelist();
                    } else {
                        result.innerHTML = '<p class="error">❌ ' + data.error + '</p>';
                    }
                    setTimeout(() => result.innerHTML = '', 3000);
                });
            }

            function removeName(name) {
                if (!confirm('Remove "' + name + '" from the whitelist?')) return;
                fetch('/api/whitelist/remove', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({name: name})
                })
                .then(r => r.json())
                .then(() => loadWhitelist());
            }

            loadWhitelist();
        </script>
    </body>
    </html>
    """
    return render_template_string(html, config=config)

@app.route('/blacklist')
def view_blacklist_page():
    html = """
    <!DOCTYPE html>
    <html>
    <head>
        <title>Profanity Filter — Blacklist</title>
        <style>
            body { font-family: Arial, sans-serif; margin: 0; padding: 20px; background: #ffffff; color: #333; }
            h1 { color: #f44336; }
            table { width: 100%; border-collapse: collapse; margin: 10px 0; }
            th, td { border: 1px solid #ddd; padding: 8px 10px; text-align: left; }
            th { background: #f44336; color: white; }
            tr:nth-child(even) { background: #f5f5f5; }
            button { background: #4CAF50; color: white; padding: 10px 20px; border: none; border-radius: 4px; cursor: pointer; margin: 5px 5px 5px 0; }
            button:hover { background: #45a049; }
            .remove-btn { background: #f44336; padding: 4px 10px; font-size: 12px; margin: 0; }
            .remove-btn:hover { background: #d32f2f; }
            .add-btn { background: #2196F3; }
            .add-btn:hover { background: #0b7dda; }
            .info { background: #fce4e4; padding: 10px; border-radius: 5px; margin: 10px 0; font-size: 14px; border: 1px solid #f5c6c6; }
            .add-row { display: flex; gap: 10px; margin: 12px 0; }
            .add-row input { flex: 1; padding: 10px; border: 1px solid #ccc; border-radius: 4px; font-size: 14px; }
            .search-row { display: flex; gap: 10px; margin: 12px 0; }
            .search-row input { flex: 1; padding: 10px; border: 2px solid #f44336; border-radius: 4px; font-size: 14px; }
            .hint { color: #888; font-size: 13px; margin: 6px 0; }
            .error { color: #f44336; font-size: 13px; }
            .success { color: #4CAF50; font-size: 13px; }
            .empty { background: #f5f5f5; padding: 30px; text-align: center; border-radius: 5px; margin: 20px 0; }
        </style>
    </head>
    <body><script>if('scrollRestoration'in history)history.scrollRestoration='manual';function _toTop(){window.scrollTo(0,0);document.documentElement.scrollTop=0;document.body.scrollTop=0;try{window.parent.postMessage({type:'scrollTop'},'*');}catch(e){}}_toTop();document.addEventListener('DOMContentLoaded',_toTop);window.addEventListener('load',_toTop);</script>
        <h1>🚫 Profanity Blacklist</h1>
        <div class="info">
            ℹ️ Messages containing any word on this list are rejected by the profanity filter. &nbsp;|&nbsp; <strong id="count">Loading...</strong>
        </div>
        {% if not config.get('profanity_filter', True) %}
        <div style="background:#fff3cd; border:1px solid #ffc107; color:#856404; padding:10px 14px; border-radius:5px; margin:10px 0; font-size:14px; display:flex; align-items:center; gap:12px; flex-wrap:wrap;">
            <span>⚠️ <strong>Blacklist is not enabled</strong> — Words on this list will still be shown.</span>
            <button onclick="toggleSetting('profanity_filter', true)" style="background:#4CAF50; color:white; border:none; padding:6px 14px; border-radius:4px; cursor:pointer; font-size:13px; white-space:nowrap;">✓ Enable Profanity Filter</button>
        </div>
        {% else %}
        <div style="background:#e8f5e9; border:1px solid #a5d6a7; color:#2e7d32; padding:10px 14px; border-radius:5px; margin:10px 0; font-size:14px; display:flex; align-items:center; gap:12px; flex-wrap:wrap;">
            <span>✅ <strong>Profanity Filter is enabled</strong></span>
            <button onclick="toggleSetting('profanity_filter', false)" style="background:#f44336; color:white; border:none; padding:6px 14px; border-radius:4px; cursor:pointer; font-size:13px; white-space:nowrap;">✗ Disable Profanity Filter</button>
        </div>
        {% endif %}
        <button onclick="location.href='/'">← Back to Config</button>

        <script>
        function toggleSetting(key, value) {
            fetch('/api/config', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({[key]: value})
            }).then(() => location.reload());
        }
        </script>

        <h3>Add a Word</h3>
        <div class="add-row">
            <input type="text" id="add_word" placeholder="Type a word to block and press Enter..." onkeydown="if(event.key==='Enter') addWord()">
            <button class="add-btn" onclick="addWord()">+ Add</button>
        </div>
        <div id="add_result"></div>

        <h3>Search / Browse</h3>
        <div class="search-row">
            <input type="text" id="search" placeholder="Search words..." oninput="renderTable()">
        </div>
        <div id="hint" class="hint"></div>
        <div id="list_area"></div>
        <div id="scroll_sentinel" style="height:1px;"></div>

        <script>
            var allWords = [];

            function loadBlacklist() {
                document.getElementById('count').textContent = 'Loading...';
                fetch('/api/blacklist')
                .then(r => r.json())
                .then(data => {
                    allWords = data.blacklist || [];
                    document.getElementById('count').textContent = allWords.length.toLocaleString() + ' blocked words';
                    renderTable();
                    window.scrollTo(0, 0);
                })
                .catch(() => {
                    document.getElementById('count').textContent = 'Error loading';
                });
            }

            var visibleCount = 100;
            var currentFiltered = [];
            var PAGE_SIZE = 100;
            var observer = null;

            function setupSentinel() {
                if (observer) observer.disconnect();
                observer = new IntersectionObserver(function(entries) {
                    if (entries[0].isIntersecting) appendRows();
                }, { rootMargin: '400px' });
                observer.observe(document.getElementById('scroll_sentinel'));
            }

            function renderTable() {
                const query = document.getElementById('search').value.trim().toLowerCase();
                const area = document.getElementById('list_area');
                const hint = document.getElementById('hint');

                visibleCount = PAGE_SIZE;

                if (allWords.length === 0) {
                    hint.textContent = '';
                    area.innerHTML = '<div class="empty"><h3>No words in blacklist yet</h3><p>Add words above to block them.</p></div>';
                    return;
                }

                currentFiltered = query
                    ? allWords.filter(w => w.toLowerCase().includes(query))
                    : allWords;

                if (currentFiltered.length === 0) {
                    hint.textContent = 'No words match "' + query + '"';
                    area.innerHTML = '<div class="empty"><p>No words match your search.</p></div>';
                    return;
                }

                updateHint();
                const showing = currentFiltered.slice(0, visibleCount);
                area.innerHTML = '<table id="words_table"><tr><th>Word</th><th></th><th>Word</th><th></th></tr>' + buildRows(showing) + '</table>';
                setupSentinel();
            }

            function appendRows() {
                if (visibleCount >= currentFiltered.length) return;
                visibleCount = Math.min(visibleCount + PAGE_SIZE, currentFiltered.length);
                updateHint();
                const showing = currentFiltered.slice(0, visibleCount);
                const area = document.getElementById('list_area');
                area.innerHTML = '<table id="words_table"><tr><th>Word</th><th></th><th>Word</th><th></th></tr>' + buildRows(showing) + '</table>';
            }

            function buildRows(items) {
                let rows = '';
                for (let i = 0; i < items.length; i += 2) {
                    const a = items[i];
                    const b = items[i + 1];
                    const ae = a.replace(/'/g, "&#39;");
                    const be = b ? b.replace(/'/g, "&#39;") : '';
                    rows += `<tr>` +
                        `<td>${a}</td>` +
                        `<td><button class="remove-btn" onclick="removeWord('${ae}')">✕ Remove</button></td>` +
                        (b
                            ? `<td>${b}</td><td><button class="remove-btn" onclick="removeWord('${be}')">✕ Remove</button></td>`
                            : `<td></td><td></td>`) +
                        `</tr>`;
                }
                return rows;
            }

            function updateHint() {
                const hint = document.getElementById('hint');
                const query = document.getElementById('search').value.trim();
                const showing = Math.min(visibleCount, currentFiltered.length);
                if (query) {
                    hint.textContent = 'Showing ' + showing + ' of ' + currentFiltered.length + ' matches';
                } else {
                    hint.textContent = 'Showing ' + showing + ' of ' + allWords.length.toLocaleString() + ' words' +
                        (showing < allWords.length ? ' — scroll down to load more' : '');
                }
            }

            function addWord() {
                const input = document.getElementById('add_word');
                const word = input.value.trim();
                const result = document.getElementById('add_result');
                if (!word) return;
                fetch('/api/blacklist/add', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({word: word})
                })
                .then(r => r.json())
                .then(data => {
                    if (data.success) {
                        result.innerHTML = '<p class="success">✅ Added: ' + word + '</p>';
                        input.value = '';
                        loadBlacklist();
                    } else {
                        result.innerHTML = '<p class="error">❌ ' + data.error + '</p>';
                    }
                    setTimeout(() => result.innerHTML = '', 3000);
                });
            }

            function removeWord(word) {
                if (!confirm('Remove "' + word + '" from the blacklist?')) return;
                fetch('/api/blacklist/remove', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({word: word})
                })
                .then(r => r.json())
                .then(() => loadBlacklist());
            }

            loadBlacklist();
        </script>
    </body>
    </html>
    """
    return render_template_string(html, config=config)

@app.route('/blocklist')
def view_blocklist():
    blocklist = load_blocklist()
    html = """
    <!DOCTYPE html>
    <html>
    <head>
        <title>Blocked Phone Numbers</title>
        <style>
            body { font-family: Arial, sans-serif; margin: 0; padding: 20px; background: #ffffff; color: #333; }
            h1 { color: #f44336; }
            table { width: 100%; border-collapse: collapse; margin: 20px 0; }
            th, td { border: 1px solid #ddd; padding: 10px; text-align: left; }
            th { background: #f44336; color: white; }
            tr:nth-child(even) { background: #f5f5f5; }
            button { background: #4CAF50; color: white; padding: 10px 20px; border: none; border-radius: 4px; cursor: pointer; margin: 10px 5px 10px 0; }
            .unblock-btn { background: #4CAF50; padding: 5px 10px; font-size: 12px; }
            .info { background: #ffebee; padding: 10px; border-radius: 5px; margin: 10px 0; font-size: 14px; border: 1px solid #ffcdd2; color: #333; }
            .no-blocked { background: #f5f5f5; padding: 40px; text-align: center; border-radius: 5px; margin: 20px 0; }
        </style>
    </head>
    <body><script>if('scrollRestoration'in history)history.scrollRestoration='manual';function _toTop(){window.scrollTo(0,0);document.documentElement.scrollTop=0;document.body.scrollTop=0;try{window.parent.postMessage({type:'scrollTop'},'*');}catch(e){}}_toTop();document.addEventListener('DOMContentLoaded',_toTop);window.addEventListener('load',_toTop);</script>
        <h1>🚫 Blocked Phone Numbers</h1>
        <div class="info">
            ℹ️ Blocked numbers cannot send messages | Total Blocked: {{ blocklist|length }}
        </div>
        <button onclick="location.href='/'">← Back to Config</button>
        <button onclick="location.href='/messages'">📋 View Messages</button>
        
        {% if blocklist|length == 0 %}
        <div class="no-blocked">
            <h2>No blocked numbers</h2>
            <p>Block numbers from the Messages page.</p>
        </div>
        {% else %}
        <table>
            <tr>
                <th>Phone Number</th>
                <th>Action</th>
            </tr>
            {% for phone in blocklist %}
            <tr>
                <td>{{ phone }}</td>
                <td><button class="unblock-btn" onclick="unblockPhone('{{ phone }}')">✅ Unblock</button></td>
            </tr>
            {% endfor %}
        </table>
        {% endif %}
        
        <script>
            function unblockPhone(phone) {
                if (confirm('Unblock ' + phone + '?')) {
                    fetch('/api/phone/unblock', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({phone: phone})
                    })
                    .then(r => r.json())
                    .then(data => {
                        if (data.success) {
                            alert('✅ Phone number unblocked!');
                            location.reload();
                        }
                    });
                }
            }
        </script>
    </body>
    </html>
    """
    return render_template_string(html, blocklist=blocklist)

@app.route('/status')
def status_page():
    status_html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>Plugin Status</title>
        <style>
            body {{ font-family: monospace; background: #ffffff; color: #333; padding: 20px; }}
            .section {{ background: #f5f5f5; padding: 15px; margin: 15px 0; border: 1px solid #ddd; }}
            .ok {{ color: #4CAF50; }}
            .error {{ color: #f44336; }}
            button {{ background: #4CAF50; color: white; padding: 10px; border: none; cursor: pointer; margin: 5px; }}
        </style>
    </head>
    <body><script>if('scrollRestoration'in history)history.scrollRestoration='manual';function _toTop(){{window.scrollTo(0,0);document.documentElement.scrollTop=0;document.body.scrollTop=0;try{{window.parent.postMessage({{type:'scrollTop'}},'*');}}catch(e){{}}}}_toTop();document.addEventListener('DOMContentLoaded',_toTop);window.addEventListener('load',_toTop);</script>
        <h1>🔧 FPP SMS Plugin Status v2.5</h1>
        <button onclick="location.href='/'">← Back</button>
        <button onclick="location.reload()">🔄 Refresh</button>
        
        <div class="section">
            <h2>Plugin State</h2>
            <p>Enabled: <span class="{'ok' if config.get('enabled') else 'error'}">{config.get('enabled')}</span></p>
            <p>Display Worker: <span class="{'ok' if display_thread and display_thread.is_alive() else 'error'}">{display_thread and display_thread.is_alive()}</span></p>
            <p>Polling Worker: <span class="{'ok' if polling_thread and polling_thread.is_alive() else 'error'}">{polling_thread and polling_thread.is_alive()}</span></p>
        </div>
        
        <div class="section">
            <h2>Queue Status</h2>
            <p>Currently Displaying: {currently_displaying.get('name') if currently_displaying else 'Nothing'}</p>
            <p>Queue Length: {len(message_queue)}</p>
        </div>
    </body>
    </html>
    """
    return status_html

@app.route('/messages')
def view_messages():
    today = datetime.now().date()
    tabs = []
    for i in range(7):
        d = today - timedelta(days=i)
        if i == 0:
            label = f"Today ({d.strftime('%b %-d')})"
        else:
            label = d.strftime("%a %b %-d")
        tabs.append({"date": d.isoformat(), "label": label, "is_today": (i == 0)})
    try:
        with open(get_day_log_path(today), 'r') as f:
            today_messages = list(reversed(json.load(f)))
    except (FileNotFoundError, json.JSONDecodeError):
        today_messages = []

    html = """
    <!DOCTYPE html>
    <html>
    <head>
        <title>Message History & Queue</title>
        <style>
            body { font-family: Arial, sans-serif; margin: 0; padding: 20px; background: #ffffff; color: #333; }
            h1 { color: #4CAF50; }
            table { width: 100%; border-collapse: collapse; margin: 20px 0; }
            th, td { border: 1px solid #ddd; padding: 10px; text-align: left; }
            th { background: #4CAF50; color: white; }
            tr:nth-child(even) { background: #f5f5f5; }
            .displaying { background: #4CAF50 !important; color: white; font-weight: bold; }
            .queued { color: #e65100; }
            .displayed { color: #4CAF50; }
            button { background: #4CAF50; color: white; padding: 10px 20px; border: none; border-radius: 4px; cursor: pointer; margin: 10px 5px 10px 0; }
            .block-btn { background: #f44336; padding: 5px 10px; font-size: 12px; }
            .clear-btn { background: #f44336; }
            .info { background: #e3f2fd; padding: 10px; border-radius: 5px; margin: 10px 0; font-size: 14px; border: 1px solid #90caf9; color: #333; }
            .queue-box { background: #f3e5f5; padding: 20px; border-radius: 5px; margin: 20px 0; border: 1px solid #ce93d8; color: #333; }
            .current-display { background: #4CAF50; padding: 15px; border-radius: 5px; margin: 10px 0; font-size: 18px; font-weight: bold; color: white; }
            .queue-item { background: #f9f9f9; padding: 10px; border-radius: 5px; margin: 5px 0; border-left: 4px solid #FF9800; }
            .tab-bar { display: flex; flex-wrap: wrap; gap: 4px; margin: 16px 0 0; border-bottom: 2px solid #4CAF50; }
            .tab-btn { padding: 8px 14px; border: 1px solid #ddd; border-bottom: none; background: #f5f5f5; cursor: pointer; border-radius: 4px 4px 0 0; font-size: 13px; color: #555; }
            .tab-btn:hover { background: #e8f5e9; }
            .tab-btn.active { background: #4CAF50; color: white; border-color: #4CAF50; font-weight: bold; }
            .tab-panel { display: none; padding-top: 16px; }
            .tab-panel.active { display: block; }
            .history-note { background: #fff9c4; padding: 8px 12px; border-radius: 4px; font-size: 13px; color: #5d4037; margin-bottom: 12px; border: 1px solid #f9a825; }
        </style>
    </head>
    <body><script>if('scrollRestoration'in history)history.scrollRestoration='manual';function _toTop(){window.scrollTo(0,0);document.documentElement.scrollTop=0;document.body.scrollTop=0;try{window.parent.postMessage({type:'scrollTop'},'*');}catch(e){}}_toTop();document.addEventListener('DOMContentLoaded',_toTop);window.addEventListener('load',_toTop);</script>
        <h1>Message History & Queue</h1>
        <button onclick="location.href='/'">Back to Config</button>
        <button class="clear-btn" onclick="clearHistory()">Clear Today's Messages</button>

        <div class="tab-bar">
            {% for tab in tabs %}
            <button class="tab-btn {% if tab.is_today %}active{% endif %}"
                    id="tab-btn-{{ tab.date }}"
                    onclick="switchTab('{{ tab.date }}', {{ tab.is_today | tojson }})">{{ tab.label }}</button>
            {% endfor %}
        </div>

        <!-- TODAY TAB -->
        <div class="tab-panel active" id="panel-{{ tabs[0].date }}">
            <div class="info">Auto-refreshes every 5 seconds | Messages today: <span id="msg-count">{{ today_messages | length }}</span></div>
            <div class="queue-box">
                <h2>Current Display Queue</h2>
                <div id="queue-box-content"><p style="color:#aaa;">Loading...</p></div>
            </div>
            <h2>Today's Messages</h2>
            <div id="today-messages-content"><p style="color:#aaa;">Loading...</p></div>
        </div>

        <!-- PAST DAY TAB PANELS -->
        {% for tab in tabs[1:] %}
        <div class="tab-panel" id="panel-{{ tab.date }}">
            <div class="history-note">Past day snapshot — no live queue.</div>
            <div id="history-content-{{ tab.date }}"><p style="color:#aaa;">Click tab to load.</p></div>
        </div>
        {% endfor %}

        <!-- Block modal -->
        <div id="block-modal" style="display:none; position:fixed; top:0; left:0; width:100%; height:100%; background:rgba(0,0,0,0.5); z-index:1000; align-items:center; justify-content:center;">
            <div style="background:#fff; border-radius:8px; padding:28px; max-width:420px; width:90%; box-shadow:0 4px 20px rgba(0,0,0,0.3);">
                <h3 style="margin-top:0; color:#333;">Block Action</h3>
                <p style="color:#555; margin-bottom:6px;">Phone: <strong id="modal-phone"></strong></p>
                <p style="color:#555; margin-bottom:20px;">Name: <strong id="modal-name-text"></strong></p>
                <p style="color:#333; font-weight:bold; margin-bottom:16px;">What would you like to block?</p>
                <div style="display:flex; flex-direction:column; gap:10px;">
                    <button style="background:#f44336; color:white; padding:12px; border:none; border-radius:5px; cursor:pointer;"
                            onclick="blockPhone(document.getElementById('block-modal').dataset.phone)">Block this number from texting again</button>
                    <button id="modal-block-name-btn" style="background:#FF9800; color:white; padding:12px; border:none; border-radius:5px; cursor:pointer;"
                            onclick="blockNameFromDisplay()">Block this name from being displayed</button>
                    <p id="whitelist-warning" style="color:#f44336; font-size:12px; margin:0; padding:4px 0; display:none;">
                        Whitelist is not enabled — this name may appear again
                    </p>
                    <button style="background:#aaa; color:white; padding:10px; border:none; border-radius:5px; cursor:pointer;"
                            onclick="closeBlockModal()">Cancel</button>
                </div>
            </div>
        </div>

        <script>
            var useWhitelist = {{ config.get('use_whitelist', False) | tojson }};
            var modalOpen = false;
            var refreshTimer = null;
            var prevQueueJson = null;
            var prevTodayJson = null;
            var loadedTabs = {};

            function esc(s) {
                return String(s || '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
            }

            function fmtTime(ts) {
                if (!ts) return '';
                try {
                    var d = new Date(ts);
                    var mo = String(d.getMonth()+1).padStart(2,'0');
                    var dy = String(d.getDate()).padStart(2,'0');
                    var yr = d.getFullYear();
                    var hr = d.getHours();
                    var mn = String(d.getMinutes()).padStart(2,'0');
                    var sc = String(d.getSeconds()).padStart(2,'0');
                    var ampm = hr >= 12 ? 'PM' : 'AM';
                    hr = hr % 12 || 12;
                    return mo+'-'+dy+'-'+yr+' '+hr+':'+mn+':'+sc+' '+ampm;
                } catch(e) { return ts; }
            }

            function switchTab(date, isToday) {
                document.querySelectorAll('.tab-btn').forEach(function(b) { b.classList.remove('active'); });
                document.querySelectorAll('.tab-panel').forEach(function(p) { p.classList.remove('active'); });
                document.getElementById('tab-btn-' + date).classList.add('active');
                document.getElementById('panel-' + date).classList.add('active');
                if (isToday) {
                    scheduleRefresh();
                } else {
                    clearTimeout(refreshTimer);
                    if (!loadedTabs[date]) { loadedTabs[date] = true; loadHistoryTab(date); }
                }
                // Class change doesn't trigger MutationObserver — report height explicitly
                requestAnimationFrame(function() {
                    window.parent.postMessage({ type: 'iframeHeight', height: document.body.scrollHeight }, '*');
                });
            }

            function loadHistoryTab(date) {
                var container = document.getElementById('history-content-' + date);
                container.innerHTML = '<p style="color:#aaa;">Loading...</p>';
                fetch('/api/messages/' + date)
                    .then(function(r) { return r.json(); })
                    .then(function(msgs) { container.innerHTML = renderTable(msgs, false); })
                    .catch(function(e) { container.innerHTML = '<p style="color:#f44336;">Failed to load.</p>'; });
            }

            function scheduleRefresh() {
                clearTimeout(refreshTimer);
                refreshTimer = setTimeout(function() {
                    if (!modalOpen) refreshData(); else scheduleRefresh();
                }, 5000);
            }

            function refreshData() {
                Promise.all([
                    fetch('/api/queue/status').then(function(r) { return r.json(); }),
                    fetch('/api/messages').then(function(r) { return r.json(); })
                ]).then(function(results) {
                    renderQueue(results[0]);
                    renderTodayMessages(results[1]);
                    scheduleRefresh();
                }).catch(scheduleRefresh);
            }

            function renderQueue(status) {
                var json = JSON.stringify(status);
                if (json === prevQueueJson) return;
                prevQueueJson = json;
                var html = '';
                if (status.currently_displaying) {
                    html += '<div class="current-display">NOW DISPLAYING: ' + esc(status.currently_displaying.name) +
                            ' (from ***' + esc(status.currently_displaying.phone_last4) + ')</div>';
                } else {
                    html += '<div class="current-display" style="background:#bdbdbd;color:#333;">Nothing currently displaying</div>';
                }
                if (status.queue_length > 0) {
                    html += '<h3 style="color:#FF9800;margin-top:20px;">Queue (' + status.queue_length + ' waiting):</h3>';
                    status.queue.forEach(function(item, i) {
                        html += '<div class="queue-item"><strong>Queue Position ' + (i+1) + ':</strong> ' +
                                esc(item.name) + ' (from ***' + esc(item.phone_last4) + ')</div>';
                    });
                } else {
                    html += '<p style="color:#aaa;font-style:italic;margin-top:15px;">Queue is empty</p>';
                }
                document.getElementById('queue-box-content').innerHTML = html;
            }

            function renderTodayMessages(messages) {
                var json = JSON.stringify(messages);
                if (json === prevTodayJson) return;
                prevTodayJson = json;
                document.getElementById('msg-count').textContent = messages.length;
                document.getElementById('today-messages-content').innerHTML = renderTable(messages, true);
            }

            function renderTable(messages, showBlock) {
                if (!messages || messages.length === 0) {
                    return '<div style="background:#f5f5f5;padding:40px;text-align:center;border-radius:5px;"><h3>No messages</h3></div>';
                }
                var statusLabel = {'displaying':'DISPLAYING NOW','queued':'Queued','displayed':'Displayed'};
                var rows = messages.map(function(msg) {
                    var label = statusLabel[msg.status] || esc(msg.status);
                    var btn = '';
                    if (showBlock && msg.phone_full !== 'Local Testing') {
                        btn = '<button class="block-btn" data-phone="' + esc(msg.phone_full) + '" data-name="' + esc(msg.extracted_name) +
                              '" onclick="showBlockModal(this.dataset.phone,this.dataset.name)">Block</button>';
                    }
                    return '<tr class="' + esc(msg.status) + '">' +
                        '<td>' + fmtTime(msg.timestamp) + '</td>' +
                        '<td>' + esc(msg.phone) + '</td>' +
                        '<td>' + esc(msg.message) + '</td>' +
                        '<td>' + esc(msg.extracted_name) + '</td>' +
                        '<td class="' + esc(msg.status) + '">' + label + '</td>' +
                        '<td>' + btn + '</td></tr>';
                }).join('');
                return '<table><tr><th>Timestamp</th><th>Phone</th><th>Message</th><th>Name</th><th>Status</th><th>Action</th></tr>' + rows + '</table>';
            }

            function showBlockModal(phone, name) {
                modalOpen = true;
                document.getElementById('modal-phone').textContent = phone;
                document.getElementById('modal-name-text').textContent = name || '(no name)';
                document.getElementById('modal-block-name-btn').disabled = !name;
                document.getElementById('modal-block-name-btn').style.opacity = name ? '1' : '0.4';
                document.getElementById('whitelist-warning').style.display = useWhitelist ? 'none' : 'block';
                document.getElementById('block-modal').dataset.phone = phone;
                document.getElementById('block-modal').dataset.name = name || '';
                document.getElementById('block-modal').style.display = 'flex';
            }

            function closeBlockModal() {
                modalOpen = false;
                document.getElementById('block-modal').style.display = 'none';
                scheduleRefresh();
            }

            function blockPhone(phone) {
                closeBlockModal();
                fetch('/api/phone/block', { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({phone:phone}) })
                    .then(function(r) { return r.json(); })
                    .then(function(data) { if (data.success) alert('Phone number blocked!'); refreshData(); });
            }

            function blockNameFromDisplay() {
                var modal = document.getElementById('block-modal');
                var name = modal.dataset.name;
                closeBlockModal();
                fetch('/api/whitelist/remove', { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({name:name}) })
                    .then(function(r) { return r.json(); })
                    .then(function(data) {
                        alert(data.success ? '"' + name + '" blocked from display!' : 'Error: ' + data.error);
                        refreshData();
                    });
            }

            function clearHistory() {
                if (confirm("Clear all of today's messages?")) {
                    fetch('/api/messages/clear', { method:'POST' })
                        .then(function(r) { return r.json(); })
                        .then(function(data) { if (data.success) alert("Today's messages cleared!"); refreshData(); });
                }
            }

            refreshData();
        </script>
    </body>
    </html>
    """
    return render_template_string(html, config=config, tabs=tabs, today_messages=today_messages)


@app.route('/api/activate', methods=['GET', 'POST'])
def api_activate():
    """FPP scheduler hook: enable the plugin, start SMS polling, and start the waiting playlist."""
    global polling_thread, stop_polling

    # Require a default waiting playlist — without one the show has no defined state
    if not config.get('default_playlist', '').strip():
        msg = "ERROR: No Default Waiting Playlist configured. Set one in the plugin settings before running TwilioStart."
        logging.error(msg)
        return jsonify({"success": False, "error": msg}), 400

    config['enabled'] = True
    stop_polling = False
    save_config()

    # Start polling thread if not already running
    if twilio_client:
        if not polling_thread or not polling_thread.is_alive():
            polling_thread = threading.Thread(target=poll_twilio, daemon=True)
            polling_thread.start()
            logging.info("▶️  Activate: SMS polling started")
    else:
        logging.warning("⚠️  Activate: Twilio credentials not configured, polling not started")

    # Start the default waiting playlist
    result = start_default_playlist()

    logging.info(f"✅ TwilioStart activated — playlist {'started' if result else 'FAILED to start'}")
    return jsonify({"success": True, "playlist_started": result,
                    "message": "Twilio SMS plugin activated"})


@app.route('/api/deactivate', methods=['GET', 'POST'])
def api_deactivate():
    """FPP scheduler hook: disable plugin and stop the current playlist/sequence.
    Polling thread keeps running in standby to send show_not_live replies."""
    config['enabled'] = False
    save_config()

    # Stop the current sequence/playlist and any background FSEQ effect
    try:
        import urllib.parse

        default = config.get('default_playlist', '')
        if default.startswith('seq:'):
            # FSEQ Effect Stop also uses display name without .fseq
            seq_name = default[4:].removesuffix('.fseq')
            effect_stop_url = f"{FPP_HOST}/api/command/{urllib.parse.quote('FSEQ Effect Stop')}/{urllib.parse.quote(seq_name)}"
            r = requests.get(effect_stop_url, timeout=3)
            logging.info(f"🛑 FSEQ Effect Stop: {r.status_code} - {r.text}")

        # Stop Now catches playlists, videos, and foreground sequences
        command_url = f"{FPP_HOST}/api/command/{urllib.parse.quote('Stop Now')}"
        r2 = requests.get(command_url, timeout=3)
        logging.info(f"🛑 Stop Now: {r2.status_code} - {r2.text}")

        # Image content renders via overlay model (State 2 Opaque) — must clear explicitly
        overlay_model = config.get('overlay_model_name', '')
        if default.startswith('img:') and overlay_model:
            encoded = urllib.parse.quote(overlay_model)
            state_url = f"{FPP_HOST}/api/overlays/model/{encoded}/state"
            requests.put(state_url, json={"State": 0}, timeout=3)
            logging.info(f"🛑 Overlay cleared (img content stopped)")
    except Exception as e:
        logging.warning(f"Could not stop FPP playback: {e}")

    logging.info("🛑 TwilioStop: disabled, polling stopped, playlist stopped")
    return jsonify({"success": True, "message": "Twilio SMS plugin deactivated"})


if __name__ == '__main__':
    # Migrate files from old scattered paths to the new plugin data directory
    _migrations = [
        ("/home/fpp/media/config/plugin.fpp-sms-twilio.json", CONFIG_FILE),
        ("/home/fpp/media/config/blocked_phones.json",         BLOCKLIST_FILE),
        ("/home/fpp/media/config/last_message_sid.txt",        LAST_SID_FILE),
        ("/home/fpp/media/config/queue_pending.json",          QUEUE_FILE),
    ]
    for _old, _new in _migrations:
        if not os.path.exists(_new) and os.path.exists(_old):
            try:
                import shutil
                shutil.copy2(_old, _new)
                logging.error(f"Migrated {_old} → {_new}")
            except Exception as _e:
                logging.error(f"Migration failed {_old}: {_e}")

    load_config()

    # Clean up log files older than 7 days
    cleanup_old_logs()

    # Restore any pending queue items saved before the last shutdown
    load_queue_from_file()

    # Pre-warm caches in background so first test/message isn't slow
    def _warm_caches():
        try:
            load_blacklist()
            load_whitelist()
            logging.info("Cache pre-warm complete")
        except Exception as e:
            logging.warning(f"Cache pre-warm failed: {e}")
    threading.Thread(target=_warm_caches, daemon=True).start()

    # Display worker always runs so Testing Tools work without Twilio credentials
    display_thread = threading.Thread(target=display_worker, daemon=True)
    display_thread.start()

    # Polling thread starts if Twilio is configured — runs in standby (show_not_live
    # replies) when disabled, and processes names normally when enabled
    if twilio_client:
        polling_thread = threading.Thread(target=poll_twilio, daemon=True)
        polling_thread.start()

    # Start the default waiting playlist on launch if the plugin is already enabled
    if config['enabled']:
        def _start_default():
            import time
            time.sleep(3)  # brief delay to let FPP settle before sending commands
            start_default_playlist()
        threading.Thread(target=_start_default, daemon=True).start()

    logging.info("FPP SMS Plugin v2.5 starting...")
    app.run(host='0.0.0.0', port=5000, debug=False, threaded=True)
