#!/bin/bash
# FPP SMS Twilio Plugin - Deactivate
# Disables the plugin, stops SMS polling, and stops the current playlist/sequence.
# FPP Scheduler: Command → Run Script → TwilioStop

PLUGIN_URL="http://127.0.0.1:5000/api/deactivate"

RESPONSE=$(curl -s -w "\n%{http_code}" -X POST "$PLUGIN_URL" 2>&1)
HTTP_CODE=$(echo "$RESPONSE" | tail -1)
BODY=$(echo "$RESPONSE" | head -1)

if [ "$HTTP_CODE" = "200" ]; then
    echo "TwilioStop OK: $BODY"
    exit 0
else
    echo "TwilioStop ERROR ($HTTP_CODE): $BODY"
    exit 1
fi
