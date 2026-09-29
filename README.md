# facetrace

A real-time face detection and matching rig, built to be used from a phone.

Live at https://faces.simplu.ie (behind the household portal).

---

## What actually works

Two stages run locally and are yours. One stage reaches out to the web, and it is
deliberately a separate, explicitly-pressed button.

**Fast path (~25-40 ms per frame)**

  camera frame -> YuNet face detection -> SFace embedding -> cosine match against
  a local index

YuNet and SFace are OpenCV's own neural models. They run on CPU here in tens of
milliseconds, which is what makes the live overlay possible. Enrol a face once
and it is recognised from a different photo of the same person - resized,
re-encoded, brightened, cropped - at around 0.93 cosine similarity. Different
people score well below the 0.363 same-identity threshold, so a stranger does
not match.

**Slow path (seconds, rate-limited, someone else's service)**

  the Look up button -> PimEyes

PimEyes has no public API. What exists is their own web application, and it has
two doors:

  POST /api/face accepts an image as base64 and returns the face hash they
  assign. No browser, no captcha, no account. The Cloudflare cookie has to be
  collected from the homepage first, and this module does that.

  Starting an actual search is gated behind a Prosopo captcha session that
  cannot be solved server-side, so that step drives a real Chromium. It is
  capped at ten searches per IP per day - the Quota button reads that live -
  and it will break whenever the site's markup changes.

## What this is not

There is no face recognition of the kind that identifies a stranger from a
database of the public. Nothing here indexes the internet. The local index holds
only the faces you enrol yourself, and the web lookup is you asking someone
else's search engine about a face you chose to send.

Operating a live identification system on people in public would require a legal
basis that this does not have, and is not what this is for.

## The premium key option

Settings accepts a key and an endpoint. If both are present, lookups go through
them instead. PimEyes does not publish an endpoint, so a commercial key still
needs one supplied by them - the interface is there, the URL is theirs to
give. With nothing configured it falls through to the free path above.

## Layout

    app/engine.py    detection, embedding, local index
    app/lookup.py    the PimEyes doors, and the premium interface
    app/server.py    Starlette API
    app/ui.html      the phone page
    data/models/     yunet.onnx, sface.onnx
    data/index.json  enrolled faces

    GET  /api/quota            searches left today, read live
    POST /api/scan             the fast path: boxes + local matches
    POST /api/lookup           the slow path: PimEyes
    GET  /api/index            list enrolled faces
    POST /api/index            enrol a face
    POST /api/index/clear      empty the index
    GET  /api/settings         is a premium key configured
    POST /api/settings         store one

## Running it

    systemctl status facetrace.service      # port 18500
    systemctl restart facetrace.service

Caddy fronts it on 18501 behind the portal, and the named Cloudflare tunnel
serves faces.simplu.ie.

## On a phone

The camera requires a secure context, so it works over https or on localhost,
not over plain http on a LAN address. Add it to the home screen and it opens
fullscreen without browser chrome.

## Honest status

This is a testing rig. The fast path is solid and measured. The web lookup is a
scraper driving a site that does not want to be scraped, and it is one markup
change away from breaking - the failure message says so rather than pretending
otherwise.
