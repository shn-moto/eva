#!/usr/bin/env python3
"""Eva's print service and file inbox on the hub. Files sent to her in Telegram
are kept here (for printing and for her eyes); a print job becomes a PDF here and goes to
the home's CUPS (the cups_airprint container, host network) with `lp`.

    GET  /health
    GET  /printers                 queues, their state, the jobs waiting
    POST /files   raw bytes, X-Name / X-Mime -> {"file_id", "name", "kind", "pages"}
                                   (a file sent to Eva in Telegram; kept FILES_DAYS)
    POST /print   {kind, ...}      -> {"job", "pages"}
         kind "file":   file_id [from /files], copies, pages ("1-3,5")
    An office file (odt, ods, odp, docx, xlsx, pptx, doc, xls, rtf...) is made a PDF
    when it comes in, by the host's own LibreOffice (convert_host.py, CONVERT_URL):
    she reads and prints that PDF.
         kind "photos": photo_ids [photolib], copies, fit ("fit" | "fill"), color (false: grey)
         kind "text":   title, text (lines; "- " bullets, "# " headings)
         kind "url":    url of a PDF or a picture (a web page is not printed)
         kind "html":   html (a page she wrote: a chart, a report, a table), title,
                        layout "document" (A4 pages, as a browser prints it) | "screen"
                        (a 1280x720 shot of the page as the TV shows it, on one A4 sheet;
                        a dark page is printed inverted - light paper, dark lines)
    POST /cancel  {job}

PDFs are made here because the CUPS container has no fonts at all (its text
filter would print Cyrillic as boxes) and so that every job is A4 with margins.

    CUPS_SERVER   127.0.0.1:631    PRINTER  the default queue
    PHOTOLIB_URL  photolib         MAX_PAGES  a job's cap (20)
"""
import io
import json
import os
import re
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import Response
from fastapi.concurrency import run_in_threadpool

PHOTOLIB = os.environ.get("PHOTOLIB_URL", "http://127.0.0.1:8000")
PRINTER = os.environ.get("PRINTER", "")
MAX_PAGES = int(os.environ.get("MAX_PAGES", "20"))
MAX_COPIES = int(os.environ.get("MAX_COPIES", "5"))
FILES = os.environ.get("FILES_DIR", "/files")
FILES_DAYS = int(os.environ.get("FILES_DAYS", "7"))
FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
FONT_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
DPI = 200
A4 = (int(8.27 * DPI), int(11.69 * DPI))            # 1654 x 2338 px
MARGIN = int(0.3 * DPI)                              # ~7.6 mm: inside the printer's unprintable edge
app = FastAPI()


def _run(*args, timeout=30):
    r = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    return r.returncode, (r.stdout + r.stderr).strip()


@app.get("/health")
def health():
    code, out = _run("lpstat", "-r")
    return {"ok": code == 0 and "not running" not in out, "cups": out, "printer": PRINTER}


@app.get("/printers")
def printers():
    code, out = _run("lpstat", "-p")
    if code != 0 and "No destinations" in out:
        return {"printers": [], "jobs": [], "note": "в CUPS нет ни одного принтера"}
    found, cur = [], None
    for line in out.splitlines():
        m = re.match(r"printer (\S+) (?:is )?(idle|now printing \S+|disabled)", line)
        if m:
            cur = {"name": m.group(1), "state": m.group(2).replace("now printing", "печатает"),
                   "default": m.group(1) == PRINTER, "message": ""}
            found.append(cur)
        elif cur is not None and line[:1] in ("\t", " ") and line.strip():
            # lpstat -p: a reason (paper out, paused...) is an indented line under the queue
            cur["message"] = (cur["message"] + " " + line.strip()).strip()[:300]
    _, d = _run("lpstat", "-d")                       # "system default destination: X"
    default = d.split(":")[-1].strip() if ":" in d else ""
    for x in found:
        x["default"] = x["name"] == (PRINTER or default)
    _, jobs = _run("lpstat", "-o")
    return {"printers": found, "jobs": [j for j in jobs.splitlines() if j.strip()],
            "note": "лазерный, чёрно-белый, A4" if PRINTER else ""}


# ---- making the PDF ----------------------------------------------------------------------

def _photo(image_id):
    with urllib.request.urlopen(f"{PHOTOLIB}/image/{int(image_id)}/full", timeout=60) as r:
        return r.read()


def _pages_from_images(blobs, fit="fit", color=False):
    from PIL import Image, ImageOps
    pages = []
    for blob in blobs:
        img = Image.open(io.BytesIO(blob))
        img = ImageOps.exif_transpose(img).convert("RGB")
        if not color:
            # a mono laser: grey with the contrast stretched prints better than its own dithering of colour
            img = ImageOps.autocontrast(img.convert("L"), cutoff=1)
        landscape = img.width > img.height
        W, H = (A4[1], A4[0]) if landscape else A4
        box = (W - 2 * MARGIN, H - 2 * MARGIN)
        if fit == "fill":
            img = ImageOps.fit(img, box, Image.Resampling.LANCZOS)
        else:
            img = ImageOps.contain(img, box, Image.Resampling.LANCZOS)
        page = Image.new(img.mode, (W, H), 255 if img.mode == "L" else (255, 255, 255))
        page.paste(img, ((W - img.width) // 2, (H - img.height) // 2))
        pages.append(page)
    if not pages:
        raise HTTPException(400, "нечего печатать")
    buf = io.BytesIO()
    pages[0].save(buf, "PDF", resolution=DPI, save_all=True, append_images=pages[1:])
    return buf.getvalue(), len(pages)


def _pdf_from_text(title, text):
    from fpdf import FPDF
    from fpdf.enums import XPos, YPos
    nl = dict(new_x=XPos.LMARGIN, new_y=YPos.NEXT)     # every block ends at the start of the next line
    pdf = FPDF(format="A4", unit="mm")
    pdf.set_margins(18, 16, 18)
    pdf.set_auto_page_break(True, 16)
    pdf.add_font("dv", "", FONT)
    pdf.add_font("dv", "B", FONT_BOLD)
    pdf.add_page()
    if title:
        pdf.set_font("dv", "B", 17)
        pdf.multi_cell(0, 9, title.strip(), **nl)
        pdf.ln(3)
    for raw in (text or "").splitlines():
        line = raw.rstrip()
        if not line.strip():
            pdf.ln(4)
            continue
        if line.lstrip().startswith("# "):
            pdf.ln(2)
            pdf.set_font("dv", "B", 13)
            pdf.multi_cell(0, 7, line.lstrip()[2:].strip(), **nl)
            continue
        pdf.set_font("dv", "", 12)
        m = re.match(r"\s*(\d+[.)]|[-*•])\s+(.*)", line)
        if m:
            bullet = "•" if m.group(1) in "-*•" else m.group(1)
            pdf.set_x(pdf.l_margin)
            pdf.cell(8, 6.5, bullet, new_x=XPos.RIGHT, new_y=YPos.TOP)
            pdf.multi_cell(0, 6.5, m.group(2), **nl)
        else:
            pdf.multi_cell(0, 6.5, line, **nl)
        if pdf.page_no() > MAX_PAGES:
            break
    return bytes(pdf.output()), pdf.page_no()


CHROME = os.environ.get("CHROME", "chromium")
LIB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib")   # Chart.js, as the TV's /lib


def _chrome(work, html, *args):
    """Her page in headless Chromium, offline: /lib/... resolves to the copy here. -> its output"""
    with open(os.path.join(work, "page.html"), "w", encoding="utf-8") as f:
        f.write(re.sub(r"""(src|href)=(["'])/lib/""", r"\1=\2lib/", html))
    os.symlink(LIB, os.path.join(work, "lib"))
    _, out = _run(CHROME, "--headless=new", "--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage",
                  "--hide-scrollbars", "--no-pdf-header-footer", "--virtual-time-budget=8000",
                  f"--user-data-dir={work}/profile", *args, f"file://{work}/page.html", timeout=90)
    return out


def _from_html(html, layout="document"):
    import shutil
    if not (html or "").strip():
        raise HTTPException(400, "пустая страница")
    work = tempfile.mkdtemp()
    try:
        if layout == "screen":
            shot = os.path.join(work, "shot.png")
            out = _chrome(work, html, "--window-size=1280,720", "--force-device-scale-factor=2", "--screenshot=" + shot)
            if not os.path.exists(shot):
                raise HTTPException(502, f"страница не отрисовалась: {out[-300:]}")
            from PIL import Image, ImageOps, ImageStat
            img = Image.open(shot).convert("RGB")
            if ImageStat.Stat(img.convert("L")).mean[0] < 100:
                # a TV page is dark; on a mono laser that is a sheet of toner
                img = ImageOps.invert(img)
            buf = io.BytesIO()
            img.save(buf, "PNG")
            return _pages_from_images([buf.getvalue()])
        path = os.path.join(work, "out.pdf")
        out = _chrome(work, html, "--print-to-pdf=" + path)
        if not os.path.exists(path):
            raise HTTPException(502, f"страница не напечаталась в PDF: {out[-300:]}")
        data = open(path, "rb").read()
        return data, _pdf_pages(data)
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _from_url(url):
    if not url.startswith(("http://", "https://")):
        raise HTTPException(400, "нужна ссылка http(s)")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (print service)"})
    with urllib.request.urlopen(req, timeout=60) as r:
        kind = (r.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        data = r.read(25 * 1024 * 1024 + 1)
    if len(data) > 25 * 1024 * 1024:
        raise HTTPException(400, "файл больше 25 МБ")
    if kind == "application/pdf" or data[:5] == b"%PDF-":
        return data, None
    if kind.startswith("image/"):
        return _pages_from_images([data])
    raise HTTPException(400, f"по ссылке не PDF и не картинка ({kind or 'неизвестно'}): веб-страницы не печатаю - "
                             "перескажи текстом и напечатай как текст")


# ---- files sent to Eva (Telegram) ---------------------------------------------------------

def _pdf_pages(data):
    try:
        from pypdf import PdfReader
        return len(PdfReader(io.BytesIO(data)).pages)
    except Exception:
        return None


OFFICE = re.compile(r"\.(odt|ott|ods|ots|odp|otp|odg|fodt|fods|doc|docx|dot|dotx|xls|xlsx|xlsm|ppt|pptx|pps|ppsx|rtf|wpd)$", re.I)


CONVERT = os.environ.get("CONVERT_URL", "http://127.0.0.1:8910/convert")


def _to_pdf(data, name):
    """An office file -> PDF bytes, by the host's own LibreOffice (convert_host.py).
    No LibreOffice on the host (or the converter down): said so, nothing converted."""
    req = urllib.request.Request(CONVERT, data=data, headers={
        "Content-Type": "application/octet-stream", "X-Name": urllib.parse.quote(name or "file")})
    try:
        with urllib.request.urlopen(req, timeout=200) as r:
            return r.read()
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read()).get("detail")
        except Exception:
            detail = str(e)
        raise HTTPException(400, f"«{name}» в PDF не перевёлся: {detail}")
    except OSError as e:
        raise HTTPException(503, f"конвертер офисных файлов на хосте не отвечает ({e}) - "
                                 "нужен LibreOffice на хосте и convert_host.py")


def _kind(name, mime, data):
    if data[:5] == b"%PDF-" or mime == "application/pdf":
        return "pdf"
    if OFFICE.search(name or "") or "opendocument" in (mime or "") or "officedocument" in (mime or "") \
            or mime in ("application/msword", "application/vnd.ms-excel", "application/vnd.ms-powerpoint",
                        "application/rtf", "text/rtf"):
        return "office"
    if (mime or "").startswith("image/") or re.search(r"\.(jpe?g|png|webp|heic|bmp|gif|tiff?)$", name or "", re.I):
        return "image"
    return "other"


@app.post("/files")
async def files(request: Request):
    data = await request.body()
    if not data or len(data) > 25 * 1024 * 1024:
        raise HTTPException(400, "пустой файл или больше 25 МБ")
    name = urllib.parse.unquote(request.headers.get("X-Name") or "file")[:120]   # quoted: a header is latin-1
    mime = (request.headers.get("X-Mime") or "").lower()
    os.makedirs(FILES, exist_ok=True)
    now = time.time()
    for fn in os.listdir(FILES):                      # a week is enough to get it printed
        if now - os.path.getmtime(os.path.join(FILES, fn)) > FILES_DAYS * 86400:
            os.remove(os.path.join(FILES, fn))
    fid = uuid.uuid4().hex[:10]
    kind = _kind(name, mime, data)
    with open(os.path.join(FILES, fid), "wb") as f:
        f.write(data)
    meta = {"name": name, "mime": mime, "kind": kind, "size": len(data),
            "pages": _pdf_pages(data) if kind == "pdf" else (1 if kind == "image" else None)}
    if kind == "office":
        # converted now, while she gets the note: she reads and prints the PDF
        try:
            pdf = await run_in_threadpool(_to_pdf, data, name)
            with open(os.path.join(FILES, fid + ".pdf"), "wb") as f:
                f.write(pdf)
            meta.update(pdf=True, pages=_pdf_pages(pdf))
        except HTTPException as e:
            meta.update(pdf=False, error=str(e.detail)[:300])
    with open(os.path.join(FILES, fid + ".json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False)
    return dict(meta, file_id=fid)


def _file(fid):
    if not re.fullmatch(r"[0-9a-f]{10}", fid or ""):
        raise HTTPException(400, "file_id?")
    p = os.path.join(FILES, fid)
    if not os.path.exists(p):
        raise HTTPException(404, "файла уже нет (хранятся неделю) - пусть пришлют ещё раз")
    meta = json.load(open(p + ".json", encoding="utf-8"))
    if meta.get("kind") == "office" and os.path.exists(p + ".pdf"):
        return open(p + ".pdf", "rb").read(), meta          # an office file: its PDF
    return open(p, "rb").read(), meta


def _lp(pdf, printer, copies, title, pages=None):
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
        f.write(pdf)
        path = f.name
    try:
        if pages and not re.fullmatch(r"\d+(-\d+)?(,\d+(-\d+)?)*", pages):
            raise HTTPException(400, "страницы: например 1-3,5")
        args = ["lp", "-n", str(copies), "-t", (title or "Ева")[:60], "-o", "media=A4",
                "-o", f"page-ranges={pages or f'1-{MAX_PAGES}'}"]
        if printer:
            args += ["-d", printer]
        code, out = _run(*args, path, timeout=60)
    finally:
        os.unlink(path)
    if code != 0:
        raise HTTPException(502, f"принтер не принял задание: {out}")
    m = re.search(r"request id is (\S+)", out)
    return m.group(1) if m else out


def _print(b):
    kind = b.get("kind")
    copies = max(1, min(int(b.get("copies") or 1), MAX_COPIES))
    printer = b.get("printer") or PRINTER
    title = (b.get("title") or "").strip()
    if kind == "photos":
        ids = list(b.get("photo_ids") or [])[:MAX_PAGES]
        pdf, pages = _pages_from_images([_photo(i) for i in ids], b.get("fit") or "fit", bool(b.get("color")))
        title = title or f"фото {', '.join(str(i) for i in ids[:5])}"
    elif kind == "text":
        pdf, pages = _pdf_from_text(title, b.get("text") or "")
    elif kind == "url":
        pdf, pages = _from_url(b.get("url") or "")
        title = title or (b.get("url") or "")[-60:]
    elif kind == "html":
        pdf, pages = _from_html(b.get("html") or "", b.get("layout") or "document")
        title = title or "страница"
    elif kind == "file":
        data, meta = _file(b.get("file_id"))
        if meta["kind"] == "pdf" or (meta["kind"] == "office" and meta.get("pdf")):
            pdf, pages = data, meta.get("pages")
        elif meta["kind"] == "image":
            pdf, pages = _pages_from_images([data], b.get("fit") or "fit", bool(b.get("color")))
        else:
            raise HTTPException(400, f"«{meta['name']}» - не PDF, не картинка и не офисный документ: "
                                     "такое не печатаю, пусть пришлют PDF")
        title = title or meta["name"]
    else:
        raise HTTPException(400, "kind: photos | text | url | file | html")
    job = _lp(pdf, printer, copies, title, b.get("pages"))
    return {"job": job, "pages": pages, "copies": copies, "printer": printer}


@app.post("/print")
async def print_route(request: Request):
    return await run_in_threadpool(_print, await request.json())


@app.post("/cancel")
async def cancel(request: Request):
    job = str((await request.json()).get("job") or "")
    if not re.fullmatch(r"[\w.-]+", job):
        raise HTTPException(400)
    code, out = _run("cancel", job)
    if code != 0:
        raise HTTPException(400, out)
    return {"ok": True}


# ---- a file for Eva's eyes -----------------------------------------------------------------

@app.get("/files/{fid}/meta")
def file_meta(fid: str):
    return dict(_file(fid)[1], file_id=fid)


@app.get("/files/{fid}/raw")
def file_raw(fid: str):
    data, meta = _file(fid)
    mime = "application/pdf" if meta.get("kind") == "office" and meta.get("pdf") else meta.get("mime")
    return Response(content=data, media_type=mime or "application/octet-stream")


@app.on_event("startup")
def convert_waiting():
    """Office files that came before LibreOffice was here (kind "other") or whose
    conversion failed: made PDFs now, so nobody has to send them again."""
    if not os.path.isdir(FILES):
        return
    for fn in os.listdir(FILES):
        if not fn.endswith(".json"):
            continue
        p = os.path.join(FILES, fn)
        try:
            meta = json.load(open(p, encoding="utf-8"))
            fid = fn[:-5]
            if meta.get("pdf") or not (OFFICE.search(meta.get("name", "")) or meta.get("kind") == "office"):
                continue
            pdf = _to_pdf(open(os.path.join(FILES, fid), "rb").read(), meta["name"])
            with open(os.path.join(FILES, fid + ".pdf"), "wb") as f:
                f.write(pdf)
            meta.update(kind="office", pdf=True, pages=_pdf_pages(pdf))
            meta.pop("error", None)
            with open(p, "w", encoding="utf-8") as f:
                json.dump(meta, f, ensure_ascii=False)
        except Exception as e:
            print("convert", fn, e, flush=True)


@app.get("/files/{fid}/jpeg")
def file_jpeg(fid: str, size: int = 1568):
    """A picture as JPEG of at most `size` px (Claude sees up to ~1568 px)."""
    from PIL import Image, ImageOps
    data, meta = _file(fid)
    if meta["kind"] != "image":
        raise HTTPException(400, "это не картинка")
    img = ImageOps.exif_transpose(Image.open(io.BytesIO(data))).convert("RGB")
    img.thumbnail((size, size), Image.Resampling.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=88)
    return Response(content=buf.getvalue(), media_type="image/jpeg")
