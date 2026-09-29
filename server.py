import asyncio
import base64
import json
import os
import time

from starlette.applications import Starlette
from starlette.responses import JSONResponse, HTMLResponse, FileResponse
from starlette.routing import Route
from starlette.staticfiles import StaticFiles

import engine
import live
import lookup

APP = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(APP)
SETTINGS = os.path.join(ROOT, "data", "settings.json")


def _settings():
    try:
        with open(SETTINGS) as f:
            return json.load(f)
    except Exception:
        return {}


def _save_settings(d):
    cur = _settings()
    cur.update(d)
    tmp = SETTINGS + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cur, f, indent=1)
    os.replace(tmp, SETTINGS)
    return cur


async def _image_from(request):
    """Accept either multipart upload or a base64 JSON body."""
    ct = (request.headers.get("content-type") or "").lower()
    if "application/json" in ct:
        d = await request.json()
        b64 = d.get("image") or ""
        if "," in b64:
            b64 = b64.split(",", 1)[1]
        return base64.b64decode(b64), d
    form = await request.form()
    up = form.get("image") or form.get("file")
    if up is None:
        return None, {}
    return await up.read(), dict(form)


async def index(request):
    return FileResponse(os.path.join(APP, "ui.html"))


async def api_scan(request):
    """The one call the phone makes per frame. Fast path: detect + match.
    Does NOT hit PimEyes - that is the separate, slow, button-driven step."""
    t0 = time.time()
    try:
        img, meta = await _image_from(request)
    except Exception as e:
        return JSONResponse({"ok": False, "error": "bad image payload: %s" % str(e)[:120]}, status_code=400)
    if not img:
        return JSONResponse({"ok": False, "error": "no image supplied"}, status_code=400)

    want_boxes = meta.get("boxes") in (True, "1", "true")
    try:
        res = engine.scan(img, with_boxes=want_boxes)
    except Exception as e:
        return JSONResponse({"ok": False, "error": "scan failed: %s" % str(e)[:200]})
    if not res.get("ok"):
        return JSONResponse(res, status_code=400)
    return JSONResponse({"ok": True,
                         "ms": int((time.time() - t0) * 1000),
                         "boxes": res.get("boxes") or [],
                         "hits": res.get("hits") or [],
                         "has_face": res.get("has_face")})


async def api_lookup(request):
    """The slow stage: send the face to PimEyes. Explicitly triggered."""
    img, meta = await _image_from(request)
    if not img:
        return JSONResponse({"ok": False, "error": "no image supplied"}, status_code=400)
    face, err = engine.normalise_face(img)
    if not face:
        return JSONResponse({"ok": False, "error": err})

    st = _settings()
    key = st.get("premium_key") or ""
    endpoint = st.get("premium_endpoint") or ""
    if key and endpoint:
        res, lerr = lookup.premium_lookup(face, key, endpoint)
        if lerr:
            return JSONResponse({"ok": False, "error": lerr})
        return JSONResponse({"ok": True, "mode": "premium", "result": res})

    faceinfo, ferr = await asyncio.to_thread(lookup.api_face, face)
    if ferr:
        return JSONResponse({"ok": False, "error": ferr})
    out = {"ok": True, "mode": "free", "face": faceinfo,
           "quota": lookup.quota(),
           "crop": faceinfo.get("crop")}
    if meta.get("full") in (True, "1", "true"):
        # browser_search drives real Chromium, solves the Prosopo captcha and
        # blocks for a minute or more. Run it off the event loop so the live
        # scan keeps answering meanwhile, and collect its play-by-play so the
        # page can show what the solver is actually doing.
        notes = []
        res, berr = await asyncio.to_thread(
            lookup.browser_search, face, 150, notes.append)
        out["steps"] = notes[-40:]
        if berr:
            out["search_error"] = berr
        else:
            out["result"] = res
    return JSONResponse(out)


async def api_quota(request):
    return JSONResponse(lookup.quota())


async def api_index(request):
    if request.method == "POST":
        img, meta = await _image_from(request)
        if not img:
            return JSONResponse({"ok": False, "error": "no image"}, status_code=400)
        rec, err = engine.index_add(img, meta.get("label") or "unnamed")
        if err:
            return JSONResponse({"ok": False, "error": err})
        return JSONResponse({"ok": True, "label": rec["label"], "added": rec["added"]})
    return JSONResponse({"ok": True, "faces": engine.index_list()})


async def api_index_clear(request):
    engine.index_clear()
    return JSONResponse({"ok": True})


async def api_settings(request):
    if request.method == "POST":
        d = await request.json()
        allow = {k: d[k] for k in ("premium_key", "premium_endpoint") if k in d}
        _save_settings(allow)
        return JSONResponse({"ok": True, "has_key": bool(_settings().get("premium_key"))})
    s = _settings()
    return JSONResponse({"ok": True,
                         "has_key": bool(s.get("premium_key")),
                         "endpoint": s.get("premium_endpoint") or "",
                         "threshold": s.get("threshold", 0.80)})


async def health(request):
    return JSONResponse({"ok": True, "service": "facetrace"})




async def api_live_start(request):
    """Open a real, watchable browser on the server so the operator can solve
    the PimEyes captcha from their phone. No model can solve a captcha -- that
    is what it is for -- so we give the human the browser instead."""
    body = {}
    try:
        body = json.loads(await request.body() or b"{}")
    except Exception:
        pass
    okb = await asyncio.to_thread(live.start, body.get("url") or "https://pimeyes.com/en")
    return JSONResponse({"ok": bool(okb), "state": live.status(),
                         "error": None if okb else live.status().get("note")})


async def api_live_status(request):
    return JSONResponse(live.status())


async def api_live_stop(request):
    await asyncio.to_thread(live.stop)
    return JSONResponse({"ok": True, "state": live.status()})


async def api_live_send(request):
    """Push the face the phone just captured into the live browser."""
    img, meta = await _image_from(request)
    if not img:
        return JSONResponse({"ok": False, "error": "no image"}, status_code=400)
    pg, err = await asyncio.to_thread(live.submit_face, img)
    if err:
        return JSONResponse({"ok": False, "error": err})
    return JSONResponse({"ok": True})


async def api_live_solve(request):
    """Run the captcha solver against the live browser, so the operator does
    not have to fight noVNC on a phone. Falls back to a human-readable reason
    when the widget never opens."""
    notes = []
    def _log(m):
        notes.append(m)
        if len(notes) > 40:
            del notes[0]
    try:
        res = await asyncio.to_thread(live.solve_captcha, _log)
    except Exception as e:
        res = {"ok": False, "reason": str(e)[:200]}
    res = dict(res or {})
    res["steps"] = notes
    return JSONResponse({"ok": bool(res.get("ok")), "result": res,
                         "error": None if res.get("ok") else res.get("reason")})


async def api_live_results(request):
    res, err = await asyncio.to_thread(live.read_results)
    if err:
        return JSONResponse({"ok": False, "error": err})
    return JSONResponse({"ok": True, "result": res})




NOVNC_DIR = "/usr/share/novnc"
LIVE_WS_PORT = 6083


async def novnc_page(request):
    fp = os.path.join(NOVNC_DIR, "vnc.html")
    if not os.path.exists(fp):
        return JSONResponse({"error": "novnc not installed"}, status_code=404)
    with open(fp, "rb") as f:
        return HTMLResponse(f.read())


async def novnc_asset(request):
    rel = request.path_params.get("path") or ""
    base = os.path.realpath(NOVNC_DIR)
    fp = os.path.realpath(os.path.join(base, rel))
    if not fp.startswith(base) or not os.path.isfile(fp):
        return JSONResponse({"error": "not found"}, status_code=404)
    return FileResponse(fp)


routes = [
    Route("/", index),
    Route("/api/scan", api_scan, methods=["POST"]),
    Route("/api/lookup", api_lookup, methods=["POST"]),
    Route("/api/quota", api_quota),
    Route("/api/index", api_index, methods=["GET", "POST"]),
    Route("/api/index/clear", api_index_clear, methods=["POST"]),
    Route("/api/settings", api_settings, methods=["GET", "POST"]),
    Route("/novnc/vnc.html", novnc_page),
    Route("/novnc/{path:path}", novnc_asset),
    Route("/api/live/start", api_live_start, methods=["POST"]),
    Route("/api/live/status", api_live_status),
    Route("/api/live/stop", api_live_stop, methods=["POST"]),
    Route("/api/live/send", api_live_send, methods=["POST"]),
    Route("/api/live/results", api_live_results),
    Route("/api/live/solve", api_live_solve, methods=["POST"]),
    Route("/health", health),
]

app = Starlette(routes=routes)
app.mount("/static", StaticFiles(directory=APP), name="static")
