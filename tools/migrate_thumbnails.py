# ===============================================================================
# Script Name:  migrate_thumbnails.py
# Purpose:      Backfill grid thumbnails for photos uploaded before the
#               thumbnail pipeline existed, and (optionally) shrink the
#               oversized originals those uploads left behind.
#
#               Background: every photo used to be stored at whatever
#               resolution the phone shot it — up to 32 MB apiece — and the
#               hike detail page rendered all of them at once in a 140px
#               grid. A hike with five completions could push 80 MB down the
#               wire to display twenty postage stamps. process_photo() now
#               caps new uploads and writes a thumbnail alongside; this
#               script applies the same treatment retroactively.
#
#               Idempotent — a photo that already has a thumbnail is skipped,
#               so re-running only picks up whatever is still outstanding.
#
#               Run tools/migrate_open_hikes.py FIRST: it adds the
#               completion_photos.thumb_filename column this script writes to.
# Author:       Jamie F. Weleber
# Created:      August 2026
#
# Usage:        From the project root:
#                   python tools/migrate_thumbnails.py
#                   python tools/migrate_thumbnails.py --resize-originals
#                   python tools/migrate_thumbnails.py --dry-run
# ===============================================================================

# --- Imports ---
import argparse                      # --resize-originals / --dry-run flags
import sqlite3                       # read the photo rows, write back thumb filenames
import sys                           # stdout encoding fix + import path
from pathlib import Path             # OS-agnostic path handling

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")   # Windows cp1252 can't render the ✓ below

from PIL import Image, ImageOps      # same Pillow pipeline the upload path uses
import pillow_heif                   # so a legacy .heic in the uploads dir still opens

from app.config import BaseConfig
from app.photo_utils import (AVATAR_MAX_EDGE, PHOTO_MAX_EDGE, THUMB_MAX_EDGE,
                             THUMB_QUALITY, flatten_to_rgb, is_animated,
                             thumb_dir_for)

pillow_heif.register_heif_opener()


# ===============================================================================
# STEP 1: Thumbnail backfill
# ===============================================================================

def backfill_thumbs(con, photos_dir, thumbs_dir, dry_run):
    """Generate a thumbnail for every completion_photos row missing one.

    The thumbnail filename mirrors the original's stem with a .jpg suffix —
    matching what process_photo() does for new uploads, so there's exactly
    one naming convention to reason about. Always JPEG, because the grid
    never needs transparency and JPEG is the smallest option for photos.
    """
    rows = con.execute(
        "SELECT id, filename FROM completion_photos WHERE thumb_filename IS NULL"
    ).fetchall()
    if not rows:
        print("  · every photo already has a thumbnail — nothing to do.")
        return

    print(f"  {len(rows)} photo(s) without a thumbnail:")
    made = skipped = failed = 0

    for photo_id, filename in rows:
        src = photos_dir / filename
        if not src.exists():
            # A DB row whose file is gone. Not fatal, and not something this
            # script should "fix" by deleting the row — that's a judgment call
            # for a human. Report and move on.
            print(f"    ! {filename}: file missing on disk, skipped")
            failed += 1
            continue

        try:
            img = Image.open(src)
            img = ImageOps.exif_transpose(img)

            # Animated GIFs are never thumbnailed — Pillow's resize keeps only
            # the current frame, so a thumbnail would silently kill the
            # animation. Leaving thumb_filename NULL makes the template fall
            # back to the full file, which is the correct outcome.
            if is_animated(img):
                print(f"    · {filename}: animated, left as-is")
                skipped += 1
                continue

            thumb = flatten_to_rgb(img)
            thumb.thumbnail((THUMB_MAX_EDGE, THUMB_MAX_EDGE), Image.LANCZOS)
            thumb_name = f"{Path(filename).stem}.jpg"

            if not dry_run:
                thumb.save(thumbs_dir / thumb_name, "JPEG", quality=THUMB_QUALITY)
                con.execute(
                    "UPDATE completion_photos SET thumb_filename = ? WHERE id = ?",
                    (thumb_name, photo_id),
                )
            made += 1
        except Exception as exc:
            print(f"    ! {filename}: {exc}")
            failed += 1

    verb = "would generate" if dry_run else "✓ generated"
    print(f"  {verb} {made} thumbnail(s); {skipped} skipped, {failed} failed.")


# ===============================================================================
# STEP 2: Original-file resize (opt-in)
# ===============================================================================

def resize_in_place(path, max_edge):
    """Shrink one image file to max_edge on its long side, overwriting it.

    Returns (before_bytes, after_bytes), or None if the file was already
    small enough or can't be resized. Reads fully into a Pillow object
    before writing, so the overwrite is safe even though source and
    destination are the same path.
    """
    before = path.stat().st_size
    img = Image.open(path)
    img = ImageOps.exif_transpose(img)
    if is_animated(img):
        return None
    if max(img.size) <= max_edge:
        return None

    img.thumbnail((max_edge, max_edge), Image.LANCZOS)
    ext = path.suffix.lower()
    if ext in (".jpg", ".jpeg"):
        img.save(path, quality=92)
    elif ext in (".heic", ".heif"):
        # Shouldn't occur — process_photo has always converted HEIC to JPEG on
        # the way in — but if a stray one exists, don't try to re-encode HEIC.
        return None
    else:
        img.save(path)
    return before, path.stat().st_size


def resize_originals(photos_dir, avatars_dir, dry_run):
    """Cap stored photos at PHOTO_MAX_EDGE and avatars at AVATAR_MAX_EDGE.

    DESTRUCTIVE and irreversible: the uploader's full-resolution file is
    replaced by the downsized version. That's why it's opt-in rather than
    part of the default run — the thumbnail backfill above fixes the grid
    with no data loss at all, and this second step only matters for what
    the lightbox downloads when someone clicks a photo.

    Copy the uploads directory somewhere safe before running this.
    """
    saved = 0
    touched = 0

    for label, directory, max_edge in (
        ("photo", photos_dir, PHOTO_MAX_EDGE),
        ("avatar", avatars_dir, AVATAR_MAX_EDGE),
    ):
        if not directory.exists():
            continue
        # iterdir (not rglob) so the thumbs/ subdirectory is never walked —
        # thumbnails are already at their target size and re-resizing them
        # would just cost quality.
        for path in sorted(directory.iterdir()):
            if not path.is_file():
                continue
            try:
                if dry_run:
                    img = Image.open(path)
                    if max(img.size) > max_edge and not is_animated(img):
                        print(f"    would resize {label} {path.name} "
                              f"({img.size[0]}x{img.size[1]})")
                        touched += 1
                    continue
                result = resize_in_place(path, max_edge)
                if result:
                    before, after = result
                    saved += before - after
                    touched += 1
            except Exception as exc:
                print(f"    ! {path.name}: {exc}")

    if dry_run:
        print(f"  would resize {touched} file(s).")
    else:
        print(f"  ✓ resized {touched} file(s), reclaiming "
              f"{saved / (1024 * 1024):.1f} MB.")


# ===============================================================================
# STEP 3: Entry point
# ===============================================================================

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=BaseConfig.DATABASE,
                        help="path to the SQLite DB file")
    parser.add_argument("--uploads", default=BaseConfig.UPLOAD_DIR,
                        help="path to the uploads directory")
    parser.add_argument("--resize-originals", action="store_true",
                        help="also shrink stored photos/avatars in place "
                             "(destructive — back up uploads/ first)")
    parser.add_argument("--dry-run", action="store_true",
                        help="report what would change without writing anything")
    args = parser.parse_args()

    db_path = Path(args.db)
    uploads = Path(args.uploads)
    photos_dir = uploads / "photos"
    avatars_dir = uploads / "avatars"

    print(f"Database: {db_path}")
    print(f"Uploads:  {uploads}")
    if args.dry_run:
        print("DRY RUN — no files or rows will be modified.\n")

    if not db_path.exists():
        raise SystemExit("No database file. Run tools/init_db.py first.")

    con = sqlite3.connect(str(db_path))

    # Fail early with a useful message rather than a confusing "no such column"
    # halfway through the loop.
    cols = [row[1] for row in con.execute("PRAGMA table_info(completion_photos)")]
    if "thumb_filename" not in cols:
        con.close()
        raise SystemExit(
            "completion_photos.thumb_filename is missing. "
            "Run tools/migrate_open_hikes.py first."
        )

    thumbs_dir = thumb_dir_for(photos_dir)

    try:
        print("Thumbnails:")
        backfill_thumbs(con, photos_dir, thumbs_dir, args.dry_run)
        if not args.dry_run:
            con.commit()
    finally:
        con.close()

    if args.resize_originals:
        print("Originals:")
        resize_originals(photos_dir, avatars_dir, args.dry_run)
    else:
        print("\nOriginals left at full size. The photo grids are fixed either "
              "way; pass --resize-originals to also shrink what the lightbox "
              "downloads (back up uploads/ first — it overwrites in place).")

    print("Done.")


# Entry-point guard: run only on direct execution, not on import.
if __name__ == "__main__":
    main()
