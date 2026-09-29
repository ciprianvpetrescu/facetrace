"""A real, watchable Chromium session, streamed to the phone.

Why this exists: PimEyes gates the actual face search behind a Prosopo
captcha that cannot be solved from a server. No model can solve it -- a
CAPTCHA exists precisely to prove a human is present. So instead of faking
it, we hand the human the browser: Chromium runs on a virtual display, the
screen is exported over VNC, and the phone renders it in a noVNC canvas.
The operator taps the checkbox themselves, then the search continues and
we scrape the result list.

One session at a time, deliberately: this is a single-operator tool.
"""
from __future__ import annotations

import os
import re
import shutil
import signal
import socket
import subprocess
import threading
import time

DISPLAY_NUM = 98
DISPLAY = ":%d" % DISPLAY_NUM
VNC_PORT = 5902
WS_PORT = 6083
CHROME = "/root/.cache/ms-playwright/chromium-1234/chrome-linux64/chrome"
ALT_CHROME = "/root/.cache/ms-playwright/chromium-1234/chrome-linux/chrome"

_lock = threading.RLock()
_S = {"xvfb": None, "chrome": None, "vnc": None, "ws": None,
      "started": 0, "stage": "idle", "note": ""}


def chrome_bin():
    for c in (CHROME, ALT_CHROME):
        if os.path.exists(c):
            return c
    found = shutil.which("chromium") or shutil.which("chromium-browser")
    return found


def _wait_port(port, timeout=8.0):
    end = time.time() + timeout
    while time.time() < end:
        s = socket.socket()
        s.settimeout(0.4)
        try:
            s.connect(("127.0.0.1", port))
            s.close()
            return True
        except Exception:
            time.sleep(0.25)
        finally:
            try:
                s.close()
            except Exception:
                pass
    return False


def _kill(proc):
    if not proc:
        return
    try:
        proc.terminate()
        proc.wait(timeout=4)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def stop():
    with _lock:
        for k in ("chrome", "vnc", "ws", "xvfb"):
            _kill(_S.get(k))
            _S[k] = None
        _S["stage"] = "idle"
        _S["note"] = ""
    return True


def start(url="https://pimeyes.com/en"):
    """Boot the virtual display + Chromium + VNC + websocket bridge."""
    with _lock:
        stop()
        cb = chrome_bin()
        if not cb:
            _S["note"] = "no chromium binary found"
            _S["stage"] = "error"
            return False
        try:
            env = dict(os.environ, DISPLAY=DISPLAY)
            _S["xvfb"] = subprocess.Popen(
                ["Xvfb", DISPLAY, "-screen", "0", "420x900x24", "-nolisten", "tcp"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            time.sleep(1.2)
            profile = "/tmp/facetrace-live-profile"
            shutil.rmtree(profile, ignore_errors=True)
            _S["chrome"] = subprocess.Popen(
                [cb, "--no-sandbox", "--disable-dev-shm-usage",
                 "--no-first-run", "--disable-gpu", "--remote-debugging-port=9222",
                 "--window-size=420,900", "--window-position=0,0",
                 "--user-data-dir=" + profile, url],
                env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            time.sleep(3.0)
            _S["vnc"] = subprocess.Popen(
                ["x11vnc", "-display", DISPLAY, "-rfbport", str(VNC_PORT),
                 "-nopw", "-forever", "-shared", "-quiet", "-localhost"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if not _wait_port(VNC_PORT):
                _S["note"] = "the display server did not come up"
                _S["stage"] = "error"
                return False
            _S["ws"] = subprocess.Popen(
                ["websockify", "127.0.0.1:%d" % WS_PORT,
                 "127.0.0.1:%d" % VNC_PORT],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if not _wait_port(WS_PORT):
                _S["note"] = "the screen bridge did not come up"
                _S["stage"] = "error"
                return False
            _S["started"] = time.time()
            _S["stage"] = "live"
            _S["note"] = "solve the captcha, then press Continue"
            return True
        except Exception as e:
            _S["note"] = str(e)[:200]
            _S["stage"] = "error"
            return False


def status():
    with _lock:
        alive = bool(_S.get("chrome") and _S["chrome"].poll() is None)
        return {"ok": True, "stage": _S["stage"] if alive else "idle",
                "note": _S["note"], "up": alive,
                "seconds": int(time.time() - _S["started"]) if _S["started"] else 0,
                "ws": WS_PORT}


# --- driving the live session -------------------------------------------

_scratch = {"pw": None, "br": None, "pg": None}


def attach():
    """Connect Playwright to the already-running Chromium via CDP so we can
    both watch it over VNC and control/read it programmatically."""
    from playwright.sync_api import sync_playwright
    if _scratch.get("pg"):
        try:
            _scratch["pg"].title()
            return _scratch["pg"], None
        except Exception:
            _scratch.update({"pw": None, "br": None, "pg": None})
    try:
        pw = sync_playwright().start()
        br = pw.chromium.connect_over_cdp("http://127.0.0.1:9222")
        ctx = br.contexts[0] if br.contexts else br.new_context()
        pg = ctx.pages[0] if ctx.pages else ctx.new_page()
        _scratch.update({"pw": pw, "br": br, "pg": pg})
        return pg, None
    except Exception as e:
        return None, "could not reach the live browser: %s" % str(e)[:160]


def submit_face(image_bytes):
    """Put the captured face into the live browser's upload control."""
    import tempfile
    pg, err = attach()
    if err:
        return None, err
    tf = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
    tf.write(image_bytes)
    tf.close()
    try:
        kill = ("()=>{document.querySelectorAll('div[id*=Cookiebot],'+"
                "'div[class*=Cookiebot]').forEach(e=>e.remove());"
                "document.body.style.overflow='auto'}")
        try:
            pg.evaluate(kill)
        except Exception:
            pass
        ins = pg.query_selector_all("input[type=file]")
        if not ins:
            return None, "the upload control is not on screen yet"
        ins[-1].set_input_files(tf.name)
        time.sleep(1.0)
        return pg, None
    except Exception as e:
        return None, "could not place the face: %s" % str(e)[:160]
    finally:
        try:
            os.unlink(tf.name)
        except Exception:
            pass


def read_results():
    """Read whatever the live page currently shows, once the human has dealt
    with the captcha and the search has run."""
    pg, err = attach()
    if err:
        return None, err
    try:
        url = pg.url
        body = pg.inner_text("body") or ""
        imgs = []
        for tag in pg.query_selector_all("img"):
            src = tag.get_attribute("src") or ""
            if src.startswith("http") and "pimeyes" in src.lower():
                if src not in imgs:
                    imgs.append(src)
        blocked = bool(re.search(r"just a moment|captcha|unusual traffic",
                                 body, re.I))
        return {"url": url, "text": body[:2000],
                "matches": imgs[:24], "captcha": blocked}, None
    except Exception as e:
        return None, "could not read the page: %s" % str(e)[:160]


def solve_captcha(log=None):
    """Try to solve the Prosopo captcha inside the live browser without the
    operator touching noVNC. Returns a dict describing what happened; it never
    raises, because the answer may simply be 'the widget never opened'."""
    pg, err = attach()
    if err:
        return {"ok": False, "reason": err}
    try:
        from . import captcha as _cap
    except Exception:
        import captcha as _cap
    try:
        return _cap.solve(pg, log=log, shot_dir="/opt/apps/facetrace/data")
    except Exception as e:
        return {"ok": False, "reason": "solver error: %s" % str(e)[:160]}
