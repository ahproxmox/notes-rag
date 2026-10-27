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

// Session gone (oauth2-proxy answers fetch() with 401): reload the pages so the
// browser does a real navigation, which Caddy sends to the Keycloak login.
function reloadClients() {
  return self.clients.matchAll({ type: "window" }).then(cs => cs.forEach(c => c.navigate(c.url)));
}

self.addEventListener("fetch", e => {
  const url = new URL(e.request.url);
  // Network-first for all API calls - always want fresh data
  if (url.pathname.startsWith("/api/")) {
    e.respondWith(
      fetch(e.request).then(res => {
        if (res.status === 401) reloadClients();
        return res;
      }).catch(() => caches.match(e.request))
    );
    return;
  }
  // Network-first for pages too, so a login redirect is never hidden behind the cache;
  // the cache is only the offline fallback. Redirects and errors are never cached.
  e.respondWith(
    fetch(e.request).then(res => {
      if (res.ok && res.type === "basic") {
        const clone = res.clone();
        caches.open(CACHE).then(c => c.put(e.request, clone));
      }
      return res;
    }).catch(() => caches.match(e.request))
  );
});
