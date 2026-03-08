<?php
$host = preg_replace('/:\d+$/', '', $_SERVER['HTTP_HOST']);
$pluginUrl = "http://$host:5000/";
?>
<style>
    #sms-plugin-frame {
        width: 100%;
        height: calc(100vh - 120px);
        border: none;
        display: block;
    }
</style>
<iframe id="sms-plugin-frame" src="<?php echo htmlspecialchars($pluginUrl); ?>"></iframe>
