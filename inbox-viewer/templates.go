package main

const indexTmpl = `<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Reports &amp; Inbox</title>
<link rel="manifest" href="/reports/manifest.json">
<meta name="theme-color" content="#0d1117">
<style>
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
:root {
  --bg:        #0d1117;
  --surface:   #161b22;
  --surface2:  #1c2128;
  --border:    #30363d;
  --text:      #e6edf3;
  --text-muted:#8b949e;
  --accent:    #58a6ff;
  --green:     #3fb950;
  --red:       #f85149;
  --tag-bg:    #21262d;
  --radius:    12px;
}
html, body {
  min-height: 100%;
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
  background: var(--bg);
  color: var(--text);
  font-size: 14px;
  line-height: 1.5;
}
header {
  border-bottom: 1px solid var(--border);
  padding: 14px 24px;
  display: flex;
  align-items: center;
  gap: 10px;
  background: var(--surface);
}
.logo {
  width: 26px; height: 26px;
  background: var(--surface2);
  border-radius: 6px;
  display: flex; align-items: center; justify-content: center;
  flex-shrink: 0;
}
.logo svg { width: 16px; height: 16px; }
header h1 { font-size: 15px; font-weight: 600; letter-spacing: -0.3px; flex: 1; }
.back-link {
  font-size: 12px;
  color: var(--text-muted);
  text-decoration: none;
  padding: 4px 10px;
  border: 1px solid var(--border);
  border-radius: 6px;
  transition: color 0.15s, border-color 0.15s;
}
.back-link:hover { color: var(--text); border-color: var(--accent); }
main { max-width: 900px; margin: 0 auto; padding: 32px 24px; }
.filters {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
  margin-bottom: 24px;
}
.filter-btn {
  background: var(--surface);
  border: 1px solid var(--border);
  color: var(--text-muted);
  padding: 5px 14px;
  border-radius: 20px;
  cursor: pointer;
  font-size: 12px;
  font-family: inherit;
  transition: color 0.15s, border-color 0.15s, background 0.15s;
}
.filter-btn:hover { color: var(--text); border-color: var(--accent); }
.filter-btn.active { background: var(--surface2); border-color: var(--accent); color: var(--text); }
.section-label {
  font-size: 11px;
  font-weight: 600;
  color: var(--text-muted);
  text-transform: uppercase;
  letter-spacing: 0.6px;
  margin-bottom: 12px;
}
.grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(260px, 1fr));
  gap: 12px;
}
.card {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 18px 20px;
  text-decoration: none;
  color: var(--text);
  display: block;
  transition: border-color 0.15s, background 0.15s, transform 0.1s;
}
.card:hover { border-color: var(--accent); background: var(--surface2); transform: translateY(-1px); }
.card:active { transform: translateY(0); }
.card-meta { display: flex; align-items: center; gap: 8px; margin-bottom: 10px; }
.badge {
  font-size: 10px;
  font-weight: 700;
  padding: 2px 7px;
  border-radius: 10px;
  text-transform: uppercase;
  letter-spacing: 0.5px;
}
.badge-report   { background: #1d2d3e; color: var(--accent); }
.badge-research { background: #1a2d1e; color: #3fb950; }
.badge-review   { background: #2d261a; color: #d29922; }
.badge-inbox    { background: #231a2d; color: #bc8cff; }
.card-date { font-size: 11px; color: var(--text-muted); }
.card-title { font-size: 13px; font-weight: 600; margin-bottom: 6px; line-height: 1.4; letter-spacing: -0.1px; }
.card-excerpt { font-size: 12px; color: var(--text-muted); line-height: 1.5; }
.empty { color: var(--text-muted); font-size: 13px; padding: 40px 0; }
@media (max-width: 600px) {
  main { padding: 20px 16px; }
  .grid { grid-template-columns: 1fr; }
}
</style>
</head>
<body>
<header>
  <div class="logo">
    <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round">
      <rect x="2.5" y="1.5" width="11" height="13" rx="1.5"/>
      <line x1="5" y1="5.5" x2="11" y2="5.5"/>
      <line x1="5" y1="8" x2="11" y2="8"/>
      <line x1="5" y1="10.5" x2="8" y2="10.5"/>
    </svg>
  </div>
  <h1>Reports &amp; Inbox</h1>
  <a class="back-link" href="/">&#8592; Home</a>
</header>
<main>
  <div class="filters">
    <button class="filter-btn active" data-cat="">All ({{.Count}})</button>
    <button class="filter-btn" data-cat="report">Reports</button>
    <button class="filter-btn" data-cat="research">Research</button>
    <button class="filter-btn" data-cat="review">Reviews</button>
    <button class="filter-btn" data-cat="inbox">Inbox</button>
  </div>
  <div class="section-label">Items</div>
  <div class="grid" id="grid">
  {{range .Items}}
  <a class="card" href="{{.URLPath}}" data-cat="{{.Category}}">
    <div class="card-meta">
      <span class="badge badge-{{.Category}}">{{.Category}}</span>
      {{if .DateFormatted}}<span class="card-date">{{.DateFormatted}}</span>{{end}}
    </div>
    <div class="card-title">{{.Title}}</div>
    {{if .Excerpt}}<div class="card-excerpt">{{.Excerpt}}</div>{{end}}
  </a>
  {{end}}
  {{if not .Items}}<div class="empty">No items found.</div>{{end}}
  </div>
</main>
<script>
document.querySelectorAll('.filter-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('.filter-btn').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    const cat = btn.dataset.cat;
    document.querySelectorAll('.card').forEach(c => {
      c.style.display = (!cat || c.dataset.cat === cat) ? '' : 'none';
    });
  });
});
if ('serviceWorker' in navigator) navigator.serviceWorker.register('/reports/sw.js');
</script>
</body>
</html>`

const pageTmpl = `<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{{.Title}}</title>
<link rel="manifest" href="/reports/manifest.json">
<meta name="theme-color" content="#0d1117">
<style>
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
:root {
  --bg:        #0d1117;
  --surface:   #161b22;
  --surface2:  #1c2128;
  --border:    #30363d;
  --text:      #e6edf3;
  --text-muted:#8b949e;
  --accent:    #58a6ff;
  --radius:    12px;
}
html, body {
  min-height: 100%;
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
  background: var(--bg);
  color: var(--text);
  font-size: 14px;
  line-height: 1.5;
}
header {
  border-bottom: 1px solid var(--border);
  padding: 14px 24px;
  display: flex;
  align-items: center;
  gap: 10px;
  background: var(--surface);
}
.back-link {
  font-size: 12px;
  color: var(--text-muted);
  text-decoration: none;
  padding: 4px 10px;
  border: 1px solid var(--border);
  border-radius: 6px;
  transition: color 0.15s, border-color 0.15s;
}
.back-link:hover { color: var(--text); border-color: var(--accent); }
article { max-width: 780px; margin: 0 auto; padding: 32px 24px; }
.page-meta { display: flex; align-items: center; gap: 8px; margin-bottom: 16px; }
.badge {
  font-size: 10px; font-weight: 700;
  padding: 2px 7px; border-radius: 10px;
  text-transform: uppercase; letter-spacing: 0.5px;
}
.badge-report   { background: #1d2d3e; color: var(--accent); }
.badge-research { background: #1a2d1e; color: #3fb950; }
.badge-review   { background: #2d261a; color: #d29922; }
.badge-inbox    { background: #231a2d; color: #bc8cff; }
.page-date { font-size: 12px; color: var(--text-muted); }
h1.page-title { font-size: 22px; font-weight: 700; margin-bottom: 24px; line-height: 1.3; letter-spacing: -0.4px; }
.content h1 { font-size: 18px; font-weight: 600; margin: 24px 0 10px; }
.content h2 { font-size: 15px; font-weight: 600; color: var(--text); margin: 20px 0 8px; }
.content h3 { font-size: 13px; font-weight: 600; color: var(--text-muted); margin: 16px 0 6px; }
.content p  { line-height: 1.75; margin-bottom: 14px; color: var(--text); }
.content ul, .content ol { margin: 8px 0 14px 20px; }
.content li { line-height: 1.7; margin-bottom: 3px; }
.content code { background: var(--surface2); padding: 2px 5px; border-radius: 4px; font-size: 12px; font-family: 'SF Mono', 'Fira Code', monospace; }
.content pre  { background: var(--surface); border: 1px solid var(--border); padding: 14px 16px; border-radius: var(--radius); overflow-x: auto; margin: 12px 0; }
.content pre code { background: none; padding: 0; }
.content blockquote { border-left: 3px solid var(--border); padding-left: 14px; margin: 12px 0; color: var(--text-muted); }
.content a  { color: var(--accent); }
.content hr { border: none; border-top: 1px solid var(--border); margin: 20px 0; }
.content table { border-collapse: collapse; width: 100%; margin: 12px 0; }
.content th, .content td { border: 1px solid var(--border); padding: 7px 12px; text-align: left; }
.content th { background: var(--surface); font-size: 12px; }
@media (max-width: 600px) { article { padding: 20px 16px; } }
</style>
</head>
<body>
<header>
  <a class="back-link" href="/reports">&#8592; Reports &amp; Inbox</a>
</header>
<article>
  <div class="page-meta">
    <span class="badge badge-{{.Category}}">{{.Category}}</span>
    {{if .DateFormatted}}<span class="page-date">{{.DateFormatted}}</span>{{end}}
  </div>
  <h1 class="page-title">{{.Title}}</h1>
  <div class="content">{{.Content}}</div>
</article>
<script>
if ('serviceWorker' in navigator) navigator.serviceWorker.register('/reports/sw.js');
</script>
</body>
</html>`

const offlineHTML = `<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Offline</title>
<style>
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
body { background: #0d1117; color: #e6edf3; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
       display: flex; align-items: center; justify-content: center; min-height: 100vh; text-align: center; }
h1 { font-size: 18px; font-weight: 600; margin-bottom: 8px; }
p  { color: #8b949e; font-size: 13px; }
</style>
</head>
<body>
<div><h1>You're offline</h1><p>Connect to the network and try again.</p></div>
</body>
</html>`

const manifestJSON = `{
  "name": "Reports & Inbox",
  "short_name": "Reports",
  "start_url": "/reports",
  "display": "standalone",
  "background_color": "#0d1117",
  "theme_color": "#0d1117",
  "description": "Browse research reports and inbox notes",
  "icons": []
}`

const serviceWorkerJS = `const CACHE = 'reports-v1';
const OFFLINE = '/reports/offline.html';

self.addEventListener('install', e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll([OFFLINE, '/reports/manifest.json'])));
  self.skipWaiting();
});

self.addEventListener('activate', e => {
  e.waitUntil(caches.keys().then(keys =>
    Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k)))
  ));
  self.clients.claim();
});

self.addEventListener('fetch', e => {
  if (e.request.mode === 'navigate') {
    e.respondWith(fetch(e.request).catch(() => caches.match(OFFLINE)));
    return;
  }
  e.respondWith(fetch(e.request).catch(() => caches.match(e.request)));
});`
