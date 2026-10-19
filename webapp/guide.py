"""Инструкция внутри программы: /guide рендерит INSTRUCTION_GET_TOKEN.md в стиле программы.

Файл в ZIP — сырой Markdown, а имя «*.md» Telegram и браузеры принимают за адрес сайта
(.md — доменная зона). Поэтому люди открывают инструкцию здесь, а не файлом.
"""

from __future__ import annotations

import re

from fastapi import APIRouter
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse

from webapp import paths

router = APIRouter()

GUIDE_FILE = paths.PROJECT / "INSTRUCTION_GET_TOKEN.md"
GUIDE_IMAGES = paths.PROJECT / "docs" / "images"
GUIDE_GITHUB_URL = "https://github.com/PXSYS13/yadisk_backup/blob/main/INSTRUCTION_GET_TOKEN.md"
_GUIDE_IMAGE_RE = re.compile(r"[a-z0-9-]+\.(?:webp|png)")
_guide_cache: dict = {"mtime": None, "html": None}
_ALERT_TITLES = {"NOTE": "Примечание", "TIP": "Совет", "IMPORTANT": "Важно",
                 "WARNING": "Внимание", "CAUTION": "Осторожно"}

_GUIDE_PAGE = """<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Инструкция · Yandex.Disk Backup</title>
<style>
  :root{--bg:#000;--surface:#1c1c1e;--surface2:#2c2c2e;--text:#f5f5f7;--muted:#aeaeb2;
        --line:rgba(255,255,255,.1);--link:#6cb8ff;--green:#30d158;--yellow:#ffd60a;--red:#ff453a}
  body{background:var(--bg);color:var(--text);margin:0;padding:24px 16px 64px;
       font:15px/1.6 -apple-system,BlinkMacSystemFont,'Segoe UI',Inter,system-ui,sans-serif}
  main{max-width:860px;margin:0 auto}
  a{color:var(--link)} a:hover{text-decoration:underline}
  .top{display:flex;justify-content:space-between;align-items:center;margin-bottom:24px;gap:12px;flex-wrap:wrap}
  .top a{text-decoration:none;padding:6px 12px;border:1px solid var(--line);border-radius:10px}
  h1{font-size:26px;line-height:1.25} h2{font-size:21px;margin-top:40px;padding-top:16px;border-top:1px solid var(--line)}
  h3{font-size:17px;margin-top:24px}
  code{background:var(--surface2);padding:2px 6px;border-radius:6px;font-size:13.5px}
  pre{background:var(--surface);padding:12px 14px;border-radius:10px;overflow:auto} pre code{background:none;padding:0}
  kbd{background:var(--surface2);border:1px solid var(--line);border-bottom-width:2px;border-radius:5px;padding:1px 6px;font-size:13px}
  table{border-collapse:collapse;width:100%;margin:12px 0;font-size:14px;display:block;overflow-x:auto}
  th,td{border:1px solid var(--line);padding:8px 10px;text-align:left;vertical-align:top} th{background:var(--surface)}
  blockquote{margin:16px 0;padding:10px 16px;border-left:3px solid var(--link);background:var(--surface);border-radius:0 10px 10px 0}
  blockquote.tip{border-color:var(--green)} blockquote.warning,blockquote.caution{border-color:var(--red)}
  blockquote.important{border-color:var(--yellow)}
  img{max-width:100%;vertical-align:middle} hr{border:0;border-top:1px solid var(--line);margin:28px 0}
  img[src^="docs/"]{display:block;margin:16px 0;border:1px solid var(--line);border-radius:12px}
</style></head><body><main>
<div class="top"><a href="/">← Вернуться в программу</a>
<a href="https://github.com/PXSYS13/yadisk_backup/blob/main/INSTRUCTION_GET_TOKEN.md" target="_blank" rel="noopener">Открыть на GitHub</a></div>
{{body}}
</main></body></html>"""


_GUIDE_SCHEME = (
    "**Схема:** ваш аккаунт Яндекса → 🔑 токен (Полигон) и 🍪 cookies (Cookie-Editor) → "
    "программа на `http://localhost:8000` → «Скачать всё» → 📁 папка на вашем компьютере.\n\n"
)


def _github_slug(value: str, separator: str = "-") -> str:
    """Якоря как у GitHub — оглавление инструкции работает и внутри программы."""
    value = value.strip().lower()
    value = re.sub(r"[^\w\- ]", "", value)
    return value.replace(" ", separator)


def render_guide() -> str:
    """INSTRUCTION_GET_TOKEN.md → HTML в стиле программы."""
    import markdown

    mtime = GUIDE_FILE.stat().st_mtime
    if _guide_cache["mtime"] == mtime:
        return _guide_cache["html"]
    text = GUIDE_FILE.read_text(encoding="utf-8")
    # Диаграмма mermaid без GitHub выглядит как код — показываем ту же схему текстом
    text = re.sub(r"```mermaid.*?```\s*", _GUIDE_SCHEME, text, flags=re.S)
    body = markdown.markdown(
        text,
        extensions=["tables", "fenced_code", "toc", "sane_lists"],
        extension_configs={"toc": {"slugify": _github_slug}},
    )
    body = re.sub(
        r"<blockquote>\s*<p>\[!(NOTE|TIP|IMPORTANT|WARNING|CAUTION)\]\s*",
        lambda m: f'<blockquote class="{m.group(1).lower()}"><p><strong>{_ALERT_TITLES[m.group(1)]}.</strong> ',
        body,
    )
    html = _GUIDE_PAGE.replace("{{body}}", body)
    _guide_cache.update({"mtime": mtime, "html": html})
    return html


@router.get("/guide", response_class=HTMLResponse)
def guide():
    if not GUIDE_FILE.exists():
        return RedirectResponse(url=GUIDE_GITHUB_URL, status_code=307)
    return HTMLResponse(render_guide())


@router.get("/docs/images/{name}")
def guide_image(name: str):
    """Картинки инструкции: один и тот же путь docs/images/… работает и на GitHub, и в /guide."""
    path = GUIDE_IMAGES / name
    if not _GUIDE_IMAGE_RE.fullmatch(name) or not path.is_file():
        return JSONResponse({"ok": False, "error": "нет такой картинки"}, status_code=404)
    return FileResponse(str(path), media_type="image/webp" if name.endswith(".webp") else "image/png")
