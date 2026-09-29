"""Prosopo captcha solving for the PimEyes browser path.

Technique taken from github.com/shanthanu47/Prim-Eyes-Automation (kept for
reference at research/prim-eyes/): the Prosopo widget hides its contents in an
open shadow root, so the only way in is page.evaluate() piercing shadowRoot,
reading the grid geometry off the element, and converting the model's cell
indices into x/y inside that box. That repo hardcoded the NEXT button at
box.x+width-80 / box.y+height-30; we search the shadow DOM for a real button
first and only fall back to the geometric guess.

The vision brain is DeepSeek, not Gemini - Gemini refuses from this server
("User location is not supported for the API use"). DeepSeek reads a 3x3 grid
correctly and quickly, but sometimes answers in prose instead of JSON, so the
reply is parsed defensively and re-asked once.
"""

import base64
import json
import os
import re
import time
import urllib.request

APP = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(APP)

DEEPSEEK_URL = "https://api.deepseek.com/chat/completions"
MODEL = "deepseek-chat"

GRID_PROMPT = """This is a CAPTCHA challenge. It shows a grid of images with an instruction above it.

1. Read the instruction (for example "Select all images containing a bicycle").
2. Extract the target object from it.
3. Look at every image in the grid.

Number the grid 1..9 left to right, top to bottom:
[1] [2] [3]
[4] [5] [6]
[7] [8] [9]

Answer with ONE JSON object and nothing else:
{"target":"the object asked for","indices":[1,5,9]}

Use [] for indices when the target appears in none of them. Do not explain."""

CHECK_PROMPT = """This is a CAPTCHA challenge widget. Answer with ONE JSON object only,
no explanation: {"grid":true} if it currently shows a grid of images to click,
or {"grid":false} if it only shows a checkbox or a loading state."""

SHADOW_PROBE = """() => {
  const w = document.querySelector('prosopo-procaptcha');
  if (!w) return {present:false};
  const sr = w.shadowRoot;
  const r = w.getBoundingClientRect();
  const info = {present:true, hasShadow: !!sr, x:r.x, y:r.y,
                width:r.width, height:r.height, src:''};
  if (!sr) return info;
  const t = sr.querySelector('iframe');
  if (t) { const q = t.getBoundingClientRect(); info.src = t.src||'';
           info.x=q.x; info.y=q.y; info.width=q.width; info.height=q.height; }
  else {
    const im = sr.querySelector('img');
    if (im) { const q = im.getBoundingClientRect();
              info.x=q.x; info.y=q.y; info.width=q.width; info.height=q.height; }
  }
  return info;
}"""

CLICK_CHECKBOX = """() => {
  const w = document.querySelector('prosopo-procaptcha');
  if (!w || !w.shadowRoot) return {ok:false, why:'no-shadow'};
  const sr = w.shadowRoot;
  const box = sr.querySelector('[data-cy="captcha-checkbox"]') ||
              sr.querySelector('input[type="checkbox"]') ||
              sr.querySelector('label') ||
              sr.querySelector('.checkbox__content') ||
              sr.querySelector('[class*="checkbox"]');
  if (!box) return {ok:false, why:'no-checkbox'};
  const r = box.getBoundingClientRect();
  const ev = {bubbles:true, cancelable:true, clientX:r.x+r.width/2,
              clientY:r.y+r.height/2};
  ['pointerdown','mousedown','pointerup','mouseup','click'].forEach(t => {
    box.dispatchEvent(new MouseEvent(t, ev));
  });
  if (typeof box.click === 'function') box.click();
  return {ok:true, x:r.x+r.width/2, y:r.y+r.height/2};
}"""

CLICK_NEXT = """() => {
  const w = document.querySelector('prosopo-procaptcha');
  if (!w || !w.shadowRoot) return {ok:false, why:'no-shadow'};
  const sr = w.shadowRoot;
  const nodes = [...sr.querySelectorAll('button, [role="button"], a')];
  for (const b of nodes) {
    const t = (b.innerText || b.textContent || '').trim().toLowerCase();
    if (t === 'next' || t.includes('next') || t.includes('verify') ||
        t.includes('submit')) {
      const r = b.getBoundingClientRect();
      b.click();
      return {ok:true, how:'button', text:t, x:r.x+r.width/2, y:r.y+r.height/2};
    }
  }
  const r = w.getBoundingClientRect();
  return {ok:false, why:'no-next-button', x:r.x+r.width-80, y:r.y+r.height-30};
}"""


def _key():
    for line in open('/opt/apps/fred/.env'):
        line = line.strip()
        if line.startswith('DEEPSEEK_API_KEY='):
            return line.split('=', 1)[1].strip().strip('"').strip("'")
    return os.environ.get('DEEPSEEK_API_KEY', '')


def small_png(png_bytes, w=1000):
    """Shrink a full-page screenshot before it goes to the model - a raw
    1400px viewport PNG is ~200KB and needlessly slow."""
    try:
        import io
        from PIL import Image
        im = Image.open(io.BytesIO(png_bytes)).convert('RGB')
        if im.width > w:
            im = im.resize((w, int(im.height * w / im.width)))
        out = io.BytesIO()
        im.save(out, 'JPEG', quality=80)
        return out.getvalue(), im.width, im.height
    except Exception:
        return png_bytes, 1400, 1100


def ask(img_bytes, prompt=GRID_PROMPT, timeout=90):
    """One DeepSeek vision call. Returns the parsed JSON dict, or {}."""
    key = _key()
    if not key:
        return {}
    mime = 'image/jpeg' if img_bytes[:2] == b'\xff\xd8' else 'image/png'
    b64 = base64.b64encode(img_bytes).decode()
    body = {
        "model": MODEL,
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": prompt},
            {"type": "image_url",
             "image_url": {"url": "data:%s;base64,%s" % (mime, b64)}},
        ]}],
        "temperature": 0,
        "max_tokens": 800,
    }
    req = urllib.request.Request(
        DEEPSEEK_URL, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer " + key})
    try:
        d = json.loads(urllib.request.urlopen(req, timeout=timeout).read())
    except Exception:
        return {}
    txt = ((d.get('choices') or [{}])[0].get('message') or {}).get('content') or ''
    return _json_from(txt)


def _json_from(txt):
    """The model sometimes wraps JSON in prose or a code fence. Dig it out."""
    if not txt:
        return {}
    t = txt.strip()
    if '```' in t:
        for part in t.split('```'):
            part = part.strip()
            if part.startswith('json'):
                part = part[4:].strip()
            if part.startswith('{'):
                t = part
                break
    m = re.search(r'\{[^{}]*\}', t, re.S)
    cand = m.group(0) if m else t
    try:
        return json.loads(cand)
    except Exception:
        return {}


def solve(page, out=None, max_rounds=6, log=None, shot_dir=None):
    """Drive the Prosopo widget until PimEyes lets the search through.

    Returns a dict: {ok, rounds, reason, clicks}. Does not raise.
    """
    out = out if out is not None else {}
    log = log or (lambda *a: None)
    clicks = 0

    def probe():
        try:
            return page.evaluate(SHADOW_PROBE) or {}
        except Exception:
            return {}

    def widget_h():
        p = probe()
        return float(p.get('height') or 0)

    base_h = widget_h()
    log("widget is %.0fpx tall at rest" % base_h)

    # pick something to click: the checkbox first, then the widget itself.
    clicked_box = False
    for attempt in range(6):
        if not clicked_box:
            try:
                r = page.evaluate(CLICK_CHECKBOX) or {}
                if r.get('ok'):
                    clicked_box = True
                    clicks += 1
                    log("clicked the checkbox at (%.0f, %.0f)" %
                        (r.get('x') or 0, r.get('y') or 0))
                else:
                    log("no checkbox yet: %s" % r.get('why'))
            except Exception as e:
                log("checkbox click failed: %s" % e)
        page.wait_for_timeout(2500)
        if widget_h() > base_h + 60:
            break

    result = {"ok": False, "rounds": 0, "reason": "the widget never grew",
              "clicks": clicks}

    for round_no in range(1, max_rounds + 1):
        h = widget_h()
        if h <= base_h + 60:
            log("round %d: widget still %.0fpx - nothing to solve yet" % (round_no, h))
            result["reason"] = ("the challenge never opened (widget stayed "
                                "%.0fpx, needs a human click or a residential IP)" % h)
            page.wait_for_timeout(2000)
            continue

        shot = page.screenshot(full_page=False)
        try:
            _sd = shot_dir or os.path.join(ROOT, 'data')
            try:
                _sd = _sd()
            except Exception:
                pass
            with open(os.path.join(_sd, 'captcha_%d.png' % round_no), 'wb') as f:
                f.write(shot)
        except Exception:
            pass

        big, sw, sh = small_png(shot)
        # crop nothing; the model needs the whole widget, and asking it for the
        # grid alone is unreliable when the widget moves.
        d = ask(big, GRID_PROMPT)
        if not d:
            d = ask(big, GRID_PROMPT)
        idx = d.get('indices')
        if idx is None:
            # maybe it is only a checkbox screenshot
            c = ask(big, CHECK_PROMPT)
            if c and c.get('grid') is False:
                result["reason"] = "the widget shows no image grid yet"
                page.wait_for_timeout(2500)
                continue
            idx = []
        if not isinstance(idx, list):
            idx = []
        idx = [int(i) for i in idx if str(i).strip().isdigit() and 1 <= int(i) <= 16]
        log("round %d: target=%r indices=%r" % (round_no, d.get('target'), idx))

        p = probe()
        bx, by = float(p.get('x') or 0), float(p.get('y') or 0)
        bw, bh = float(p.get('width') or 0), float(p.get('height') or 0)
        if bw < 60 or bh < 60:
            result["reason"] = "the widget box could not be measured"
            break
        cols = 3 if len(idx) <= 9 else 4
        grid_top = by + max(60, bh * 0.18)
        grid_left = bx + bw * 0.03
        gw = bw * 0.94
        gh = bh - (grid_top - by) - max(40, bh * 0.10)
        cw, ch = gw / cols, gh / cols

        for i in idx:
            row, col = (i - 1) // cols, (i - 1) % cols
            x = grid_left + col * cw + cw / 2
            y = grid_top + row * ch + ch / 2
            try:
                page.mouse.click(x, y)
                clicks += 1
                log("clicked cell %d at (%.0f, %.0f)" % (i, x, y))
            except Exception as e:
                log("cell click failed: %s" % e)
            page.wait_for_timeout(350)

        page.wait_for_timeout(700)
        try:
            n = page.evaluate(CLICK_NEXT) or {}
            if n.get('ok'):
                clicks += 1
                log("clicked NEXT (%s)" % n.get('text'))
            else:
                page.mouse.click(n.get('x') or (bx + bw - 80),
                                 n.get('y') or (by + bh - 30))
                clicks += 1
                log("NEXT not found in the DOM, used the geometric guess")
        except Exception as e:
            log("NEXT click failed: %s" % e)

        page.wait_for_timeout(4000)
        result["rounds"] = round_no
        if widget_h() <= base_h + 60:
            result.update({"ok": True, "reason": "the challenge closed after %d round(s)" % round_no})
            return result

    result["rounds"] = result.get("rounds") or 0
    if not result.get("reason"):
        result["reason"] = "gave up after %d rounds" % max_rounds
    return result
