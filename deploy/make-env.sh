#!/usr/bin/env bash
# Prints a production .env with freshly generated secrets. Run on the server (so the secrets never leave it):
#
#   bash deploy/make-env.sh rideleader.cyclingtools.cc > .env && chmod 600 .env
#
# Then add by hand (never commit them): SMTP_* (so sign-in links can be emailed) and, when ready, STRAVA_*.
set -euo pipefail
DOMAIN="${1:?usage: make-env.sh <domain>}"
rand() { openssl rand -base64 "$1" | tr -d '\n=+/' | cut -c1-"$2"; }
fernet() { openssl rand -base64 32 | tr '+/' '-_' | tr -d '\n'; }
cat <<EOF
DOMAIN=$DOMAIN
APP_BASE_URL=https://$DOMAIN
POSTGRES_PASSWORD=$(rand 48 40)
SECRET_KEY=$(rand 64 64)
TOKEN_ENCRYPTION_KEY=$(fernet)
STRAVA_VERIFY_TOKEN=$(rand 32 24)
STRAVA_MODE=off
CONTROLLER_NAME=
CONTROLLER_CONTACT=
MAIL_FROM=Ride Leader Kit <no-reply@${DOMAIN#*.}>
EOF
