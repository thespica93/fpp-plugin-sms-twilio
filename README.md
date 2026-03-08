# FPP SMS Twilio Plugin

> Let your audience text their name and watch it light up on your display!

The FPP SMS Twilio Plugin connects your [Falcon Player (FPP)](https://falconchristmas.com/) LED display to Twilio's SMS platform. Visitors text their name to your Twilio number, and it appears on your pixel display — no interruptions, no bad words, fully automatic.

---

## How It Works

```
Visitor texts name → Twilio → Plugin polls & receives → Filters applied → Queue → FPP Display
```

1. A visitor texts a name to your Twilio phone number
2. The plugin polls Twilio for new messages every few seconds
3. The name passes through the profanity filter, whitelist, and rate limiter
4. If approved, it joins the queue and displays on your LED overlay when it's its turn
5. An optional SMS reply is sent back to the visitor

---

## Documentation

| Page | Description |
|------|-------------|
| [Twilio Setup](docs/01-twilio-setup.md) | Create a Twilio account, get a phone number, and configure credentials |
| [Installation](docs/02-installation.md) | Install the plugin on your FPP device |
| [Plugin Configuration](docs/03-plugin-configuration.md) | Full reference for all settings |
| [Message Queue](docs/04-message-queue.md) | How the queue works, monitoring, and manual controls |
| [Whitelist](docs/05-whitelist.md) | Only allow pre-approved names on your display |
| [Profanity Blacklist](docs/06-blacklist.md) | Block specific words from appearing |
| [Phone Blocklist](docs/07-phone-blocklist.md) | Block specific phone numbers |
| [SMS Auto-Responses](docs/08-sms-responses.md) | Customize the replies sent back to texters |
| [Troubleshooting](docs/09-troubleshooting.md) | Common problems and solutions |

---

## Quick Start

1. **[Create a Twilio account](docs/01-twilio-setup.md)** and get a phone number
2. **[Install the plugin](docs/02-installation.md)** via FPP Plugin Manager
3. Open `http://YOUR_FPP_IP:5000` and enter your Twilio credentials
4. Set your FPP display settings (playlist, overlay model, font)
5. Save and test — text your Twilio number with a name!

---

## Features

- **Message Queue** — Names queue up and display one at a time, no overlap
- **Profanity Filter** — Customizable blocked word list (includes a large default list)
- **Name Whitelist** — Optional: only show pre-approved names (22,000+ included)
- **Phone Blocklist** — Block specific numbers from texting in
- **Rate Limiting** — Limit how many texts one number can send per day
- **Duplicate Detection** — Prevents the same name from the same number appearing twice in a day
- **SMS Auto-Responses** — Automatic replies for success, rejection, rate limiting, etc.
- **Web UI** — Full browser-based configuration at port 5000
- **FPP Integration** — Controls playlists and overlay models via FPP's API

---

## Requirements

- FPP (Falcon Player) v6.0 or higher
- Twilio account (free trial available)
- Internet connection on your FPP device
- An LED matrix configured as an FPP Overlay Model

---

## File Locations

| File | Path |
|------|------|
| Plugin directory | `/opt/fpp/plugins/fpp-plugin-sms-twilio/` |
| Configuration | `/home/fpp/media/config/plugin.fpp-sms-twilio.json` |
| Whitelist | `[plugin dir]/whitelist.txt` |
| Blacklist | `[plugin dir]/blacklist.txt` |
| Blocked phones | `[plugin dir]/blocked_phones.json` |
| Log file | `/home/fpp/media/logs/sms_plugin.log` |
| Message history | `/home/fpp/media/logs/received_messages.json` |

---

## Twilio Pricing

| Item | Cost |
|------|------|
| Phone number rental | ~$1.00/month |
| Incoming SMS | ~$0.0075/message |
| Outgoing SMS (responses) | ~$0.0079/message |

**Example:** 200 incoming texts + auto-responses ≈ **$4.16/month**

---

## Support

- **Issues:** [GitHub Issues](https://github.com/thespica93/fpp-plugin-sms-twilio/issues)
- **FPP Community:** [FalconChristmas.com Forums](https://falconchristmas.com/)
- **Logs:** `/home/fpp/media/logs/sms_plugin.log`

---

## License

GPL v3 — Free to use, modify, and share.

Built for the FPP Christmas light display community! 🎄
