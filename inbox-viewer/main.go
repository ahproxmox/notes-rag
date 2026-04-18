package main

import (
	"bytes"
	"html/template"
	"log"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"time"
)

var (
	indexTemplate = template.Must(template.New("index").Parse(indexTmpl))
	pageTemplate  = template.Must(template.New("page").Parse(pageTmpl))
)

type indexData struct {
	Items []cardData
	Count int
}

type cardData struct {
	Title         string
	DateFormatted string
	Category      string
	Excerpt       string
	URLPath       string
}

type pageData struct {
	Title         string
	DateFormatted string
	Category      string
	Content       template.HTML
}

func main() {
	mux := http.NewServeMux()
	mux.HandleFunc("/reports/manifest.json", manifestHandler)
	mux.HandleFunc("/reports/sw.js", swHandler)
	mux.HandleFunc("/reports/offline.html", offlineHandler)
	mux.HandleFunc("/reports/inbox/", inboxHandler)
	mux.HandleFunc("/reports/report/", reportFileHandler)
	mux.HandleFunc("/reports", indexHandler)
	mux.HandleFunc("/reports/", indexHandler)

	log.Println("inbox-viewer listening on :8082")
	log.Fatal(http.ListenAndServe(":8082", mux))
}

func indexHandler(w http.ResponseWriter, r *http.Request) {
	if r.URL.Path != "/reports" && r.URL.Path != "/reports/" {
		http.NotFound(w, r)
		return
	}
	items := scanAll()
	data := indexData{Count: len(items)}
	for _, item := range items {
		data.Items = append(data.Items, cardData{
			Title:         item.Title,
			DateFormatted: formatDate(item.Date),
			Category:      item.Category,
			Excerpt:       item.Excerpt,
			URLPath:       item.URLPath,
		})
	}
	var buf bytes.Buffer
	if err := indexTemplate.Execute(&buf, data); err != nil {
		log.Printf("index template error: %v", err)
		http.Error(w, "render error", http.StatusInternalServerError)
		return
	}
	w.Header().Set("Content-Type", "text/html; charset=utf-8")
	buf.WriteTo(w)
}

func inboxHandler(w http.ResponseWriter, r *http.Request) {
	slug := strings.TrimPrefix(r.URL.Path, "/reports/inbox/")
	slug = strings.Trim(slug, "/")
	if slug == "" || strings.HasPrefix(slug, ".") || strings.Contains(slug, "/") || strings.Contains(slug, "\\") {
		http.NotFound(w, r)
		return
	}

	path, ok := slugToFile(slug)
	if !ok {
		http.NotFound(w, r)
		return
	}

	raw, err := os.ReadFile(path)
	if err != nil {
		http.Error(w, "could not read file", http.StatusInternalServerError)
		return
	}

	fm, body := splitFrontmatter(raw)

	title := fm["title"]
	if title == "" {
		title = filenameToTitle(slug)
	}
	date := parseDateString(fm["date"])
	if date.IsZero() {
		date = parseDateFromFilename(slug)
	}

	category := "inbox"
	if strings.Contains(path, "/Research/") {
		category = "research"
	} else if strings.Contains(path, "/Reviews/") {
		category = "review"
	}

	content, err := renderMarkdown(body)
	if err != nil {
		http.Error(w, "render error", http.StatusInternalServerError)
		return
	}

	data := pageData{
		Title:         title,
		DateFormatted: formatDate(date),
		Category:      category,
		Content:       template.HTML(content),
	}
	var buf bytes.Buffer
	if err := pageTemplate.Execute(&buf, data); err != nil {
		log.Printf("page template error: %v", err)
		http.Error(w, "render error", http.StatusInternalServerError)
		return
	}
	w.Header().Set("Content-Type", "text/html; charset=utf-8")
	buf.WriteTo(w)
}

func reportFileHandler(w http.ResponseWriter, r *http.Request) {
	folder := strings.TrimPrefix(r.URL.Path, "/reports/report/")
	folder = strings.Trim(folder, "/")
	if folder == "" || strings.Contains(folder, "..") || strings.Contains(folder, "/") {
		http.NotFound(w, r)
		return
	}
	clean := filepath.Join("/mnt/Claude/reports", folder, "report_dark.html")
	if !strings.HasPrefix(clean, "/mnt/Claude/reports/") {
		http.NotFound(w, r)
		return
	}
	http.ServeFile(w, r, clean)
}

func manifestHandler(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Content-Type", "application/manifest+json")
	w.Write([]byte(manifestJSON))
}

func swHandler(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Content-Type", "application/javascript")
	w.Header().Set("Service-Worker-Allowed", "/reports/")
	w.Write([]byte(serviceWorkerJS))
}

func offlineHandler(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Content-Type", "text/html; charset=utf-8")
	w.Write([]byte(offlineHTML))
}

func formatDate(t time.Time) string {
	if t.IsZero() {
		return ""
	}
	return t.Format("Jan 2, 2006")
}
