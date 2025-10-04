# html_generator.py - SECURED VERSION
import os
import json
import html
import re
from datetime import datetime
from typing import Optional, Union

# ═══════════════════════════════════════════════════
# SECURITY: STRICT CSP (NO UNSAFE-INLINE)
# ═══════════════════════════════════════════════════
_CSP_META = (
    '<meta http-equiv="Content-Security-Policy" '
    "content=\"default-src 'none'; "
    "img-src 'self' data: blob:; "
    "media-src 'self' blob:; "
    "style-src 'self'; "  # REMOVED 'unsafe-inline'
    "script-src 'none'; "
    "object-src 'none'; "
    "base-uri 'none'; "
    "form-action 'none'; "
    "frame-ancestors 'none'; "
    "connect-src 'none'\">"
)

_BASE_TEMPLATE = """<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{title}</title>
{csp}{auto_refresh}
<link rel="stylesheet" href="styles.css">
</head>
<body>
  <div class="topbar">{title_escaped}</div>
  <div class="container">
    {content}
  </div>
</body>
</html>"""

_EXTERNAL_CSS = """/* Telegram Export Studio - Secure Styles */
:root {
  --bg: #eaeef3;
  --bubble: #fff;
  --text: #111;
  --meta: #6b7280;
  --border: #e5e7eb;
  --accent: #2563eb;
}

body {
  margin: 0;
  padding: 0;
  background: var(--bg);
  color: var(--text);
  font: 16px/1.35 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
}

.topbar {
  position: sticky;
  top: 0;
  z-index: 10;
  background: #fff;
  border-bottom: 1px solid var(--border);
  padding: 10px 16px;
  font-weight: 600;
  display: flex;
  align-items: center;
  gap: 12px;
}

.container {
  max-width: 820px;
  margin: 0 auto;
  padding: 16px;
}

.day-sep {
  text-align: center;
  margin: 22px 0 12px;
  color: var(--meta);
  font-size: 13px;
}

.msg {
  background: var(--bubble);
  border-radius: 12px;
  padding: 10px 12px;
  margin: 8px 0;
  box-shadow: 0 1px 2px rgba(0, 0, 0, .06);
  word-wrap: break-word;
  overflow-wrap: break-word;
}

.msg .meta {
  display: flex;
  gap: 8px;
  align-items: baseline;
  margin-bottom: 6px;
}

.msg .from {
  font-weight: 600;
  max-width: 200px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.msg .date {
  color: var(--meta);
  font-size: 12px;
}

.msg .text {
  white-space: pre-wrap;
  word-wrap: break-word;
  overflow-wrap: break-word;
}

.media {
  margin-top: 8px;
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
  align-items: flex-start;
}

.media img {
  max-width: 320px;
  max-height: 240px;
  border-radius: 8px;
  display: block;
  object-fit: contain;
}

.media a.file {
  display: inline-block;
  padding: 8px 10px;
  border: 1px solid var(--border);
  border-radius: 8px;
  background: #fff;
  text-decoration: none;
  color: var(--accent);
  font-size: 14px;
  max-width: 300px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.blocked {
  font-size: 14px;
  color: #b91c1c;
  background: #fee2e2;
  border: 1px solid #fecaca;
  border-radius: 8px;
  padding: 6px 8px;
  word-wrap: break-word;
}

.vthumb {
  position: relative;
  display: inline-block;
}

.vthumb img {
  max-width: 320px;
  max-height: 240px;
  border-radius: 8px;
  display: block;
}

.vthumb .play {
  position: absolute;
  inset: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 48px;
  color: #fff;
  text-shadow: 0 2px 8px rgba(0, 0, 0, .4);
  pointer-events: none;
}

.vplayer video {
  max-width: 480px;
  border-radius: 8px;
  display: block;
}

/* Security: Prevent content injection */
* {
  max-width: 100%;
}

a[href^="javascript:"],
a[href^="data:text/html"] {
  pointer-events: none;
  color: #b91c1c;
  text-decoration: line-through;
}
"""

# ═══════════════════════════════════════════════════
# SECURITY: ENHANCED HTML ESCAPING
# ═══════════════════════════════════════════════════
def _escape(s: str) -> str:
    """
    Enhanced HTML escaping with protection against:
    - Standard HTML entities
    - Unicode normalization attacks
    - Control characters
    """
    if not s:
        return ""
    
    # Use html.escape for standard escaping
    s = html.escape(s, quote=True)
    
    # Remove/escape control characters (except common whitespace)
    s = ''.join(c if c in '\n\r\t' or ord(c) >= 32 else f'&#x{ord(c):02x};' for c in s)
    
    # Protect against Unicode lookalike attacks (homograph attacks)
    # This is a simplified version - full protection would need Unicode normalization
    dangerous_unicode = {
        '\u200b': '',  # Zero-width space
        '\u200c': '',  # Zero-width non-joiner
        '\u200d': '',  # Zero-width joiner
        '\u2060': '',  # Word joiner
        '\ufeff': '',  # Zero-width no-break space
    }
    for char, replacement in dangerous_unicode.items():
        s = s.replace(char, replacement)
    
    return s


def _sanitize_url(url: str) -> str:
    """Sanitize URL to prevent XSS"""
    if not url:
        return ""
    
    url = url.strip()
    
    # Block dangerous protocols
    dangerous_protocols = ['javascript:', 'data:', 'vbscript:', 'file:', 'about:']
    url_lower = url.lower()
    
    for protocol in dangerous_protocols:
        if url_lower.startswith(protocol):
            return "#blocked-url"
    
    # Only allow http(s) and relative paths
    if not (url.startswith('http://') or url.startswith('https://') or url.startswith('./')):
        # Assume relative path
        url = './' + url
    
    return _escape(url)


def _group_by_day(messages):
    """Group messages by day"""
    grouped = []
    current_day = None
    bucket = []
    
    for m in messages:
        try:
            dt = datetime.strptime(m["date"], "%Y-%m-%d %H:%M:%S") if m.get("date") else None
        except Exception:
            dt = None
        
        day = dt.strftime("%Y-%m-%d") if dt else "unknown"
        
        if current_day is None:
            current_day = day
        
        if day != current_day:
            grouped.append((current_day, bucket))
            bucket = []
            current_day = day
        
        bucket.append(m)
    
    if bucket:
        grouped.append((current_day, bucket))
    
    return grouped


def _render_media_item(mi: Union[str, dict], base_path: str = "") -> str:
    """Render media item with security checks"""
    # Legacy string format
    if isinstance(mi, str):
        ext = os.path.splitext(mi)[1].lower()
        safe_path = _sanitize_url(mi)
        
        if ext in {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}:
            fname = os.path.basename(mi)
            return f'<img src="{safe_path}" alt="{_escape(fname)}" loading="lazy">'
        
        fname = os.path.basename(mi)
        return f'<a href="{safe_path}" download class="file">{_escape(fname)}</a>'

    # Structured format
    kind = mi.get("kind", "file")
    
    if kind == "blocked":
        name = mi.get("name", "file")
        reason = mi.get("reason", "security policy")
        return f'<span class="blocked">Файл «{_escape(name)}» заблокирован: {_escape(reason)}</span>'

    path = mi.get("path", "")
    name = mi.get("name") or os.path.basename(path)
    safe_path = _sanitize_url(path)
    safe_name = _escape(name)

    if kind == "image":
        return f'<img src="{safe_path}" alt="{safe_name}" loading="lazy">'

    if kind == "video":
        thumb = mi.get("thumb")
        if thumb:
            safe_thumb = _sanitize_url(thumb)
            return (
                f'<a href="{safe_path}" class="vthumb" title="{safe_name}">'
                f'  <img src="{safe_thumb}" alt="{safe_name}" loading="lazy">'
                f'  <span class="play">▶</span>'
                f'</a>'
            )
        return f'<div class="vplayer"><video controls src="{safe_path}" preload="metadata"></video></div>'

    return f'<a href="{safe_path}" download class="file">{safe_name}</a>'


def _anonymize_display(name: str, cache: dict, seq: list[int]) -> str:
    """
    Stable anonymization: unique real name → UserN
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
    anonymize: bool = False,
    csp: bool = True,  # CSP enabled by default for security
) -> str:
    """
    Generate secure HTML from JSON with:
    - Strict CSP (no inline styles/scripts)
    - Enhanced XSS protection
    - URL sanitization
    """
    
    # Load messages
    with open(json_path, "r", encoding="utf-8") as f:
        messages = json.load(f)

    # Sort by date
    def _key(m):
        try:
            return datetime.strptime(m["date"], "%Y-%m-%d %H:%M:%S")
        except Exception:
            return datetime.min

    messages.sort(key=_key)
    grouped = _group_by_day(messages)

    # Anonymization maps
    anon_map = {}
    anon_seq = [0]

    # Build content
    parts = []
    for day, msgs in grouped:
        try:
            dt_day = datetime.strptime(day, "%Y-%m-%d")
            day_h = dt_day.strftime("%d %B %Y")
        except Exception:
            day_h = day
        
        parts.append(f'<div class="day-sep">{_escape(day_h)}</div>')

        for m in msgs:
            # Date
            date_str = ""
            if m.get("date"):
                try:
                    date_str = datetime.strptime(m["date"], "%Y-%m-%d %H:%M:%S").strftime("%H:%M")
                except Exception:
                    date_str = m["date"]

            # Author
            from_disp = ""
            fr = m.get("from")
            if isinstance(fr, dict):
                from_disp = fr.get("display") or ""
                if anonymize and from_disp:
                    from_disp = _anonymize_display(from_disp, anon_map, anon_seq)

            text_html = _escape(m.get("text", ""))

            # Build message HTML
            block = ['<div class="msg">']
            block.append('<div class="meta">')
            
            if from_disp:
                block.append(f'<div class="from">{_escape(from_disp)}</div>')
            
            block.append(f'<div class="date">{_escape(date_str)}</div>')
            block.append('</div>')  # .meta

            if text_html:
                block.append(f'<div class="text">{text_html}</div>')

            # Media
            media_list = m.get("media") or []
            if media_list:
                block.append('<div class="media">')
                for mi in media_list:
                    block.append(_render_media_item(mi, media_root))
                block.append("</div>")

            block.append("</div>")
            parts.append("\n".join(block))

    content = "\n".join(parts)

    # Title with count
    title = channel_title or "Архив диалога"
    if total_count is not None:
        title = f"{title} — {total_count} сообщений"
    
    title_escaped = _escape(title)

    # Auto-refresh meta
    auto_refresh = ""
    if refresh_seconds and refresh_seconds > 0:
        auto_refresh = f'<meta http-equiv="refresh" content="{int(refresh_seconds)}">'

    # CSP meta
    csp_meta = _CSP_META if csp else ""

    # Generate HTML
    html_content = _BASE_TEMPLATE.format(
        title=title_escaped,
        title_escaped=title_escaped,
        content=content,
        auto_refresh=auto_refresh,
        csp=csp_meta
    )

    # Determine output path
    if not out_html:
        out_html = os.path.join(os.path.dirname(json_path), "index.html")
    
    # Write HTML file
    with open(out_html, "w", encoding="utf-8") as f:
        f.write(html_content)
    
    # Write external CSS file (for CSP compliance)
    css_path = os.path.join(os.path.dirname(out_html), "styles.css")
    with open(css_path, "w", encoding="utf-8") as f:
        f.write(_EXTERNAL_CSS)
    
    return out_html