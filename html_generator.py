# html_generator.py
import os
import json
from datetime import datetime
from typing import Optional, Union

_CSP_META = (
    '<meta http-equiv="Content-Security-Policy" '
    "content=\"default-src 'self'; img-src 'self' data: blob:; "
    "media-src 'self' blob:; object-src 'none'; script-src 'none'; "
    "style-src 'self' 'unsafe-inline'; base-uri 'none'; frame-ancestors 'none'\">"
)

_BASE_TEMPLATE = """<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{title}</title>
{csp}{auto_refresh}
<style>
  :root {{
    --bg: #eaeef3; --bubble: #fff; --text: #111; --meta: #6b7280; --border: #e5e7eb; --accent: #2563eb;
  }}
  body {{ margin:0; padding:0; background:var(--bg); color:var(--text);
         font:16px/1.35 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial; }}
  .topbar {{ position:sticky; top:0; z-index:10; background:#fff; border-bottom:1px solid var(--border);
            padding:10px 16px; font-weight:600; display:flex; align-items:center; gap:12px; }}
  .container {{ max-width:820px; margin:0 auto; padding:16px; }}
  .day-sep {{ text-align:center; margin:22px 0 12px; color:var(--meta); font-size:13px; }}
  .msg {{ background:var(--bubble); border-radius:12px; padding:10px 12px; margin:8px 0;
          box-shadow:0 1px 2px rgba(0,0,0,.06); }}
  .msg .meta {{ display:flex; gap:8px; align-items:baseline; margin-bottom:6px; }}
  .msg .from {{ font-weight:600; }}
  .msg .date {{ color: var(--meta); font-size:12px; }}
  .msg .text {{ white-space:pre-wrap; word-wrap:break-word; }}
  .media {{ margin-top:8px; display:flex; flex-wrap:wrap; gap:10px; align-items:flex-start; }}
  .media img {{ max-width:320px; max-height:240px; border-radius:8px; display:block; }}
  .media a.file {{ display:inline-block; padding:8px 10px; border:1px solid var(--border);
                   border-radius:8px; background:#fff; text-decoration:none; color:var(--accent); font-size:14px; }}
  .blocked {{ font-size:14px; color:#b91c1c; background:#fee2e2; border:1px solid #fecaca; border-radius:8px; padding:6px 8px; }}
  .vthumb {{ position:relative; display:inline-block; }}
  .vthumb img {{ max-width:320px; max-height:240px; border-radius:8px; display:block; }}
  .vthumb .play {{ position:absolute; inset:0; display:flex; align-items:center; justify-content:center;
                   font-size:48px; color:#fff; text-shadow:0 2px 8px rgba(0,0,0,.4); }}
  .vplayer video {{ max-width:480px; border-radius:8px; display:block; }}
</style>
</head>
<body>
  <div class="topbar">{title}</div>
  <div class="container">
    {content}
  </div>
</body>
</html>"""

def _escape(s: str) -> str:
    return (
        s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
         .replace('"', "&quot;").replace("'", "&#39;")
    )

def _group_by_day(messages):
    grouped = []; current_day = None; bucket = []
    for m in messages:
        dt = datetime.strptime(m["date"], "%Y-%m-%d %H:%M:%S") if m["date"] else None
        day = dt.strftime("%Y-%m-%d") if dt else "unknown"
        if current_day is None:
            current_day = day
        if day != current_day:
            grouped.append((current_day, bucket)); bucket = []; current_day = day
        bucket.append(m)
    if bucket: grouped.append((current_day, bucket))
    return grouped

def _render_media_item(mi: Union[str, dict]) -> str:
    if isinstance(mi, str):
        ext = os.path.splitext(mi)[1].lower()
        if ext in {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}:
            return f'<img src="{_escape(mi)}" alt="media">'
        fname = os.path.basename(mi)
        return f'<a href="{_escape(mi)}" download class="file">{_escape(fname)}</a>'

    kind = mi.get("kind", "file")
    if kind == "blocked":
        name = mi.get("name", "file")
        return f'<span class="blocked">Файл «{_escape(name)}» не загружен по правилам безопасности</span>'

    path = mi.get("path", "")
    name = mi.get("name") or os.path.basename(path)

    if kind == "image":
        return f'<img src="{_escape(path)}" alt="{_escape(name)}">'

    if kind == "video":
        thumb = mi.get("thumb")
        if thumb:
            return (
                f'<a href="{_escape(path)}" class="vthumb" title="{_escape(name)}">'
                f'  <img src="{_escape(thumb)}" alt="{_escape(name)}">'
                f'  <span class="play">▶</span>'
                f'</a>'
            )
        return f'<div class="vplayer"><video controls src="{_escape(path)}"></video></div>'

    return f'<a href="{_escape(path)}" download class="file">{_escape(name)}</a>'

def _anonymize_display(name: str, cache: dict, seq: list[int]) -> str:
    """
    Стабильная анонимизация: уникальное реальное имя → UserN.
    cache: real_display -> UserN
    seq: [counter]
    """
    if not name:
        return "User?"
    if name not in cache:
        seq[0] += 1
        cache[name] = f"User{seq[0]}"
    return cache[name]

def generate_html(
    json_path: str,
    media_root: str,
    channel_title: Optional[str] = "Архив диалога",
    out_html: Optional[str] = None,
    refresh_seconds: Optional[int] = None,
    total_count: Optional[int] = None,
    anonymize: bool = False,   # <<<
    csp: bool = False,         # <<<
) -> str:
    with open(json_path, "r", encoding="utf-8") as f:
        messages = json.load(f)

    def _key(m):
        try:
            return datetime.strptime(m["date"], "%Y-%m-%d %H:%M:%S")
        except Exception:
            return datetime.min

    messages.sort(key=_key)
    grouped = _group_by_day(messages)

    # анонимизация имён
    anon_map = {}; anon_seq = [0]

    parts = []
    for day, msgs in grouped:
        try:
            dt_day = datetime.strptime(day, "%Y-%m-%d")
            day_h = dt_day.strftime("%d %B %Y")
        except Exception:
            day_h = day
        parts.append(f'<div class="day-sep">{_escape(day_h)}</div>')

        for m in msgs:
            # дата
            date_str = ""
            if m.get("date"):
                try:
                    date_str = datetime.strptime(m["date"], "%Y-%m-%d %H:%M:%S").strftime("%H:%M")
                except Exception:
                    date_str = m["date"]

            # автор
            from_disp = ""
            fr = m.get("from")
            if isinstance(fr, dict):
                from_disp = fr.get("display") or ""
                if anonymize and from_disp:
                    from_disp = _anonymize_display(from_disp, anon_map, anon_seq)

            text_html = _escape(m.get("text", ""))

            block = [f'<div class="msg">']
            block.append('<div class="meta">')
            if from_disp:
                block.append(f'<div class="from">{_escape(from_disp)}</div>')
            block.append(f'<div class="date">{_escape(date_str)}</div>')
            block.append('</div>')  # .meta

            if text_html:
                block.append(f'<div class="text">{text_html}</div>')

            media_list = m.get("media") or []
            if media_list:
                block.append('<div class="media">')
                for mi in media_list:
                    block.append(_render_media_item(mi))
                block.append("</div>")

            block.append("</div>")
            parts.append("\n".join(block))

    content = "\n".join(parts)

    title = channel_title or "Архив диалога"
    if total_count is not None:
        title = f"{title} — выгружено {total_count} сообщений"

    auto_refresh = ""
    if refresh_seconds and refresh_seconds > 0:
        auto_refresh = f'<meta http-equiv="refresh" content="{int(refresh_seconds)}">'

    csp_meta = _CSP_META if csp else ""

    html = _BASE_TEMPLATE.format(title=_escape(title), content=content, auto_refresh=auto_refresh, csp=csp_meta)

    if not out_html:
        out_html = os.path.join(os.path.dirname(json_path), "index.html")
    with open(out_html, "w", encoding="utf-8") as f:
        f.write(html)
    return out_html
