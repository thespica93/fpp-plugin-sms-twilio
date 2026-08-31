#!/bin/bash
###############################################################################
# FPP SMS Twilio Plugin (beta) - Post Start Script
# This runs automatically after FPPD starts
###############################################################################

# Resolve this plugin's own install directory dynamically, same as fpp_install.sh —
# lets this script work under whatever directory FPP installed this variant into.
PLUGIN_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Stop any existing instance of THIS variant only (full path, not bare filename,
# so a co-installed stable variant's own sms_plugin.py is never touched)
pkill -f "$PLUGIN_DIR/sms_plugin.py" 2>/dev/null || true
sleep 1

# Grant fpp user write access to FPP shared memory files (created by root on FPPD start)
chmod 666 /dev/shm/FPP-Model-Data-* 2>/dev/null || true

# Start the service as fpp user
cd "$PLUGIN_DIR"
su fpp -c "cd '$PLUGIN_DIR' && nohup python3 sms_plugin.py > /dev/null 2>/home/fpp/media/logs/sms_plugin_beta.log &"

exit 0
