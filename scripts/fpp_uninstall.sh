#!/bin/bash
###############################################################################
# FPP SMS Twilio Plugin - FPP Uninstall Script
###############################################################################

echo "Uninstalling FPP SMS Twilio Plugin..."

# Stop the service
pkill -f sms_plugin.py 2>/dev/null || true

echo "✅ Plugin uninstalled"
exit 0
