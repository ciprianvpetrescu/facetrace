"""
The web-lookup stage: PimEyes.

Two paths, and it matters which one you are on:

  api_face()   POST /api/face with the image as base64. Needs no browser, no
               captcha, no account. It returns the face hash PimEyes assigns.

  browser_search()  drives a real Chromium through the site. This is required
               because starting an actual search is gated behind a Prosopo
               captcha session that cannot be solved server-side. It is slow,
               it is limited to ten searches per IP per day, and it will break
               whenever the site's markup changes. It is a scraper.

There is no official PimEyes API. If you hold a commercial key, drop it in the
settings and premium_lookup() will use that instead - the interface is here for
you, but the endpoint is theirs to document.
"""

import base64
import json
import captcha
import os
import re
import time
import urllib.request
import http.cookiejar

APP = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(APP)
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/151.0 Safari/537.36")


class Session:
    """A cookie-jar session. The Cloudflare cookie has to be earned on the
    homepage before /api/face will answer."""

    def __init__(self):
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar))
        self.warm = False

    def _headers(self, json_body=True):
        h = {"User-Agent": UA, "Referer": "https://pimeyes.com/en",
             "Origin": "https://pimeyes.com", "Accept": "application/json"}
        if json_body:
            h["Content-Type"] = "application/json"
        return h

    def ensure(self):
        if self.warm:
            return
        try:
            self.opener.open(urllib.request.Request(
                "https://pimeyes.com/en", headers={"User-Agent": UA}), timeout=25).read()
        except Exception:
            pass
        self.warm = True

    def get(self, path, timeout=25):
        self.ensure()
        req = urllib.request.Request("https://pimeyes.com/api/" + path,
                                     headers=self._headers(False))
        return json.loads(self.opener.open(req, timeout=timeout).read())

    def post(self, path, body, timeout=30):
        self.ensure()
        req = urllib.request.Request(
            "https://pimeyes.com/api/" + path,
            data=json.dumps(body).encode(),
            headers=self._headers(), method="POST")
        return json.loads(self.opener.open(req, timeout=timeout).read())


_SESSION = None


def session():
    global _SESSION
    if _SESSION is None:
        _SESSION = Session()
    return _SESSION


def quota():
    """How many searches this IP has left today."""
    try:
        d = session().get("search/info")
        return {"ok": True, "used": d.get("searchPerformed"),
                "limit": d.get("searchLimit"),
                "blocked": d.get("searchBlocked"),
                "deep": d.get("deepSearchAvailable"),
                "reset": d.get("usageResetAt")}
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}


def api_face(image_bytes):
    """Upload a face to PimEyes and get back its hash plus a normalised crop.
    This is the endpoint that works without a browser."""
    b64 = base64.b64encode(image_bytes).decode()
    try:
        d = session().post("face", {"base64": b64})
    except Exception as e:
        return None, "pimeyes refused the upload: %s" % str(e)[:160]
    faces = d.get("faces") or []
    if not faces:
        return None, "pimeyes found no face in that image"
    f = faces[0]
    return {"hash": f.get("hash"),
            "crop": f.get("base64"),
            "count": len(faces)}, None


BLOCK_PAT = re.compile(r"just a moment|checking your browser|unusual traffic|"
                       r"cf-chl|turnstile|enable javascript and cookies", re.I)


def _block_wall(pg):
    """True when the origin has thrown a Cloudflare challenge instead of the
    page. That is a different problem from the Prosopo captcha: nothing can be
    clicked, the page itself was never served."""
    try:
        body = (pg.inner_text("body") or "")[:4000]
        title = (pg.title() or "")
    except Exception:
        return False
    return bool(BLOCK_PAT.search(body) or BLOCK_PAT.search(title))


def browser_search(image_bytes, timeout=120, log=None):
    """Run a full search through a real Chromium. Slow, captcha-gated, and
    capped at ten a day per IP. Returns the result URL and whatever page text
    we can read; a failure here is expected sometimes and is reported, not
    hidden."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return None, "playwright is not installed on the server"

    import tempfile
    tf = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
    tf.write(image_bytes)
    tf.close()

    kill = (
        "()=>{document.querySelectorAll('div[id*=Cookiebot],div[class*=Cookiebot]')"
        ".forEach(e=>e.remove());['CybotCookiebotDialog',"
        "'CybotCookiebotDialogBodyUnderlay'].forEach(i=>{const d="
        "document.getElementById(i);if(d)d.remove()});document.body.style.overflow='auto'}"
    )
    say = log or (lambda *a: None)
    out = {"url": None, "text": "", "matches": [], "stage": "start",
           "captcha": None}
    try:
        with sync_playwright() as p:
            b = p.chromium.launch(args=["--no-sandbox", "--disable-dev-shm-usage"])
            ctx = b.new_context(viewport={"width": 1400, "height": 1100})
            pg = ctx.new_page()
            pg.goto("https://pimeyes.com/en", timeout=60000)
            pg.wait_for_timeout(3500)
            pg.evaluate(kill)
            pg.wait_for_timeout(1000)
            out["stage"] = "home"
            pg.get_by_role("button", name="Start Search").first.click()
            pg.wait_for_timeout(2500)
            pg.evaluate(kill)
            inputs = pg.query_selector_all("input[type=file]")
            if len(inputs) < 2:
                return None, "the upload control was not found (site markup changed)"
            inputs[1].set_input_files(tf.name)
            pg.wait_for_timeout(6000)
            pg.evaluate(kill)
            out["stage"] = "uploaded"
            for txt in ["over 18", "Terms of Service", "Privacy Policy"]:
                try:
                    pg.get_by_text(txt, exact=False).first.click(timeout=3000)
                except Exception:
                    pass
            pg.wait_for_timeout(1200)
            # the modal's own submit
            pg.evaluate(
                "()=>{const m=[...document.querySelectorAll('.fixed')].find(e=>"
                "e.innerText&&e.innerText.includes('Face Search'));if(m){const t="
                "[...m.querySelectorAll('button')].filter(e=>e.offsetParent!==null)"
                ".find(e=>/Start Search/.test(e.innerText||''));if(t)t.click();}}"
            )
            out["stage"] = "submitted"
            # the Prosopo challenge sits between the modal and the results:
            # pierce its shadow root, read the grid with DeepSeek vision, click.
            try:
                if _block_wall(pg):
                    res = {"ok": False, "rounds": 0, "clicks": 0,
                           "reason": ("this server's IP hit Cloudflare's bot "
                                      "check - the PimEyes page was never served, "
                                      "so there is no captcha to solve here")}
                    say("cloudflare bot check on this IP - stopping")
                else:
                    res = captcha.solve(pg, out, log=say)
                out["captcha"] = res
                say("captcha: %s (%d round(s), %d click(s))" % (
                    res.get("reason"), res.get("rounds") or 0,
                    res.get("clicks") or 0))
            except Exception as e:
                out["captcha"] = {"ok": False, "reason": "%s: %s" % (
                    type(e).__name__, str(e)[:200])}
                say("captcha solver blew up: %s" % e)
            for _ in range(16):
                pg.wait_for_timeout(2500)
                try:
                    pg.evaluate(kill)
                except Exception:
                    pass
                if pg.url != "https://pimeyes.com/en":
                    out["url"] = pg.url
                    break
            try:
                out["text"] = (pg.inner_text("body") or "")[:1500]
            except Exception:
                pass
            b.close()
    except Exception as e:
        return None, "the browser search failed at stage '%s': %s" % (
            out.get("stage"), str(e)[:200])
    finally:
        try:
            os.unlink(tf.name)
        except Exception:
            pass

    if not out["url"]:
        cap = out.get("captcha") or {}
        why = cap.get("reason")
        if cap.get("ok"):
            return None, ("the captcha was solved but the search still did not "
                          "start - that is the daily ten-search limit on this IP")
        if why:
            return None, ("the search did not start: the captcha was not solved "
                          "(%s)" % why)
        return None, ("the search did not start. This is almost always the "
                      "captcha or the daily ten-search limit, not your photo.")
    return out, None


def premium_lookup(image_bytes, key, endpoint=None):
    """Interface for a commercial key, if you ever hold one. There is no
    documented public endpoint, so the endpoint must be supplied."""
    if not key:
        return None, "no key configured"
    if not endpoint:
        return None, ("no endpoint configured. PimEyes does not publish a "
                      "public API; a commercial key still needs the endpoint "
                      "from them.")
    try:
        req = urllib.request.Request(
            endpoint,
            data=json.dumps({"image": base64.b64encode(image_bytes).decode()}).encode(),
            headers={"Authorization": "Bearer %s" % key,
                     "Content-Type": "application/json"}, method="POST")
        return json.loads(urllib.request.urlopen(req, timeout=60).read()), None
    except Exception as e:
        return None, "premium lookup failed: %s" % str(e)[:200]
