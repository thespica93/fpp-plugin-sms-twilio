# FPP SMS Twilio Plugin

Allow viewers to text names that appear on your Christmas light display using Twilio SMS!

## Features

- 📱 SMS Integration via Twilio
- 🎄 Message queueing system (no interruptions!)
- 🚫 Profanity filter with customizable blacklist
- ✅ Optional name whitelist
- 📲 Automatic SMS responses
- 🎨 Full FPP integration (playlists, overlays, text effects)
- ⚡ Optimized performance with caching
- 🔧 Web-based configuration UI

## Installation

### Via FPP Plugin Manager (Recommended)

1. In FPP, go to **Content Setup** → **Plugin Manager**
2. Search for "SMS Twilio"
3. Click **Install**
4. Plugin will automatically install dependencies

### Manual Installation

```bash
cd /opt/fpp/plugins
git clone https://github.com/YOUR_USERNAME/fpp-plugin-sms-twilio.git
cd fpp-plugin-sms-twilio
sudo ./install.sh
```

## Requirements

- FPP (Falcon Player) v6.0 or higher
- Twilio Account (free trial available at twilio.com)
- Internet connection

## Setup

### 1. Get Twilio Credentials

1. Sign up at [twilio.com](https://www.twilio.com/)
2. Get a phone number (~$1/month)
3. Copy your Account SID and Auth Token from the dashboard

### 2. Configure Plugin

1. Access the plugin UI: `http://YOUR_FPP_IP:5000`
2. Enter your Twilio credentials
3. Configure FPP settings (playlist, overlay model, etc.)
4. Check "Enable Plugin"
5. Save configuration

### 3. Test

- Use the built-in testing tool to send a test name
- Or text your Twilio number with a name
- Watch it appear on your display!

## Configuration

Access the web interface at: `http://YOUR_FPP_IP:5000`

### Twilio Settings
- **Account SID**: From Twilio dashboard
- **Auth Token**: From Twilio dashboard  
- **Phone Number**: Format: +18005551234

### Display Settings
- **Name Display Playlist**: Playlist that runs when showing names
- **Overlay Model**: LED matrix for text display
- **Message Template**: Customize display text (use `{name}`)
- **Display Duration**: How long each name shows (seconds)

### Filtering
- **Profanity Filter**: Enable/disable curse word blocking
- **Whitelist**: Only allow pre-approved names (optional)
- **Rate Limiting**: Max messages per phone number per day

### SMS Responses
- **Enable Auto-Responses**: Send automatic replies to texters
- Customize all response messages in the web interface

## File Locations

```
Plugin:       /opt/fpp/plugins/fpp-plugin-sms-twilio/
Config:       /home/fpp/media/config/plugin.fpp-sms-twilio.json
Blacklist:    /home/fpp/media/config/blacklist.txt
Whitelist:    /home/fpp/media/config/whitelist.txt
Logs:         /home/fpp/media/logs/sms_plugin.log
```

## Usage

### Starting/Stopping

The plugin automatically starts when enabled in FPP.

**Via FPP:**
- Enable/Disable in Plugin Manager

**Via Command Line:**
```bash
# Check status
curl "http://localhost/plugin.php?plugin=fpp-plugin-sms-twilio&page=plugin_setup.php&status"

# Start
curl "http://localhost/plugin.php?plugin=fpp-plugin-sms-twilio&page=plugin_setup.php&enable"

# Stop
curl "http://localhost/plugin.php?plugin=fpp-plugin-sms-twilio&page=plugin_setup.php&disable"
```

### Monitoring

- **Web Interface**: `http://YOUR_FPP_IP:5000/messages`
- **Logs**: `/home/fpp/media/logs/sms_plugin.log`
- **Message Queue**: View real-time queue status in web interface

## Customization

### Edit Blacklist
```bash
sudo nano /home/fpp/media/config/blacklist.txt
```
Add one word per line (case-insensitive)

### Edit Whitelist
```bash
sudo nano /home/fpp/media/config/whitelist.txt
```
Add approved names (one per line, case-insensitive)

### SMS Response Messages
Edit all messages directly in the web interface under "SMS Auto-Response Settings"

## Troubleshooting

### Plugin won't start
```bash
# Check logs
tail -f /home/fpp/media/logs/sms_plugin.log

# Check if Python packages installed
pip3 list | grep -E "flask|twilio|requests"

# Reinstall dependencies
cd /opt/fpp/plugins/fpp-plugin-sms-twilio
sudo ./install.sh
```

### Can't access web interface
- Verify plugin is running (check FPP Plugin Manager)
- Try: `http://YOUR_FPP_IP:5000`
- Check firewall: `sudo ufw allow 5000`

### Messages not received
- Verify Twilio credentials are correct
- Check phone number format: +18005551234
- Ensure "Enable Plugin" is checked
- Check logs for errors

## Costs

### Twilio Pricing (as of 2024)
- **Phone Number**: ~$1.00/month
- **Incoming SMS**: $0.0075 per message
- **Outgoing SMS** (responses): $0.0079 per message

### Example Monthly Cost
- 200 texts received = $1.50
- Phone rental = $1.00
- **Total: ~$2.50/month**

(Add ~$1.58 if SMS responses enabled)

## Support

- **Issues**: [GitHub Issues](https://github.com/YOUR_USERNAME/fpp-plugin-sms-twilio/issues)
- **FPP Forums**: [FalconChristmas.com](https://falconchristmas.com/)
- **Logs**: Check `/home/fpp/media/logs/sms_plugin.log`

## Version History

**v2.5** (Current)
- SMS auto-response system
- Optimized list caching (10-100x faster)
- SMS testing tool
- Blocked phone numbers feature

**v2.4**
- Message queueing
- Multi-line templates
- Font selection

## License

GPL v3 - Free to use and modify

## Credits

Built for the FPP Christmas light display community! 🎄

Merry Christmas! 🎅

---

## Development

To contribute or modify:

```bash
git clone https://github.com/YOUR_USERNAME/fpp-plugin-sms-twilio.git
cd fpp-plugin-sms-twilio
# Make changes
# Test on FPP
git commit -am "Description"
git push
```

## Architecture

```
fpp-plugin-sms-twilio/
├── plugin_setup.php      # FPP plugin configuration
├── sms_plugin.py         # Main Python service
├── install.sh           # Dependency installer
├── README.md            # This file
├── LICENSE              # GPL v3
└── requirements.txt     # Python dependencies
```

The plugin runs as a separate Python Flask service on port 5000 and integrates with FPP via HTTP API calls.
