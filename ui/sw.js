const CACHE = "notes-rag-526";
const SHELL = ["/", "/notes", "/chat", "/review", "/reports", "/new", "/projects", "/kanban"];

self.addEventListener("install", e => {
  // Shell pages need a login now (identity-lab todo 526); precache whatever is reachable
  // and don't fail the install when the user isn't signed in yet.
  e.waitUntil(caches.open(CACHE).then(c =>
    Promise.all(SHELL.map(u => fetch(u, { redirect: "manual" })
      .then(res => (res.ok ? c.put(u, res) : null)).catch(() => null)))));
  self.skipWaiting();
});

self.addEventListener("activate", e => {
  e.waitUntil(
    caches.keys().then(keys =>
      Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k)))
    ).then(() => clients.claim())
  );
});

self.addEventListener("fetch", e => {
  const url = new URL(e.request.url);
  // The login flow (oauth2-proxy → Keycloak) must run as plain browser navigations.
  if (url.pathname.startsWith("/oauth2/")) return;
  // Network-first for all API calls - always want fresh data
  if (url.pathname.startsWith("/api/")) {
    e.respondWith(
      // A 401 here means the Keycloak session expired. Don't auto-reload: with forward_auth
      // the stale cookie isn't cleared, so a reload can loop. The next page load goes to login.
      fetch(e.request).catch(() => caches.match(e.request))
    );
    return;
  }
  // Network-first for pages too, so a login redirect is never hidden behind the cache;
  // the cache is only the offline fallback. Redirects and errors are never cached.
  e.respondWith(
    fetch(e.request).then(res => {
      // Signed out: a fetch from the worker doesn't count as a navigation for Caddy, so it
      // answers 401; send the page to the sign-in flow ourselves.
      if (res.status === 401 && e.request.mode === "navigate") {
        return Response.redirect("/oauth2/sign_in?rd=" + encodeURIComponent(e.request.url), 302);
      }
      if (res.ok && res.type === "basic") {
        const clone = res.clone();
        caches.open(CACHE).then(c => c.put(e.request, clone));
      }
      return res;
    }).catch(() => caches.match(e.request))
  );
});
