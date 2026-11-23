"""Eva's face, generated in its native resolution through the SAM Grid image API
(the one the Kalah game art uses: D:/PRG/Pyton/kallah/deploy/art/gen_images.py).

The first face (a VRM model rendered at 256 px) was good for the pseudo-graphics
but blurry as a picture (Иван, 2026-11-20). Now: one base portrait from text,
then every pose of the atlas made from it by the edit model, so it stays the
same face; the picture avatar shows them at 1024 px, the pseudo-graphics get an
atlas made of the same poses.

    python ui/spike/gen_face.py base --n 3        # candidates -> face-gen/base_<model>_<i>.png
    python ui/spike/gen_face.py poses face-gen/base_x.png   # all poses from the chosen base
    python ui/spike/gen_face.py atlas             # face-atlas.png (4x4 x 256) + face/<pose>.jpg

Key: GRID_KEY in the environment.
"""
import base64
import json
import os
import sys
import urllib.error
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

BASE_URL = "https://api.grid.sam-solutions.net/v1"
GEN_MODELS = ["saipixel-qwen-image-2.1", "saipixel-krea2-turbo"]
EDIT_MODEL = "saipixel-flux2-klein-9b-edit"
HERE = Path(__file__).resolve().parent
OUT = HERE / "face-gen"
SIZE = "1024x1024"

PORTRAIT = (
    "Soft 2D anime illustration, close-up portrait of a friendly young woman's face, front view, looking "
    "straight at the viewer, centered and symmetrical. Framing: the whole head with a little empty space "
    "above the top of the hair, cropped at the neck just below the chin - no shoulders, no clothes; the face "
    "is large and fills most of the frame. Long straight chestnut-brown hair with soft highlights, warm "
    "brown eyes, gentle calm expression with a slight closed-mouth smile, clean cel shading, soft even light, "
    "plain pure black background, no text, no watermark")

KEEP = (" Keep everything else exactly the same: the same person, face shape, hair, eyes colour, clothes, "
        "lighting, framing, head position and black background. Change only what is described.")
# the atlas order is the contract with ascii.html (POSE)
# Speech poses are small: a talking mouth, not a shout (the first set opened
# the mouth wide like a scream, Иван 2026-11-20).
SPEAK = " Natural relaxed speech, a small calm mouth movement - not shouting, not singing, not exaggerated."
POSES = [
    ("neutral", None),
    ("blink", "Her eyes are fully closed, a natural blink, relaxed eyelids."),
    ("aa", "Her lips are slightly parted with a small natural opening, as when calmly saying 'a' in conversation; "
           "the jaw only a little lower." + SPEAK),
    ("ih", "Her lips are barely parted and a little wider, as when calmly saying 'i'; teeth almost together." + SPEAK),
    ("ou", "Her lips are softly rounded with a very small opening, as when calmly saying 'u'." + SPEAK),
    ("ee", "Her lips are slightly parted in a soft wide shape, as when calmly saying 'e'; a hint of the upper teeth."
           + SPEAK),
    ("oh", "Her lips are slightly parted in a soft oval, as when calmly saying 'o'; a small opening." + SPEAK),
    ("smile", "She smiles warmly with closed lips, cheeks slightly raised, eyes a little narrowed with joy."),
    ("smile_blink", "She smiles warmly with closed lips and her eyes are closed in a happy smile."),
    ("smile_aa", "She smiles and talks: lips slightly parted in a small open smile." + SPEAK),
    ("smile_oh", "She smiles and talks: lips slightly parted in a small soft oval." + SPEAK),
    ("surprised", "She looks gently surprised: eyebrows raised, eyes a little wider, lips slightly parted."),
    ("sad", "She looks a little sad: eyebrows tilted up in the middle, gentle downturned mouth."),
    ("think", "She is calmly thinking: her eyes look up and slightly to the side, eyebrows relaxed and level (not "
              "worried, not sad), lips closed in a soft neutral line, a curious peaceful look."),
    ("half_blink", "Her eyes are half closed, halfway through a blink."),
    ("smile_ee", "She smiles and talks: lips slightly parted in a small wide smile, a hint of teeth." + SPEAK),
]


def _key():
    k = os.environ.get("GRID_KEY")
    if not k:
        sys.exit("GRID_KEY is not set")
    return k


def _image(item):
    if item.get("b64_json"):
        return base64.b64decode(item["b64_json"])
    with urllib.request.urlopen(item["url"], timeout=120) as r:
        return r.read()


def generate(prompt, model, n=1):
    body = json.dumps({"model": model, "prompt": prompt, "size": SIZE, "n": n}).encode()
    req = urllib.request.Request(BASE_URL + "/images/generations", data=body, method="POST", headers={
        "Authorization": "Bearer " + _key(), "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=300) as r:
        return [_image(i) for i in json.load(r).get("data", [])]


def edit(png, prompt):
    b = uuid.uuid4().hex
    parts = [f'--{b}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode()
             for k, v in {"model": EDIT_MODEL, "prompt": prompt, "size": SIZE, "n": 1}.items()]
    parts.append(f'--{b}\r\nContent-Disposition: form-data; name="image"; filename="base.png"\r\n'
                 f'Content-Type: image/png\r\n\r\n'.encode() + png + b"\r\n")
    parts.append(f"--{b}--\r\n".encode())
    req = urllib.request.Request(BASE_URL + "/images/edits", data=b"".join(parts), method="POST", headers={
        "Authorization": "Bearer " + _key(), "Content-Type": f"multipart/form-data; boundary={b}"})
    with urllib.request.urlopen(req, timeout=600) as r:
        return _image(json.load(r)["data"][0])


def cmd_base(n):
    OUT.mkdir(exist_ok=True)

    def one(model):
        try:
            imgs = generate(PORTRAIT, model, n)
        except urllib.error.HTTPError as e:
            return f"{model}: HTTP {e.code} {e.read()[:200]!r}"
        names = []
        for i, raw in enumerate(imgs):
            p = OUT / f"base_{model.replace('saipixel-', '')}_{i}.png"
            p.write_bytes(raw)
            names.append(p.name)
        return f"{model}: " + ", ".join(names)
    with ThreadPoolExecutor(2) as ex:
        for line in ex.map(one, GEN_MODELS):
            print(line, flush=True)


def cmd_poses(base):
    png = Path(base).read_bytes()
    (OUT / "poses").mkdir(parents=True, exist_ok=True)
    (OUT / "poses" / "neutral.png").write_bytes(png)

    def one(pose):
        name, what = pose
        if what is None:
            return f"{name}: base"
        try:
            (OUT / "poses" / f"{name}.png").write_bytes(edit(png, what + KEEP))
            return f"{name}: ok"
        except urllib.error.HTTPError as e:
            return f"{name}: HTTP {e.code} {e.read()[:200]!r}"
    with ThreadPoolExecutor(4) as ex:
        for line in ex.map(one, POSES):
            print(line, flush=True)


MOUTH_ONLY = {"aa", "ih", "ou", "ee", "oh", "smile_aa", "smile_oh", "smile_ee"}
EYES_ONLY = {"blink", "half_blink"}


def part_mask(kind):
    """The edit redraws the whole face: another nose, thinner lips in every pose,
    and while she talks the face 'swam' (Иван, 2026-11-20). Speech poses take
    the lips only, blinks the eyelids only; the rest is the neutral face, pixel
    for pixel. Places found from the differences (lips ~(520,745), lids y 480-570)."""
    from PIL import Image, ImageDraw, ImageFilter
    m = Image.new("L", (1024, 1024), 0)
    d = ImageDraw.Draw(m)
    if kind == "mouth":
        d.ellipse((430, 698, 610, 808), fill=255)
        return m.filter(ImageFilter.GaussianBlur(9))
    d.ellipse((296, 446, 506, 596), fill=255)
    d.ellipse((524, 446, 736, 596), fill=255)
    return m.filter(ImageFilter.GaussianBlur(10))


def face_mask():
    """A soft oval over the face: only it is taken from a pose, hair and shoulders
    stay the neutral one's (the edit redraws them a little differently every time,
    2-5/255 - it shimmers when poses change 50 times a second while she talks)."""
    from PIL import Image, ImageDraw, ImageFilter
    m = Image.new("L", (1024, 1024), 0)
    ImageDraw.Draw(m).ellipse((245, 250, 779, 905), fill=255)
    return m.filter(ImageFilter.GaussianBlur(22))


def lift(im, gamma=0.5, sat=1.25):
    """For the pseudo-graphics only: the glyph density follows brightness, and the
    dark chestnut hair came out as nothing - shadows are lifted (black stays
    black), colour a little stronger."""
    from PIL import ImageEnhance
    lut = [round(255 * (v / 255) ** gamma) for v in range(256)] * 3
    return ImageEnhance.Color(im.point(lut)).enhance(sat)


TONE_PATCHES = [(440, 360, 590, 440), (485, 470, 540, 560)]   # forehead, bridge of the nose: no blush, no expression


def match_tone(im, ref):
    """The edit shifts the skin tone a little per pose (warmer, darker); poses
    change many times a second while she talks, so the face would flicker in
    colour. Gains per channel bring the pose's skin to the neutral one's."""
    import numpy as np
    from PIL import Image
    a, r = np.asarray(im, np.float32), np.asarray(ref, np.float32)
    pa = np.concatenate([a[y0:y1, x0:x1].reshape(-1, 3) for x0, y0, x1, y1 in TONE_PATCHES])
    pr = np.concatenate([r[y0:y1, x0:x1].reshape(-1, 3) for x0, y0, x1, y1 in TONE_PATCHES])
    gain = pr.mean(0) / np.maximum(pa.mean(0), 1)
    return Image.fromarray(np.clip(a * gain, 0, 255).astype(np.uint8))


def cmd_atlas():
    from PIL import Image
    face = HERE / "face"
    face.mkdir(exist_ok=True)
    atlas = Image.new("RGB", (1024, 1024))
    base = Image.open(OUT / "poses" / "neutral.png").convert("RGB")
    mask = face_mask()
    parts = {"mouth": part_mask("mouth"), "eyes": part_mask("eyes")}
    for i, (name, _) in enumerate(POSES):
        m = parts["mouth"] if name in MOUTH_ONLY else parts["eyes"] if name in EYES_ONLY else mask
        im = Image.composite(match_tone(Image.open(OUT / "poses" / f"{name}.png").convert("RGB"), base), base, m)
        im.save(face / f"{name}.jpg", quality=88)          # the picture avatar, native size
        atlas.paste(lift(im).resize((256, 256), Image.LANCZOS), ((i % 4) * 256, (i // 4) * 256))
    atlas.save(HERE / "face-atlas.png")
    print("face-atlas.png +", len(POSES), "jpg in face/")


if __name__ == "__main__":
    what = sys.argv[1]
    if what == "base":
        cmd_base(int(sys.argv[sys.argv.index("--n") + 1]) if "--n" in sys.argv else 2)
    elif what == "poses":
        cmd_poses(sys.argv[2])
    elif what == "atlas":
        cmd_atlas()
