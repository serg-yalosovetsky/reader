"""Fetch an arbitrary web article and extract readable text for TTS listening.
GET /api/article?url=... -> {title, text, html, final_url}"""
from __future__ import annotations
import re
import httpx
from bs4 import BeautifulSoup
from fastapi import APIRouter, HTTPException, Query
from readability import Document

router = APIRouter(prefix="/api", tags=["article"])

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

@router.get("/article")
async def article(url: str = Query(..., min_length=8)):
    if not re.match(r"^https?://", url, re.I):
        raise HTTPException(400, "требуется http(s) URL")
    try:
        async with httpx.AsyncClient(headers={"User-Agent": UA},
                                     follow_redirects=True, timeout=25) as c:
            r = await c.get(url)
            ct = r.headers.get("content-type", "")
            if "html" not in ct and "xml" not in ct and "text" not in ct:
                raise HTTPException(415, f"не текст/HTML: {ct[:40]}")
            html = r.text
            final = str(r.url)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(502, f"не удалось загрузить: {type(e).__name__}")

    try:
        doc = Document(html)
        title = (doc.short_title() or "").strip()
        content_html = doc.summary(html_partial=True)
    except Exception:
        content_html, title = html, ""

    soup = BeautifulSoup(content_html, "lxml")
    for tag in soup(["script", "style", "nav", "footer", "aside"]):
        tag.decompose()
    blocks = [el.get_text(" ", strip=True)
              for el in soup.find_all(["p", "li", "h1", "h2", "h3", "h4", "blockquote", "pre"])]
    text = "\n\n".join(b for b in blocks if b)
    if len(text) < 200:
        text = soup.get_text("\n", strip=True)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if not title:
        st = BeautifulSoup(html, "lxml").find("title")
        title = st.get_text(strip=True) if st else final
    if not text:
        raise HTTPException(422, "не удалось извлечь читаемый текст")
    return {"title": title[:300], "text": text[:80000],
            "chars": len(text), "final_url": final}
