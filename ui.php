<?php
$host = preg_replace('/:\d+$/', '', $_SERVER['HTTP_HOST']);
$pluginUrl = "http://$host:5001/"; // beta variant runs on 5001, stable stays on 5000
?>
<style>
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
    document.getElementById('sms-plugin-frame').addEventListener('load', function() {
        window.scrollTo(0, 0);
    });
    window.addEventListener('message', function(e) {
        if (e.data && e.data.type === 'iframeHeight') {
            document.getElementById('sms-plugin-frame').style.height = (e.data.height + 20) + 'px';
        }
        if (e.data && e.data.type === 'scrollTop') {
            window.scrollTo(0, 0);
        }
    });
</script>
