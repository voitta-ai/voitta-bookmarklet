#!/usr/bin/env bash
# One-time: put hg.voitta.ai on this host's nginx with a Let's Encrypt certificate.
# Run with sudo from the checkout:   sudo deploy/nginx/setup-hg.sh
#
# 1. an HTTP-only site so Let's Encrypt can check the domain
# 2. the certificate (webroot, same as bookmarklet.voitta.ai; renewal is automatic)
# 3. the full site (HTTPS → the app on 127.0.0.1:12358)
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "run with sudo" >&2; exit 1; }
cd "$(dirname "$0")"
SITE=/etc/nginx/sites-available/hg.voitta.ai

# 1. HTTP only (the first server block of the real config)
awk 'BEGIN{n=0} /^server \{/{n++} n==1' hg.voitta.ai.conf > "$SITE"
ln -sf "$SITE" /etc/nginx/sites-enabled/hg.voitta.ai
nginx -t
systemctl reload nginx

# 2. certificate
if [ ! -e /etc/letsencrypt/live/hg.voitta.ai/fullchain.pem ]; then
  certbot certonly --webroot -w /var/www/html -d hg.voitta.ai --non-interactive --agree-tos \
    --register-unsafely-without-email --keep-until-expiring
fi

# 3. full site
cp hg.voitta.ai.conf "$SITE"
nginx -t
systemctl reload nginx
echo "hg.voitta.ai: nginx + certificate ready"
