#!/bin/sh
# First start on an empty volume: seed the store's catalog and offers (later edits in the ops console are kept).
set -e
for f in catalog.json promotions.json; do
  [ -f "/data/$f" ] || cp "/app/defaults/$f" "/data/$f"
done
exec "$@"
