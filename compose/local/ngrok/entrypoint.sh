#!/bin/sh -e

if [ -n "$@" ]; then
  exec "$@"
fi

# Legacy compatible:
if [ -z "$NGROK_PORT" ]; then
  if [ -n "$HTTPS_PORT" ]; then
    NGROK_PORT="$HTTPS_PORT"
  elif [ -n "$HTTP_PORT" ]; then
    NGROK_PORT="$HTTP_PORT"
  elif [ -n "$APP_PORT" ]; then
    NGROK_PORT="$APP_PORT"
  fi
fi

ARGS="ngrok"

# Set the protocol.
if [ "$NGROK_PROTOCOL" = "TCP" ]; then
  ARGS="$ARGS tcp"
else
  ARGS="$ARGS http"
  NGROK_PORT="${NGROK_PORT:-80}"
fi

# Set the TLS binding flag
if [ -n "$NGROK_BINDTLS" ]; then
  ARGS="$ARGS --bind-tls=$NGROK_BINDTLS "
fi

# Set the authorization token.
if [ -n "$NGROK_AUTH" ]; then
  echo "authtoken: $NGROK_AUTH" >> ~/.ngrok2/ngrok.yml
fi

# We use the forced NGROK_HOSTNAME here.
# This requires a valid Ngrok auth token in $NGROK_AUTH
if [ -n "$NGROK_HOSTNAME" ]; then
  if [ -z "$NGROK_AUTH" ]; then
    echo "You must set NGROK_AUTH (your Ngrok auth token) to use a custom domain."
    exit 1
  fi
  ARGS="$ARGS --url=$NGROK_HOSTNAME "
fi

# Set the remote-addr if specified
if [ -n "$NGROK_REMOTE_ADDR" ]; then
  if [ -z "$NGROK_AUTH" ]; then
    echo "You must specify an authentication token to use reserved IP addresses."
    exit 1
  fi
  ARGS="$ARGS --remote-addr=$NGROK_REMOTE_ADDR "
fi

# Set a custom region
if [ -n "$NGROK_REGION" ]; then
  ARGS="$ARGS --region=$NGROK_REGION "
fi

if [ -n "$NGROK_HEADER" ]; then
  ARGS="$ARGS --host-header=$NGROK_HEADER "
fi

# HTTP Auth config
if [ -n "$NGROK_USERNAME" ] && [ -n "$NGROK_PASSWORD" ] && [ -n "$NGROK_AUTH" ]; then
  ARGS="$ARGS --auth=$NGROK_USERNAME:$NGROK_PASSWORD "
elif [ -n "$NGROK_USERNAME" ] || [ -n "$NGROK_PASSWORD" ]; then
  if [ -z "$NGROK_AUTH" ]; then
    echo "You must specify NGROK_USERNAME, NGROK_PASSWORD, and NGROK_AUTH for custom HTTP authentication."
    exit 1
  fi
fi

# Always log to stdout in debug mode
ARGS="$ARGS --log stdout --log-level=debug"

# Set the port.
if [ -z "$NGROK_PORT" ]; then
  echo "You must specify an NGROK_PORT to expose."
  exit 1
fi

# Finally, add the port to the command
ARGS="$ARGS $(echo $NGROK_PORT | sed 's|^tcp://||')"

set -x
exec $ARGS
