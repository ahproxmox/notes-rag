package main

import (
	"bufio"
	"bytes"
	"log"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"time"
)

// ContentItem represents one piece of content: a report or an inbox .md file.
type ContentItem struct {
	Title    string
	Date     time.Time
	Category string // "report", "research", "review", "recipe", "inbox"
	Slug     string // filename without extension
	Excerpt  string
	URLPath  string // href for links
	FilePath string // absolute filesystem path
}

// scanAll returns all items from Obsidian inbox and /mnt/Claude/reports, sorted newest first.
func scanAll() []ContentItem {
	var items []ContentItem
	items = append(items, scanInboxDir("/mnt/Obsidian/Inbox", "inbox")...)
	items = append(items, scanInboxDir("/mnt/Obsidian/Inbox/Research", "research")...)
	items = append(items, scanInboxDir("/mnt/Obsidian/Inbox/Reviews", "review")...)
	items = append(items, scanInboxDir("/mnt/Obsidian/Inbox/Recipes", "recipe")...)
	items = append(items, scanReports("/mnt/Claude/reports")...)
	seen := make(map[string]struct{})
	var deduped []ContentItem
	for _, item := range items {
		if _, dup := seen[item.FilePath]; !dup {
			seen[item.FilePath] = struct{}{}
			deduped = append(deduped, item)
		}
	}
	items = deduped
	sort.Slice(items, func(i, j int) bool {
		return items[i].Date.After(items[j].Date)
	})
	return items
}

func scanInboxDir(dir, category string) []ContentItem {
	entries, err := os.ReadDir(dir)
	if err != nil {
		log.Printf("scanInboxDir: cannot read %s: %v", dir, err)
		return nil
	}
	var items []ContentItem
	for _, e := range entries {
		if e.IsDir() || !strings.HasSuffix(e.Name(), ".md") {
			continue
		}
		item, ok := parseMarkdownFile(filepath.Join(dir, e.Name()), category)
		if ok {
			items = append(items, item)
		}
	}
	return items
}

func scanReports(dir string) []ContentItem {
	entries, err := os.ReadDir(dir)
	if err != nil {
		log.Printf("scanReports: cannot read %s: %v", dir, err)
		return nil
	}
	var items []ContentItem
	for _, e := range entries {
		if !e.IsDir() {
			continue
		}
		htmlPath := filepath.Join(dir, e.Name(), "report_dark.html")
		if _, err := os.Stat(htmlPath); err != nil {
			continue
		}
		items = append(items, parseReportFolder(e.Name(), htmlPath))
	}
	return items
}

func parseMarkdownFile(path, category string) (ContentItem, bool) {
	data, err := os.ReadFile(path)
	if err != nil {
		return ContentItem{}, false
	}
	fm, body := splitFrontmatter(data)
	filename := strings.TrimSuffix(filepath.Base(path), ".md")

	title := fm["title"]
	if title == "" {
		title = filenameToTitle(filename)
	}

	date := parseDateString(fm["date"])
	if date.IsZero() {
		date = parseDateFromFilename(filename)
	}

	return ContentItem{
		Title:    title,
		Date:     date,
		Category: category,
		Slug:     filename,
		Excerpt:  extractExcerpt(body),
		URLPath:  "/reports/inbox/" + filename,
		FilePath: path,
	}, true
}

func parseReportFolder(name, htmlPath string) ContentItem {
	parts := strings.Split(name, "_")
	var titleParts []string
	var date time.Time
	for _, p := range parts {
		if len(p) == 8 {
			if t, err := time.Parse("20060102", p); err == nil {
				date = t
				continue
			}
		}
		titleParts = append(titleParts, p)
	}
	return ContentItem{
		Title:    strings.Join(titleParts, " "),
		Date:     date,
		Category: "report",
		Slug:     name,
		URLPath:  "/reports/report/" + name,
		FilePath: htmlPath,
	}
}

// splitFrontmatter splits YAML frontmatter (between --- delimiters) from the body.
func splitFrontmatter(data []byte) (map[string]string, []byte) {
	fm := make(map[string]string)
	if !bytes.HasPrefix(data, []byte("---\n")) {
		return fm, data
	}
	rest := data[4:]
	end := bytes.Index(rest, []byte("\n---\n"))
	if end == -1 {
		if bytes.HasSuffix(rest, []byte("\n---")) {
			end = len(rest) - 4
		} else {
			return fm, data
		}
	}
	block := rest[:end]
	body := rest[end+5:]
	if end == len(rest)-4 {
		body = []byte{}
	}
	sc := bufio.NewScanner(bytes.NewReader(block))
	for sc.Scan() {
		line := sc.Text()
		idx := strings.Index(line, ": ")
		if idx < 1 {
			continue
		}
		key := strings.TrimSpace(line[:idx])
		val := strings.Trim(strings.TrimSpace(line[idx+2:]), `"'`)
		fm[key] = val
	}
	return fm, body
}

func parseDateString(s string) time.Time {
	for _, f := range []string{"2006-01-02", "2006-01-02T15:04:05Z07:00", "2006-01-02 15:04:05"} {
		if t, err := time.Parse(f, s); err == nil {
			return t
		}
	}
	return time.Time{}
}

func parseDateFromFilename(name string) time.Time {
	if len(name) >= 10 {
		if t, err := time.Parse("2006-01-02", name[:10]); err == nil {
			return t
		}
	}
	return time.Time{}
}

func filenameToTitle(name string) string {
	if len(name) >= 11 && name[4] == '-' && name[7] == '-' && name[10] == '-' {
		if _, err := time.Parse("2006-01-02", name[:10]); err == nil {
			name = name[11:]
		}
	}
	name = strings.ReplaceAll(name, "-", " ")
	name = strings.ReplaceAll(name, "_", " ")
	if name == "" {
		return ""
	}
	return strings.ToUpper(name[:1]) + name[1:]
}

func extractExcerpt(body []byte) string {
	sc := bufio.NewScanner(bytes.NewReader(body))
	for sc.Scan() {
		line := strings.TrimSpace(sc.Text())
		if line == "" || strings.HasPrefix(line, "#") || strings.HasPrefix(line, "---") {
			continue
		}
		line = strings.ReplaceAll(line, "**", "")
		line = strings.ReplaceAll(line, "*", "")
		line = strings.ReplaceAll(line, "`", "")
		if len(line) > 200 {
			line = line[:200] + "…"
		}
		return line
	}
	return ""
}

// slugToFile finds the filesystem path for a slug by checking all inbox directories.
func slugToFile(slug string) (string, bool) {
	if strings.Contains(slug, "/") || strings.Contains(slug, "\\") || strings.Contains(slug, "..") {
		return "", false
	}
	dirs := []string{
		"/mnt/Obsidian/Inbox",
		"/mnt/Obsidian/Inbox/Research",
		"/mnt/Obsidian/Inbox/Reviews",
		"/mnt/Obsidian/Inbox/Recipes",
	}
	for _, dir := range dirs {
		path := filepath.Join(dir, slug+".md")
		if _, err := os.Stat(path); err == nil {
			return path, true
		}
	}
	return "", false
}
