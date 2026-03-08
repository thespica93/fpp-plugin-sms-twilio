# Twilio Setup

> This guide walks you through creating a Twilio account, purchasing a phone number, and collecting the credentials needed for the plugin.

---

## Table of Contents

- [Step 1 — Create a Twilio Account](#step-1--create-a-twilio-account)
- [Step 2 — Get Your Credentials](#step-2--get-your-credentials)
- [Step 3 — Purchase a Phone Number](#step-3--purchase-a-phone-number)
- [Step 4 — No Webhook Needed](#step-4--no-webhook-needed)
- [Twilio Free Trial Notes](#twilio-free-trial-notes)
- [Twilio Pricing](#twilio-pricing)

---

## Step 1 — Create a Twilio Account

1. Go to [twilio.com](https://www.twilio.com/) and click **Sign Up**
2. Enter your name, email, and password
3. Verify your email address
4. Verify your personal phone number (required by Twilio)
5. Answer the onboarding questions (select "SMS", "Receive messages", etc. — these don't affect functionality)

![Twilio signup page](images/twilio-signup.png)

---

## Step 2 — Get Your Credentials

Your **Account SID** and **Auth Token** are the two credentials the plugin uses to read your incoming messages.

1. After logging in, go to the **Twilio Console** home page: [console.twilio.com](https://console.twilio.com/)
2. You will see your credentials in the **Account Info** section on the dashboard

![Twilio dashboard with Account SID and Auth Token](images/twilio-dashboard-credentials.png)

| Field | Description |
|-------|-------------|
| **Account SID** | Starts with `AC...` — uniquely identifies your account |
| **Auth Token** | Secret key — keep this private, treat it like a password |

> ⚠️ **Never share your Auth Token publicly.** Anyone with your SID + Token can read your messages and send SMS from your number.

Copy both values — you will paste them into the plugin configuration.

---

## Step 3 — Purchase a Phone Number

1. In the Twilio Console, go to **Phone Numbers** → **Manage** → **Buy a Number**

   ![Twilio Buy a Number menu](images/twilio-buy-number-menu.png)

2. Search for a number. For best results:
   - Filter by **SMS** capability
   - Choose your country (US recommended for local displays)
   - Pick a local area code your visitors will recognize, or use a toll-free number

   ![Twilio number search](images/twilio-number-search.png)

3. Click **Buy** next to your chosen number
4. Confirm the purchase (~$1.00/month)

5. Once purchased, go to **Phone Numbers** → **Manage** → **Active Numbers** and click your number

   ![Twilio active number settings](images/twilio-active-number.png)

6. Note the phone number in **E.164 format**: `+1XXXXXXXXXX`

   You will enter this in the plugin settings as the **Twilio Phone Number**.

---

## Step 4 — No Webhook Needed

Unlike many Twilio integrations, **this plugin does not use webhooks**. Instead, it polls the Twilio REST API at a regular interval to check for new messages.

This means:
- Your FPP device does **not** need to be publicly accessible from the internet
- You do **not** need to configure any webhook URL in Twilio
- The plugin simply reads your incoming message list on its own schedule

---

## Twilio Free Trial Notes

Twilio offers a free trial with ~$15 in credit. During the trial:

| Limitation | Details |
|------------|---------|
| Outgoing SMS | Can only be sent to **verified** phone numbers |
| Incoming SMS | Works from any number |
| "Powered by Twilio" prefix | May be added to outgoing messages |
| Credit expiry | Trial credit expires after ~30 days of inactivity |

For a public display, you will want to **upgrade to a paid account** so anyone can text in (not just verified numbers). Upgrading requires adding a credit card — typical cost is $2–5/month for a seasonal display.

---

## Twilio Pricing

All prices are approximate and subject to change. See [twilio.com/en-us/sms/pricing](https://www.twilio.com/en-us/sms/pricing) for current rates.

| Item | Approximate Cost |
|------|-----------------|
| US phone number rental | $1.00/month |
| Incoming SMS (US) | $0.0075 per message |
| Outgoing SMS (US) | $0.0079 per message |

**Example seasonal display (6 weeks, 200 texts/night, 42 nights):**
- 8,400 incoming SMS = $63.00
- 8,400 outgoing responses = $66.36 *(if auto-responses enabled)*
- Phone number = $1.50 (1.5 months)
- **Total: ~$65–$131 for the season**

To reduce costs, you can **disable SMS auto-responses** in the plugin settings — visitors won't get a reply, but the display still works.

---

## Next Step

Once you have your **Account SID**, **Auth Token**, and **Phone Number**, proceed to:

[→ Installation](02-installation.md)
