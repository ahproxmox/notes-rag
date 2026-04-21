---
title: "OPNsense DMZ firewall"
date_created: 2026-02-12
tags: [opnsense, dmz, infra]
---

# OPNsense DMZ firewall

VM 200 runs OPNsense as the DMZ firewall on vmbr1. Allocated
10.0.1.0/24 for DMZ-resident services. DMZ→LAN traffic is blocked;
LAN→DMZ is permitted on service-specific ports only.

## Rules
- DMZ→LAN: DROP
- LAN→DMZ: allow 80/443 to published services
- WAN→DMZ: allow only what Cloudflare Tunnel needs
