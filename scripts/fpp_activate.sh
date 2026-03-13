#!/bin/bash
# FPP SMS Twilio Plugin - Activate
# Enables the plugin, starts SMS polling, and starts the default waiting playlist.
# FPP Scheduler: Command → Run Script → TwilioStart

PLUGIN_URL="http://127.0.0.1:5000/api/activate"

# Wait up to 15s for the plugin service to be ready (in case FPP just started)
for i in $(seq 1 5); do
    RESPONSE=$(curl -s -w "\n%{http_code}" -X POST "$PLUGIN_URL" 2>&1)
    HTTP_CODE=$(echo "$RESPONSE" | tail -1)
    BODY=$(echo "$RESPONSE" | head -1)
    if [ "$HTTP_CODE" = "200" ]; then
        echo "TwilioStart OK: $BODY"
        exit 0
    fi
    # 400 = configuration error (e.g. no default playlist) — don't retry, fail immediately
    if [ "$HTTP_CODE" = "400" ]; then
        ERROR=$(echo "$BODY" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('error','Unknown error'))" 2>/dev/null || echo "$BODY")
        echo "TwilioStart FAILED: $ERROR"
        exit 1
    fi
    echo "TwilioStart: plugin not ready (attempt $i), retrying in 3s..."
    sleep 3
done

echo "TwilioStart ERROR: could not reach plugin at $PLUGIN_URL"
exit 1
