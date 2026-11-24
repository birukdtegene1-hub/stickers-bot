"""Validate artwork and keep print originals in a private storage bucket."""
from io import BytesIO
import re
from xml.etree.ElementTree import ParseError, tostring

from defusedxml import ElementTree
from defusedxml.common import DefusedXmlException
from PIL import Image, UnidentifiedImageError
import requests

from config import settings
from services import database as db

MAX_BYTES = 8 * 1024 * 1024
MAX_PIXELS = 24_000_000
EXTENSIONS = {"image/jpeg": "jpg", "image/png": "png", "image/svg+xml": "svg"}
SVG_NS = "{http://www.w3.org/2000/svg}"
SVG_TAGS = {SVG_NS + tag for tag in ("svg g path rect circle ellipse line polyline polygon defs linearGradient radialGradient stop title desc".split())}
SVG_ATTRS = set(("id viewBox width height x y x1 y1 x2 y2 cx cy r rx ry d points fill stroke stroke-width "
                 "fill-rule fill-opacity stroke-opacity opacity transform stroke-linecap stroke-linejoin "
                 "offset stop-color stop-opacity gradientUnits gradientTransform preserveAspectRatio version").split())


class ArtworkError(ValueError):
    pass


def download_file(bot, document):
    if (document.file_size or 0) > MAX_BYTES:
        raise ArtworkError("Send a file of 8 MiB or smaller.")
    path = bot.get_file(document.file_id).file_path
    # Never propagate the token-bearing download URL into application logs.
    with requests.get(f"https://api.telegram.org/file/bot{settings.telegram_bot_token}/{path}",
                      stream=True, timeout=(5, 20)) as response:
        response.raise_for_status()
        result = bytearray()
        for chunk in response.iter_content(65536):
            if len(result) + len(chunk) > MAX_BYTES:
                raise ArtworkError("Send a file of 8 MiB or smaller.")
            result.extend(chunk)
        return bytes(result)


def validate_image(data, mime):
    if not data or len(data) > MAX_BYTES or mime not in EXTENSIONS:
        raise ArtworkError("Send a PNG, JPEG or SVG file up to 8 MiB.")
    if mime == "image/svg+xml":
        try:
            root = ElementTree.fromstring(data, forbid_dtd=True)
            if root.tag != SVG_NS + "svg":
                raise ValueError()
            for node in root.iter():
                if node.tag not in SVG_TAGS:
                    raise ValueError()
                for key, value in node.attrib.items():
                    if key not in SVG_ATTRS or not re.fullmatch(r"[A-Za-z0-9 #.,%()_+\-\s]*", value):
                        raise ValueError()
                    if "url" in value.lower() and not re.fullmatch(r"url\(#[\w-]+\)", value):
                        raise ValueError()
            # Serialize the validated tree. This also drops XML processing instructions.
            return tostring(root, encoding="utf-8")
        except (DefusedXmlException, ParseError, ValueError, RecursionError) as exc:
            raise ArtworkError("Export a plain vector SVG with outlined text, or send a PNG.") from exc
    expected = "JPEG" if mime == "image/jpeg" else "PNG"
    try:
        with Image.open(BytesIO(data), formats=[expected]) as image:
            if image.width * image.height > MAX_PIXELS or getattr(image, "n_frames", 1) != 1:
                raise ArtworkError("Send a single image of up to 24 megapixels.")
            image.verify()
        with Image.open(BytesIO(data), formats=[expected]) as image:
            image.load()
    except (UnidentifiedImageError, OSError, SyntaxError, Image.DecompressionBombError) as exc:
        raise ArtworkError("This image could not be read. Export it again as PNG or JPEG.") from exc
    return data


def object_path(upload):
    return f"uploads/{upload['owner_id']}/{upload['id']}.{upload['extension']}"


def private_bucket():
    storage = db.client().storage
    if storage.get_bucket(settings.supabase_bucket).public is not False:
        raise RuntimeError("ARTWORK_BUCKET must be private.")
    return storage.from_(settings.supabase_bucket)


def store(upload, data, mime):
    private_bucket().upload(object_path(upload), data, {"content-type": mime, "upsert": "true"})


def preview_url(upload):
    return private_bucket().create_signed_url(object_path(upload), 900)["signedURL"]


def print_url(order):
    if order.get("upload_id"):
        return private_bucket().create_signed_url(order["image_url"], 900, {"download": True})["signedURL"]
    # Legacy records stored public URLs. Only sign originals in this user's old folder.
    prefix = f"{settings.supabase_url}/storage/v1/object/public/{settings.legacy_bucket}/uploads/{order['telegram_user_id']}/"
    url = order.get("image_url") or ""
    if not url.startswith(prefix):
        return None
    name = url[len(prefix):].strip()
    if not re.fullmatch(r"[a-f0-9]{32}\.(png|jpg|svg)", name):
        return None
    path = f"uploads/{order['telegram_user_id']}/{name}"
    return db.client().storage.from_(settings.legacy_bucket).create_signed_url(path, 900, {"download": True})["signedURL"]


def cleanup():
    rows = db.client().rpc("claim_expired_artwork", {}).execute().data
    if rows:
        private_bucket().remove([object_path(row) for row in rows])
        db.client().table("artwork_uploads").delete().in_("id", [row["id"] for row in rows]).eq("deleting", True).execute()
    return len(rows)
