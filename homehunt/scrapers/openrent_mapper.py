"""Map OpenRent parsed-detail dicts to PropertyListing.

The probe's extract_detail output dict has a slightly different shape than the
PropertyListing Pydantic schema. This mapper normalises field names and types
so the result drops cleanly into the existing pipeline.

Field correspondences:
  parsed['title']           -> PropertyListing.title and .address
  parsed['price_monthly']   -> PropertyListing.price ("£X pcm"); validator auto-fills
                                .price_numeric (in pence)
  parsed['bedrooms']        -> PropertyListing.bedrooms
  parsed['bathrooms']       -> PropertyListing.bathrooms (best-effort, may be None)
  parsed['postcode_area']   -> PropertyListing.area (OpenRent gives outcode only)
  parsed['furnishing']      -> PropertyListing.furnished
  parsed['garden']          -> PropertyListing.garden
  parsed['bills_included']  -> PropertyListing.bills_included
  parsed['available_from']  -> PropertyListing.let_available_date
  parsed['photos']          -> PropertyListing.images (list[str])
  parsed['let_agreed']      -> consumed by lifecycle layer; not on PropertyListing
"""

from datetime import datetime, timezone

from homehunt.core.models import ExtractionMethod, Portal, PropertyListing


def to_property_listing(
    *,
    parsed: dict,
    property_id: int,
    search_lat: float | None,
    search_lng: float | None,
) -> PropertyListing:
    """Build a PropertyListing from a parsed-detail dict + search-page coords."""
    pid = str(property_id)
    raw_price = parsed.get("price_monthly")
    price_text = f"£{int(raw_price):,} pcm" if raw_price else None
    photos = list(parsed.get("photos") or [])
    title = parsed.get("title")
    now = datetime.now(timezone.utc)

    return PropertyListing(
        portal=Portal.OPENRENT,
        property_id=pid,
        url=f"https://www.openrent.co.uk/{pid}",
        address=title,
        title=title,
        postcode=None,
        area=parsed.get("postcode_area"),
        latitude=search_lat,
        longitude=search_lng,
        price=price_text,
        bedrooms=parsed.get("bedrooms"),
        bathrooms=parsed.get("bathrooms"),
        furnished=parsed.get("furnishing"),
        garden=parsed.get("garden"),
        bills_included=parsed.get("bills_included"),
        let_available_date=parsed.get("available_from"),
        epc_rating=parsed.get("epc_rating"),
        images=photos,
        extraction_method=ExtractionMethod.DIRECT_HTTP,
        first_seen=now,
        last_scraped=now,
    )
