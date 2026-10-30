"""把 vite 的 demo 产物（dist-demo）合并成一个独立 HTML。

用法：cd web && npx vite build --config vite.demo.config.ts && python3 scripts/build-demo-singlefile.py
产物：仓库根 iterwave-prototype.html（JS / CSS / 图标全部内联，双击即可打开）
"""
import os
import re
import sys

WEB = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DIST = os.path.join(WEB, "dist-demo")
OUT = os.path.join(os.path.dirname(WEB), "iterwave-prototype.html")

html = open(os.path.join(DIST, "demo.html"), encoding="utf-8").read()
js = open(os.path.join(DIST, "demo.js"), encoding="utf-8").read()
css = open(os.path.join(DIST, "demo.css"), encoding="utf-8").read()

# 防止内容里出现 </script> / </style> 提前闭合标签
js = js.replace("</script>", "<\\/script>")
css = css.replace("</style>", "<\\/style>")

html = re.sub(r'<script[^>]*src="./demo\.js"[^>]*></script>', "", html)
html = re.sub(r'<link[^>]*href="./demo\.css"[^>]*>', "", html)

favicon = (
    '<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns=\'http://www.w3.org/2000/svg\' '
    "viewBox='0 0 32 32'%3E%3Crect width='32' height='32' rx='8' fill='%234263eb'/%3E"
    "%3Ctext x='16' y='23' font-size='20' text-anchor='middle' fill='white' "
    "font-family='sans-serif'%3EI%3C/text%3E%3C/svg%3E\" />"
)

html = html.replace(
    "<title>IterWave — 全页面静态原型演示</title>",
    "<title>IterWave — 全页面静态原型演示</title>\n    " + favicon + "\n    <style>\n" + css + "\n    </style>",
).replace(
    '<div id="root"></div>',
    '<div id="root"></div>\n    <script type="module">\n' + js + "\n    </script>",
)

with open(OUT, "w", encoding="utf-8") as f:
    f.write(html)
print("written", OUT, os.path.getsize(OUT), "bytes")
