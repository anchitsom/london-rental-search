"""OpenRent inline-JS-global + detail-page extractors.

Lifted unchanged from experiments/openrent-probe/probe/extract.py (probe REPORT
2026-05-23). The probe REPORT documents the page architecture: search page uses
`var PROPERTYIDS = [...]`, `var PROPERTYLISTLATITUDES = [...]`, parallel arrays
indexed by position. Detail page uses an `<h2>`-anchored `<tr><td>label</td>
<td>{svg|text}</td></tr>` grid. Boolean fields render as svg with class
text-success (yes) or text-danger (no).
"""

import json
import re
from typing import Any


def _extract_int_list(html: str, var_name: str) -> list[int]:
    """Pull `var <var_name> = [int, int, ...]` out of the HTML."""
    m = re.search(rf'var\s+{re.escape(var_name)}\s*=\s*\[([^\]]*)\]', html)
    if not m:
        return []
    raw = m.group(1).strip()
    if not raw:
        return []
    out = []
    for x in raw.split(","):
        x = x.strip()
        if x.lstrip("-").isdigit():
            out.append(int(x))
    return out


def _extract_float_list(html: str, var_name: str) -> list[float]:
    m = re.search(rf'var\s+{re.escape(var_name)}\s*=\s*\[([^\]]*)\]', html)
    if not m:
        return []
    raw = m.group(1).strip()
    if not raw:
        return []
    out = []
    for x in raw.split(","):
        x = x.strip()
        try:
            out.append(float(x))
        except ValueError:
            continue
    return out


def _extract_int(html: str, var_name: str) -> int | None:
    m = re.search(rf'var\s+{re.escape(var_name)}\s*=\s*([0-9]+)', html)
    return int(m.group(1)) if m else None


def extract_search(html: str) -> dict[str, Any]:
    """Pull the load-bearing search globals out of OpenRent's HTML."""
    return {
        "property_ids": _extract_int_list(html, "PROPERTYIDS"),
        "latitudes": _extract_float_list(html, "PROPERTYLISTLATITUDES"),
        "longitudes": _extract_float_list(html, "PROPERTYLISTLONGITUDES"),
        "number_of_properties": _extract_int(html, "NUMBEROFPROPERTIES") or 0,
    }


_BED_RE = re.compile(r"\b(\d+)\s*Bed\b", re.I)
_STUDIO_RE = re.compile(r"\bStudio\b", re.I)
_POSTCODE_AREA_RE = re.compile(r"\b([A-Z]{1,2}[0-9R][0-9A-Z]?)\b\s*$")
_BATHROOM_RE = re.compile(r"(\d+)[\-\s]?bathroom", re.I)
_MONEY_RE = re.compile(r"£\s*([\d,]+(?:\.\d+)?)")


def _money_to_float(text: str) -> float | None:
    m = _MONEY_RE.search(text or "")
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", ""))
    except ValueError:
        return None


def _section_text(soup, anchor_text: str) -> str | None:
    """Find the h2 with `anchor_text` and return its enclosing section text."""
    for h2 in soup.find_all("h2"):
        if h2.get_text(strip=True) == anchor_text:
            # Walk up to a container that holds more than just the heading
            container = h2.parent
            for _ in range(4):
                if container is None:
                    break
                txt = container.get_text(" | ", strip=True)
                if len(txt) > len(anchor_text) + 10:
                    return txt
                container = container.parent
            return h2.parent.get_text(" | ", strip=True) if h2.parent else None
    return None


def _label_value(section_text: str, label: str) -> str | None:
    """Given a ' | '-joined section text, return the token immediately after `label`."""
    if not section_text:
        return None
    parts = [p.strip() for p in section_text.split("|")]
    for i, p in enumerate(parts):
        if p == label and i + 1 < len(parts):
            return parts[i + 1]
    return None


def _parse_tr_grid(soup) -> dict[str, str | bool | float | None]:
    """Walk every `<tr><td>label</td><td>{svg or text}</td></tr>` and return label->value.

    Boolean labels (Garden, Parking, Student Friendly, etc.) get True/False from svg
    class `text-success` / `text-danger`. Text-value labels (Rent PCM, EPC Rating, ...)
    get the cell text, stripped. Money values stay as strings here; callers convert.

    Trailing disclaimer text (after the value) is dropped by taking only the first
    direct text node inside the value cell.
    """
    out: dict[str, str | bool | float | None] = {}
    for tr in soup.find_all("tr"):
        tds = tr.find_all("td")
        if len(tds) != 2:
            continue
        # Take only the first text node of the label cell — disclaimers in nested
        # divs/popovers concatenate with .get_text() and corrupt the label.
        label_parts = []
        for child in tds[0].children:
            if hasattr(child, "get_text"):
                t = child.get_text(strip=True)
            else:
                t = str(child).strip()
            if t:
                label_parts.append(t)
                break
        label = label_parts[0] if label_parts else tds[0].get_text(strip=True)
        svg = tds[1].find("svg")
        if svg is not None:
            cls = " ".join(svg.get("class", []) or [])
            if "text-success" in cls:
                out[label] = True
            elif "text-danger" in cls:
                out[label] = False
            else:
                out[label] = None
        else:
            # Take only the first text content of the value cell, drop nested help text
            first_text = None
            for child in tds[1].children:
                txt = child.get_text(strip=True) if hasattr(child, "get_text") else str(child).strip()
                if txt:
                    first_text = txt
                    break
            out[label] = first_text or tds[1].get_text(strip=True) or None
    return out


def extract_detail(html: str) -> dict:
    """Extract structured fields from an OpenRent detail-page HTML (2026-05 template).

    The page uses h2-anchored section blocks each containing a `<tr><td>label</td><td>value</td></tr>`
    grid. Booleans are svg text-success / text-danger icons; text values are in <td>.
    """
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")

    h1 = soup.find("h1")
    title = h1.get_text(strip=True) if h1 else None
    bedrooms = None
    if title:
        m = _BED_RE.search(title)
        if m:
            bedrooms = int(m.group(1))
        elif _STUDIO_RE.search(title):
            bedrooms = 0
    postcode_area = None
    if title:
        m = _POSTCODE_AREA_RE.search(title)
        postcode_area = m.group(1) if m else None

    grid = _parse_tr_grid(soup)

    def text_field(k: str) -> str | None:
        v = grid.get(k)
        return v if isinstance(v, str) else None

    def bool_field(k: str) -> bool | None:
        v = grid.get(k)
        return v if isinstance(v, bool) else None

    price_monthly = _money_to_float(text_field("Rent PCM") or "")
    deposit = _money_to_float(text_field("Deposit") or "")

    furnishing = text_field("Furnishing")
    epc_rating = text_field("EPC Rating")
    garden = bool_field("Garden")
    parking = bool_field("Parking")
    fireplace = bool_field("Fireplace")

    available_from = text_field("Available From")
    min_tenancy = text_field("Preferred Minimum Tenancy")

    bills_included = bool_field("Bills Included")

    preferences = {
        "Student Friendly": bool_field("Student Friendly"),
        "Families Allowed": bool_field("Families Allowed"),
        "Pets Allowed": bool_field("Pets Allowed"),
        "Smokers Allowed": bool_field("Smokers Allowed"),
        "DSS/LHA Covers Rent": bool_field("DSS/LHA Covers Rent"),
    }

    # photos: swiper carousel slides
    photos = []
    for slide in soup.select(".swiper-slide img[src]"):
        src = slide["src"]
        if src.startswith("//"):
            src = "https:" + src
        if "imagescdn.openrent.co.uk/listings/" in src:
            photos.append(src)

    # description: walk the "About This Property" block
    description = None
    for text_node in soup.find_all(string="About This Property"):
        parent = text_node.parent
        if parent:
            container = parent
            for _ in range(3):
                if container is None or container.name == "body":
                    break
                container = container.parent
            if container:
                txt = container.get_text(" ", strip=True)
                idx = txt.find("About This Property")
                if idx >= 0:
                    description = txt[idx + len("About This Property"):].strip()
                    if len(description) > 4000:
                        description = description[:4000]
                break

    # bathrooms: regex over description (free-text)
    bathrooms = None
    if description:
        m = _BATHROOM_RE.search(description)
        if m:
            try:
                bathrooms = int(m.group(1))
            except ValueError:
                pass

    # let_agreed banner
    let_banner = soup.select_one("div.alert.alert-warning")
    let_agreed = bool(let_banner and "Let Agreed" in let_banner.get_text())

    # transport: nearby stations live in the area-named section ("London, WC2R" style)
    transport_nearby = []
    for h2 in soup.find_all("h2"):
        t = h2.get_text(strip=True)
        if t.startswith("London, ") or (postcode_area and postcode_area in t and "," in t):
            sec = _section_text(soup, t)
            if sec:
                parts = [p.strip() for p in sec.split("|")]
                # Pair station name with walk time: alternating after the heading
                i = 1
                while i + 1 < len(parts):
                    name, dur = parts[i], parts[i + 1]
                    if "min" in dur:
                        transport_nearby.append({"station": name, "duration": dur})
                        i += 2
                    else:
                        i += 1
            break

    return {
        "title": title,
        "price_monthly": price_monthly,
        "bedrooms": bedrooms,
        "postcode_area": postcode_area,
        "bathrooms": bathrooms,
        "deposit": deposit,
        "bills_included": bills_included,
        "furnishing": furnishing,
        "epc_rating": epc_rating,
        "garden": garden,
        "parking": parking,
        "fireplace": fireplace,
        "available_from": available_from,
        "min_tenancy": min_tenancy,
        "preferences": preferences,
        "photos": photos,
        "description": description,
        "let_agreed": let_agreed,
        "transport_nearby": transport_nearby,
    }


def extract_propertybyid(html: str) -> dict[int, int]:
    """Pull `var PROPERTYBYID = {id : idx, ...};`.

    Despite the name, this is an id -> array-index lookup table, not a metadata dict.
    Indices reference positions in the parallel PROPERTYLISTLATITUDES / LONGITUDES arrays.
    """
    m = re.search(r'var\s+PROPERTYBYID\s*=\s*\{([^}]*)\}', html)
    if not m:
        return {}
    raw = m.group(1)
    out: dict[int, int] = {}
    for pair in raw.split(","):
        if ":" not in pair:
            continue
        k, v = pair.split(":", 1)
        k, v = k.strip(), v.strip()
        if k.lstrip("-").isdigit() and v.lstrip("-").isdigit():
            out[int(k)] = int(v)
    return out
