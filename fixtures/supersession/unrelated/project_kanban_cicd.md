---
title: "Kanban CI/CD"
date_created: 2026-02-18
tags: [kanban, ci, deploy]
---

# Kanban CI/CD

The kanban web app runs on CT 122. CI runs ESLint and a node syntax
check on every PR; on merge to main the org runner SSHes to CT 122 and
restarts the service under pm2.
