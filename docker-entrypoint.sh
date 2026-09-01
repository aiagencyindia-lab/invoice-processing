#!/bin/sh
# Materialise the Google credential, if one was supplied as a variable.
#
# Railway holds configuration as environment variables, and the Google
# libraries want a file path. So GOOGLE_CREDENTIALS_JSON is written to a file
# on tmpfs and GOOGLE_APPLICATION_CREDENTIALS is pointed at it. The value is
# never logged and never echoed -- the only thing printed is whether one was
# found, because "no credential" and "a bad credential" fail very differently
# and the logs should say which.
set -e

if [ -n "${GOOGLE_CREDENTIALS_JSON:-}" ]; then
  KEY_PATH="${GOOGLE_CREDENTIALS_PATH:-/tmp/gcp-credentials.json}"
  printf '%s' "$GOOGLE_CREDENTIALS_JSON" > "$KEY_PATH"
  chmod 600 "$KEY_PATH"
  export GOOGLE_APPLICATION_CREDENTIALS="$KEY_PATH"
  echo "Credentials: wrote GOOGLE_CREDENTIALS_JSON to $KEY_PATH"
else
  echo "Credentials: GOOGLE_CREDENTIALS_JSON is not set; relying on ambient credentials."
fi

exec "$@"
