---
title: "Caddy internal HTTPS"
date_created: 2026-02-10
tags: [caddy, tls, infra]
---

# Caddy internal HTTPS

Caddy on CT 126 issues certificates for `*.internal.ahproxmox-claude.cc`
via Cloudflare DNS-01. Four internal services are fronted by it.

## Config notes
- Provider: cloudflare
- Renewal: automatic, 30 days before expiry
- Storage: /etc/caddy/certs
