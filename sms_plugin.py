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
from twilio.rest import Client
from collections import deque
import os

# Configuration
PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = "/home/fpp/media/config/plugin.fpp-sms-twilio.json"
LOG_FILE = "/home/fpp/media/logs/sms_plugin.log"
MESSAGE_LOG = "/home/fpp/media/logs/received_messages.json"
BLACKLIST_FILE = os.path.join(PLUGIN_DIR, "blacklist.txt")
BLACKLIST_REMOVED_FILE = os.path.join(PLUGIN_DIR, "blacklist_removed.txt")
WHITELIST_FILE = os.path.join(PLUGIN_DIR, "whitelist.txt")
WHITELIST_REMOVED_FILE = os.path.join(PLUGIN_DIR, "whitelist_removed.txt")
LAST_SID_FILE = "/home/fpp/media/config/last_message_sid.txt"
BLOCKLIST_FILE = "/home/fpp/media/config/blocked_phones.json"

# Setup logging
logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)

app = Flask(__name__)

IFRAME_RESIZE_SCRIPT = """<script>
(function() {
    function reportHeight() {
        window.parent.postMessage({ type: 'iframeHeight', height: document.documentElement.scrollHeight }, '*');
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
    "message_template": "Merry Christmas {name}!",
    "scroll_speed": 20,
    "text_offset_x": 0,
    "text_offset_y": 0,
    "send_sms_responses": False,
    "response_success": "Thanks! Your name will appear on our display soon! 🎄",
    "response_profanity": "Sorry, your message contains inappropriate content and cannot be displayed.",
    "response_blocked": "You have been blocked from sending messages.",
    "response_rate_limited": "You've reached the maximum number of messages allowed. Please try again tomorrow!",
    "response_duplicate": "You've already sent this name today!",
    "response_invalid_format": "Please send only a name (1-2 words, no sentences).",
    "response_not_whitelisted": "Sorry, that name is not on our approved list.",
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

def get_fpp_playlists():
    """Get list of playlists from FPP"""
    try:
        fpp_host = config.get('fpp_host', 'http://127.0.0.1')
        playlists = []
        
        try:
            response = requests.get(f"{fpp_host}/api/playlists", timeout=5)
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
        fpp_host = config.get('fpp_host', 'http://127.0.0.1')
        response = requests.get(f"{fpp_host}/api/sequence", timeout=5)
        if response.status_code == 200:
            sequences = response.json()
            return sequences if isinstance(sequences, list) else []
        return []
    except Exception as e:
        logging.error(f"Error fetching FPP sequences: {e}")
        return []

def get_fpp_models():
    """Get list of overlay models from FPP"""
    try:
        fpp_host = config.get('fpp_host', 'http://127.0.0.1')
        response = requests.get(f"{fpp_host}/api/models", timeout=5)
        if response.status_code == 200:
            data = response.json()
            logging.info(f"FPP Models API response: {data}")
            
            models = []
            
            if isinstance(data, dict) and 'models' in data:
                for model in data['models']:
                    if isinstance(model, dict) and 'Name' in model:
                        models.append(model['Name'])
            elif isinstance(data, list):
                for model in data:
                    if isinstance(model, dict) and 'Name' in model:
                        models.append(model['Name'])
            elif isinstance(data, dict):
                models = list(data.keys())
            
            logging.info(f"Extracted {len(models)} models: {models}")
            return models
        
        logging.warning(f"FPP models API returned status {response.status_code}")
        return []
    except Exception as e:
        logging.error(f"Error fetching FPP models: {e}")
        return []

def get_fpp_fonts():
    """Get list of supported fonts from FPP"""
    try:
        fpp_host = config.get('fpp_host', 'http://127.0.0.1')
        response = requests.get(f"{fpp_host}/api/overlays/fonts", timeout=5)
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
        fpp_host = config.get('fpp_host', 'http://127.0.0.1')
        response = requests.get(f"{fpp_host}/api/fppd/status", timeout=5)
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
    """Load and cache the whitelist, reload if file has changed"""
    global _whitelist_cache, _whitelist_mtime

    try:
        current_mtime = os.path.getmtime(WHITELIST_FILE)

        # Only reload if file changed or not yet loaded
        if _whitelist_cache is None or _whitelist_mtime != current_mtime:
            with open(WHITELIST_FILE, 'r', encoding='latin-1') as f:
                names = [line.strip().lower() for line in f if line.strip() and not line.startswith('#')]

            # Filter out names the user has explicitly removed (prevents git pull re-adding them)
            removed = load_removed_names()
            if removed:
                names = [n for n in names if n not in removed]

            _whitelist_cache = names
            _whitelist_mtime = current_mtime
            logging.info(f"Loaded {len(_whitelist_cache)} names into whitelist cache")

        return _whitelist_cache

    except FileNotFoundError:
        logging.info(f"Whitelist file not found: {WHITELIST_FILE}")
        return []
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
    if not config.get('send_sms_responses', False):
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

def load_blacklist_words():
    """Return the raw word list from blacklist.txt (for API use)"""
    if not os.path.exists(BLACKLIST_FILE):
        return []
    try:
        with open(BLACKLIST_FILE, 'r', encoding='latin-1') as f:
            words = [line.strip().lower() for line in f if line.strip() and not line.startswith('#')]
        removed = load_blacklist_removed()
        if removed:
            words = [w for w in words if w not in removed]
        return sorted(words)
    except Exception as e:
        logging.error(f"Error reading blacklist words: {e}")
        return []

def load_blacklist():
    """Load and cache the profanity blacklist with pre-compiled regex patterns"""
    global _blacklist_cache, _blacklist_mtime

    try:
        current_mtime = os.path.getmtime(BLACKLIST_FILE)

        # Only reload if file changed or not yet loaded
        if _blacklist_cache is None or _blacklist_mtime != current_mtime:
            with open(BLACKLIST_FILE, 'r', encoding='latin-1') as f:
                words = [line.strip().lower() for line in f if line.strip() and not line.startswith('#')]

            # Filter out words the user has explicitly removed
            removed = load_blacklist_removed()
            if removed:
                words = [w for w in words if w not in removed]

            # Pre-compile all regex patterns for maximum speed
            _blacklist_cache = [
                re.compile(r'\b' + re.escape(word) + r'\b')
                for word in words
            ]
            _blacklist_mtime = current_mtime
            logging.info(f"Loaded {len(_blacklist_cache)} words into profanity filter cache")

        return _blacklist_cache

    except FileNotFoundError:
        logging.warning(f"Blacklist file not found: {BLACKLIST_FILE}")
        return []
    except Exception as e:
        logging.error(f"Error reading blacklist: {e}")
        return []

def contains_profanity(text):
    """Check for profanity using cached blacklist with pre-compiled patterns"""
    if not config['profanity_filter']:
        return False
    
    patterns = load_blacklist()
    if not patterns:
        return False
    
    text_lower = text.lower()
    
    for pattern in patterns:
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
        fpp_host = config.get('fpp_host', 'http://127.0.0.1')
        name_playlist = config.get('name_display_playlist', '')
        overlay_model = config.get('overlay_model_name', 'Texting Matrix')
        
        # FIXED: Get message template and replace {name} with actual name
        message_template = config.get('message_template', 'Merry Christmas {name}!')
        display_message = message_template.replace('{name}', name)
        
        logging.info(f"🎄 ========== STARTING DISPLAY FOR: {name} ==========")
        logging.info(f"📺 FPP Host: {fpp_host}")
        logging.info(f"🎬 Name Display Playlist: {name_playlist}")
        logging.info(f"📝 Overlay Model: {overlay_model}")
        logging.info(f"📝 Message Template: {message_template}")
        logging.info(f"📝 Display Message: {display_message}")
        
        # Step 1: Start the name display playlist/sequence (background)
        if name_playlist:
            try:
                logging.info(f"⏸️  STEP 1: Stopping all playlists...")
                requests.get(f"{fpp_host}/api/playlists/stop", timeout=5)
                time.sleep(0.5)
                
                logging.info(f"▶️  STEP 2: Starting name display playlist: {name_playlist}")
                
                import urllib.parse
                command = "Start Playlist"
                encoded_playlist = urllib.parse.quote(name_playlist)
                command_url = f"{fpp_host}/api/command/{urllib.parse.quote(command)}/{encoded_playlist}/true/false"
                
                start_response = requests.get(command_url, timeout=5)
                logging.info(f"   Start response: {start_response.status_code}")
                
                time.sleep(1.0)
                
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
                
                # Handle newlines for multi-line support
                # Replace actual newlines with URL-encoded version
                display_message_encoded = display_message.replace('\n', '%0A')
                
                # URL encode the message (keeping %0A intact)
                message_for_url = urllib.parse.quote(display_message_encoded, safe='%')
                
                encoded_model = urllib.parse.quote(overlay_model)
                
                if not text_color.startswith('#'):
                    text_color = '#' + text_color
                
                command = "Overlay Model Effect"
                auto_enable = "Transparent"
                
                # Build command based on text position
                if text_position in ['L2R', 'R2L', 'T2B', 'B2T']:
                    # Scrolling text
                    effect = "Scroll Text"
                    direction = text_position
                    position_value = "Center"
                    
                    color_encoded = urllib.parse.quote(text_color)
                    
                    # Command: Model/AutoEnable/Effect/Message/Color/Position/Speed/Font/FontSize/AntiAlias/Direction/Iterate
                    fpp_url = f"{fpp_host}/api/command/{urllib.parse.quote(command)}/{encoded_model}/{auto_enable}/{urllib.parse.quote(effect)}/{message_for_url}/{color_encoded}/{urllib.parse.quote(position_value)}/{scroll_speed}/{text_font}/{font_size}/1/{direction}/0"
                    
                    logging.info(f"📡 Sending Overlay Model Effect (Scroll Text):")
                    logging.info(f"   Message to display: {display_message}")
                    logging.info(f"   Encoded for URL: {message_for_url}")
                    logging.info(f"   Full URL: {fpp_url}")
                    
                    response = requests.get(fpp_url, timeout=10)
                    logging.info(f"   Response: {response.status_code} - {response.text}")
                    
                else:
                    # Static text
                    effect = "Text"
                    position_value = "Center"
                    display_duration = config.get('display_duration', 30)
                    
                    color_encoded = urllib.parse.quote(text_color)
                    
                    # Command: Model/AutoEnable/Effect/Color/Font/FontSize/AntiAlias/Position/0/Duration/Text
                    fpp_url = f"{fpp_host}/api/command/{urllib.parse.quote(command)}/{encoded_model}/{auto_enable}/{urllib.parse.quote(effect)}/{color_encoded}/{text_font}/{font_size}/1/{position_value}/0/{display_duration}/{message_for_url}"
                    
                    logging.info(f"📡 Sending Overlay Model Effect (Static Text):")
                    logging.info(f"   Message to display: {display_message}")
                    logging.info(f"   Encoded for URL: {message_for_url}")
                    logging.info(f"   Full URL: {fpp_url}")
                    
                    response = requests.get(fpp_url, timeout=10)
                    logging.info(f"   Response: {response.status_code} - {response.text}")
                
                if response.status_code == 200:
                    logging.info(f"✅ Overlay Model Effect command sent successfully")
                else:
                    logging.error(f"❌ OVERLAY MODEL EFFECT FAILED! Status: {response.status_code}")
                
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
def return_to_default_playlist():
    """Clear text and trigger next scheduled item"""
    try:
        fpp_host = config.get('fpp_host', 'http://127.0.0.1')
        overlay_model = config.get('overlay_model_name', 'Texting Matrix')
        
        if overlay_model:
            try:
                logging.info(f"🧹 Clearing text from model: {overlay_model}")
                import urllib.parse
                
                command = "Overlay Model Clear"
                encoded_model = urllib.parse.quote(overlay_model)
                fpp_url = f"{fpp_host}/api/command/{urllib.parse.quote(command)}/{encoded_model}"
                
                logging.info(f"   Clear URL: {fpp_url}")
                response = requests.get(fpp_url, timeout=5)
                logging.info(f"   Clear response: {response.status_code} - {response.text}")
                
                if response.status_code == 200:
                    logging.info(f"✅ Text cleared")
                else:
                    logging.warning(f"⚠️  Could not clear text: {response.status_code}")
            except Exception as e:
                logging.warning(f"Could not clear text: {e}")
        
        with queue_lock:
            queue_length = len(message_queue)
        
        if queue_length > 0:
            logging.info(f"📋 Queue has {queue_length} more names - NOT returning to scheduled item")
            return
        
        try:
            logging.info(f"🔄 Queue empty - Starting Next Scheduled Item")
            
            import urllib.parse
            command = "Start Next Scheduled Item"
            command_url = f"{fpp_host}/api/command/{urllib.parse.quote(command)}"
            
            logging.info(f"   Command URL: {command_url}")
            response = requests.get(command_url, timeout=5)
            logging.info(f"   Response: {response.status_code} - {response.text}")
            
            if response.status_code == 200:
                logging.info(f"✅ Started next scheduled item")
            else:
                logging.error(f"❌ Failed to start next scheduled item: {response.status_code}")
                
        except Exception as e:
            logging.error(f"Error starting next scheduled item: {e}")
    
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
                    time.sleep(1)
                    continue
                
                currently_displaying = message_queue.popleft()
            
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
        "queue_length": len(queue_list)
    }
    
    return status

def poll_twilio():
    """Poll Twilio for new messages"""
    global last_message_sid, stop_polling
    
    logging.info("🚀 Twilio polling started")
    first_run = last_message_sid is None
    
    while not stop_polling:
        try:
            if not config['enabled'] or not twilio_client:
                time.sleep(config['poll_interval'])
                continue
            
            logging.info("📡 Polling Twilio for new messages...")
            
            messages = twilio_client.messages.list(
                to=config['twilio_phone_number'],
                date_sent_after=datetime.utcnow() - timedelta(minutes=10),
                limit=20
            )
            
            logging.info(f"📨 Found {len(messages)} total messages in last 10 minutes")
            
            new_messages = []
            for msg in messages:
                if last_message_sid and msg.sid == last_message_sid:
                    logging.info(f"✓ Reached last processed message SID: {last_message_sid[:10]}...")
                    break
                new_messages.append(msg)
            
            logging.info(f"🆕 Found {len(new_messages)} NEW messages to process")
            
            if first_run:
                if len(messages) > 0:
                    last_message_sid = messages[0].sid
                    save_last_sid(messages[0].sid)
                    logging.info(f"⚙️ First run: Initialized with message SID {messages[0].sid[:10]}..., will process new messages from now on")
                else:
                    logging.info("⚙️ First run: No messages found, will process new messages from now on")
                first_run = False
                time.sleep(config['poll_interval'])
                continue
            
            for msg in reversed(new_messages):
                from_number = msg.from_
                body = msg.body
                
                logging.info(f"📱 NEW SMS from {from_number[-4:]}: '{body}'")
                
                if is_blocked(from_number):
                    logging.info(f"🚫 Blocked number: {from_number}")
                    log_message(from_number, body, "", "blocked")
                    send_sms_response(from_number, "blocked")
                    last_message_sid = msg.sid
                    save_last_sid(msg.sid)
                    continue
                
                msg_count = get_message_count(from_number)
                max_msgs = config.get('max_messages_per_phone', 0)
                if max_msgs > 0 and msg_count >= max_msgs:
                    logging.info(f"⛔ Rate limited: {from_number}")
                    log_message(from_number, body, "", "rate_limited")
                    send_sms_response(from_number, "rate_limited")
                    last_message_sid = msg.sid
                    save_last_sid(msg.sid)
                    continue
                
                name = extract_name(body)
                logging.info(f"👤 Extracted name: '{name}'")
                
                try:
                    if has_sent_name_today(from_number, name):
                        logging.info(f"🔄 Duplicate name from same phone today: {name} from {from_number[-4:]}")
                        log_message(from_number, body, name, "duplicate_name_today")
                        send_sms_response(from_number, "duplicate")
                        last_message_sid = msg.sid
                        save_last_sid(msg.sid)
                        continue
                    
                    logging.info(f"🔍 Checking name format validity...")
                    is_valid, _ = is_valid_name(name)
                    logging.info(f"🔍 Name format check result: valid={is_valid}")
                    
                    if not is_valid:
                        logging.info(f"❌ Rejected invalid name format: {body}")
                        log_message(from_number, body, name, "invalid_format")
                        send_sms_response(from_number, "invalid_format")
                        last_message_sid = msg.sid
                        save_last_sid(msg.sid)
                        continue
                    
                    # Check whitelist first if enabled
                    logging.info(f"🔍 Checking whitelist...")
                    if not is_on_whitelist(name):
                        logging.info(f"❌ Rejected name not on whitelist: {name}")
                        log_message(from_number, body, name, "not_on_whitelist")
                        send_sms_response(from_number, "not_whitelisted")
                        last_message_sid = msg.sid
                        save_last_sid(msg.sid)
                        continue
                    
                    # Only check profanity if whitelist is disabled or name passed whitelist
                    logging.info(f"🔍 Checking profanity...")
                    if config['profanity_filter'] and contains_profanity(body):
                        logging.info(f"❌ Rejected profanity from {from_number}")
                        log_message(from_number, body, name, "profanity")
                        send_sms_response(from_number, "profanity")
                        last_message_sid = msg.sid
                        save_last_sid(msg.sid)
                        continue
                    
                    logging.info(f"📋 Adding to queue...")
                    success = add_to_queue(name, from_number, body)
                    logging.info(f"📋 Add to queue result: {success}")
                    
                    if success:
                        logging.info(f"✅ SUCCESS! Queued: {name}")
                        log_message(from_number, body, name, "queued")
                        send_sms_response(from_number, "success")
                    else:
                        logging.info(f"❌ Error queuing: {name}")
                        log_message(from_number, body, name, "error")
                    
                    last_message_sid = msg.sid
                    save_last_sid(msg.sid)
                    logging.info(f"💾 Saved last message SID: {msg.sid[:10]}...")
                    
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
            .columns { display: flex; gap: 20px; margin: 0; }
            .column { flex: 1; min-width: 0; }
            .top-actions { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; margin: 15px 0; padding: 15px; background: #f8f8f8; border-radius: 5px; border: 1px solid #ddd; }
            .tabs { display: flex; gap: 0; margin: 20px 0 0 0; border-bottom: 2px solid #4CAF50; }
            .tab-btn { background: #f0f0f0; color: #555; padding: 10px 24px; border: 1px solid #ddd; border-bottom: none; border-radius: 4px 4px 0 0; cursor: pointer; font-size: 14px; font-weight: bold; margin-right: 4px; }
            .tab-btn.active { background: #4CAF50; color: white; border-color: #4CAF50; }
            .tab-btn:hover:not(.active) { background: #e8e8e8; }
            .tab-content { display: none; }
            .tab-content.active { display: block; }
        </style>
    </head>
    <body>

        <!-- Top action bar -->
        <div class="top-actions">
            <button onclick="saveConfig()">💾 Save Configuration</button>
            <button class="view-btn" onclick="viewMessages()">📋 View Message Queue</button>
            <div id="message"></div>
        </div>

        <!-- Tab navigation -->
        <div class="tabs">
            <button class="tab-btn active" onclick="showTab('settings', this)">⚙️ Settings</button>
            <button class="tab-btn" onclick="showTab('testing', this)">🧪 Testing</button>
        </div>

        <!-- Settings Tab -->
        <div id="tab-settings" class="tab-content active">

            <!-- Two-column: Twilio + FPP Connection -->
            <div class="columns">
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
                    </div>
                </div>
                <div class="column">
                    <div class="section">
                        <h2>FPP Connection</h2>
                        <label>FPP Host URL:</label>
                        <input type="text" id="fpp_host" value="{{ config.fpp_host }}" placeholder="http://127.0.0.1">
                        <p class="help-text">💡 Use http://127.0.0.1 for local FPP, or http://192.168.x.x for remote</p>

                        <button class="test-btn" onclick="testFPP()">🔌 Test FPP Connection</button>
                        <button class="refresh-btn" onclick="refreshFPPData()">🔄 Refresh Playlists/Models/Fonts</button>
                        <div id="fpp_status"></div>
                    </div>

                    <div class="section">
                        <h2>Filters</h2>
                        <input type="checkbox" id="profanity_filter" {{ 'checked' if config.profanity_filter else '' }}>
                        <label class="checkbox-label">✓ Enable Profanity Filter</label><br>
                        <p class="help-text">ℹ️ Rejects messages containing words from blacklist.txt</p>
                        <button class="view-btn" onclick="location.href='/blacklist'" style="margin-top: 6px;">🚫 Manage Blacklist</button>

                        <hr style="border: none; border-top: 1px solid #ddd; margin: 15px 0;">

                        <input type="checkbox" id="use_whitelist" {{ 'checked' if config.get('use_whitelist', False) else '' }}>
                        <label class="checkbox-label">✓ Enable Name Whitelist — only allow approved names</label><br>
                        <p class="help-text">ℹ️ When enabled, only names on the approved list are accepted.</p>

                        <button class="view-btn" onclick="location.href='/whitelist'" style="margin-top: 10px;">📋 Manage Whitelist</button>
                    </div>
                </div>
            </div>

            <div class="section">
                <h2>FPP Display Settings</h2>

                <label>Default "Waiting" Playlist:</label>
                <select id="default_playlist">
                    <option value="">-- None (Manual Control) --</option>
                </select>
                <p class="help-text">📺 This playlist loops while waiting for text messages</p>

                <label>Name Display Playlist:</label>
                <select id="name_display_playlist">
                    <option value="">-- None (No Playlist Change) --</option>
                </select>
                <p class="help-text">🎬 This playlist plays when displaying a name</p>

                <label>Overlay Model Name:</label>
                <select id="overlay_model_name">
                    <option value="">-- None --</option>
                </select>
                <p class="help-text">📝 The pixel overlay model for text (e.g., "Texting Matrix")</p>

                <h3>Text Display Options</h3>
                <label>Message Template:</label>
                <textarea id="message_template" rows="3">{{ config.get('message_template', 'Merry Christmas {name}!') }}</textarea>
                <p class="help-text">💬 Use {name} as placeholder. Press Enter for new lines. Use spaces to shift text position.</p>

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

                <label>Scroll Speed (pixels per second):</label>
                <input type="number" id="scroll_speed" value="{{ config.get('scroll_speed', 20) }}" min="5" max="100">
                <p class="help-text">⚡ Higher = faster scrolling (5=slow, 50=fast)</p>

                <label>Text Position:</label>
                <select id="text_position">
                    <option value="Center" {{ 'selected' if config.get('text_position') == 'Center' else '' }}>Static</option>
                    <option value="L2R" {{ 'selected' if config.get('text_position') == 'L2R' else '' }}>Scroll Left to Right</option>
                    <option value="R2L" {{ 'selected' if config.get('text_position') == 'R2L' else '' }}>Scroll Right to Left</option>
                    <option value="T2B" {{ 'selected' if config.get('text_position') == 'T2B' else '' }}>Scroll Top to Bottom</option>
                    <option value="B2T" {{ 'selected' if config.get('text_position') == 'B2T' else '' }}>Scroll Bottom to Top</option>
                </select>
                <p class="help-text">💡 Choose "Static" for centered text or select scroll direction</p>
            </div>

            <div class="section">
                <h2>Message Settings</h2>
                <label>Display Duration (seconds):</label>
                <input type="number" id="display_duration" value="{{ config.display_duration }}" min="5" max="300">
                <p class="help-text">⏱️ Each message will display for this many seconds before moving to the next</p>

                <label>Max Messages Per Phone (0 = unlimited):</label>
                <input type="number" id="max_messages" value="{{ config.max_messages_per_phone }}" min="0" max="100">

                <label>Max Message Length:</label>
                <input type="number" id="max_length" value="{{ config.max_message_length }}" min="10" max="200">

                <h3>Name Format Rules</h3>
                <input type="checkbox" id="one_word_only" {{ 'checked' if config.get('one_word_only', False) else '' }}>
                <label class="checkbox-label">✓ One Word Only (e.g., "John" ✓, "John Smith" ✗)</label><br>

                <input type="checkbox" id="two_words_max" {{ 'checked' if config.get('two_words_max', True) else '' }}>
                <label class="checkbox-label">✓ Two Words Maximum (e.g., "John Smith" ✓, sentences ✗)</label><br>

                <p class="help-text">ℹ️ Hyphenated names like "Jean-Luc" count as one word. All names are converted to Proper Case.</p>
            </div>

            <div class="section" style="border: 2px solid #2196F3;">
                <h2>📱 SMS Auto-Response Settings</h2>
                <input type="checkbox" id="send_sms_responses" {{ 'checked' if config.get('send_sms_responses', False) else '' }}>
                <label class="checkbox-label">✓ Enable Automatic SMS Responses</label><br>
                <p class="help-text">💡 When enabled, users receive automatic text replies based on their message status</p>

                <h3>Response Messages</h3>

                <label>✅ Success Message (when name is queued):</label>
                <textarea id="response_success" rows="2">{{ config.get('response_success', 'Thanks! Your name will appear on our display soon! 🎄') }}</textarea>

                <label>🚫 Profanity Detected:</label>
                <textarea id="response_profanity" rows="2">{{ config.get('response_profanity', 'Sorry, your message contains inappropriate content and cannot be displayed.') }}</textarea>

                <label>⛔ Rate Limited:</label>
                <textarea id="response_rate_limited" rows="2">{{ config.get('response_rate_limited', "You've reached the maximum number of messages allowed. Please try again tomorrow!") }}</textarea>

                <label>🔄 Duplicate Name:</label>
                <textarea id="response_duplicate" rows="2">{{ config.get('response_duplicate', "You've already sent this name today!") }}</textarea>

                <label>❌ Invalid Format:</label>
                <textarea id="response_invalid_format" rows="2">{{ config.get('response_invalid_format', 'Please send only a name (1-2 words, no sentences).') }}</textarea>

                <label>📋 Not on Whitelist:</label>
                <textarea id="response_not_whitelisted" rows="2">{{ config.get('response_not_whitelisted', 'Sorry, that name is not on our approved list.') }}</textarea>

                <label>🚫 Blocked Number:</label>
                <textarea id="response_blocked" rows="2">{{ config.get('response_blocked', 'You have been blocked from sending messages.') }}</textarea>
            </div>

        </div>

        <!-- Testing Tab -->
        <div id="tab-testing" class="tab-content">

            <div class="section" style="border: 2px solid #FF9800; margin-top: 20px;">
                <h2>🧪 Message Testing</h2>
                <p style="color: #FF9800; font-size: 14px;">
                    ⚠️ Use this to test messages without sending actual texts. Works without Twilio credentials.
                </p>

                <label>Test Name:</label>
                <input type="text" id="test_name" placeholder="Enter a name to test (e.g., John or Mary Smith)">

                <button class="test-btn" onclick="submitTestMessage()">🧪 Submit Test Message</button>

                <div id="test_result" style="margin-top: 10px;"></div>
            </div>

            <div class="section" style="border: 2px solid #FF9800;">
                <h2>🧪 SMS Response Testing</h2>
                <p style="color: #FF9800; font-size: 14px;">
                    ⚠️ Test sending SMS responses to a phone number. Requires Twilio credentials and "Enable Automatic SMS Responses" checked in Settings.
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
            window.onload = function() {
                loadFPPData();
            };

            function showTab(tabName, btn) {
                document.querySelectorAll('.tab-content').forEach(t => t.classList.remove('active'));
                document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
                document.getElementById('tab-' + tabName).classList.add('active');
                btn.classList.add('active');
            }

            function loadFPPData() {
                fetch('/api/fpp/data')
                .then(r => r.json())
                .then(data => {
                    const defaultSelect = document.getElementById('default_playlist');
                    const nameSelect = document.getElementById('name_display_playlist');
                    const currentDefault = "{{ config.get('default_playlist', '') }}";
                    const currentName = "{{ config.get('name_display_playlist', '') }}";

                    defaultSelect.innerHTML = '<option value="">-- None (Manual Control) --</option>';
                    nameSelect.innerHTML = '<option value="">-- None (No Playlist Change) --</option>';

                    if (data.playlists && data.playlists.length > 0) {
                        data.playlists.forEach(playlist => {
                            const opt1 = new Option(playlist, playlist, false, playlist === currentDefault);
                            const opt2 = new Option(playlist, playlist, false, playlist === currentName);
                            defaultSelect.add(opt1);
                            nameSelect.add(opt2);
                        });
                    }

                    const modelSelect = document.getElementById('overlay_model_name');
                    const currentModel = "{{ config.get('overlay_model_name', 'Texting Matrix') }}";
                    modelSelect.innerHTML = '<option value="">-- None --</option>';

                    if (data.models && data.models.length > 0) {
                        data.models.forEach(model => {
                            const opt = new Option(model, model, false, model === currentModel);
                            modelSelect.add(opt);
                        });
                    }

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
                });
            }

            function refreshFPPData() {
                document.getElementById('fpp_status').innerHTML = '<p>Refreshing FPP data...</p>';
                loadFPPData();
                setTimeout(() => {
                    document.getElementById('fpp_status').innerHTML = '<p class="success">✅ Refreshed!</p>';
                }, 1000);
            }

            function testFPP() {
                document.getElementById('fpp_status').innerHTML = '<p>Testing FPP connection...</p>';
                const fppHost = document.getElementById('fpp_host').value;

                fetch('/api/fpp/test', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({fpp_host: fppHost})
                })
                .then(r => r.json())
                .then(data => {
                    if (data.success) {
                        document.getElementById('fpp_status').innerHTML =
                            '<p class="success">✅ FPP connection successful! Status: ' + data.status + '</p>';
                    } else {
                        document.getElementById('fpp_status').innerHTML =
                            '<p class="error">❌ Connection failed: ' + data.error + '</p>';
                    }
                });
            }

            function saveConfig() {
                const data = {
                    enabled: document.getElementById('enabled').checked,
                    twilio_account_sid: document.getElementById('account_sid').value,
                    twilio_auth_token: document.getElementById('auth_token').value,
                    twilio_phone_number: document.getElementById('phone_number').value,
                    poll_interval: parseInt(document.getElementById('poll_interval').value),
                    display_duration: parseInt(document.getElementById('display_duration').value),
                    max_messages_per_phone: parseInt(document.getElementById('max_messages').value),
                    max_message_length: parseInt(document.getElementById('max_length').value),
                    one_word_only: document.getElementById('one_word_only').checked,
                    two_words_max: document.getElementById('two_words_max').checked,
                    profanity_filter: document.getElementById('profanity_filter').checked,
                    use_whitelist: document.getElementById('use_whitelist').checked,
                    fpp_host: document.getElementById('fpp_host').value,
                    default_playlist: document.getElementById('default_playlist').value,
                    name_display_playlist: document.getElementById('name_display_playlist').value,
                    overlay_model_name: document.getElementById('overlay_model_name').value,
                    text_color: document.getElementById('text_color_hex').value,
                    text_font: document.getElementById('text_font').value,
                    text_font_size: parseInt(document.getElementById('text_font_size').value),
                    scroll_speed: parseInt(document.getElementById('scroll_speed').value),
                    text_position: document.getElementById('text_position').value,
                    message_template: document.getElementById('message_template').value,
                    send_sms_responses: document.getElementById('send_sms_responses').checked,
                    response_success: document.getElementById('response_success').value,
                    response_profanity: document.getElementById('response_profanity').value,
                    response_rate_limited: document.getElementById('response_rate_limited').value,
                    response_duplicate: document.getElementById('response_duplicate').value,
                    response_invalid_format: document.getElementById('response_invalid_format').value,
                    response_not_whitelisted: document.getElementById('response_not_whitelisted').value,
                    response_blocked: document.getElementById('response_blocked').value
                };

                fetch('/api/config', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify(data)
                })
                .then(r => r.json())
                .then(data => {
                    document.getElementById('message').innerHTML = '<p class="success">✅ Configuration saved! Plugin will restart.</p>';
                    setTimeout(() => location.reload(), 2000);
                });
            }

            function testConnection() {
                document.getElementById('message').innerHTML = '<p>Testing Twilio connection...</p>';
                fetch('/api/test')
                .then(r => r.json())
                .then(data => {
                    if (data.success) {
                        document.getElementById('message').innerHTML = '<p class="success">✅ Twilio connection successful!</p>';
                    } else {
                        document.getElementById('message').innerHTML = '<p class="error">❌ Connection failed: ' + data.error + '</p>';
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
    try:
        playlists = get_fpp_playlists()
        sequences = get_fpp_sequences()
        models = get_fpp_models()
        fonts = get_fpp_fonts()
        
        return jsonify({
            "playlists": playlists,
            "sequences": sequences,
            "models": models,
            "fonts": fonts
        })
    except Exception as e:
        return jsonify({"error": str(e)})

@app.route('/api/fpp/test', methods=['POST'])
def test_fpp_api():
    try:
        data = request.json
        old_host = config.get('fpp_host')
        config['fpp_host'] = data.get('fpp_host', 'http://127.0.0.1')
        
        success, status = test_fpp_connection()
        config['fpp_host'] = old_host
        
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
        test_phone = data.get('phone', '+15555550000')
        
        logging.info(f"🧪 ===== TEST MESSAGE RECEIVED =====")
        logging.info(f"🧪 Raw input: '{test_name}'")
        
        if not test_name:
            logging.warning(f"🧪 TEST FAILED: Name is empty")
            return jsonify({"success": False, "error": "Name is required"})
        
        test_name = extract_name(test_name)
        logging.info(f"🧪 Extracted name (proper case): '{test_name}'")
        
        logging.info(f"🧪 Checking name format...")
        is_valid, validation_msg = is_valid_name(test_name)
        logging.info(f"🧪 Name format valid: {is_valid}")
        
        if not is_valid:
            logging.warning(f"🧪 TEST FAILED: Invalid format - {validation_msg}")
            return jsonify({"success": False, "error": validation_msg, "reason": "invalid_format"})
        
        logging.info(f"🧪 Checking whitelist...")
        if not is_on_whitelist(test_name):
            logging.warning(f"🧪 TEST FAILED: Not on whitelist")
            return jsonify({"success": False, "error": "Name not on whitelist", "reason": "not_on_whitelist"})
        
        logging.info(f"🧪 Checking profanity...")
        if config['profanity_filter'] and contains_profanity(test_name):
            logging.warning(f"🧪 TEST FAILED: Profanity detected")
            return jsonify({"success": False, "error": "Profanity detected", "reason": "profanity"})
        
        logging.info(f"🧪 Adding to queue...")
        success = add_to_queue(test_name, test_phone, f"TEST: {test_name}")
        logging.info(f"🧪 Add to queue result: {success}")
        
        if success:
            logging.info(f"🧪 ✅ TEST MESSAGE QUEUED: {test_name}")
            log_message(test_phone, f"TEST: {test_name}", test_name, "queued")
            return jsonify({"success": True, "message": f"Test message '{test_name}' queued successfully!"})
        else:
            logging.error(f"🧪 ❌ TEST FAILED: Could not add to queue")
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
        
        logging.info(f"🧪 TEST SMS: Sending '{message_type}' response to {phone}")
        
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
        file_exists = os.path.exists(BLACKLIST_FILE)
        words = load_blacklist_words()
        return jsonify({"blacklist": words, "file_path": BLACKLIST_FILE, "file_exists": file_exists})
    except Exception as e:
        return jsonify({"error": str(e), "file_path": BLACKLIST_FILE, "file_exists": os.path.exists(BLACKLIST_FILE)})

@app.route('/api/blacklist/add', methods=['POST'])
def api_add_blacklist():
    global _blacklist_cache, _blacklist_mtime
    try:
        data = request.json
        word = data.get('word', '').strip().lower()
        if not word:
            return jsonify({"success": False, "error": "Word is required"})
        words = set()
        if os.path.exists(BLACKLIST_FILE):
            with open(BLACKLIST_FILE, 'r', encoding='latin-1') as f:
                words = {line.strip().lower() for line in f if line.strip()}
        if word in words:
            return jsonify({"success": False, "error": "Word already in blacklist"})
        words.add(word)
        with open(BLACKLIST_FILE, 'w', encoding='utf-8') as f:
            f.write('\n'.join(sorted(words)) + '\n')
        # If word was previously removed, un-remove it
        removed = load_blacklist_removed()
        if word in removed:
            removed.discard(word)
            with open(BLACKLIST_REMOVED_FILE, 'w', encoding='utf-8') as f:
                f.write('\n'.join(sorted(removed)) + '\n')
        _blacklist_cache = None
        _blacklist_mtime = None
        logging.info(f"Added '{word}' to blacklist")
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})

@app.route('/api/blacklist/remove', methods=['POST'])
def api_remove_blacklist():
    global _blacklist_cache, _blacklist_mtime
    try:
        data = request.json
        word = data.get('word', '').strip().lower()
        words = set()
        if os.path.exists(BLACKLIST_FILE):
            with open(BLACKLIST_FILE, 'r', encoding='latin-1') as f:
                words = {line.strip().lower() for line in f if line.strip()}
        words.discard(word)
        with open(BLACKLIST_FILE, 'w', encoding='utf-8') as f:
            f.write('\n'.join(sorted(words)) + '\n' if words else '')
        # Track removal so git pull can't re-add it
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
        names = []
        file_exists = os.path.exists(WHITELIST_FILE)
        if file_exists:
            with open(WHITELIST_FILE, 'r', encoding='latin-1') as f:
                names = sorted([line.strip() for line in f if line.strip() and not line.startswith('#')])
        return jsonify({"whitelist": names, "file_path": WHITELIST_FILE, "file_exists": file_exists})
    except Exception as e:
        return jsonify({"error": str(e), "file_path": WHITELIST_FILE, "file_exists": os.path.exists(WHITELIST_FILE)})

@app.route('/api/whitelist/add', methods=['POST'])
def api_add_whitelist():
    global _whitelist_cache, _whitelist_mtime
    try:
        data = request.json
        name = data.get('name', '').strip().lower()
        if not name:
            return jsonify({"success": False, "error": "Name is required"})
        names = set()
        if os.path.exists(WHITELIST_FILE):
            with open(WHITELIST_FILE, 'r', encoding='latin-1') as f:
                names = {line.strip().lower() for line in f if line.strip()}
        if name in names:
            return jsonify({"success": False, "error": "Name already in whitelist"})
        names.add(name)
        with open(WHITELIST_FILE, 'w', encoding='utf-8') as f:
            f.write('\n'.join(sorted(names)) + '\n')
        # If name was previously removed, un-remove it so it isn't filtered out
        removed = load_removed_names()
        if name in removed:
            removed.discard(name)
            with open(WHITELIST_REMOVED_FILE, 'w', encoding='utf-8') as f:
                f.write('\n'.join(sorted(removed)) + '\n')
        _whitelist_cache = None
        _whitelist_mtime = None
        logging.info(f"Added '{name}' to whitelist")
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})

@app.route('/api/whitelist/remove', methods=['POST'])
def api_remove_whitelist():
    global _whitelist_cache, _whitelist_mtime
    try:
        data = request.json
        name = data.get('name', '').strip().lower()
        names = set()
        if os.path.exists(WHITELIST_FILE):
            with open(WHITELIST_FILE, 'r', encoding='latin-1') as f:
                names = {line.strip().lower() for line in f if line.strip()}
        names.discard(name)
        with open(WHITELIST_FILE, 'w', encoding='utf-8') as f:
            f.write('\n'.join(sorted(names)) + '\n' if names else '')
        # Track removal so git pull can't re-add this name
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
    <body>
        <h1>Global Name Whitelist</h1>
        <div class="info">
            Only names on this list are accepted when the whitelist is enabled. &nbsp;|&nbsp; <strong id="count">Loading...</strong><br>
            <span id="filepath" style="font-size:12px; color:#555;"></span>
        </div>
        <button onclick="location.href='/'">← Back to Config</button>
        <button onclick="location.href='/messages'">📬 View Messages</button>

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

        <script>
            var allNames = [];

            function loadWhitelist() {
                document.getElementById('count').textContent = 'Loading...';
                fetch('/api/whitelist')
                .then(r => r.json())
                .then(data => {
                    allNames = data.whitelist || [];
                    document.getElementById('count').textContent = allNames.length.toLocaleString() + ' approved names';
                    document.getElementById('filepath').textContent = 'File: ' + data.file_path + (data.file_exists ? ' ✓' : ' ✗ NOT FOUND');
                    renderTable();
                })
                .catch(() => {
                    document.getElementById('count').textContent = 'Error loading';
                });
            }

            function renderTable() {
                const query = document.getElementById('search').value.trim().toLowerCase();
                const area = document.getElementById('list_area');
                const hint = document.getElementById('hint');

                if (allNames.length === 0) {
                    hint.textContent = '';
                    area.innerHTML = '<div class="empty"><h3>No names in whitelist yet</h3><p>Add names above to approve them.</p></div>';
                    return;
                }

                let filtered = query
                    ? allNames.filter(n => n.toLowerCase().includes(query))
                    : allNames;

                const LIMIT = 100;
                const showing = filtered.slice(0, LIMIT);

                if (query) {
                    hint.textContent = filtered.length === 0
                        ? 'No names match "' + query + '"'
                        : 'Showing ' + Math.min(filtered.length, LIMIT) + ' of ' + filtered.length + ' matches';
                } else {
                    hint.textContent = 'Showing first ' + showing.length + ' of ' + allNames.length.toLocaleString() + ' names — use search to find specific names';
                }

                if (filtered.length === 0) {
                    area.innerHTML = '<div class="empty"><p>No names match your search.</p></div>';
                    return;
                }

                let rows = '';
                for (let i = 0; i < showing.length; i += 2) {
                    const a = showing[i];
                    const b = showing[i + 1];
                    rows += `<tr>` +
                        `<td style="text-transform:capitalize">${a}</td>` +
                        `<td><button class="remove-btn" onclick="removeName('${a}')">✕ Remove</button></td>` +
                        (b
                            ? `<td style="text-transform:capitalize">${b}</td><td><button class="remove-btn" onclick="removeName('${b}')">✕ Remove</button></td>`
                            : `<td></td><td></td>`) +
                        `</tr>`;
                }
                area.innerHTML = '<table><tr><th>Name</th><th></th><th>Name</th><th></th></tr>' + rows + '</table>';
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
    return render_template_string(html)

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
    <body>
        <h1>🚫 Profanity Blacklist</h1>
        <div class="info">
            ℹ️ Messages containing any word on this list are rejected by the profanity filter. &nbsp;|&nbsp; <strong id="count">Loading...</strong><br>
            <span id="filepath" style="font-size:12px; color:#555;"></span>
        </div>
        <button onclick="location.href='/'">← Back to Config</button>
        <button onclick="location.href='/messages'">📬 View Messages</button>

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

        <script>
            var allWords = [];

            function loadBlacklist() {
                document.getElementById('count').textContent = 'Loading...';
                fetch('/api/blacklist')
                .then(r => r.json())
                .then(data => {
                    allWords = data.blacklist || [];
                    document.getElementById('count').textContent = allWords.length.toLocaleString() + ' blocked words';
                    document.getElementById('filepath').textContent = 'File: ' + data.file_path + (data.file_exists ? ' ✓' : ' ✗ NOT FOUND');
                    renderTable();
                })
                .catch(() => {
                    document.getElementById('count').textContent = 'Error loading';
                });
            }

            function renderTable() {
                const query = document.getElementById('search').value.trim().toLowerCase();
                const area = document.getElementById('list_area');
                const hint = document.getElementById('hint');

                if (allWords.length === 0) {
                    hint.textContent = '';
                    area.innerHTML = '<div class="empty"><h3>No words in blacklist yet</h3><p>Add words above to block them.</p></div>';
                    return;
                }

                let filtered = query
                    ? allWords.filter(w => w.toLowerCase().includes(query))
                    : allWords;

                const LIMIT = 100;
                const showing = filtered.slice(0, LIMIT);

                if (query) {
                    hint.textContent = filtered.length === 0
                        ? 'No words match "' + query + '"'
                        : 'Showing ' + Math.min(filtered.length, LIMIT) + ' of ' + filtered.length + ' matches';
                } else {
                    hint.textContent = 'Showing first ' + showing.length + ' of ' + allWords.length.toLocaleString() + ' words — use search to find specific words';
                }

                if (filtered.length === 0) {
                    area.innerHTML = '<div class="empty"><p>No words match your search.</p></div>';
                    return;
                }

                let rows = '';
                for (let i = 0; i < showing.length; i += 2) {
                    const a = showing[i];
                    const b = showing[i + 1];
                    rows += `<tr>` +
                        `<td>${a}</td>` +
                        `<td><button class="remove-btn" onclick="removeWord('${a}')">✕ Remove</button></td>` +
                        (b
                            ? `<td>${b}</td><td><button class="remove-btn" onclick="removeWord('${b}')">✕ Remove</button></td>`
                            : `<td></td><td></td>`) +
                        `</tr>`;
                }
                area.innerHTML = '<table><tr><th>Word</th><th></th><th>Word</th><th></th></tr>' + rows + '</table>';
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
    return render_template_string(html)

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
    <body>
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
    <body>
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
    <body>
        <h1>📋 Message History & Queue Status</h1>

        <div class="queue-info">
            • Messages are queued and displayed one at a time<br>
            • Each message displays for the full display duration<br>
            • View real-time queue status
        </div>
        
        <div class="info">
            Auto-refreshes every 5 seconds | Total Messages: {{ messages|length }}
        </div>
        <button onclick="location.href='/'">← Back to Config</button>
        <button onclick="location.reload()">🔄 Refresh</button>
        <button onclick="location.href='/whitelist'" style="background: #4CAF50;">📋 Manage Whitelist</button>
        <button onclick="location.href='/blocklist'" style="background: #f44336;">🚫 View Blocklist</button>
        <button class="clear-btn" onclick="clearHistory()">🗑️ Clear All Messages</button>
        
        <div class="queue-box">
            <h2>🎬 Current Display Queue</h2>
            {% if queue_status.currently_displaying %}
            <div class="current-display">
                🎄 NOW DISPLAYING: {{ queue_status.currently_displaying.name }} 
                (from ***{{ queue_status.currently_displaying.phone_last4 }})
            </div>
            {% else %}
            <div class="current-display" style="background: #bdbdbd; color: #333;">
                💤 Nothing currently displaying
            </div>
            {% endif %}
            
            {% if queue_status.queue_length > 0 %}
            <h3 style="color: #FF9800; margin-top: 20px;">📋 Queue ({{ queue_status.queue_length }} waiting):</h3>
            {% for item in queue_status.queue %}
            <div class="queue-item">
                <strong>Queue Position {{ loop.index }}:</strong> {{ item.name }} 
                (from ***{{ item.phone_last4 }})
            </div>
            {% endfor %}
            {% else %}
            <p style="color: #aaa; font-style: italic; margin-top: 15px;">Queue is empty - waiting for messages</p>
            {% endif %}
        </div>
        
        <h2>📜 Complete Message History</h2>
        {% if messages|length == 0 %}
        <div style="background: #333; padding: 40px; text-align: center; border-radius: 5px;">
            <h3>No messages yet</h3>
        </div>
        {% else %}
        <table>
            <tr>
                <th>Timestamp</th>
                <th>Phone</th>
                <th>Message</th>
                <th>Name</th>
                <th>Status</th>
                <th>Action</th>
            </tr>
            {% for msg in messages %}
            <tr class="{{ msg.status }}">
                <td>{{ msg.timestamp }}</td>
                <td>{{ msg.phone }}</td>
                <td>{{ msg.message }}</td>
                <td>{{ msg.extracted_name }}</td>
                <td class="{{ msg.status }}">
                    {% if msg.status == 'displaying' %}
                        🎬 DISPLAYING NOW
                    {% elif msg.status == 'queued' %}
                        📋 Queued
                    {% elif msg.status == 'displayed' %}
                        ✅ Displayed
                    {% else %}
                        {{ msg.status }}
                    {% endif %}
                </td>
                <td>
                    {% if msg.status != 'blocked' %}
                    <button class="block-btn" onclick="blockPhone('{{ msg.phone_full }}')">🚫 Block</button>
                    {% endif %}
                </td>
            </tr>
            {% endfor %}
        </table>
        {% endif %}
        
        <script>
            setTimeout(function() { location.reload(); }, 5000);
            
            function blockPhone(phone) {
                if (confirm('Block ' + phone + ' from sending messages?')) {
                    fetch('/api/phone/block', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({phone: phone})
                    })
                    .then(r => r.json())
                    .then(data => {
                        if (data.success) {
                            alert('✅ Phone number blocked!');
                            location.reload();
                        }
                    });
                }
            }
            
            function clearHistory() {
                if (confirm('Clear all message history?')) {
                    fetch('/api/messages/clear', { method: 'POST' })
                    .then(r => r.json())
                    .then(data => {
                        if (data.success) {
                            alert('✅ Message history cleared!');
                            location.reload();
                        }
                    });
                }
            }
        </script>
    </body>
    </html>
    """
    return render_template_string(html, messages=list(reversed(messages)), queue_status=queue_status)

if __name__ == '__main__':
    load_config()

    # Display worker always runs so Testing Tools work without Twilio credentials
    display_thread = threading.Thread(target=display_worker, daemon=True)
    display_thread.start()

    # Polling thread only starts when plugin is enabled (requires Twilio credentials)
    if config['enabled']:
        polling_thread = threading.Thread(target=poll_twilio, daemon=True)
        polling_thread.start()

    logging.info("FPP SMS Plugin v2.5 starting...")
    app.run(host='0.0.0.0', port=5000, debug=False)
