<?php
// FPP SMS Twilio Plugin - Help / Documentation page
$pluginName = "fpp-plugin-sms-twilio";
$githubBase = "https://github.com/thespica93/fpp-plugin-sms-twilio";
$docsBase   = $githubBase . "/blob/main/docs";
?>
<style>
    .sms-help { max-width: 900px; margin: 0 auto; font-family: Arial, sans-serif; }
    .sms-help h2 { color: #4CAF50; border-bottom: 2px solid #4CAF50; padding-bottom: 6px; margin-top: 28px; }
    .sms-help h3 { color: #333; margin-top: 20px; }
    .doc-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(260px, 1fr)); gap: 14px; margin: 16px 0; }
    .doc-card { background: #f9f9f9; border: 1px solid #ddd; border-radius: 6px; padding: 14px 16px; text-decoration: none; color: #333; display: block; transition: box-shadow .15s; }
    .doc-card:hover { box-shadow: 0 2px 8px rgba(0,0,0,0.15); text-decoration: none; color: #333; }
    .doc-card .icon { font-size: 22px; margin-bottom: 6px; }
    .doc-card .title { font-weight: bold; font-size: 15px; margin-bottom: 4px; }
    .doc-card .desc { font-size: 13px; color: #666; }
    .quick-ref table { width: 100%; border-collapse: collapse; margin: 10px 0; }
    .quick-ref th { background: #4CAF50; color: white; padding: 8px 10px; text-align: left; }
    .quick-ref td { padding: 7px 10px; border-bottom: 1px solid #eee; }
    .quick-ref tr:hover td { background: #f5f5f5; }
    .ui-link { display: inline-block; background: #4CAF50; color: white; padding: 10px 20px; border-radius: 5px; text-decoration: none; font-weight: bold; margin: 6px 6px 6px 0; }
    .ui-link:hover { background: #45a049; color: white; text-decoration: none; }
    .ui-link.secondary { background: #2196F3; }
    .ui-link.secondary:hover { background: #0b7dda; }
    .ui-link.danger { background: #f44336; }
    .tag { display: inline-block; background: #e8f5e9; color: #2e7d32; font-size: 11px; font-weight: bold; padding: 2px 7px; border-radius: 10px; margin-left: 6px; vertical-align: middle; }
    .filter-status { background: #fff3cd; border: 1px solid #ffc107; border-radius: 5px; padding: 10px 14px; margin: 10px 0; font-size: 13px; }
</style>

<div class="sms-help">

    <h2>📱 FPP SMS Twilio Plugin — Help &amp; Documentation</h2>
    <p>This plugin lets visitors text their name to your Twilio number and have it appear on your pixel LED display.</p>

    <a href="http://<?php echo $_SERVER['HTTP_HOST']; ?>:5000" target="_blank" class="ui-link">🔧 Open Plugin Config UI</a>
    <a href="http://<?php echo $_SERVER['HTTP_HOST']; ?>:5000/messages" target="_blank" class="ui-link secondary">📋 View Message Queue</a>
    <a href="<?php echo $githubBase; ?>" target="_blank" class="ui-link secondary">📖 GitHub Repository</a>

    <h2>📚 Documentation</h2>
    <div class="doc-grid">
        <a href="<?php echo $docsBase; ?>/01-twilio-setup.md" target="_blank" class="doc-card">
            <div class="icon">📞</div>
            <div class="title">Twilio Setup</div>
            <div class="desc">Create a Twilio account, get a phone number, and find your credentials</div>
        </a>
        <a href="<?php echo $docsBase; ?>/02-installation.md" target="_blank" class="doc-card">
            <div class="icon">⚙️</div>
            <div class="title">Installation</div>
            <div class="desc">Install via FPP Plugin Manager or manually via SSH</div>
        </a>
        <a href="<?php echo $docsBase; ?>/03-plugin-configuration.md" target="_blank" class="doc-card">
            <div class="icon">🔧</div>
            <div class="title">Plugin Configuration</div>
            <div class="desc">Full reference for every setting in the web interface</div>
        </a>
        <a href="<?php echo $docsBase; ?>/04-message-queue.md" target="_blank" class="doc-card">
            <div class="icon">📋</div>
            <div class="title">Message Queue</div>
            <div class="desc">How the queue works, name validation rules, and blocking senders</div>
        </a>
        <a href="<?php echo $docsBase; ?>/05-whitelist.md" target="_blank" class="doc-card">
            <div class="icon">✅</div>
            <div class="title">Name Whitelist</div>
            <div class="desc">Only allow pre-approved names on your display (22,000+ included)</div>
        </a>
        <a href="<?php echo $docsBase; ?>/06-blacklist.md" target="_blank" class="doc-card">
            <div class="icon">🚫</div>
            <div class="title">Profanity Blacklist</div>
            <div class="desc">Block specific words, how matching works, managing the list</div>
        </a>
        <a href="<?php echo $docsBase; ?>/07-phone-blocklist.md" target="_blank" class="doc-card">
            <div class="icon">📵</div>
            <div class="title">Phone Blocklist</div>
            <div class="desc">Block specific phone numbers from submitting names</div>
        </a>
        <a href="<?php echo $docsBase; ?>/08-sms-responses.md" target="_blank" class="doc-card">
            <div class="icon">💬</div>
            <div class="title">SMS Auto-Responses</div>
            <div class="desc">Customize the automatic replies sent back to visitors</div>
        </a>
        <a href="<?php echo $docsBase; ?>/09-troubleshooting.md" target="_blank" class="doc-card">
            <div class="icon">🔍</div>
            <div class="title">Troubleshooting</div>
            <div class="desc">Common problems and step-by-step solutions</div>
        </a>
    </div>

    <h2>⚡ Quick Reference</h2>

    <h3>Web Interface URLs</h3>
    <div class="quick-ref">
    <table>
        <tr><th>Page</th><th>URL</th></tr>
        <tr><td>Plugin Configuration</td><td><code>http://<?php echo $_SERVER['HTTP_HOST']; ?>:5000/</code></td></tr>
        <tr><td>Message Queue &amp; History</td><td><code>http://<?php echo $_SERVER['HTTP_HOST']; ?>:5000/messages</code></td></tr>
        <tr><td>Name Whitelist Manager</td><td><code>http://<?php echo $_SERVER['HTTP_HOST']; ?>:5000/whitelist</code></td></tr>
        <tr><td>Profanity Blacklist Manager</td><td><code>http://<?php echo $_SERVER['HTTP_HOST']; ?>:5000/blacklist</code></td></tr>
        <tr><td>Phone Blocklist</td><td><code>http://<?php echo $_SERVER['HTTP_HOST']; ?>:5000/blocklist</code></td></tr>
    </table>
    </div>

    <h3>File Locations</h3>
    <div class="quick-ref">
    <table>
        <tr><th>File</th><th>Path</th></tr>
        <tr><td>Plugin directory</td><td><code>/opt/fpp/plugins/fpp-plugin-sms-twilio/</code></td></tr>
        <tr><td>Configuration</td><td><code>/home/fpp/media/config/plugin.fpp-sms-twilio.json</code></td></tr>
        <tr><td>Name whitelist</td><td><code>/opt/fpp/plugins/fpp-plugin-sms-twilio/whitelist.txt</code></td></tr>
        <tr><td>Profanity blacklist</td><td><code>/opt/fpp/plugins/fpp-plugin-sms-twilio/blacklist.txt</code></td></tr>
        <tr><td>Blocked phones</td><td><code>/opt/fpp/plugins/fpp-plugin-sms-twilio/blocked_phones.json</code></td></tr>
        <tr><td>Plugin log</td><td><code>/home/fpp/media/logs/sms_plugin.log</code></td></tr>
        <tr><td>Message history</td><td><code>/home/fpp/media/logs/received_messages.json</code></td></tr>
    </table>
    </div>

    <h3>How a Message Gets Approved</h3>
    <div class="quick-ref">
    <table>
        <tr><th>#</th><th>Check</th><th>If it fails</th></tr>
        <tr><td>1</td><td>Phone is not on blocklist</td><td>Silently ignored</td></tr>
        <tr><td>2</td><td>Rate limit not exceeded</td><td>Auto-response: rate limited</td></tr>
        <tr><td>3</td><td>Name format is valid (1–2 words, letters only)</td><td>Auto-response: invalid format</td></tr>
        <tr><td>4</td><td>Not a duplicate from same phone today</td><td>Auto-response: duplicate</td></tr>
        <tr><td>5</td><td>Profanity filter passes <span class="tag">if enabled</span></td><td>Auto-response: blocked</td></tr>
        <tr><td>6</td><td>Name is on whitelist <span class="tag">if enabled</span></td><td>Auto-response: not on list</td></tr>
        <tr><td>✅</td><td>Added to display queue</td><td>Auto-response: success</td></tr>
    </table>
    </div>

    <h3>Common SSH Commands</h3>
    <div class="quick-ref">
    <table>
        <tr><th>Task</th><th>Command</th></tr>
        <tr><td>View live log</td><td><code>tail -f /home/fpp/media/logs/sms_plugin.log</code></td></tr>
        <tr><td>Check if plugin is running</td><td><code>ps aux | grep sms_plugin</code></td></tr>
        <tr><td>Restart plugin</td><td><code>pkill -f sms_plugin.py &amp;&amp; sleep 2 &amp;&amp; python3 /opt/fpp/plugins/fpp-plugin-sms-twilio/sms_plugin.py &amp;</code></td></tr>
        <tr><td>Clear blocked phones</td><td><code>echo '{}' &gt; /opt/fpp/plugins/fpp-plugin-sms-twilio/blocked_phones.json</code></td></tr>
    </table>
    </div>

    <h2>💰 Twilio Pricing (approximate)</h2>
    <div class="quick-ref">
    <table>
        <tr><th>Item</th><th>Cost</th></tr>
        <tr><td>Phone number rental</td><td>~$1.00/month</td></tr>
        <tr><td>Incoming SMS</td><td>~$0.0075 per message</td></tr>
        <tr><td>Outgoing SMS (auto-responses)</td><td>~$0.0079 per message</td></tr>
    </table>
    </div>

    <h2>🆘 Support</h2>
    <p>
        <a href="<?php echo $githubBase; ?>/issues" target="_blank" class="ui-link danger">🐛 Report a Bug on GitHub</a>
        <a href="https://falconchristmas.com/" target="_blank" class="ui-link secondary">💬 FPP Community Forums</a>
    </p>
    <p style="color:#666; font-size:13px;">Plugin version 2.5 &nbsp;|&nbsp; Built for the FPP Christmas light community 🎄</p>

</div>
