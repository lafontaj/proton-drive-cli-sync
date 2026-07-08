#!/usr/bin/env python3
"""Génère un PDF imprimable depuis un document Markdown du projet.
Réglages validés : corps 13 pt, interligne 1.5, DejaVu Sans ; emoji remplacés
par des équivalents imprimables (pastilles colorées, glyphes couverts).
Usage : python3 build_pdf.py SOURCE.md SORTIE.pdf [TITRE]"""
import sys, subprocess, markdown

src, out = sys.argv[1], sys.argv[2]
title = sys.argv[3] if len(sys.argv) > 3 else out.rsplit(".", 1)[0]

text = open(src, encoding="utf-8").read()

# Emoji -> équivalents imprimables (DejaVu ne couvre pas les emoji couleur).
REPL = {
    "🟢": '<span style="color:#2e9e3f">●</span>',
    "🟠": '<span style="color:#e08a00">●</span>',
    "🔴": '<span style="color:#d23b3b">●</span>',
    "➕": "+", "➖": "−", "🔄": "↻", "⟳": "↻",
    "⚡": "", "⏰": "", "⏳": "", "🔓": "", "🌍": "", "📜": "", "📅": "",
    "🚫": "⊘", "🗑": "[corbeille]",
    "✅": "[OK]", "❌": "[X]", "⚠": "[!]",
    "🧪": "", "🧹": "", "💾": "", "📂": "", "🔃": "", "🔎": "",
    "▶": ">", "⏭": "»", "⏸": "||", "⏹": "[stop]", "↪": "->", "⊘": "x",
    "✓": "OK", "✗": "X", "🌐": "", "•": "•",
}
for k, v in REPL.items():
    text = text.replace(k, v)

body = markdown.markdown(text, extensions=["tables", "fenced_code"])
html = f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<style>
body {{ font-family: "DejaVu Sans", sans-serif; font-size: 13pt;
       line-height: 1.5; color: #1a1a1a; }}
h1 {{ font-size: 21pt; border-bottom: 2px solid #444; padding-bottom: 4px; }}
h2 {{ font-size: 17pt; border-bottom: 1px solid #999; padding-bottom: 3px;
      margin-top: 26px; }}
h3 {{ font-size: 14.5pt; margin-top: 20px; }}
code {{ font-family: "DejaVu Sans Mono", monospace; font-size: 11pt;
        background: #f2f2f2; padding: 1px 4px; border-radius: 3px; }}
pre {{ background: #f2f2f2; padding: 10px; border-radius: 4px;
       font-size: 10.5pt; line-height: 1.35; white-space: pre-wrap; }}
pre code {{ background: none; padding: 0; }}
table {{ border-collapse: collapse; width: 100%; font-size: 11.5pt; }}
th, td {{ border: 1px solid #999; padding: 5px 8px; text-align: left; }}
th {{ background: #e8e8e8; }}
blockquote {{ border-left: 4px solid #bbb; margin-left: 0; padding-left: 12px;
              color: #444; }}
li {{ margin-bottom: 4px; }}
</style></head><body>{body}</body></html>"""

open("/tmp/doc.html", "w", encoding="utf-8").write(html)
r = subprocess.run(["wkhtmltopdf", "--encoding", "utf-8", "--enable-local-file-access",
                    "--margin-top", "16mm", "--margin-bottom", "16mm",
                    "--margin-left", "15mm", "--margin-right", "15mm",
                    "--quiet", "/tmp/doc.html", out])
sys.exit(r.returncode)
