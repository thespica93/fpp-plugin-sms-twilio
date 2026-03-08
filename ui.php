<?php
// Redirect to the SMS plugin Flask UI on port 5000
// Uses the same hostname so it works regardless of IP address
$host = preg_replace('/:\d+$/', '', $_SERVER['HTTP_HOST']);
header("Location: http://$host:5000/");
exit;
?>
