# ===============================================================================
# Module:   app/photo_utils.py
# Purpose:  Shared photo-upload processing used by every upload path in the
#           app — registration avatars (auth.py), profile-edit avatars
#           (profiles.py), and completion photos (completions.py).
#           Centralizing here means an iPhone user gets the same HEIC
#           conversion and EXIF auto-rotation whether they're uploading
#           a selfie, a profile photo update, or a summit shot.
#
#           Also the single place where upload dimensions are capped and
#           grid thumbnails are generated. See the SIZING section below
#           for why that matters more than it sounds like it should.
# Author:   Jamie F. Weleber
# Created:  May 19, 2026
# ===============================================================================
"""Pillow-based photo processing helpers shared across upload paths."""

import uuid                              # filenames for saved photos (collision-free, path-safe)
from pathlib import Path                 # cross-platform path handling

from PIL import Image, ImageOps          # Pillow: image decoding, EXIF auto-rotate, resampling
import pillow_heif                       # extension that teaches Pillow to read HEIC/HEIF (iOS default)

# Registering the HEIF opener teaches Pillow to recognize .heic/.heif files
# the same way it does built-in formats. Idempotent — safe to call at every
# import. Without this, Image.open() on an iPhone photo raises
# UnidentifiedImageError.
pillow_heif.register_heif_opener()

# Photo formats we accept on upload. HEIC/HEIF come in from iPhones; the rest
# are common digital camera and screen-capture outputs (Strava/Garmin Connect
# screenshots are typically PNG or JPG). All get normalized through Pillow.
PHOTO_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic", ".heif"}


# ===============================================================================
# SIZING
# ===============================================================================
# Why any of this exists: a current iPhone shoots ~12 MP, which lands around
# 4 MB per photo. A hike detail page renders every photo from every completion
# in one grid — five completions averaging four photos each is 20 images. Served
# at full resolution that's ~80 MB for a page whose thumbnails display at 140
# CSS pixels. On a phone on a trailhead LTE bar, it never finishes loading.
#
# Two independent caps fix it:
#
#   PHOTO_MAX_EDGE — the stored "full size." 2560px on the long edge still
#     exceeds any laptop screen the lightbox will open on, and it cuts a 12 MP
#     original by roughly 5x. This is lossy and irreversible: the uploader's
#     original is not kept. That's a deliberate trade — this is a completion
#     log, not a photo archive, and the member still has the original on their
#     phone.
#
#   THUMB_MAX_EDGE — the grid copy. 480px covers a 140px CSS thumbnail at 3x
#     device pixel ratio, so it stays sharp on a retina phone while weighing
#     ~40 KB instead of ~800 KB.
#
#   AVATAR_MAX_EDGE — avatars render at 50px at their largest anywhere on the
#     site, but registration happily accepted a full-resolution selfie. 512px
#     is generous headroom for a 50px circle and keeps the file trivial.
PHOTO_MAX_EDGE = 2560
THUMB_MAX_EDGE = 480
AVATAR_MAX_EDGE = 512

# Subdirectory under uploads/photos/ where grid thumbnails live. Kept as a
# child of photos/ rather than a fourth top-level bucket so the existing
# /uploads/<subdir>/<path:filename> route serves it with no change — the
# path "photos/thumbs/abc.jpg" already matches, and send_from_directory's
# traversal guard still applies.
THUMB_SUBDIR = "thumbs"

# JPEG quality for generated thumbnails. 82 is the usual "can't see the
# difference at display size" number; going higher mostly buys file size.
THUMB_QUALITY = 82


def thumb_dir_for(photos_dir):
    """The thumbs/ directory for a given photos directory, created if missing.

    Callers pass their configured uploads/photos path and get back
    uploads/photos/thumbs. mkdir is idempotent, which means neither the
    submit route nor the backfill script needs its own existence check.
    """
    d = Path(photos_dir) / THUMB_SUBDIR
    d.mkdir(parents=True, exist_ok=True)
    return d


def flatten_to_rgb(img):
    """Composite any transparency onto white and return an RGB image.

    JPEG has no alpha channel. Saving an RGBA PNG (or a palettized GIF with a
    transparent index) straight to JPEG either raises or silently renders the
    transparent regions black, which looks like a corrupted photo. Compositing
    onto white first is what makes a screenshot with a transparent background
    thumbnail correctly.
    """
    if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
        rgba = img.convert("RGBA")
        canvas = Image.new("RGB", rgba.size, (255, 255, 255))
        canvas.paste(rgba, mask=rgba.split()[-1])   # alpha channel as the paste mask
        return canvas
    return img.convert("RGB")


def is_animated(img):
    """True for a multi-frame image (animated GIF/WEBP).

    Matters because Pillow's resize operations act on the current frame only —
    resizing an animated GIF silently throws away the animation. We'd rather
    store one oversized animated GIF than quietly flatten someone's upload.
    """
    return getattr(img, "n_frames", 1) > 1


def process_photo(file_storage, dest_dir, max_edge=None, thumb_dir=None):
    """Save an uploaded photo, optionally downsized, optionally with a thumbnail.

    Returns a (filename, thumb_filename) tuple. thumb_filename is None when no
    thumb_dir was passed (the avatar paths) or when the source is animated and
    was left alone.

    Why every uploaded photo goes through Pillow rather than a direct save:
      1. HEIC photos from iOS can't be rendered by browsers — converting
         to JPEG here means the photo grid works in any browser without
         client-side help.
      2. iPhone landscape photos carry an EXIF Orientation tag that some
         browsers honor and others don't. Running exif_transpose() bakes
         the rotation into the pixels so the photo always displays
         upright.
      3. Round-tripping through Pillow strips most EXIF metadata as a
         side effect, which removes embedded GPS coordinates that phones
         add by default — a small privacy win for a team-internal site.
      4. It's the only place we can enforce the dimension caps described
         in the SIZING block above.

    Raises ValueError on an unsupported extension.
    """
    src_ext = Path(file_storage.filename).suffix.lower()
    if src_ext not in PHOTO_EXTS:
        raise ValueError(f"Unsupported photo format: {src_ext}")

    img = Image.open(file_storage.stream)
    # exif_transpose() returns a new image with EXIF Orientation applied
    # to the pixels and the tag cleared. No-op for formats / files without
    # an orientation tag, so safe to call unconditionally.
    img = ImageOps.exif_transpose(img)
    animated = is_animated(img)

    # ---- Downsize the stored copy ----
    # thumbnail() resizes in place, preserves aspect ratio, and — importantly —
    # only ever shrinks. A photo already under the cap passes through untouched
    # rather than being upscaled into a blurry larger file.
    if max_edge and not animated:
        img.thumbnail((max_edge, max_edge), Image.LANCZOS)

    # ---- Save the stored copy ----
    stem = uuid.uuid4().hex
    if src_ext in (".heic", ".heif"):
        # Force JPEG output for HEIC inputs. RGB conversion because HEIC
        # can be in YUV or other colorspaces that JPEG doesn't support
        # directly; without it Pillow raises on save.
        filename = f"{stem}.jpg"
        img = img.convert("RGB")
        img.save(Path(dest_dir) / filename, "JPEG", quality=92)
    else:
        # Preserve the input format. Pillow infers it from the extension
        # we tack onto the UUID name.
        filename = f"{stem}{src_ext}"
        save_kwargs = {}
        if src_ext in (".jpg", ".jpeg"):
            # High quality re-save minimizes the small loss from going
            # through Pillow's JPEG encoder. 95 is the standard "visually
            # lossless" number.
            save_kwargs["quality"] = 95
        if animated:
            # save_all tells Pillow to write every frame rather than just the
            # current one, which is what keeps an animated GIF animated.
            save_kwargs["save_all"] = True
        img.save(Path(dest_dir) / filename, **save_kwargs)

    # ---- Generate the grid thumbnail ----
    # Derived from the already-downsized image rather than re-reading the
    # upload: one decode instead of two, and 2560 -> 480 has plenty of pixels
    # to work with. Always JPEG regardless of source format, because the grid
    # doesn't need transparency and JPEG is the smallest option for photos.
    thumb_filename = None
    if thumb_dir and not animated:
        thumb = flatten_to_rgb(img.copy())
        thumb.thumbnail((THUMB_MAX_EDGE, THUMB_MAX_EDGE), Image.LANCZOS)
        thumb_filename = f"{stem}.jpg"
        thumb.save(Path(thumb_dir) / thumb_filename, "JPEG", quality=THUMB_QUALITY)

    return filename, thumb_filename
