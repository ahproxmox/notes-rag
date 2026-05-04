package main

import (
	"bytes"

	"github.com/yuin/goldmark"
	"github.com/yuin/goldmark/extension"
	"github.com/yuin/goldmark/renderer/html"
	highlighting "github.com/yuin/goldmark-highlighting/v2"
)

var md = goldmark.New(
	goldmark.WithExtensions(
		extension.GFM,
		extension.Strikethrough,
		highlighting.NewHighlighting(
			highlighting.WithStyle("gruvbox"),
		),
	),
	goldmark.WithRendererOptions(html.WithUnsafe()),
)

func renderMarkdown(src []byte) (string, error) {
	var buf bytes.Buffer
	if err := md.Convert(src, &buf); err != nil {
		return "", err
	}
	return buf.String(), nil
}
