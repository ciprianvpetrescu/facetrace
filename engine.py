"""
facetrace - reverse face search engine

Pipeline:  capture -> detect (YuNet) -> embed (SFace) -> match (local index) -> lookup (web)

The detection and recognition stages use OpenCV's own neural models, which ship
with the library and run on CPU here in tens of milliseconds. Everything in this
file is local and yours. The web lookup is a separate module and a separate
button, because it is slow, rate-limited and someone else's service.
"""

import base64
import json
import os
import time

import cv2
import numpy as np

APP = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(APP)
DATA = os.path.join(ROOT, "data")
MODELS = os.path.join(DATA, "models")
INDEX = os.path.join(DATA, "index.json")

YUNET = os.path.join(MODELS, "yunet.onnx")
SFACE = os.path.join(MODELS, "sface.onnx")

os.makedirs(DATA, exist_ok=True)

_DET = None
_REC = None


# --------------------------------------------------------------- model loading

def detector():
    """YuNet: a small CNN face detector. Input size is set per image."""
    global _DET
    if _DET is None:
        _DET = cv2.FaceDetectorYN.create(YUNET, "", (320, 320),
                                         score_threshold=0.7, nms_threshold=0.3,
                                         top_k=50)
    return _DET


def recogniser():
    """SFace: 128-d face embedding. This is the part that makes matching real."""
    global _REC
    if _REC is None:
        _REC = cv2.FaceRecognizerSF.create(SFACE, "")
    return _REC


# ------------------------------------------------------------------- detection

def _decode(image_bytes):
    if not image_bytes:
        return None
    buf = np.frombuffer(image_bytes, np.uint8)
    return cv2.imdecode(buf, cv2.IMREAD_COLOR)


def _detect(img, score=0.7):
    """Return a list of (box, landmarks) for the largest faces first."""
    h, w = img.shape[:2]
    det = detector()
    det.setInputSize((w, h))
    _, faces = det.detect(img)
    if faces is None:
        return []
    out = []
    for f in faces:
        if f[14] < score:
            continue
        x, y, bw, bh = f[:4]
        box = (int(max(0, x)), int(max(0, y)), int(bw), int(bh))
        out.append((box, f[4:14].reshape(-1, 2)))
    out.sort(key=lambda r: -(r[0][2] * r[0][3]))
    return out


def detect_faces(image_bytes):
    """Face boxes as fractions of the frame, for the phone overlay."""
    img = _decode(image_bytes)
    if img is None:
        return []
    H, W = img.shape[:2]
    return [{"x": round(x / W, 4), "y": round(y / H, 4),
             "w": round(bw / W, 4), "h": round(bh / H, 4),
             "score": 1.0}
            for (x, y, bw, bh), _ in _detect(img)]


def _align(img, box, landmarks):
    """Crop and align the face. SFace expects this alignment or accuracy drops."""
    x, y, w, h = box
    x2, y2 = min(img.shape[1], x + w), min(img.shape[0], y + h)
    crop = img[max(0, y):y2, max(0, x):x2]
    if crop.size == 0:
        return None
    try:
        rec = recogniser()
        face = np.hstack((np.array(box, dtype=np.float32),
                          landmarks.astype(np.float32).reshape(-1))).reshape(1, -1)
        aligned = rec.alignCrop(img, face.astype(np.float32))
        return aligned if aligned is not None else crop
    except Exception:
        return crop


def _embed_face(img, box, landmarks):
    aligned = _align(img, box, landmarks)
    if aligned is None:
        return None
    try:
        feat = recogniser().feature(aligned)
        return [round(float(v), 6) for v in feat.flatten()]
    except Exception:
        return None


def normalise_face(image_bytes):
    """Crop to the largest face and return JPEG bytes. This is what gets sent
    out on a lookup and stored on an enrolment."""
    img = _decode(image_bytes)
    if img is None:
        return None, "that file is not a readable image"
    found = _detect(img)
    if not found:
        return image_bytes, "no face detected - sending the whole image"
    (x, y, w, h), _ = found[0]
    pad = int(max(w, h) * 0.25)
    x0, y0 = max(0, x - pad), max(0, y - pad)
    x1, y1 = min(img.shape[1], x + w + pad), min(img.shape[0], y + h + pad)
    crop = img[y0:y1, x0:x1]
    ok, buf = cv2.imencode(".jpg", crop, [int(cv2.IMWRITE_JPEG_QUALITY), 92])
    return (buf.tobytes(), None) if ok else (image_bytes, None)


# --------------------------------------------------------------- matching

def _load_index():
    if not os.path.exists(INDEX):
        return {"faces": []}
    try:
        with open(INDEX) as f:
            return json.load(f)
    except Exception:
        return {"faces": []}


def _save_index(ix):
    tmp = INDEX + ".tmp"
    with open(tmp, "w") as f:
        json.dump(ix, f)
    os.replace(tmp, INDEX)


def cosine(a, b):
    a = np.asarray(a, dtype=np.float32).flatten()
    b = np.asarray(b, dtype=np.float32).flatten()
    if a.shape != b.shape:
        return 0.0
    d = (np.linalg.norm(a) * np.linalg.norm(b)) or 1.0
    return float(np.dot(a, b) / d)


def local_match(embedding, threshold=0.36):
    """SFace embeddings compare on cosine similarity; 0.363 is the published
    same-identity threshold for this model. Anything at or above it is a match."""
    if embedding is None:
        return []
    hits = []
    for rec in _load_index().get("faces", []):
        emb = rec.get("embedding")
        if not emb:
            continue
        s = cosine(embedding, emb)
        if s >= threshold:
            hits.append({"label": rec.get("label", "unnamed"),
                         "score": round(s, 4),
                         "added": rec.get("added")})
    hits.sort(key=lambda h: -h["score"])
    return hits


def scan(image_bytes, with_boxes=True, threshold=0.36):
    """The fast path: detect every face, embed the largest, match the index."""
    img = _decode(image_bytes)
    if img is None:
        return {"ok": False, "error": "unreadable image"}
    H, W = img.shape[:2]
    found = _detect(img)
    boxes = [{"x": round(x / W, 4), "y": round(y / H, 4),
              "w": round(bw / W, 4), "h": round(bh / H, 4)}
             for (x, y, bw, bh), _ in found] if with_boxes else []
    hits = []
    emb = None
    if found:
        emb = _embed_face(img, found[0][0], found[0][1])
        hits = local_match(emb, threshold)
    return {"ok": True, "boxes": boxes, "hits": hits,
            "has_face": bool(found), "embedding": emb}


def index_add(image_bytes, label):
    img = _decode(image_bytes)
    if img is None:
        return None, "unreadable image"
    found = _detect(img)
    if not found:
        return None, "no face found in that image"
    (box, lm) = found[0]
    emb = _embed_face(img, box, lm)
    if emb is None:
        return None, "could not compute an embedding"
    face, _ = normalise_face(image_bytes)
    ix = _load_index()
    rec = {"label": label or "unnamed", "added": int(time.time()),
           "embedding": emb,
           "thumb": "data:image/jpeg;base64," + base64.b64encode(face).decode()}
    ix.setdefault("faces", []).append(rec)
    _save_index(ix)
    return rec, None


def index_list():
    return [{"label": r.get("label"), "added": r.get("added"),
             "thumb": r.get("thumb")} for r in _load_index().get("faces", [])]


def index_clear():
    _save_index({"faces": []})
    return True
