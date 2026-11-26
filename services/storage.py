"""Artwork validation and Telegram private-channel storage."""
import html
from io import BytesIO
import re
from xml.etree.ElementTree import ParseError, tostring

from defusedxml import ElementTree
from defusedxml.common import DefusedXmlException
from PIL import Image, ImageDraw, ImageOps, UnidentifiedImageError
import requests
from telebot import types

from config import settings
from services import database as db

MAX_BYTES = 8 * 1024 * 1024
MAX_PIXELS = 24_000_000
EXTENSIONS = {"image/jpeg": "jpg", "image/png": "png", "image/svg+xml": "svg"}
SVG_NS = "{http://www.w3.org/2000/svg}"
SVG_TAGS = {
    SVG_NS + tag
    for tag in (
        "svg g path rect circle ellipse line polyline polygon defs "
        "linearGradient radialGradient stop title desc".split()
    )
}
SVG_ATTRS = set(
    (
        "id viewBox width height x y x1 y1 x2 y2 cx cy r rx ry d points fill stroke stroke-width "
        "fill-rule fill-opacity stroke-opacity opacity transform stroke-linecap stroke-linejoin "
        "offset stop-color stop-opacity gradientUnits gradientTransform preserveAspectRatio version"
    ).split()
)


class ArtworkError(ValueError):
    pass


def download_telegram_file(bot, file_id: str, file_size: int | None = None) -> bytes:
    if (file_size or 0) > MAX_BYTES:
        raise ArtworkError("Send a file of 8 MiB or smaller.")
    path = bot.get_file(file_id).file_path
    with requests.get(
        f"https://api.telegram.org/file/bot{settings.telegram_bot_token}/{path}",
        stream=True,
        timeout=(5, 20),
    ) as response:
        response.raise_for_status()
        result = bytearray()
        for chunk in response.iter_content(65536):
            if len(result) + len(chunk) > MAX_BYTES:
                raise ArtworkError("Send a file of 8 MiB or smaller.")
            result.extend(chunk)
        return bytes(result)


def validate_image(data: bytes, mime: str) -> bytes:
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


def store_in_channel(
    bot,
    data: bytes,
    filename: str,
    user_id: int,
    batch_id: str,
    full_name: str = "",
    phone: str = "",
) -> tuple[str, int]:
    """Upload validated artwork to the private Telegram storage channel.
    Returns (channel_file_id, channel_message_id).
    """
    bio = BytesIO(data)
    bio.name = filename
    caption = (
        f"Name: {full_name or 'Customer'}\n"
        f"Phone: {phone or 'N/A'}\n"
        f"Batch: {batch_id[:8]} | File: {filename}"
    )
    sent = bot.send_document(
        settings.storage_channel_id,
        bio,
        caption=caption,
        disable_notification=True,
    )
    return sent.document.file_id, sent.message_id


def store_template_sample(bot, file_id: str, title: str, is_photo: bool = True) -> tuple[str, int]:
    """Copy/send a template sample image into the private storage channel.
    Returns (stored_file_id, channel_message_id).
    """
    caption = f"TEMPLATE SAMPLE | {title}"
    if is_photo:
        sent = bot.send_photo(
            settings.storage_channel_id,
            file_id,
            caption=caption,
            disable_notification=True,
        )
        return sent.photo[-1].file_id, sent.message_id
    sent = bot.send_document(
        settings.storage_channel_id,
        file_id,
        caption=caption,
        disable_notification=True,
    )
    return sent.document.file_id, sent.message_id


def tag_order_in_channel(bot, order: dict, batch_items: list[dict]) -> None:
    """Update existing uploaded sticker captions with order number, name, and phone."""
    short_id = order["id"][:8]
    name = order.get("customer_name") or "Customer"
    phone = order["customer_phone"]
    items_by_ref = {it["ref_id"]: it for it in order["items"] if it.get("kind") == "upload"}

    for up in batch_items:
        item = items_by_ref.get(up["id"])
        size_info = f" | Size: {item['size']} × {item['quantity']}" if item else ""
        caption = (
            f"Order #{short_id}\n"
            f"Name: {name}\n"
            f"Phone: {phone}\n"
            f"File: {up['file_name']}{size_info}"
        )
        try:
            bot.edit_message_caption(
                chat_id=settings.storage_channel_id,
                message_id=up["channel_message_id"],
                caption=caption,
            )
        except Exception:
            pass


def send_order_package_to_channel(
    bot,
    order: dict,
    batch_items: list[dict],
    screenshot_file_id: str,
    *,
    is_photo: bool = True,
) -> tuple[str | None, int | None]:
    """Send the payment screenshot + all chosen sticker images together in one album with rich HTML caption."""
    short_id = html.escape(order["id"][:8])
    name = html.escape(order.get("customer_name") or "Customer")
    uid = order["telegram_user_id"]
    phone = html.escape(order["customer_phone"])

    sticker_lines = []
    for it in order["items"]:
        lbl = html.escape(it.get("label", it["size"]))
        sz = html.escape(it["size"])
        color = f" ({html.escape(it['keyboard_color'])})" if it.get("keyboard_color") else ""
        sticker_lines.append(f"• <i>{lbl}</i> · <b>{sz}</b>{color} × {it['quantity']}")

    if order["delivery_method"] == "delivery":
        loc = html.escape(order.get("delivery_address") or "Confirm address")
        handover = f"<b>Delivery Location:</b> {loc}"
    else:
        handover = "<b>Handover:</b> Pickup"

    caption = (
        f"<b>🧾 ORDER #{short_id} · PAYMENT SCREENSHOT</b>\n"
        f"<i>Order ID:</i> <code>{html.escape(order['id'])}</code>\n"
        f"<b>Name:</b> {name}\n"
        f"<b>User ID:</b> <code>{uid}</code>\n"
        f"<b>Phone:</b> <code>{phone}</code>\n"
        f"<b>Total:</b> {html.escape(str(order['total']))} ETB "
        f"(<i>Deposit: {html.escape(str(order['deposit_amount']))} ETB</i>)\n"
        f"{handover}\n"
        f"<b>Stickers:</b>\n" + "\n".join(sticker_lines)
    )[:1024]

    stored_fid = None
    msg_id = None

    # Collect photos (payment screenshot + template sample images) into one media group
    photo_media = []
    if is_photo:
        photo_media.append(types.InputMediaPhoto(screenshot_file_id, caption=caption, parse_mode="HTML"))
    for it in order["items"]:
        if it.get("kind") == "template":
            tpl = db.get_template(it["ref_id"])
            if tpl and tpl.get("sample_file_id"):
                extra_cap = None if photo_media else caption
                extra_pm = None if photo_media else "HTML"
                photo_media.append(types.InputMediaPhoto(tpl["sample_file_id"], caption=extra_cap, parse_mode=extra_pm))

    try:
        if photo_media:
            sent_list = bot.send_media_group(
                settings.storage_channel_id,
                photo_media[:10],
                disable_notification=True,
            )
            if is_photo and sent_list:
                stored_fid = sent_list[0].photo[-1].file_id
                msg_id = sent_list[0].message_id
        if not is_photo:
            sent_doc = bot.send_document(
                settings.storage_channel_id,
                screenshot_file_id,
                caption=caption,
                parse_mode="HTML",
                disable_notification=True,
            )
            stored_fid = sent_doc.document.file_id
            msg_id = sent_doc.message_id
    except Exception:
        return None, None

    # Send any uploaded custom artwork documents together in one media group
    if batch_items:
        doc_media = []
        items_by_ref = {it["ref_id"]: it for it in order["items"] if it.get("kind") == "upload"}
        for idx, up in enumerate(batch_items[:10]):
            item = items_by_ref.get(up["id"])
            size_info = f" · {item['size']} × {item['quantity']}" if item else ""
            doc_cap = (
                f"<b>Order #{short_id}</b> · {name} · <code>{phone}</code>\n"
                f"<i>{html.escape(up['file_name'])}{html.escape(size_info)}</i>"
                if idx == len(batch_items[:10]) - 1
                else None
            )
            doc_media.append(
                types.InputMediaDocument(
                    up["file_id"],
                    caption=doc_cap,
                    parse_mode="HTML" if doc_cap else None,
                )
            )
        try:
            bot.send_media_group(settings.storage_channel_id, doc_media, disable_notification=True)
        except Exception:
            pass

    return stored_fid, msg_id


def _load_item_image(bot, item: dict) -> Image.Image | None:
    """Download an uploaded item from Telegram and return an RGB/RGBA PIL Image (None for SVG/failure)."""
    if item.get("extension") == "svg":
        return None
    try:
        raw = download_telegram_file(bot, item["file_id"])
        with Image.open(BytesIO(raw)) as img:
            return ImageOps.exif_transpose(img).convert("RGBA")
    except Exception:
        return None


def build_sticker_preview(bot, item: dict, max_dim: int = 512) -> bytes | None:
    """Return PNG bytes of a single sticker for Telegram send_photo preview."""
    img = _load_item_image(bot, item)
    if img is None:
        return None
    img.thumbnail((max_dim, max_dim), Image.Resampling.LANCZOS)
    bg = Image.new("RGB", img.size, (246, 245, 238))
    bg.paste(img, (0, 0), img)
    out = BytesIO()
    bg.save(out, format="PNG")
    return out.getvalue()


def build_batch_contact_sheet(bot, items: list[dict], cols: int = 4) -> bytes | None:
    """Build a numbered visual contact sheet PNG for a batch of uploaded stickers."""
    if not items:
        return None
    display_items = items[:24]
    tile = 200
    pad = 16
    rows = (len(display_items) + cols - 1) // cols
    width = cols * tile + (cols + 1) * pad
    height = rows * tile + (rows + 1) * pad + 36
    sheet = Image.new("RGB", (width, height), (246, 245, 238))
    draw = ImageDraw.Draw(sheet)

    header = f"Your Stickers ({len(items)} total)" + (" — showing first 24" if len(items) > 24 else "")
    draw.text((pad, 10), header, fill=(24, 47, 39))

    for idx, item in enumerate(display_items):
        r, c = divmod(idx, cols)
        x = pad + c * (tile + pad)
        y = 36 + pad + r * (tile + pad)
        draw.rounded_rectangle([x, y, x + tile, y + tile], radius=12, fill=(255, 254, 249), outline=(212, 223, 212), width=2)

        img = _load_item_image(bot, item)
        if img is not None:
            img.thumbnail((tile - 16, tile - 16), Image.Resampling.LANCZOS)
            ix = x + (tile - img.width) // 2
            iy = y + (tile - img.height) // 2
            sheet.paste(img, (ix, iy), img)
        else:
            draw.text((x + 20, y + tile // 2 - 8), item.get("extension", "FILE").upper(), fill=(80, 100, 91))

        # Number badge (#1, #2, ...)
        badge = f"#{idx + 1}"
        bw = 26 + 8 * len(badge)
        draw.rounded_rectangle([x + 6, y + 6, x + 6 + bw, y + 30], radius=8, fill=(27, 88, 69))
        draw.text((x + 12, y + 12), badge, fill=(255, 254, 249))

        if item.get("size"):
            tag = f"{item['size']} × {item.get('quantity', 1)}"
            draw.rounded_rectangle([x + 6, y + tile - 28, x + tile - 6, y + tile - 6], radius=6, fill=(229, 240, 229))
            draw.text((x + 12, y + tile - 23), tag[:20], fill=(24, 47, 39))

    out = BytesIO()
    sheet.save(out, format="PNG")
    return out.getvalue()
