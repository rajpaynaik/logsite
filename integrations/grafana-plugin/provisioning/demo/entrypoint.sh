#!/bin/sh
# Regenerate the demo errors every hour, so the dashboard's "last 6 hours" always has some.
set -e
python /make_demo.py /demo
(while sleep 3600; do python /make_demo.py /demo; done) &
exec logsite --config-dir /demo alert-server --host 0.0.0.0 --port 8085 --token "$LOGSITE_ALERT_TOKEN"
