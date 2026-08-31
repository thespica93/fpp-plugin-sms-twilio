#!/bin/bash
###############################################################################
# FPP SMS Twilio Plugin (beta) - Comprehensive Uninstall Script
###############################################################################

# Resolve this plugin's own install directory dynamically, same as fpp_install.sh
PLUGIN_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

LOG="/home/fpp/media/logs/sms_plugin_uninstall_beta.log"

# Function to log and display
log_and_show() {
    echo "$1" | tee -a "$LOG"
}

log_and_show "========================================"
log_and_show "FPP SMS Twilio Plugin Uninstaller (beta)"
log_and_show "$(date)"
log_and_show "========================================"

# Stop the service — matched by full path so a co-installed stable variant's own
# sms_plugin.py process is never touched.
log_and_show "Stopping SMS Twilio service (beta)..."
pkill -f "$PLUGIN_DIR/sms_plugin.py" 2>/dev/null || true
sleep 2
log_and_show "✓ Service stopped"

# NOTE: Flask/Twilio/Requests are intentionally NOT pip-uninstalled here. They are
# shared, system-wide Python packages (not per-plugin, not reference-counted) — a
# co-installed stable variant (or any other plugin) may still depend on them, so
# removing them here could break something else on the box.

# Remove this variant's own scheduler scripts
log_and_show "Removing scheduler scripts..."
rm -f /home/fpp/media/scripts/TwilioStartBeta.sh
rm -f /home/fpp/media/scripts/TwilioStopBeta.sh
log_and_show "✓ Scheduler scripts removed"

# Remove this variant's own sudoers rule (shm chmod access) — suffixed "-beta" so
# this never removes a co-installed stable variant's rule.
log_and_show "Removing sudoers rule..."
rm -f /etc/sudoers.d/90-fpp-sms-shm-beta
log_and_show "✓ Sudoers rule removed"

# Remove this variant's own transient/install log files (its runtime config, log,
# and message history under PLUGIN_DATA_DIR are intentionally left in place so a
# reinstall doesn't lose them).
log_and_show "Removing log files..."
rm -f /home/fpp/media/logs/sms_plugin_beta.log
rm -f /home/fpp/media/logs/sms_plugin_install_beta.log
log_and_show "✓ Log files removed"

log_and_show "========================================"
log_and_show "✅ Uninstall complete!"
log_and_show "========================================"

# No errors — remove the uninstall log, it's only useful for debugging failures
rm -f "$LOG"

exit 0
