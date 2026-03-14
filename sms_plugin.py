#!/usr/bin/env python3
"""
FPP SMS Plugin v2.5 - Twilio Integration
Features:
- Message queueing
- Display duration controls how long each message shows
- Shows queue status: "Displaying Now", "Queue 1", "Queue 2"
- Profanity filtering with whole-word matching
- Optional whitelist for approved names
- Name validation (1 or 2 words, hyphens OK)
- FPP playlist switching and text overlay
- UI dropdowns to select playlists, models, and fonts from FPP
- Multi-line message templates with proper case conversion
- OPTIMIZED: Cached list loading for blacklist, whitelist, and blocked phones
"""

from flask import Flask, request, jsonify, render_template_string
import logging
import json
import requests
from datetime import datetime, timedelta
import re
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from twilio.rest import Client
from collections import deque
import os

# PIL/Pillow for pixel-accurate text rendering (optional — falls back to FPP text API if unavailable)
try:
    from PIL import Image, ImageDraw, ImageFont
    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False

# Configuration
PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = "/home/fpp/media/config/plugin.fpp-sms-twilio.json"
LOG_FILE = "/home/fpp/media/logs/sms_plugin.log"
MESSAGE_LOG = "/home/fpp/media/logs/received_messages.json"
BLACKLIST_FILE = os.path.join(PLUGIN_DIR, "blacklist.txt")
BLACKLIST_REMOVED_FILE = os.path.join(PLUGIN_DIR, "blacklist_removed.txt")
BLACKLIST_ADDED_FILE = os.path.join(PLUGIN_DIR, "blacklist_added.txt")
WHITELIST_FILE = os.path.join(PLUGIN_DIR, "whitelist.txt")
WHITELIST_REMOVED_FILE = os.path.join(PLUGIN_DIR, "whitelist_removed.txt")
WHITELIST_ADDED_FILE = os.path.join(PLUGIN_DIR, "whitelist_added.txt")
LAST_SID_FILE = "/home/fpp/media/config/last_message_sid.txt"
BLOCKLIST_FILE = "/home/fpp/media/config/blocked_phones.json"

# Setup logging — ensure the log directory exists, then write to file + stderr
os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
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
    "text_v_align": "center",
    "message_template": "Merry Christmas {name}!",
    "scroll_speed": 5,
    "overlay_model_width": 0,
    "overlay_model_height": 0,
    "text_x": -1,  # -1 = auto-center; >= 0 = pixel X in model space (PIL mode only)
    "text_y": -1,  # -1 = auto-center; >= 0 = pixel Y in model space (PIL mode only)
    "sms_response_show_not_live": False,
    "sms_response_success": False,
    "sms_response_profanity": False,
    "sms_response_rate_limited": False,
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
    search_dirs = [
        '/usr/share/fonts/truetype/freefont',
        '/usr/share/fonts/truetype',
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


def render_to_shm(display_message, model_name, width, height, x, y, font_name, font_size, color_hex):
    """Render text to FPP shared memory at a pixel-accurate position using Pillow.
    x/y < 0 means auto-center. Returns True on success, False on failure."""
    if not PIL_AVAILABLE or width <= 0 or height <= 0:
        return False
    try:
        hex_str = color_hex.lstrip('#')
        color = (int(hex_str[0:2], 16), int(hex_str[2:4], 16), int(hex_str[4:6], 16))

        img = Image.new('RGB', (width, height), (0, 0, 0))
        draw = ImageDraw.Draw(img)
        font = _find_font(font_name, font_size)

        # Strip newline padding (used only by the old valign trick — PIL uses x/y directly)
        text = display_message.strip('\n')

        # Measure text for auto-centering
        if font is not None:
            bbox = draw.textbbox((0, 0), text, font=font)
            text_w, text_h = bbox[2] - bbox[0], bbox[3] - bbox[1]
        else:
            text_w = len(text) * 6
            text_h = font_size

        draw_x = max(0, (width - text_w) // 2) if x < 0 else x
        draw_y = max(0, (height - text_h) // 2) if y < 0 else y

        draw.text((draw_x, draw_y), text, fill=color, font=font)

        shm_path = f"/dev/shm/FPP-Model-Data-{model_name}"
        raw = img.tobytes()
        expected = width * height * 3
        if len(raw) != expected:
            logging.error(f"render_to_shm: size mismatch ({len(raw)} != {expected})")
            return False
        with open(shm_path, 'r+b') as f:
            f.write(raw)
        logging.info(f"render_to_shm: wrote {len(raw)} bytes to {shm_path} at ({draw_x},{draw_y})")
        return True
    except Exception as e:
        logging.error(f"render_to_shm failed: {e}")
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

def get_message_count(phone):
    """Get number of messages from a phone number today"""
    try:
        with open(MESSAGE_LOG, 'r') as f:
            logs = json.load(f)
        
        today = datetime.now().date()
        
        count = 0
        for log in logs:
            if log.get('phone_full') == phone:
                try:
                    msg_time = datetime.fromisoformat(log.get('timestamp'))
                    if msg_time.date() == today:
                        count += 1
                except:
                    pass
        
        return count
    except:
        return 0

def has_sent_name_today(phone, name):
    """Check if this phone has already sent this specific name today"""
    try:
        with open(MESSAGE_LOG, 'r') as f:
            logs = json.load(f)
        
        today = datetime.now().date()
        
        for log in logs:
            if log.get('phone_full') == phone and log.get('extracted_name', '').lower() == name.lower():
                status = log.get('status', '')
                if status in ['displayed', 'displaying', 'queued']:
                    try:
                        msg_time = datetime.fromisoformat(log.get('timestamp'))
                        if msg_time.date() == today:
                            return True
                    except:
                        pass
        
        return False
    except:
        return False

def save_last_sid(sid):
    """Save the last processed message SID to file"""
    try:
        with open(LAST_SID_FILE, 'w') as f:
            f.write(sid)
    except Exception as e:
        logging.error(f"Error saving last SID: {e}")

def log_message(phone, message, name, status):
    """Log received message"""
    try:
        try:
            with open(MESSAGE_LOG, 'r') as f:
                logs = json.load(f)
        except:
            logs = []
        
        logs.append({
            "timestamp": datetime.now().isoformat(),
            "phone": phone,
            "phone_full": phone,
            "message": message,
            "extracted_name": name,
            "status": status
        })
        
        logs = logs[-100:]
        
        with open(MESSAGE_LOG, 'w') as f:
            json.dump(logs, f, indent=2)
        
        logging.info(f"✅ Message logged: {phone[-4:]} | {name} | {status}")
    except Exception as e:
        logging.error(f"Error logging message: {e}")

def update_message_status(phone, name, new_status):
    """Update the status of a message in the log"""
    try:
        with open(MESSAGE_LOG, 'r') as f:
            logs = json.load(f)
        
        for log in reversed(logs):
            if log.get('phone_full') == phone and log.get('extracted_name') == name:
                log['status'] = new_status
                log['status_updated'] = datetime.now().isoformat()
                break
        
        with open(MESSAGE_LOG, 'w') as f:
            json.dump(logs, f, indent=2)
        
        logging.info(f"Updated status: {phone[-4:]} | {name} | {new_status}")
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
        
        # Get message template and replace {name} with actual name
        message_template = config.get('message_template', 'Merry Christmas {name}!')
        display_message = message_template.replace('{name}', name)

        # Vertical alignment: pad with newlines to shift text on the matrix.
        # FPP "Center" centers the full text block vertically, so:
        #   top    → append trailing newlines (block grows down → text rises)
        #   bottom → prepend leading newlines (block grows up → text sinks)
        #   center → no change (FPP centers naturally)
        v_align = config.get('text_v_align', 'center')
        model_height = config.get('overlay_model_height', 0)
        font_size = config.get('text_font_size', 48)
        if v_align != 'center' and model_height > font_size:
            pad = max(0, (model_height // font_size) - 1)
            if pad > 0:
                if v_align == 'top':
                    display_message = display_message + '\n' * pad
                elif v_align == 'bottom':
                    display_message = '\n' * pad + display_message
        
        logging.info(f"🎄 ========== STARTING DISPLAY FOR: {name} ==========")
        logging.info(f"📺 FPP Host: {fpp_host}")
        logging.info(f"🎬 Name Display Playlist: {name_playlist}")
        logging.info(f"📝 Overlay Model: {overlay_model}")
        logging.info(f"📝 Message Template: {message_template}")
        logging.info(f"📝 Display Message: {display_message}")
        
        # Step 1: Start the name display playlist/sequence (background)
        if name_playlist:
            try:
                logging.info(f"⏸️  STEP 1: Stopping any running playlist...")
                requests.get(f"{fpp_host}/api/playlists/stop", timeout=3)
                # Note: background FSEQ Effect is NOT stopped here — the names sequence
                # (foreground) will automatically suppress it and it auto-resumes after.
                time.sleep(0.1)

                logging.info(f"▶️  STEP 2: Starting name display playlist: {name_playlist}")

                import urllib.parse
                if name_playlist.startswith('seq:'):
                    # FSEQ Effect (loop=true, background=true): plays as background so
                    # overlay model renders on top with correct text colors.
                    seq_name = name_playlist[4:].removesuffix('.fseq')
                    effect_url = f"{fpp_host}/api/command/{urllib.parse.quote('FSEQ Effect Start')}/{urllib.parse.quote(seq_name)}/true/true"
                    start_response = requests.get(effect_url, timeout=3)
                    logging.info(f"   FSEQ Effect Start (names): {start_response.status_code} - {start_response.text}")
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

                # PIL shm rendering for static (Center) mode — gives pixel-accurate x/y positioning.
                # Scroll modes use the FPP text API (PIL can't animate scrolling).
                shm_rendered = False
                if text_position == 'Center' and PIL_AVAILABLE:
                    tx = config.get('text_x', -1)
                    ty = config.get('text_y', -1)
                    mw = config.get('overlay_model_width', 0)
                    mh = config.get('overlay_model_height', 0)
                    if mw > 0 and mh > 0:
                        shm_rendered = render_to_shm(
                            display_message, overlay_model,
                            mw, mh, tx, ty,
                            text_font, font_size, text_color
                        )

                if not shm_rendered:
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
                    logging.info(f"📡 PUT /api/overlays/model/{overlay_model}/text")
                    logging.info(f"   Payload: {text_payload}")
                    logging.info(f"   Response: {response.status_code} - {response.text}")
                    if response.status_code == 200:
                        logging.info(f"✅ Text sent successfully")
                    else:
                        logging.error(f"❌ TEXT API FAILED! Status: {response.status_code}")
                else:
                    logging.info(f"✅ PIL shm render succeeded — skipping text API")

                state_resp = requests.put(state_url, json={"State": 3}, timeout=3)
                logging.info(f"   Set state=3 (Transparent RGB): {state_resp.status_code} - {state_resp.text}")

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
        name_playlist = config.get('name_display_playlist', '')

        if name_playlist.startswith('seq:'):
            # Stop the names FSEQ Effect — waiting FSEQ keeps running underneath
            seq_name = name_playlist[4:].removesuffix('.fseq')
            r = requests.get(f"{fpp_host}/api/command/{urllib.parse.quote('FSEQ Effect Stop')}/{urllib.parse.quote(seq_name)}", timeout=3)
            logging.info(f"⏹️  FSEQ Effect Stop (names): {r.status_code} - {r.text}")
        else:
            r = requests.get(f"{fpp_host}/api/command/{urllib.parse.quote('Stop Now')}", timeout=3)
            logging.info(f"⏹️  Stop Now ({r.status_code})")

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
    _current_day = datetime.now().date()

    while not stop_polling:
        try:
            # Midnight cleanup — clear message log when the date rolls over
            today = datetime.now().date()
            if today != _current_day:
                _current_day = today
                try:
                    with open(MESSAGE_LOG, 'w') as f:
                        json.dump([], f)
                    logging.info(f"🌙 Midnight: message log cleared for {today}")
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
                if len(messages) > 0:
                    last_message_sid = messages[0].sid
                    save_last_sid(messages[0].sid)
                    logging.info(f"⚙️ First run: initialized SID {messages[0].sid[:10]}...")
                else:
                    logging.info("⚙️ First run: no messages found, polling from now on")
                first_run = False
                time.sleep(config['poll_interval'])
                continue
            
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

                        elif max_msgs > 0 and has_sent_name_today(from_number, name):
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
            .valign-picker { display: flex; gap: 6px; margin: 4px 0; }
            .valign-btn { flex: 1; padding: 7px 4px; border: 1px solid #ccc; background: #f5f5f5; border-radius: 4px; cursor: pointer; font-size: 13px; transition: background 0.15s, border-color 0.15s; }
            .valign-btn.active { background: #4CAF50; color: white; border-color: #388E3C; font-weight: bold; }
            .valign-btn:hover:not(.active) { background: #e8e8e8; }
            #message_template { font-family: monospace; min-height: 50px; max-height: 180px; resize: vertical; }
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
                        <input type="text" id="phone_number" value="{{ config.twilio_phone_number }}" placeholder="+18555551234">

                        <label>Poll Interval (seconds):</label>
                        <input type="number" id="poll_interval" value="{{ config.poll_interval }}" min="1" max="60">

                        <button class="test-btn" onclick="testConnection()">🔌 Test Twilio Connection</button>
                        <div id="twilio_test_result" style="margin-top: 8px; font-size: 14px;"></div>

                        <hr style="border: none; border-top: 1px solid #ddd; margin: 15px 0;">
                        <h2 style="margin-top: 0;">FPP Display Settings</h2>

                        <label>Default "Waiting" Playlist: <span style="color:#f44336;font-size:12px;">* required</span></label>
                        <select id="default_playlist">
                            <option value="">-- Select a playlist --</option>
                        </select>
                        <p class="help-text">📺 This playlist or sequence loops while waiting for text messages</p>

                        <label>Name Display Playlist:</label>
                        <select id="name_display_playlist">
                            <option value="">-- None (No Playlist Change) --</option>
                        </select>
                        <p class="help-text">🎬 This playlist or sequence plays when displaying a name</p>

                        <label>Overlay Model Name: <button type="button" onclick="refreshFPPLists(this)" style="font-size:11px;padding:2px 7px;margin-left:8px;cursor:pointer;">↻ Refresh Lists</button></label>
                        <select id="overlay_model_name">
                            <option value="">-- None --</option>
                        </select>
                        <p class="help-text">📝 The pixel overlay model for text (e.g., "Texting Matrix")</p>

                        <hr style="border: none; border-top: 1px solid #ddd; margin: 15px 0;">
                        <h2 style="margin-top: 0;">Message Settings</h2>

                        <label>Display Duration (seconds):</label>
                        <input type="number" id="display_duration" value="{{ config.display_duration }}" min="5" max="300">
                        <p class="help-text">⏱️ Each message displays for this many seconds before moving to the next</p>

                        <label>Max Messages Per Phone (0 = unlimited):</label>
                        <input type="number" id="max_messages" value="{{ config.max_messages_per_phone }}" min="0" max="100">

                        <label>Max Message Length:</label>
                        <input type="number" id="max_length" value="{{ config.max_message_length }}" min="10" max="200">

                    </div>
                </div>

                <!-- RIGHT COLUMN: Filters + Text Display -->
                <div class="column">
                    <div class="section">
                        <h2>Filters</h2>

                        <div id="blacklist_section">
                            <input type="checkbox" id="profanity_filter" {{ 'checked' if config.profanity_filter else '' }} onchange="checkFiltersState()">
                            <label class="checkbox-label">✓ Enable Profanity Filter</label><br>
                            <button class="view-btn" onclick="location.href='/blacklist'" style="margin-top: 6px;">🚫 Manage Blacklist</button>
                        </div>
                        <div id="profanity_disabled_warning" style="display:none; background:#f8d7da; border:1px solid #f5c6cb; color:#721c24; border-radius:5px; padding:8px 12px; margin-top:8px; font-size:13px;">
                            ⚠️ <strong>Profanity filter is disabled</strong> — this is not recommended.
                        </div>
                        <div id="blacklist_disabled_warning" style="display:none; background:#fff3cd; border:1px solid #ffc107; color:#856404; border-radius:5px; padding:8px 12px; margin-top:8px; font-size:13px;">
                            ⚠️ <strong>Blacklist inactive</strong> — whitelist is enabled. All names are validated against the whitelist.
                        </div>

                        <hr style="border: none; border-top: 1px solid #ddd; margin: 15px 0;">

                        <input type="checkbox" id="use_whitelist" {{ 'checked' if config.get('use_whitelist', False) else '' }} onchange="updateFormatRules(); checkFiltersState();">
                        <label class="checkbox-label">✓ Enable Name Whitelist — only allow approved names</label><br>
                        <button class="view-btn" onclick="location.href='/whitelist'" style="margin-top: 6px;">📋 Manage Whitelist</button>

                        <div id="format_rules_section" style="margin-top: 12px;">
                            <h3 style="margin-bottom: 6px;">Name Format Rules</h3>
                            <div id="format_rules_disabled_note" style="display:none; background:#fff3cd; border:1px solid #ffc107; color:#856404; border-radius:5px; padding:8px 12px; margin-bottom:8px; font-size:13px;">
                                ⚠️ Name format rules are disabled when the whitelist is active.
                            </div>
                            <div id="format_rules_inputs">
                                <input type="checkbox" id="one_word_only" {{ 'checked' if config.get('one_word_only', False) and not config.get('use_whitelist', False) else '' }}
                                       onchange="if(this.checked) document.getElementById('two_words_max').checked = false; checkFormatWarning();">
                                <label class="checkbox-label">✓ One Word Only (e.g., "John" ✓, "John Smith" ✗)</label><br>

                                <input type="checkbox" id="two_words_max" {{ 'checked' if config.get('two_words_max', True) and not config.get('use_whitelist', False) else '' }}
                                       onchange="if(this.checked) document.getElementById('one_word_only').checked = false; checkFormatWarning();">
                                <label class="checkbox-label">✓ Two Words Maximum (e.g., "John Smith" ✓, sentences ✗)</label><br>

                                <div id="format_warning" style="display:none; background:#f8d7da; border:1px solid #f5c6cb; color:#721c24; border-radius:5px; padding:10px 14px; margin:8px 0; font-size:13px;">
                                    ⚠️ <strong>Warning:</strong> With no format rules enabled, viewers can send any message up to your Max Message Length. This is not recommended.
                                </div>
                            </div>
                            <p id="hyphen_note" class="help-text">ℹ️ Hyphenated names like "Jean-Luc" count as one word. All names are converted to Proper Case.</p>
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
                                // User just turned whitelist off — restore two words as default
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
                        function checkFiltersState() {
                            var whitelistOn = document.getElementById('use_whitelist').checked;
                            var profanityOn = document.getElementById('profanity_filter').checked;
                            var section = document.getElementById('blacklist_section');

                            // Grey out entire blacklist section when whitelist is active
                            section.style.opacity = whitelistOn ? '0.4' : '1';
                            section.style.pointerEvents = whitelistOn ? 'none' : '';

                            // Warnings
                            document.getElementById('blacklist_disabled_warning').style.display = whitelistOn ? 'block' : 'none';
                            document.getElementById('profanity_disabled_warning').style.display = (!whitelistOn && !profanityOn) ? 'block' : 'none';
                        }
                        updateFormatRules();
                        checkFiltersState();
                        </script>

                        <hr style="border: none; border-top: 1px solid #ddd; margin: 15px 0;">

                        <label style="font-weight:bold; margin-bottom:4px;">Phone Blocklist</label>
                        <button onclick="location.href='/blocklist'" style="background:#f44336; margin-top: 4px;">🚫 View Blocklist</button>

                        <hr style="border: none; border-top: 1px solid #ddd; margin: 15px 0;">
                        <h2 style="margin-top: 0;">Text Display Options</h2>

                        <label>Message Template:</label>
                        <textarea id="message_template">{{ config.get('message_template', 'Merry Christmas {name}!') }}</textarea>
                        <p class="help-text">💬 Use {name} as placeholder. Press Enter for new lines.</p>

                        <label>Text Color:</label>
                        <div style="display: flex; align-items: center; gap: 10px;">
                            <input type="color" id="text_color" value="{{ config.get('text_color', '#FF0000') }}"
                                   style="width: 60px; height: 40px; padding: 2px; cursor: pointer;">
                            <input type="text" id="text_color_hex" value="{{ config.get('text_color', '#FF0000') }}"
                                   placeholder="#FF0000" style="width: 100px;"
                                   onchange="document.getElementById('text_color').value = this.value">
                            <span id="color_preview" style="padding: 8px 20px; border-radius: 4px; background: {{ config.get('text_color', '#FF0000') }}; color: white; font-weight: bold;">Preview</span>
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
                        <p class="help-text">🔤 Select from FPP-supported fonts</p>

                        <label>Font Size:</label>
                        <input type="number" id="text_font_size" value="{{ config.get('text_font_size', 48) }}" min="12" max="200">

                        <label>Text Position:</label>
                        <select id="text_position" onchange="updateScrollSpeedVisibility()">
                            <option value="Center" {{ 'selected' if config.get('text_position') == 'Center' else '' }}>Static</option>
                            <option value="L2R" {{ 'selected' if config.get('text_position') == 'L2R' else '' }}>Scroll Left to Right</option>
                            <option value="R2L" {{ 'selected' if config.get('text_position') == 'R2L' else '' }}>Scroll Right to Left</option>
                            <option value="T2B" {{ 'selected' if config.get('text_position') == 'T2B' else '' }}>Scroll Top to Bottom</option>
                            <option value="B2T" {{ 'selected' if config.get('text_position') == 'B2T' else '' }}>Scroll Bottom to Top</option>
                        </select>
                        <p class="help-text">💡 Choose "Static" for centered text or select scroll direction</p>

                        <div id="scroll_speed_row">
                            <label>Scroll Speed:</label>
                            <input type="number" id="scroll_speed" value="{{ config.get('scroll_speed', 5) }}" min="1" max="10"
                                   oninput="this.value = Math.min(10, Math.max(1, parseInt(this.value)||1))">
                            <p class="help-text">⚡ 1 = slowest, 10 = fastest</p>
                        </div>

                        <input type="hidden" id="text_v_align" value="{{ config.get('text_v_align', 'center') }}">
                        <input type="hidden" id="overlay_model_width" value="{{ config.get('overlay_model_width', 0) }}">
                        <input type="hidden" id="overlay_model_height" value="{{ config.get('overlay_model_height', 0) }}">
                        <input type="hidden" id="text_x" value="{{ config.get('text_x', -1) }}">
                        <input type="hidden" id="text_y" value="{{ config.get('text_y', -1) }}">

                        <!-- Canvas: shown for Static mode -->
                        <div id="canvas_section" style="{{ 'display:none' if config.get('text_position', 'Center') != 'Center' else '' }}">
                            <label>Text Position: <span style="font-weight:normal; font-size:12px; color:#888;">click or drag to reposition</span></label>
                            <canvas id="matrix_canvas" style="display:block; width:100%; background:#000; border:2px solid #555; border-radius:4px; cursor:crosshair;"></canvas>
                            <div style="display:flex; gap:8px; margin-top:6px; align-items:center;">
                                <button type="button" onclick="resetTextPosition()" style="background:#555; padding:6px 12px; font-size:12px;">Reset to Center</button>
                                <span id="pos_display" style="font-size:12px; color:#666;"></span>
                            </div>
                            <p class="help-text">🖱️ Drag text on the matrix preview to set its position. Reset returns to auto-center.</p>
                        </div>

                        <!-- Valign: shown for Scroll modes -->
                        <div id="valign_section" style="{{ '' if config.get('text_position', 'Center') != 'Center' else 'display:none' }}">
                            <label>Vertical Alignment:</label>
                            <div class="valign-picker">
                                <button type="button" class="valign-btn" data-val="top">▲ Top</button>
                                <button type="button" class="valign-btn" data-val="center">● Center</button>
                                <button type="button" class="valign-btn" data-val="bottom">▼ Bottom</button>
                            </div>
                            <p class="help-text">↕ Shifts text vertically on the matrix</p>
                        </div>
                    </div>
                </div>

            </div>
        </div>

        <!-- SMS Responses Tab -->
        <div id="tab-sms" class="tab-content">
            <div class="section" style="border: 2px solid #2196F3; margin-top: 20px;">
                <h2>📱 SMS Auto-Response Settings</h2>
                <p class="help-text">💡 Enable a response for each event type individually. Only one response is ever sent per incoming message.</p>
                <div style="background:#fff3cd; border:1px solid #ffc107; color:#856404; border-radius:5px; padding:10px 14px; margin:10px 0; font-size:13px;">
                    ⚠️ <strong>Message &amp; data rates may apply.</strong> Each auto-response costs ~$0.0079 (Twilio US rate). Standard carrier rates also apply to recipients.
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

                <div id="row_duplicate" class="resp-row">
                    <div class="resp-toggle">
                        <input type="checkbox" id="sms_response_duplicate" {{ 'checked' if config.get('sms_response_duplicate', False) else '' }} onchange="toggleResp('duplicate')">
                        <label for="sms_response_duplicate">🔄 Duplicate Name — Send Response</label>
                    </div>
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
                    <input type="text" id="test_name" placeholder="Enter a name to test (e.g., John or Mary Smith)">

                    <button class="test-btn" onclick="submitTestMessage()">🧪 Submit Test Message</button>

                    <div id="test_result" style="margin-top: 10px;"></div>
                </div>
            </div>

            <div class="section" style="border: 2px solid #FF9800;">
                <h2>🧪 SMS Response Testing</h2>
                <p style="color: #FF9800; font-size: 14px;">
                    ⚠️ Test sending SMS responses to a phone number. Requires Twilio credentials and the response type enabled in the SMS Responses tab.
                </p>

                <label>Phone Number:</label>
                <input type="text" id="test_sms_phone" placeholder="+18005551234">

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
            function updateLiveStatus() {
                fetch('/api/queue/status').then(r => r.json()).then(data => {
                    const live = data.show_live === true;
                    const banner = document.getElementById('show_not_live_banner');
                    const form = document.getElementById('test_form_inner');
                    if (banner) banner.style.display = live ? 'none' : 'block';
                    if (form) {
                        form.style.opacity = live ? '1' : '0.4';
                        form.style.pointerEvents = live ? '' : 'none';
                    }
                }).catch(() => {});
            }

            function updateScrollSpeedVisibility() {
                var pos = document.getElementById('text_position').value;
                var isStatic = (pos === 'Center');
                var row = document.getElementById('scroll_speed_row');
                if (row) row.style.display = isStatic ? 'none' : '';
                var cs = document.getElementById('canvas_section');
                if (cs) cs.style.display = isStatic ? '' : 'none';
                var vs = document.getElementById('valign_section');
                if (vs) vs.style.display = isStatic ? 'none' : '';
            }

            function updateModelAspect(width, height) {
                var ta = document.getElementById('message_template');
                if (ta && width > 0 && height > 0) {
                    ta.style.aspectRatio = (width / height).toFixed(2);
                    ta.style.height = 'auto';
                }
                if (width > 0 && height > 0) {
                    window._canvasModelW = width;
                    window._canvasModelH = height;
                    var c = document.getElementById('matrix_canvas');
                    if (c) { c.width = 640; c.height = Math.round(640 * height / width); }
                    if (typeof window.renderCanvasPreview === 'function') window.renderCanvasPreview();
                }
            }

            function initValignButtons() {
                var current = document.getElementById('text_v_align').value || 'center';
                document.querySelectorAll('.valign-btn').forEach(function(btn) {
                    btn.classList.toggle('active', btn.dataset.val === current);
                    btn.addEventListener('click', function() {
                        document.querySelectorAll('.valign-btn').forEach(function(b) { b.classList.remove('active'); });
                        this.classList.add('active');
                        document.getElementById('text_v_align').value = this.dataset.val;
                        saveConfig();
                    });
                });
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

                var textX = parseInt(document.getElementById('text_x').value);
                var textY = parseInt(document.getElementById('text_y').value);
                var dragging = false;

                function renderCanvasPreview() {
                    var mw = window._canvasModelW || 640;
                    var mh = window._canvasModelH || 360;
                    ctx.fillStyle = '#000';
                    ctx.fillRect(0, 0, canvas.width, canvas.height);
                    var fontSize = parseInt(document.getElementById('text_font_size').value) || 48;
                    var color = document.getElementById('text_color').value || '#ff0000';
                    var tmpl = document.getElementById('message_template').value || '{name}';
                    var text = tmpl.replace('{name}', 'Santa').replace(/\n/g, ' ').trim();
                    var scaledFont = Math.round(fontSize * canvas.width / mw);
                    ctx.font = 'bold ' + scaledFont + 'px sans-serif';
                    ctx.fillStyle = color;
                    ctx.textBaseline = 'top';
                    var drawX, drawY;
                    if (textX < 0 || textY < 0) {
                        var m = ctx.measureText(text);
                        drawX = (canvas.width - m.width) / 2;
                        drawY = (canvas.height - scaledFont) / 2;
                    } else {
                        drawX = textX * canvas.width / mw;
                        drawY = textY * canvas.height / mh;
                    }
                    ctx.fillText(text, drawX, drawY);
                    var posEl = document.getElementById('pos_display');
                    if (posEl) posEl.textContent = (textX < 0 || textY < 0)
                        ? 'Position: Center (auto)'
                        : 'X:' + textX + ' Y:' + textY;
                }
                window.renderCanvasPreview = renderCanvasPreview;

                function posFromEvent(e) {
                    var rect = canvas.getBoundingClientRect();
                    var mw = window._canvasModelW || 640;
                    var mh = window._canvasModelH || 360;
                    return {
                        x: Math.max(0, Math.min(mw-1, Math.round((e.clientX-rect.left)*mw/rect.width))),
                        y: Math.max(0, Math.min(mh-1, Math.round((e.clientY-rect.top)*mh/rect.height)))
                    };
                }

                canvas.addEventListener('mousedown', function(e) {
                    dragging = true;
                    var p = posFromEvent(e); textX = p.x; textY = p.y;
                    document.getElementById('text_x').value = textX;
                    document.getElementById('text_y').value = textY;
                    renderCanvasPreview(); saveConfig();
                });
                canvas.addEventListener('mousemove', function(e) {
                    if (!dragging) return;
                    var p = posFromEvent(e); textX = p.x; textY = p.y;
                    document.getElementById('text_x').value = textX;
                    document.getElementById('text_y').value = textY;
                    renderCanvasPreview();
                });
                canvas.addEventListener('mouseup',    function() { dragging = false; saveConfig(); });
                canvas.addEventListener('mouseleave', function() { dragging = false; });

                window.resetTextPosition = function() {
                    textX = -1; textY = -1;
                    document.getElementById('text_x').value = -1;
                    document.getElementById('text_y').value = -1;
                    renderCanvasPreview(); saveConfig();
                };

                document.getElementById('message_template').addEventListener('input',  renderCanvasPreview);
                document.getElementById('text_color').addEventListener('change',        renderCanvasPreview);
                document.getElementById('text_font_size').addEventListener('change',    renderCanvasPreview);

                renderCanvasPreview();
            }

            // All DOM elements are above this script block — call init functions directly.
            try { initCanvasPreview(); } catch(e) { console.error('Canvas init error:', e); }
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

            function loadFPPData() {
                fetch('/api/fpp/data')
                .then(r => r.json())
                .then(data => {
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
                        // Set aspect ratio for the currently selected model
                        const cur = data.models.find(m => (typeof m === 'object' ? m.name : m) === currentModel);
                        if (cur && cur.width && cur.height) updateModelAspect(cur.width, cur.height);
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

                    const fontSelect = document.getElementById('text_font');
                    const currentFont = "{{ config.get('text_font', 'FreeSans') }}";
                    fontSelect.innerHTML = '<option value="">-- Select Font --</option>';

                    if (data.fonts && data.fonts.length > 0) {
                        data.fonts.forEach(font => {
                            const opt = new Option(font, font, false, font === currentFont);
                            opt.style.fontFamily = font + ', sans-serif';
                            fontSelect.add(opt);
                        });
                    } else {
                        fontSelect.innerHTML = '<option value="FreeSans">FreeSans (default)</option>';
                    }
                })
                .catch(function(e) { console.error('FPP data load failed:', e); });
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
                    text_v_align: document.getElementById('text_v_align').value,
                    overlay_model_width: parseInt(document.getElementById('overlay_model_width').value) || 0,
                    overlay_model_height: parseInt(document.getElementById('overlay_model_height').value) || 0,
                    text_x: parseInt(document.getElementById('text_x').value) || -1,
                    text_y: parseInt(document.getElementById('text_y').value) || -1,
                    message_template: document.getElementById('message_template').value,
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
                ['profanity_filter','use_whitelist','text_color',
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
                // Text, number, textarea — save when user clicks away
                ['account_sid','auth_token','phone_number',
                 'poll_interval','display_duration','max_messages','max_length',
                 'text_color_hex','text_font_size',
                 'message_template',
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
    global config, twilio_client
    try:
        new_config = request.json
        config.update(new_config)
        save_config()
        
        if config['twilio_account_sid'] and config['twilio_auth_token']:
            twilio_client = Client(
                config['twilio_account_sid'],
                config['twilio_auth_token']
            )
        
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})

@app.route('/api/fpp/data')
def get_fpp_data():
    global _fpp_data_cache, _fpp_data_cache_time
    try:
        # Return cached result if still fresh
        if _fpp_data_cache and (time.time() - _fpp_data_cache_time) < _FPP_DATA_CACHE_TTL:
            return jsonify(_fpp_data_cache)

        # Fetch all 4 in parallel instead of sequentially
        results = {}
        tasks = {
            'playlists': get_fpp_playlists,
            'sequences': get_fpp_sequences,
            'models':    get_fpp_models,
            'fonts':     get_fpp_fonts,
        }
        with ThreadPoolExecutor(max_workers=4) as executor:
            futures = {executor.submit(fn): key for key, fn in tasks.items()}
            for future in as_completed(futures):
                results[futures[future]] = future.result()

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
        with open(MESSAGE_LOG, 'r') as f:
            messages = json.load(f)
    except:
        messages = []
    return jsonify(list(reversed(messages)))

@app.route('/api/messages/clear', methods=['POST'])
def clear_messages():
    try:
        with open(MESSAGE_LOG, 'w') as f:
            json.dump([], f)
        logging.info("Message history cleared")
        return jsonify({"success": True})
    except Exception as e:
        logging.error(f"Error clearing messages: {e}")
        return jsonify({"success": False, "error": str(e)})

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
    """Test sending an SMS response"""
    try:
        data = request.json
        phone = data.get('phone', '').strip()
        message_type = data.get('message_type', 'success')
        
        if not phone:
            return jsonify({"success": False, "error": "Phone number is required"})
        
        if not config.get('send_sms_responses', False):
            return jsonify({"success": False, "error": "SMS responses are disabled. Enable them in settings first!"})
        
        logging.debug(f"🧪 TEST SMS: '{message_type}' to {phone}")
        
        success = send_sms_response(phone, message_type)
        
        if success:
            return jsonify({"success": True, "message": f"Test SMS sent to {phone}"})
        else:
            return jsonify({"success": False, "error": "Failed to send SMS. Check logs for details."})
            
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
    try:
        with open(MESSAGE_LOG, 'r') as f:
            messages = json.load(f)
    except:
        messages = []
    
    queue_status = get_queue_status()
    
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
        </style>
    </head>
    <body><script>if('scrollRestoration'in history)history.scrollRestoration='manual';function _toTop(){window.scrollTo(0,0);document.documentElement.scrollTop=0;document.body.scrollTop=0;try{window.parent.postMessage({type:'scrollTop'},'*');}catch(e){}}_toTop();document.addEventListener('DOMContentLoaded',_toTop);window.addEventListener('load',_toTop);</script>
        <h1>📋 Message History & Queue Status</h1>

        <div class="queue-info">
            • Messages are queued and displayed one at a time<br>
            • Each message displays for the full display duration<br>
            • View real-time queue status
        </div>

        <div class="info">
            Auto-refreshes every 5 seconds | Total Messages: <span id="msg-count">—</span>
        </div>
        <button onclick="location.href='/'">← Back to Config</button>
        <button onclick="refreshData()">🔄 Refresh</button>
        <button class="clear-btn" onclick="clearHistory()">🗑️ Clear All Messages</button>

        <div class="queue-box">
            <h2>🎬 Current Display Queue</h2>
            <div id="queue-box-content"><p style="color:#aaa;">Loading...</p></div>
        </div>

        <h2>📜 Complete Message History</h2>
        <div id="messages-content"><p style="color:#aaa;">Loading...</p></div>

        <!-- Block action modal -->
        <div id="block-modal" style="display:none; position:fixed; top:0; left:0; width:100%; height:100%; background:rgba(0,0,0,0.5); z-index:1000; align-items:center; justify-content:center;">
            <div style="background:#fff; border-radius:8px; padding:28px; max-width:420px; width:90%; box-shadow:0 4px 20px rgba(0,0,0,0.3);">
                <h3 style="margin-top:0; color:#333;">🚫 Block Action</h3>
                <p style="color:#555; margin-bottom:6px;">Phone: <strong id="modal-phone"></strong></p>
                <p style="color:#555; margin-bottom:20px;">Name: <strong id="modal-name-text"></strong></p>
                <p style="color:#333; font-weight:bold; margin-bottom:16px;">What would you like to block?</p>
                <div style="display:flex; flex-direction:column; gap:10px;">
                    <button style="background:#f44336; color:white; padding:12px; border:none; border-radius:5px; cursor:pointer; font-size:14px;"
                            onclick="blockPhone(document.getElementById('block-modal').dataset.phone)">
                        📵 Block this number from texting again
                    </button>
                    <button id="modal-block-name-btn" style="background:#FF9800; color:white; padding:12px; border:none; border-radius:5px; cursor:pointer; font-size:14px;"
                            onclick="blockNameFromDisplay()">
                        🚫 Block this name from being displayed
                    </button>
                    <p id="whitelist-warning" style="color:#f44336; font-size:12px; margin:0; padding:4px 0; display:none;">
                        ⚠️ Whitelist is not enabled — this name will still show again
                    </p>
                    <button style="background:#aaa; color:white; padding:10px; border:none; border-radius:5px; cursor:pointer; font-size:13px;"
                            onclick="closeBlockModal()">
                        Cancel
                    </button>
                </div>
            </div>
        </div>

        <script>
            var useWhitelist = {{ config.get('use_whitelist', False) | tojson }};
            var modalOpen = false;
            var refreshTimer = null;
            var prevQueueJson = null;
            var prevMessagesJson = null;

            function scheduleRefresh() {
                clearTimeout(refreshTimer);
                refreshTimer = setTimeout(function() {
                    if (!modalOpen) refreshData();
                    else scheduleRefresh();
                }, 5000);
            }

            function esc(s) {
                return String(s || '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
            }

            function refreshData() {
                Promise.all([
                    fetch('/api/queue/status').then(r => r.json()),
                    fetch('/api/messages').then(r => r.json())
                ]).then(function(results) {
                    renderQueue(results[0]);
                    renderMessages(results[1]);
                    scheduleRefresh();
                }).catch(scheduleRefresh);
            }

            function renderQueue(status) {
                var json = JSON.stringify(status);
                if (json === prevQueueJson) return;
                prevQueueJson = json;
                var html = '';
                if (status.currently_displaying) {
                    html += '<div class="current-display">🎄 NOW DISPLAYING: ' + esc(status.currently_displaying.name) +
                            ' (from ***' + esc(status.currently_displaying.phone_last4) + ')</div>';
                } else {
                    html += '<div class="current-display" style="background:#bdbdbd;color:#333;">💤 Nothing currently displaying</div>';
                }
                if (status.queue_length > 0) {
                    html += '<h3 style="color:#FF9800;margin-top:20px;">📋 Queue (' + status.queue_length + ' waiting):</h3>';
                    status.queue.forEach(function(item, i) {
                        html += '<div class="queue-item"><strong>Queue Position ' + (i+1) + ':</strong> ' +
                                esc(item.name) + ' (from ***' + esc(item.phone_last4) + ')</div>';
                    });
                } else {
                    html += '<p style="color:#aaa;font-style:italic;margin-top:15px;">Queue is empty - waiting for messages</p>';
                }
                document.getElementById('queue-box-content').innerHTML = html;
            }

            function renderMessages(messages) {
                var json = JSON.stringify(messages);
                if (json === prevMessagesJson) return;
                prevMessagesJson = json;
                document.getElementById('msg-count').textContent = messages.length;
                if (messages.length === 0) {
                    document.getElementById('messages-content').innerHTML =
                        '<div style="background:#333;padding:40px;text-align:center;border-radius:5px;"><h3>No messages yet</h3></div>';
                    return;
                }
                var statusLabel = {'displaying':'🎬 DISPLAYING NOW','queued':'📋 Queued','displayed':'✅ Displayed'};
                var rows = messages.map(function(msg) {
                    var label = statusLabel[msg.status] || esc(msg.status);
                    var isTest = (msg.phone_full === 'Local Testing');
                    var btn = isTest
                        ? '<button class="block-btn" disabled style="opacity:0.35;cursor:not-allowed;" title="Cannot block the local test number">🚫 Block</button>'
                        : '<button class="block-btn" data-phone="' + esc(msg.phone_full) + '" data-name="' + esc(msg.extracted_name) +
                          '" onclick="showBlockModal(this.dataset.phone,this.dataset.name)">🚫 Block</button>';
                    return '<tr class="' + esc(msg.status) + '">' +
                        '<td>' + esc(msg.timestamp) + '</td>' +
                        '<td>' + esc(msg.phone) + '</td>' +
                        '<td>' + esc(msg.message) + '</td>' +
                        '<td>' + esc(msg.extracted_name) + '</td>' +
                        '<td class="' + esc(msg.status) + '">' + label + '</td>' +
                        '<td>' + btn + '</td>' +
                        '</tr>';
                }).join('');
                document.getElementById('messages-content').innerHTML =
                    '<table><tr><th>Timestamp</th><th>Phone</th><th>Message</th><th>Name</th><th>Status</th><th>Action</th></tr>' + rows + '</table>';
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
                fetch('/api/phone/block', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({phone: phone})
                })
                .then(r => r.json())
                .then(data => {
                    if (data.success) alert('✅ Phone number blocked!');
                    refreshData();
                });
            }

            function blockNameFromDisplay() {
                const modal = document.getElementById('block-modal');
                const name = modal.dataset.name;
                closeBlockModal();
                fetch('/api/whitelist/remove', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({name: name})
                })
                .then(r => r.json())
                .then(data => {
                    alert(data.success ? '✅ "' + name + '" blocked from display!' : '❌ ' + data.error);
                    refreshData();
                });
            }

            function clearHistory() {
                if (confirm('Clear all message history?')) {
                    fetch('/api/messages/clear', { method: 'POST' })
                    .then(r => r.json())
                    .then(data => {
                        if (data.success) alert('✅ Message history cleared!');
                        refreshData();
                    });
                }
            }

            refreshData();
        </script>
    </body>
    </html>
    """
    return render_template_string(html, config=config)


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

        # Stop Now catches playlists and any foreground sequences
        command_url = f"{FPP_HOST}/api/command/{urllib.parse.quote('Stop Now')}"
        r2 = requests.get(command_url, timeout=3)
        logging.info(f"🛑 Stop Now: {r2.status_code} - {r2.text}")
    except Exception as e:
        logging.warning(f"Could not stop FPP playback: {e}")

    logging.info("🛑 TwilioStop: disabled, polling stopped, playlist stopped")
    return jsonify({"success": True, "message": "Twilio SMS plugin deactivated"})


if __name__ == '__main__':
    load_config()

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
