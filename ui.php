<?php
$host = preg_replace('/:\d+$/', '', $_SERVER['HTTP_HOST']);
$pluginUrl = "http://$host:5000/";
?>
<style>
    /* Override FPP's white content wrapper to match dark theme */
    .jumbotron { background: #1a1a1a !important; padding: 5px !important; margin-bottom: 0 !important; }
    body { background: #1a1a1a !important; }
    #sms-plugin-frame {
        width: 100%;
        border: none;
        display: block;
        overflow: hidden;
        min-height: 400px;
    }
</style>
<iframe id="sms-plugin-frame" src="<?php echo htmlspecialchars($pluginUrl); ?>" scrolling="no"></iframe>
<script>
    window.addEventListener('message', function(e) {
        if (e.data && e.data.type === 'iframeHeight') {
            document.getElementById('sms-plugin-frame').style.height = (e.data.height + 20) + 'px';
        }
    });
</script>
