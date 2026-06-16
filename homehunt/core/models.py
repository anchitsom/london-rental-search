"""
Core data models for HomeHunt property scraping and analysis
"""

import re
from datetime import date as _date, datetime
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from homehunt.feature_extractor import normalise_let_available_date as _norm_lad


class Portal(str, Enum):
    """Supported property portals"""

    RIGHTMOVE = "rightmove"
    ZOOPLA = "zoopla"
    OPENRENT = "openrent"


class PropertyType(str, Enum):
    """Property types"""

    FLAT = "flat"
    APARTMENT = "apartment"
    HOUSE = "house"
    STUDIO = "studio"
    MAISONETTE = "maisonette"
    BUNGALOW = "bungalow"
    UNKNOWN = "unknown"


class LetType(str, Enum):
    """Rental let types"""

    LONG_TERM = "long_term"
    SHORT_TERM = "short_term"
    STUDENT = "student"
    PROFESSIONAL = "professional"
    UNKNOWN = "unknown"


class ExtractionMethod(str, Enum):
    """Method used to extract property data"""

    FIRECRAWL = "firecrawl"
    DIRECT_HTTP = "direct_http"
    HYBRID = "hybrid"


class PropertyListing(BaseModel):
    """
    Property listing data model optimized for hybrid scraping approach
    Based on comprehensive testing of Rightmove and Zoopla extraction
    """

    model_config = ConfigDict(
        str_strip_whitespace=True, validate_assignment=True, extra="forbid"
    )

    # Core identification
    portal: Portal = Field(..., description="Property portal (rightmove/zoopla)")
    property_id: str = Field(..., description="Unique property ID from URL")
    url: str = Field(..., description="Direct property URL")
    uid: Optional[str] = Field(
        None, description="Unique identifier: portal:property_id"
    )

    # Location data (extracted from validation tests)
    address: Optional[str] = Field(None, description="Property address from title/h1")
    postcode: Optional[str] = Field(None, description="Full postcode if available")
    area: Optional[str] = Field(None, description="Area/district")
    latitude: Optional[float] = Field(None, description="Property latitude coordinate")
    longitude: Optional[float] = Field(None, description="Property longitude coordinate")

    # Property details (validated extractable fields)
    price: Optional[str] = Field(None, description="Raw price text (£2,385 pcm)")
    price_numeric: Optional[int] = Field(
        None, description="Parsed monthly rent in pence"
    )
    bedrooms: Optional[int] = Field(None, description="Number of bedrooms from title")
    bathrooms: Optional[int] = Field(None, description="Number of bathrooms")
    property_type: Optional[PropertyType] = Field(
        None, description="Property type from title"
    )

    # Additional property details
    furnished: Optional[str] = Field(None, description="Furnished status")
    features: List[str] = Field(default_factory=list, description="Property features")
    size_sqft: Optional[int] = Field(None, description="Internal floor area in square feet, parsed from displaySize")

    # Property amenities and features
    garden: Optional[bool] = Field(None, description="Has garden/outdoor space")
    balcony: Optional[bool] = Field(None, description="Has balcony/terrace")

    # Feature-extracted rental details (populated by feature_extractor)
    bills_included: Optional[bool] = Field(None, description="Utility bills included in rent")
    council_tax_band: Optional[str] = Field(None, description="Council tax band A-H")
    let_available_date: Optional[str] = Field(None, description="Available-from date string")
    let_available_iso: Optional[str] = Field(None, description="Normalised available-from date as YYYY-MM-DD")

    # Rental details
    new_build: Optional[bool] = Field(None, description="Is a new build property")
    is_south_london: Optional[bool] = Field(None, description="Property is in South London")

    # Agent contact (agent_name removed by user request)
    agent_phone: Optional[str] = Field(None, description="Agent contact number")

    # Extraction method tracking
    extraction_method: ExtractionMethod = Field(
        ..., description="Method used for extraction"
    )

    # Metadata
    title: Optional[str] = Field(None, description="Page title (rich source of data)")
    images: List[str] = Field(default_factory=list, description="Property image URLs")

    # Tracking fields
    first_seen: datetime = Field(
        default_factory=datetime.utcnow, description="First discovery timestamp"
    )
    last_scraped: datetime = Field(
        default_factory=datetime.utcnow, description="Last successful scrape"
    )
    scrape_count: int = Field(default=1, description="Number of times scraped")
    is_active: bool = Field(default=True, description="Property is still available")

    # Commute analysis (filled by TravelTime integration)
    tube_distance: Optional[int] = Field(None, description="Walking time in minutes from postcode to nearest tube/overground/Elizabeth-line station")
    cycle_canary_wharf: Optional[int] = Field(None, description="Cycle time in minutes to Canary Wharf via TfL Journey Planner")

    # Enrichment fields (filled post-scrape)
    carpet_detected: Optional[bool] = Field(None, description="Carpet detected in photos")
    carpet_confidence: float = Field(default=0.0, description="Carpet detection confidence 0-1")
    carpet_in_bedroom: Optional[bool] = Field(None, description="Carpet present in bedroom photos")
    carpet_other_areas: Optional[bool] = Field(None, description="Carpet present in non-bedroom photos")
    epc_rating: Optional[str] = Field(None, description="EPC energy rating A-G")
    tfl_zone: Optional[int] = Field(None, description="TfL zone of nearest station")
    nearest_station: Optional[str] = Field(None, description="Nearest tube/overground station name")
    commute_canary_wharf: Optional[int] = Field(None, description="Public transport minutes to Canary Wharf")
    commute_whitechapel: Optional[int] = Field(None, description="Public transport minutes to Whitechapel")
    commute_pass_40min: Optional[bool] = Field(None, description="Both Canary Wharf and Whitechapel under 40 mins")
    score: Optional[int] = Field(None, description="Composite 0-100 score")
    score_breakdown: Optional[Dict[str, int]] = Field(None, description="Per-criterion score breakdown")

    @model_validator(mode="after")
    def generate_uid(self):
        """Generate unique identifier from portal and property_id"""
        if not self.uid and self.portal and self.property_id:
            portal_str = (
                self.portal.value if hasattr(self.portal, "value") else str(self.portal)
            )
            self.uid = f"{portal_str}:{self.property_id}"
        return self

    @model_validator(mode="after")
    def normalise_let_available_iso(self):
        """Populate normalized available date from the raw extracted string."""
        if self.let_available_date is None:
            object.__setattr__(self, "let_available_iso", None)
        elif self.let_available_iso is None:
            object.__setattr__(
                self,
                "let_available_iso",
                _norm_lad(self.let_available_date, _date.today()),
            )
        return self

    @model_validator(mode="after")
    def parse_price_numeric(self):
        """Parse numeric price from price string"""
        if self.price_numeric is None and self.price:
            # Extract numeric value from "£2,385 pcm" format
            match = re.search(r"£([\d,]+)", self.price)
            if match:
                numeric_str = match.group(1).replace(",", "")
                try:
                    self.price_numeric = int(numeric_str) * 100  # Convert to pence
                except ValueError:
                    pass
        return self

    @field_validator("property_type", mode="before")
    @classmethod
    def normalize_property_type(cls, v):
        """Normalize property type from extracted text"""
        if v is None:
            return None

        v_lower = str(v).lower()

        # Map common variations
        type_mapping = {
            "flat": PropertyType.FLAT,
            "apartment": PropertyType.APARTMENT,
            "house": PropertyType.HOUSE,
            "studio": PropertyType.STUDIO,
            "maisonette": PropertyType.MAISONETTE,
            "bungalow": PropertyType.BUNGALOW,
            "semi-detached": PropertyType.HOUSE,
            "detached": PropertyType.HOUSE,
            "terraced": PropertyType.HOUSE,
            "end terrace": PropertyType.HOUSE,
        }

        for key, property_type in type_mapping.items():
            if key in v_lower:
                return property_type

        return PropertyType.UNKNOWN

    @field_validator("postcode", mode="before")
    @classmethod
    def validate_postcode(cls, v):
        """Validate UK postcode format"""
        if v is None or v == "":
            return None if v is None else ""

        # UK postcode regex pattern
        postcode_pattern = r"^[A-Z]{1,2}[0-9][A-Z0-9]? ?[0-9][A-Z]{2}$"
        if re.match(postcode_pattern, v.upper()):
            return v.upper()
        return v  # Return as-is if not valid postcode format
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for database storage"""
        return self.model_dump(exclude_none=True, mode='json')

    @classmethod
    def from_extraction_result(
        cls,
        portal: str,
        property_id: str,
        url: str,
        extraction_result: Dict[str, Any],
        extraction_method: str,
    ) -> "PropertyListing":
        """
        Create PropertyListing from extraction result

        Args:
            portal: Property portal name
            property_id: Unique property ID
            url: Property URL
            extraction_result: Raw extraction data
            extraction_method: Method used for extraction

        Returns:
            PropertyListing instance
        """
        from homehunt.feature_extractor import extract_all as _extract_all
        from homehunt.scrapers._geo import (
            extract_postcode, clean_address,
            extract_coordinates_from_maps_embed, extract_coordinates_from_text,
        )

        description = extraction_result.get('description', '') or ''
        features = extraction_result.get('features', []) or []
        address = extraction_result.get('address', '') or ''
        title = extraction_result.get('title', '') or ''

        raw_content = extraction_result.get('raw_content', '') or ''
        images = extraction_result.get('images', []) or []

        # Run structured-feature extraction
        nlp_features = _extract_all(description, features, title)

        # Postcode and address cleaning
        postcode = extract_postcode(f"{address} {title}") or extraction_result.get('postcode')
        if address:
            address = clean_address(address)

        # Coordinates: prefer explicit, then map-embed, then text heuristics
        latitude = extraction_result.get('latitude')
        longitude = extraction_result.get('longitude')
        if not (latitude and longitude):
            coords = extract_coordinates_from_maps_embed(raw_content)
            if coords:
                latitude, longitude = coords
            else:
                all_text = f"{raw_content} {' '.join(images) if images else ''}"
                coords = extract_coordinates_from_text(all_text)
                if coords:
                    latitude, longitude = coords

        enhanced_result = {
            **extraction_result,
            'address': address,
            'postcode': postcode,
            'latitude': latitude,
            'longitude': longitude,
            # Boolean amenity fields
            'garden': nlp_features.get('garden'),
            'balcony': nlp_features.get('balcony'),
            # Furnished enum
            'furnished': nlp_features.get('furnished') if nlp_features.get('furnished') != 'unknown' else extraction_result.get('furnished'),
            # New feature-extracted fields
            'bills_included': nlp_features.get('bills_included'),
            'council_tax_band': nlp_features.get('council_tax_band'),
            'let_available_date': nlp_features.get('let_available_date'),
            'let_available_iso': _norm_lad(nlp_features.get('let_available_date'), _date.today()),
        }

        # Drop auxiliary keys that scrapers return for downstream pipeline use
        # (raw_content, photo_urls, price_pcm, floorplan_urls, size_sqm, etc.)
        # but that PropertyListing does not declare. Without this filter the
        # extra="forbid" config raises ValidationError on every Zoopla call.
        valid_keys = set(cls.model_fields.keys())
        cleaned = {k: v for k, v in enhanced_result.items() if k in valid_keys}

        return cls(
            portal=Portal(portal),
            property_id=property_id,
            url=url,
            extraction_method=ExtractionMethod(extraction_method),
            **cleaned,
        )


class SearchConfig(BaseModel):
    """Configuration for property search"""

    model_config = ConfigDict(str_strip_whitespace=True, validate_assignment=True)

    # Location parameters
    location: str = Field(..., description="Search location (area, postcode, station)")
    radius: Optional[float] = Field(None, description="Search radius in miles")

    # Property criteria
    min_price: Optional[int] = Field(None, description="Minimum rent in pounds")
    max_price: Optional[int] = Field(None, description="Maximum rent in pounds")
    min_bedrooms: Optional[int] = Field(None, description="Minimum bedrooms")
    max_bedrooms: Optional[int] = Field(None, description="Maximum bedrooms")
    property_types: List[str] = Field(
        default_factory=list, description="Property types to include"
    )

    # Additional filters
    furnished: Optional[str] = Field(None, description="Furnished status filter")
    available_from: Optional[str] = Field(None, description="Available from date")

    # Search parameters
    portals: List[Portal] = Field(
        default_factory=lambda: [Portal.RIGHTMOVE, Portal.ZOOPLA]
    )
    max_pages: int = Field(default=20, description="Maximum pages to scrape")

    # Commute analysis
    commute_destinations: List[str] = Field(
        default_factory=list, description="Commute destinations"
    )
    max_commute_time: Optional[int] = Field(
        None, description="Maximum commute time in minutes"
    )

    def build_rightmove_url(self) -> str:
        """Build Rightmove search URL"""
        params = {
            "locationIdentifier": self.location,
            "radius": self.radius or 1.0,
            "propertyTypes": (
                ",".join(self.property_types) if self.property_types else "flat,house"
            ),
            "includeLetAgreed": "false",
            "mustHave": "",
            "dontShow": "",
            "furnishTypes": self.furnished or "",
            "keywords": "",
        }

        if self.min_price:
            params["minPrice"] = self.min_price
        if self.max_price:
            params["maxPrice"] = self.max_price
        if self.min_bedrooms is not None:
            params["minBedrooms"] = self.min_bedrooms
        if self.max_bedrooms is not None:
            params["maxBedrooms"] = self.max_bedrooms

        # Build query string
        query_params = []
        for key, value in params.items():
            if value is not None and value != "" and value != 0:
                query_params.append(f"{key}={value}")
            elif key in ["minBedrooms", "maxBedrooms"] and value == 0:
                # Include 0 bedrooms for bedroom filters
                query_params.append(f"{key}={value}")

        base_url = "https://www.rightmove.co.uk/property-to-rent/find.html"
        return f"{base_url}?{'&'.join(query_params)}"

    def build_zoopla_url(self) -> str:
        """Build Zoopla search URL"""
        params = {
            "q": self.location,
            "radius": self.radius or 1.0,
            "price_frequency": "per_month",
            "property_type": (
                ",".join(self.property_types) if self.property_types else "flats,houses"
            ),
            "furnished_state": self.furnished or "",
            "search_source": "to-rent",
        }

        if self.min_price:
            params["price_min"] = self.min_price
        if self.max_price:
            params["price_max"] = self.max_price
        if self.min_bedrooms is not None:
            params["beds_min"] = self.min_bedrooms
        if self.max_bedrooms is not None:
            params["beds_max"] = self.max_bedrooms

        # Build query string
        query_params = []
        for key, value in params.items():
            if value is not None and value != "" and value != 0:
                query_params.append(f"{key}={value}")
            elif key in ["beds_min", "beds_max"] and value == 0:
                # Include 0 bedrooms for bedroom filters
                query_params.append(f"{key}={value}")

        base_url = "https://www.zoopla.co.uk/to-rent/property"
        return f"{base_url}?{'&'.join(query_params)}"


class ScrapingResult(BaseModel):
    """Result of a scraping operation"""

    model_config = ConfigDict(validate_assignment=True)

    url: str = Field(..., description="Scraped URL")
    success: bool = Field(..., description="Whether scraping was successful")
    portal: Portal = Field(..., description="Property portal")
    property_id: Optional[str] = Field(None, description="Extracted property ID")

    # Timing and performance
    response_time: Optional[float] = Field(None, description="Response time in seconds")
    content_length: Optional[int] = Field(None, description="Content length in bytes")

    # Result data
    data: Optional[Dict[str, Any]] = Field(None, description="Extracted property data")
    error: Optional[str] = Field(None, description="Error message if failed")

    # Metadata
    extraction_method: ExtractionMethod = Field(
        ..., description="Method used for extraction"
    )
    timestamp: datetime = Field(
        default_factory=datetime.utcnow, description="Scraping timestamp"
    )
