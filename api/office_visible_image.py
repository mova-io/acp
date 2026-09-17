"""Bounded Word visible-crop extraction for reviewed OCR drafts.

Identity binds a transcript to both the original raster and its visible crop. It does
not establish that deleting the picture preserves diagram content.
"""
from __future__ import annotations
import hashlib
import io
import re
import zipfile
from lxml import etree as ET
from PIL import Image
from apply_office_image_of_text import _media_index

NS = {'w': 'http://schemas.openxmlformats.org/wordprocessingml/2006/main',
      'wp': 'http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing',
      'a': 'http://schemas.openxmlformats.org/drawingml/2006/main',
      'r': 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'}


def _crops_nothing(rectangle) -> bool:
    """True when this a:srcRect PROVABLY hides no pixels, so the raster is fully visible.

    OOXML treats an omitted inset as 0, so `<a:srcRect/>` and `<a:srcRect l="0" t="0"/>`
    crop exactly nothing — the picture Word renders is the whole raster, identical to a
    placement with no srcRect at all. Word writes such an element for a picture whose crop
    handles were never moved, or were reset, and production documents carry it.

    False for anything NOT provably a zero inset — an unknown attribute, a non-integer, a
    negative or out-of-range value. Those are malformed or real geometry and must stay on
    the fail-closed path, which is why this answers "provably harmless", not "not cropped".
    """
    if any(key not in ('l', 't', 'r', 'b') for key in rectangle.attrib):
        return False
    try:
        return not any(int(rectangle.get(key, '0')) for key in ('l', 't', 'r', 'b'))
    except (TypeError, ValueError):
        return False


def _vouched_inline_srcrect(data: bytes, locator: str):
    """(raster bytes, the one a:srcRect) for a placement this module can fully vouch for.

    THE PLACEMENT BATTERY, extracted so the two answers built on it cannot drift: the crop
    geometry `visible_word_image` computes, and the "may the caller use the raster instead?"
    decision `has_word_crop` makes for a srcRect that crops nothing. Both need exactly this
    guarantee, and a second copy of it would be a second policy.

    None — refuse — unless ALL of:
      * the locator names a media part in this package;
      * that media is referenced exactly ONCE, from word/document.xml (never shared);
      * exactly one a:blip carries that relationship, and nothing else in the part does;
      * the blip sits in a single-picture wp:inline inside the w:body (no anchor, no tile);
      * no a:xfrm in that inline rotates or flips the picture;
      * there is exactly one a:srcRect, and its attributes are only l/t/r/b.

    Everything refused here is a placement whose rendered pixels we cannot predict from the
    raster alone. That is why a rotated, flipped, shared, tiled or doubly-cropped picture
    yields no bytes at all — not even when its srcRect happens to crop nothing.
    """
    from apply_office_image_replacement import _references
    match = re.fullmatch(r'image\s+(\d+)', locator.strip(), re.I)
    if not match:
        return None
    with zipfile.ZipFile(io.BytesIO(data)) as zin:
        media = _media_index(zin)
        index = int(match[1]) - 1
        if not 0 <= index < len(media):
            return None
        raw = zin.read(media[index])
        refs = _references(zin, media[index])
        if len(refs) != 1 or refs[0][0] != 'word/document.xml':
            return None
        rid = refs[0][1]
        root = ET.fromstring(zin.read('word/document.xml'), parser=ET.XMLParser(resolve_entities=False, no_network=True))
        blips = root.xpath('.//a:blip[@r:embed=$rid]', namespaces=NS, rid=rid)
        if len(blips) != 1 or sum(v == rid for e in root.iter() for k, v in e.attrib.items() if k.startswith('{' + NS['r'] + '}')) != 1:
            return None
        inline = next((p for p in blips[0].iterancestors() if p.tag == '{' + NS['wp'] + '}inline'), None)
        if inline is None or inline.xpath('.//a:tile', namespaces=NS) or len(inline.xpath('.//a:blip', namespaces=NS)) != 1:
            return None
        if not any(p.tag == '{' + NS['w'] + '}body' for p in inline.iterancestors()):
            return None
        for transform in inline.xpath('.//a:xfrm', namespaces=NS):
            if any(transform.get(k) not in (None, '0', 'false') for k in ('rot', 'flipH', 'flipV')):
                return None
        rectangles = inline.xpath('.//a:srcRect', namespaces=NS)
        if len(rectangles) != 1 or any(k not in ('l','t','r','b') for k in rectangles[0].attrib):
            return None
        return raw, rectangles[0]


def visible_word_image(data: bytes, locator: str) -> dict | None:
    """Return a uniquely placed inline body's cropped pixels and identity, or None.

    None when there is nothing to crop, as well as when a crop cannot be read safely: a
    placement with no srcRect and one whose srcRect crops nothing both have no visible
    region distinct from the raster, so there is no crop identity to bind. Callers asking
    "may I use the full image?" must ask `has_word_crop`, which answers False for a crop
    that hides nothing ONLY on a placement `_vouched_inline_srcrect` accepts.
    """
    try:
        vouched = _vouched_inline_srcrect(data, locator)
        if vouched is None:
            return None
        raw, rectangle = vouched
        crop = {k: int(rectangle.get(k, '0')) for k in ('l','t','r','b')}
        # All-zero: nothing is hidden, so there is no visible crop to describe — the raster
        # itself is it. Not a refusal, and NOT a licence either: `has_word_crop` decides
        # whether the raster may be used, and it re-runs the battery above before saying yes.
        if not any(crop.values()) or any(v < 0 or v >= 100000 for v in crop.values()) or crop['l'] + crop['r'] >= 100000 or crop['t'] + crop['b'] >= 100000:
            return None
        image = Image.open(io.BytesIO(raw))
        if image.width * image.height > 20000000 or getattr(image, 'n_frames', 1) != 1:
            return None
        # Round inward: never introduce pixels outside the visible crop.
        import math
        box = (math.ceil(image.width * crop['l'] / 100000), math.ceil(image.height * crop['t'] / 100000),
               math.floor(image.width * (100000-crop['r']) / 100000), math.floor(image.height * (100000-crop['b']) / 100000))
        if box[0] >= box[2] or box[1] >= box[3]:
            return None
        out = io.BytesIO()
        visible_image = image.crop(box)
        # Keep transparency: flattening onto black can change visible text/graphics.
        mode = 'RGBA' if 'A' in image.getbands() or 'transparency' in image.info else 'RGB'
        visible_image.convert(mode).save(out, format='PNG')
        visible = out.getvalue()
        return {'image_bytes': visible, 'source_image_sha256': hashlib.sha256(raw).hexdigest(),
                'crop': crop, 'visible_image_sha256': hashlib.sha256(visible).hexdigest()}
    except (ValueError, TypeError, KeyError, OSError, zipfile.BadZipFile, ET.XMLSyntaxError):
        return None


def has_word_crop(data: bytes, locator: str) -> bool:
    """Detect any crop placement for this media, including unsupported/shared ones.

    False answers "the caller may use the raster it already has". That needs TWO facts, not
    one: the srcRect must crop nothing (`_crops_nothing`), and the placement must be one
    `_vouched_inline_srcrect` accepts. Word writes `<a:srcRect/>` for a picture whose crop
    handles were never moved, and calling that a crop made every draft for such a picture
    fail closed — no vision call, no proposal, no preview, for an image with nothing to
    withhold. But "the insets are zero" says nothing about a picture that is also FLIPPED,
    ROTATED, TILED, SHARED between drawings or carrying a second srcRect: there the rendered
    pixels are not predictable from the raster, so those still answer True and yield no
    bytes. Unreadable geometry answers True as well.
    """
    from apply_office_image_replacement import _references
    try:
        match = re.fullmatch(r'image\s+(\d+)', locator.strip(), re.I)
        if not match:
            return False
        with zipfile.ZipFile(io.BytesIO(data)) as zin:
            media = _media_index(zin)
            index = int(match[1]) - 1
            if not 0 <= index < len(media):
                return False
            rectangles = []
            for owner, rid in _references(zin, media[index]):
                if not owner:
                    continue
                root = ET.fromstring(zin.read(owner), parser=ET.XMLParser(resolve_entities=False, no_network=True))
                for blip in root.xpath('.//a:blip[@r:embed=$rid]', namespaces=NS, rid=rid):
                    rectangles += blip.getparent().xpath('./a:srcRect', namespaces=NS)
        if not rectangles:
            return False
        # Exactly one srcRect in the whole package for this media, cropping nothing, on a
        # vouched placement. Any other shape — two rectangles, a real inset, or a placement
        # the battery refuses — is a crop as far as every caller is concerned.
        return not (len(rectangles) == 1 and _crops_nothing(rectangles[0])
                    and _vouched_inline_srcrect(data, locator) is not None)
    except (ValueError, TypeError, KeyError, OSError, zipfile.BadZipFile, ET.XMLSyntaxError):
        # Unable to establish geometry: don't issue a supposedly uncropped draft.
        return True


def visible_word_relationship(entries: dict, part: str, rid: str) -> tuple[bool, bytes | None]:
    """(is cropped/unknown, safe visible pixels) for the existing alt-image lookup.

    Uncropped images retain the existing lookup bytes — including a picture whose srcRect
    crops nothing on a placement `_vouched_inline_srcrect` accepts, which is uncropped in
    every sense that matters here. Any unsupported cropped use returns no pixels, so a
    vision call cannot describe hidden content; a zero-inset srcRect on a flipped, rotated,
    tiled or shared placement is one of those, and returns (True, None) like any other.
    """
    if not part.startswith("word/"):
        return False, None
    from apply_office_image_of_text import _rid_map
    part_xml = entries.get(part, b"")
    if isinstance(part_xml, str):
        part_xml = part_xml.encode("utf-8")
    if b"srcRect" not in part_xml:
        return False, None
    try:
        package = io.BytesIO()
        with zipfile.ZipFile(package, 'w') as zout:
            for name, value in entries.items():
                zout.writestr(name, value)
        data = package.getvalue()
        with zipfile.ZipFile(io.BytesIO(data)) as zin:
            target = _rid_map(zin, part).get(rid)
            media = _media_index(zin)
            if target not in media:
                return True, None
            locator = f"image {media.index(target) + 1}"
        if not has_word_crop(data, locator):
            return False, None
        visible = visible_word_image(data, locator)
        return True, visible['image_bytes'] if visible is not None else None
    except (ValueError, TypeError, KeyError, OSError, zipfile.BadZipFile, ET.XMLSyntaxError):
        return True, None


def word_media_visible_placements(data: bytes, media_name: str) -> list[bytes] | None:
    """Visible pixels for every Word use; [] unused, None unsupported geometry.

    Shared relationships are safe for detection only when every placement can be
    examined. This does not authorize replacing/describing shared pictures.
    """
    from apply_office_image_replacement import _references
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            raw = z.read(media_name)
            image = Image.open(io.BytesIO(raw))
            if image.width * image.height > 20000000 or getattr(image, 'n_frames', 1) != 1:
                return None
            result = []
            for part, rid in _references(z, media_name):
                if not part.startswith('word/'):
                    return None
                root = ET.fromstring(z.read(part), parser=ET.XMLParser(resolve_entities=False, no_network=True))
                blips = root.xpath('.//a:blip[@r:embed=$rid]', namespaces=NS, rid=rid)
                uses = sum(v == rid for e in root.iter() for k,v in e.attrib.items() if k.startswith('{'+NS['r']+'}'))
                if uses != len(blips):
                    return None
                for blip in blips:
                    if len(result) >= 30:
                        return None
                    inline = next((p for p in blip.iterancestors() if p.tag == '{'+NS['wp']+'}inline'), None)
                    if inline is None or inline.xpath('.//a:tile', namespaces=NS) or len(inline.xpath('.//a:blip', namespaces=NS)) != 1:
                        return None
                    for transform in inline.xpath('.//a:xfrm', namespaces=NS):
                        if any(transform.get(k) not in (None,'0','false') for k in ('rot','flipH','flipV')):
                            return None
                    rects = blip.getparent().xpath('./a:srcRect', namespaces=NS)
                    if len(rects) > 1:
                        return None
                    if not rects:
                        result.append(raw)
                        continue
                    if any(k not in ('l','t','r','b') for k in rects[0].attrib):
                        return None
                    crop = {k:int(rects[0].get(k,'0')) for k in ('l','t','r','b')}
                    if any(v < 0 or v >= 100000 for v in crop.values()) or crop['l']+crop['r'] >= 100000 or crop['t']+crop['b'] >= 100000:
                        return None
                    # Outward rounding includes boundary pixels rather than hiding
                    # a possibly visible glyph at a fractional crop edge.
                    import math
                    box=(math.floor(image.width*crop['l']/100000),math.floor(image.height*crop['t']/100000),math.ceil(image.width*(100000-crop['r'])/100000),math.ceil(image.height*(100000-crop['b'])/100000))
                    if box[0]>=box[2] or box[1]>=box[3]:
                        return None
                    out=io.BytesIO(); image.crop(box).save(out,format='PNG'); result.append(out.getvalue())
            return result
    except (ValueError,TypeError,KeyError,OSError,zipfile.BadZipFile,ET.XMLSyntaxError):
        return None
