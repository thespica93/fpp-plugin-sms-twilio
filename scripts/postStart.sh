#!/bin/bash
###############################################################################
# FPP SMS Twilio Plugin - Post Start Script
# This runs automatically after FPPD starts
###############################################################################

PLUGIN_DIR="/home/fpp/media/plugins/fpp-plugin-sms-twilio"

# Stop any existing service
pkill -f sms_plugin.py 2>/dev/null || true
sleep 1

# Start the service as fpp user
cd "$PLUGIN_DIR"
su fpp -c "cd '$PLUGIN_DIR' && nohup python3 sms_plugin.py > /home/fpp/media/logs/sms_plugin.log 2>&1 &"

exit 0
