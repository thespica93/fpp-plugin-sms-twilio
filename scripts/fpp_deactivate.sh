#!/bin/bash
# FPP SMS Twilio Plugin - Deactivate
# Disables the plugin and stops SMS polling.
# Add this to the FPP Scheduler: Command → Run Script → TwilioStop
curl -s -X POST http://127.0.0.1:5000/api/deactivate
