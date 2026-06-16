"""
Database models and connection management for HomeHunt
"""

import json
import logging
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Optional

from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlmodel import Field, Session, SQLModel, create_engine, func, select, update

from .models import ExtractionMethod, LetType, Portal, PropertyType

if TYPE_CHECKING:
    from .models import PropertyListing


class Listing(SQLModel, table=True):
    """
    Main property listing table
    Stores all scraped property data with full history tracking
    """

    # Primary identification
    uid: str = Field(
        primary_key=True, description="Unique identifier: portal:property_id"
    )
    portal: Portal = Field(index=True, description="Property portal")
    property_id: str = Field(index=True, description="Portal-specific property ID")
    url: str = Field(description="Direct property URL")

    # Location data
    address: str | None = Field(None, description="Property address")
    postcode: str | None = Field(None, index=True, description="Full postcode")
    area: str | None = Field(None, index=True, description="Area/district")
    latitude: float | None = Field(None, description="Property latitude coordinate")
    longitude: float | None = Field(
        None, description="Property longitude coordinate"
    )

    # Property details
    price: str | None = Field(None, description="Raw price text")
    price_numeric: int | None = Field(
        None, index=True, description="Monthly rent in pence"
    )
    bedrooms: int | None = Field(None, index=True, description="Number of bedrooms")
    bathrooms: int | None = Field(None, description="Number of bathrooms")
    size_sqft: int | None = Field(None, description="Internal floor area in square feet, parsed from displaySize")
    property_type: PropertyType | None = Field(
        None, index=True, description="Property type"
    )

    # Additional details
    furnished: str | None = Field(None, description="Furnished status")
    features: str | None = Field(None, description="JSON array of features")

    # Property amenities and features
    garden: bool | None = Field(None, description="Has garden/outdoor space")
    balcony: bool | None = Field(None, description="Has balcony/terrace")

    # Feature-extracted rental details (populated by feature_extractor)
    bills_included: bool | None = Field(None, description="Utility bills included in rent")
    council_tax_band: str | None = Field(None, description="Council tax band A-H")
    let_available_date: str | None = Field(None, description="Available-from date string")
    let_available_iso: str | None = Field(None, description="Normalised available-from date as YYYY-MM-DD")

    # Agent contact (kept for outbound enquiry; agent_name removed by user request)
    agent_phone: str | None = Field(None, description="Agent contact number")

    # Extraction metadata
    extraction_method: ExtractionMethod = Field(
        description="Method used for extraction"
    )
    title: str | None = Field(None, description="Page title")
    images: str | None = Field(None, description="JSON array of image URLs")

    # Tracking and timestamps
    first_seen: datetime = Field(
        default_factory=datetime.utcnow, description="First discovery"
    )
    last_scraped: datetime = Field(
        default_factory=datetime.utcnow,
        index=True,
        description="Last successful scrape",
    )
    scrape_count: int = Field(default=1, description="Number of times scraped")
    is_active: bool = Field(
        default=True, index=True, description="Property is still available"
    )
    status: str = Field(default="active", index=True, description="Property status")

    # Commute analysis
    tube_distance: int | None = Field(None, description="Walking time in minutes from postcode to nearest tube/overground/Elizabeth-line station")
    cycle_canary_wharf: int | None = Field(None, description="Cycle time in minutes to Canary Wharf via TfL Journey Planner")

    # Enrichment fields
    carpet_detected: bool | None = Field(None, description="Carpet detected in photos")
    carpet_confidence: float | None = Field(None, description="Carpet detection confidence 0-1")
    carpet_in_bedroom: bool | None = Field(None, description="Carpet present in bedroom photos")
    carpet_other_areas: bool | None = Field(None, description="Carpet present in non-bedroom photos")
    bedrooms_with_carpet: int | None = Field(None, description="Count of unique bedrooms with carpet (0/1/2). Set by detect_carpet_two_step from 2026-05-10")
    has_living_area_carpet: bool | None = Field(None, description="True if 2+ living-area photos AND majority show carpet. From detect_carpet_two_step 2026-05-10")
    photo_count: int | None = Field(None, description="Count of photos in the listing. From scraped images JSON.")
    floor_level_int: int | None = Field(None, description="Floor level as a normalised integer (Ground=0). Parsed from EPC floor_level string.")
    size_sqft_credibility: str | None = Field(None, description="trusted | conflict | missing — agreement between scraped and floorplan-derived sqft.")
    is_let_agreed: bool | None = Field(None, description="True if listing title contains let-agreed / under-offer markers.")
    current_status: str | None = Field("new", description="Lifecycle state: new | contact_queued | contacted | shortlisted | triage_reject | not_contacted | final_reject")
    current_status_reason: str | None = Field(None, description="Short tag explaining the most recent transition, e.g. 'ugly_carpet'.")
    last_transition_at: datetime | None = Field(None, description="Timestamp of the most recent lifecycle transition.")
    epc_rating: str | None = Field(None, description="EPC energy rating A-G")
    tfl_zone: int | None = Field(None, description="TfL zone of nearest station")
    nearest_station: str | None = Field(None, description="Nearest tube/overground station")
    commute_canary_wharf: int | None = Field(None, description="Public transport minutes to Canary Wharf")
    commute_whitechapel: int | None = Field(None, description="Public transport minutes to Whitechapel")
    commute_pass_40min: bool | None = Field(None, description="Both Canary Wharf and Whitechapel under 40 mins")
    score: int | None = Field(None, index=True, description="Composite 0-100 score")
    score_breakdown: str | None = Field(None, description="JSON of per-criterion scores for calibration")

    # Region resolved at enrichment time via homehunt/region.py.
    # Nearest verified neighbourhood centroid name from tiers.yaml.
    region: str | None = Field(None, index=True, description="Nearest neighbourhood centroid name")

    # Wave 2.7 Agent L: cross-portal cluster identity.
    # Foreign key to listing_cluster.cluster_id, populated by homehunt.dedup.
    # Null until cluster_listings runs. The actual table is created via the
    # alembic migration in alembic/versions/1777920000_wave27_listing_cluster.py.
    cluster_id: int | None = Field(default=None, index=True, description="Cross-portal cluster id")

    # EPC multi-field enrichment (per spec section 8a)
    total_floor_area_sqm: float | None = Field(None, description="Total floor area in square metres from EPC")
    current_energy_efficiency: int | None = Field(None, description="SAP score 0-100 (current)")
    potential_energy_efficiency: int | None = Field(None, description="SAP score 0-100 (after upgrades)")
    number_habitable_rooms: int | None = Field(None, description="Habitable rooms per EPC")
    number_heated_rooms: int | None = Field(None, description="Heated rooms per EPC")
    built_form: str | None = Field(None, description="Built form e.g. Mid-Terrace, Detached")
    property_type_epc: str | None = Field(None, description="Property type per EPC record")
    construction_age_band: str | None = Field(None, description="Construction age band e.g. 1996-2002")
    tenure_epc: str | None = Field(None, description="Tenure per EPC e.g. rental-private")
    windows_description: str | None = Field(None, description="Glazing description from EPC")
    windows_energy_eff: str | None = Field(None, description="Glazing energy efficiency band")
    walls_description: str | None = Field(None, description="Wall construction summary from EPC")
    walls_energy_eff: str | None = Field(None, description="Wall energy efficiency band")
    roof_description: str | None = Field(None, description="Roof construction summary from EPC")
    floor_level: str | None = Field(None, description="Floor level within the building")
    mains_gas_flag: str | None = Field(None, description="Mains gas hookup Y/N")
    heating_cost_current: int | None = Field(None, description="Annual heating cost in GBP")
    epc_lodgement_date: str | None = Field(None, description="Date EPC certificate was lodged")
    epc_inspection_date: str | None = Field(None, description="Date EPC inspection was carried out")

    # Provenance for size_sqft (Wave 1.6)
    size_sqft_source: str | None = Field(
        None,
        description="Source that provided size_sqft: structured|nlp|floorplan_vision|epc_direct|epc_interpolated",
    )
    size_sqft_confidence: float | None = Field(
        None, description="Confidence in size_sqft value 0.0-1.0"
    )
    # Floorplan vision output (Wave 1.6)
    floorplan_data: str | None = Field(
        None, description="JSON dict from floorplan vision extractor (qwen2.5vl)"
    )

    # Wave 1.7: EPC certificate image and provenance for epc_rating
    epc_graph_url: str | None = Field(
        None,
        description="First propertyData.epcGraphs URL from Rightmove PAGE_MODEL",
    )
    epc_rating_source: str | None = Field(
        None,
        description="Source that produced epc_rating: epc_api|vision_ocr",
    )
    epc_rating_confidence: str | None = Field(
        None,
        description="Confidence label for epc_rating: sap_derived|epc_api_match",
    )

    # Metadata storage
    property_metadata: str | None = Field(None, description="JSON metadata")

    @classmethod
    def from_property_listing(cls, listing: "PropertyListing") -> "Listing":
        """Create database record from PropertyListing model"""
        return cls(
            uid=listing.uid,
            portal=listing.portal,
            property_id=listing.property_id,
            url=listing.url,
            address=listing.address,
            postcode=listing.postcode,
            area=listing.area,
            latitude=listing.latitude,
            longitude=listing.longitude,
            price=listing.price,
            price_numeric=listing.price_numeric,
            bedrooms=listing.bedrooms,
            bathrooms=listing.bathrooms,
            property_type=listing.property_type,
            furnished=listing.furnished,
            features=(
                json.dumps(listing.features) if listing.features is not None else None
            ),
            garden=listing.garden,
            balcony=listing.balcony,
            bills_included=listing.bills_included,
            council_tax_band=listing.council_tax_band,
            let_available_date=listing.let_available_date,
            let_available_iso=listing.let_available_iso,
            agent_phone=listing.agent_phone,
            extraction_method=listing.extraction_method,
            title=listing.title,
            images=json.dumps(listing.images) if listing.images is not None else None,
            first_seen=listing.first_seen,
            last_scraped=listing.last_scraped,
            scrape_count=listing.scrape_count,
            is_active=listing.is_active,
            tube_distance=listing.tube_distance,
            cycle_canary_wharf=listing.cycle_canary_wharf,
            carpet_detected=listing.carpet_detected,
            carpet_confidence=listing.carpet_confidence if listing.carpet_confidence else None,
            carpet_in_bedroom=listing.carpet_in_bedroom,
            carpet_other_areas=listing.carpet_other_areas,
            epc_rating=listing.epc_rating,
            tfl_zone=listing.tfl_zone,
            nearest_station=listing.nearest_station,
            commute_canary_wharf=listing.commute_canary_wharf,
            commute_whitechapel=listing.commute_whitechapel,
            commute_pass_40min=listing.commute_pass_40min,
            score=listing.score,
            property_metadata=json.dumps(listing.to_dict()),
        )

    def to_property_listing(self) -> "PropertyListing":
        """Convert database record to PropertyListing model"""
        from .models import PropertyListing

        # Parse JSON fields
        features = json.loads(self.features) if self.features else []
        images = json.loads(self.images) if self.images else []

        return PropertyListing(
            portal=self.portal,
            property_id=self.property_id,
            url=self.url,
            uid=self.uid,
            address=self.address,
            postcode=self.postcode,
            area=self.area,
            latitude=self.latitude,
            longitude=self.longitude,
            price=self.price,
            price_numeric=self.price_numeric,
            bedrooms=self.bedrooms,
            bathrooms=self.bathrooms,
            property_type=self.property_type,
            furnished=self.furnished,
            features=features,
            garden=self.garden,
            balcony=self.balcony,
            bills_included=self.bills_included,
            council_tax_band=self.council_tax_band,
            let_available_date=self.let_available_date,
            let_available_iso=self.let_available_iso,
            agent_phone=self.agent_phone,
            extraction_method=self.extraction_method,
            title=self.title,
            images=images,
            first_seen=self.first_seen,
            last_scraped=self.last_scraped,
            scrape_count=self.scrape_count,
            is_active=self.is_active,
            tube_distance=self.tube_distance,
            cycle_canary_wharf=self.cycle_canary_wharf,
            carpet_detected=self.carpet_detected,
            carpet_confidence=self.carpet_confidence or 0.0,
            carpet_in_bedroom=self.carpet_in_bedroom,
            carpet_other_areas=self.carpet_other_areas,
            epc_rating=self.epc_rating,
            tfl_zone=self.tfl_zone,
            nearest_station=self.nearest_station,
            commute_canary_wharf=self.commute_canary_wharf,
            commute_whitechapel=self.commute_whitechapel,
            commute_pass_40min=self.commute_pass_40min,
            score=self.score,
        )


class PriceHistory(SQLModel, table=True):
    """
    Price change tracking for properties
    """

    id: int | None = Field(primary_key=True, default=None)
    property_uid: str = Field(
        foreign_key="listing.uid", index=True, description="Reference to property"
    )
    price: str = Field(description="Price at time of recording")
    price_numeric: int | None = Field(None, description="Numeric price in pence")
    recorded_at: datetime = Field(
        default_factory=datetime.utcnow,
        index=True,
        description="When price was recorded",
    )

    # Price change analysis
    price_change: int | None = Field(
        None, description="Change from previous price in pence"
    )
    price_change_percent: float | None = Field(
        None, description="Percentage change from previous price"
    )


class SearchHistory(SQLModel, table=True):
    """
    Track search configurations and results
    """

    id: int | None = Field(primary_key=True, default=None)
    search_config: str = Field(description="JSON search configuration")
    executed_at: datetime = Field(
        default_factory=datetime.utcnow,
        index=True,
        description="When search was executed",
    )

    # Results summary
    total_found: int = Field(description="Total properties found")
    new_properties: int = Field(description="New properties discovered")
    updated_properties: int = Field(description="Existing properties updated")

    # Performance metrics
    execution_time: float | None = Field(
        None, description="Search execution time in seconds"
    )
    api_calls: int | None = Field(None, description="Number of API calls made")
    success_rate: float | None = Field(
        None, description="Success rate of scraping attempts"
    )


class Database:
    """
    Database connection and operations manager
    """

    def __init__(self, database_url: str = ""):
        import os
        if not database_url:
            db_path = os.environ.get("HOMEHUNT_DB", "homehunt.db")
            database_url = f"sqlite:///{db_path}"
        self.database_url = database_url
        self.engine = create_engine(database_url, echo=False)
        self.async_engine = create_async_engine(
            database_url.replace("sqlite:///", "sqlite+aiosqlite:///"), echo=False
        )
        self.async_session = sessionmaker(
            self.async_engine, class_=AsyncSession, expire_on_commit=False
        )
        self.logger = logging.getLogger(__name__)

    def create_tables(self):
        """Create all database tables"""
        SQLModel.metadata.create_all(self.engine)
        self.logger.info("Database tables created")

    async def create_tables_async(self):
        """Create all database tables asynchronously"""
        async with self.async_engine.begin() as conn:
            await conn.run_sync(lambda c: SQLModel.metadata.create_all(c, checkfirst=True))
        self.logger.info("Database tables created asynchronously")

    def get_session(self) -> Session:
        """Get synchronous database session"""
        return Session(self.engine)

    async def get_async_session(self) -> AsyncSession:
        """Get asynchronous database session"""
        return self.async_session()

    async def save_property(self, listing: "PropertyListing") -> bool:
        """
        Save or update a property listing

        Args:
            listing: PropertyListing to save

        Returns:
            True if saved successfully, False otherwise
        """
        try:
            async with self.async_session() as session:
                # Check if property already exists
                result = await session.execute(
                    select(Listing).where(Listing.uid == listing.uid)
                )
                existing = result.scalar_one_or_none()

                if existing:
                    # Update existing property
                    existing.last_scraped = datetime.utcnow()
                    existing.scrape_count += 1

                    # Update fields that might have changed
                    existing.price = listing.price
                    existing.price_numeric = listing.price_numeric
                    existing.description = listing.description
                    existing.agent_phone = listing.agent_phone
                    existing.garden = listing.garden
                    existing.balcony = listing.balcony
                    existing.latitude = listing.latitude
                    existing.longitude = listing.longitude
                    existing.is_active = listing.is_active
                    existing.status = "active"

                    # Track price changes
                    if (
                        existing.price_numeric
                        and listing.price_numeric
                        and existing.price_numeric != listing.price_numeric
                    ):
                        price_change = PriceHistory(
                            property_uid=listing.uid,
                            price=listing.price,
                            price_numeric=listing.price_numeric,
                            price_change=listing.price_numeric - existing.price_numeric,
                            price_change_percent=(
                                (listing.price_numeric - existing.price_numeric)
                                / existing.price_numeric
                            )
                            * 100,
                        )
                        session.add(price_change)

                    self.logger.info(f"Updated property {listing.uid}")
                else:
                    # Create new property
                    db_listing = Listing.from_property_listing(listing)
                    session.add(db_listing)
                    self.logger.info(f"Created new property {listing.uid}")

                await session.commit()
                return True

        except Exception as e:
            self.logger.error(f"Error saving property {listing.uid}: {e}")
            return False

    async def get_property(self, uid: str) -> Optional["PropertyListing"]:
        """Get a property by UID"""
        try:
            async with self.async_session() as session:
                result = await session.execute(
                    select(Listing).where(Listing.uid == uid)
                )
                listing = result.scalar_one_or_none()

                if listing:
                    return listing.to_property_listing()
                return None

        except Exception as e:
            self.logger.error(f"Error getting property {uid}: {e}")
            return None

    async def search_properties(
        self,
        portal: Portal | None = None,
        min_price: int | None = None,
        max_price: int | None = None,
        bedrooms: int | None = None,
        property_type: PropertyType | None = None,
        postcode_area: str | None = None,
        max_commute: int | None = None,
        limit: int = 100,
    ) -> list["PropertyListing"]:
        """
        Search properties with filters

        Args:
            portal: Filter by portal
            min_price: Minimum price in pence
            max_price: Maximum price in pence
            bedrooms: Exact number of bedrooms
            property_type: Property type filter
            postcode_area: Postcode area filter
            max_commute: Maximum commute time in minutes
            limit: Maximum results to return

        Returns:
            List of PropertyListing objects
        """
        try:
            async with self.async_session() as session:
                query = select(Listing).where(Listing.is_active)

                if portal:
                    query = query.where(Listing.portal == portal)

                if min_price:
                    query = query.where(Listing.price_numeric >= min_price)

                if max_price:
                    query = query.where(Listing.price_numeric <= max_price)

                if bedrooms:
                    query = query.where(Listing.bedrooms == bedrooms)

                if property_type:
                    query = query.where(Listing.property_type == property_type)

                if postcode_area:
                    query = query.where(Listing.postcode.like(f"{postcode_area}%"))

                if max_commute:
                    query = query.where(Listing.commute_canary_wharf <= max_commute)

                query = query.limit(limit).order_by(Listing.last_scraped.desc())

                result = await session.execute(query)
                listings = result.scalars().all()

                return [listing.to_property_listing() for listing in listings]

        except Exception as e:
            self.logger.error(f"Error searching properties: {e}")
            return []

    async def get_statistics(self) -> dict[str, Any]:
        """Get database statistics"""
        try:
            async with self.async_session() as session:
                # Total properties by portal
                portal_stats = await session.execute(
                    select(
                        Listing.portal,
                        func.count(Listing.uid).label("total"),
                        func.count(Listing.price_numeric).label("with_price"),
                        func.avg(Listing.price_numeric).label("avg_price"),
                    ).group_by(Listing.portal)
                )

                # Recent activity
                recent_activity = await session.execute(
                    select(func.count(Listing.uid)).where(
                        Listing.last_scraped >= datetime.utcnow() - timedelta(days=1)
                    )
                )

                # Price ranges
                price_stats = await session.execute(
                    select(
                        func.min(Listing.price_numeric).label("min_price"),
                        func.max(Listing.price_numeric).label("max_price"),
                        func.avg(Listing.price_numeric).label("avg_price"),
                    ).where(Listing.price_numeric.is_not(None))
                )

                portal_results = portal_stats.fetchall()
                price_result = price_stats.first()

                return {
                    "portal_stats": [
                        {
                            "portal": (
                                row[0].value
                                if hasattr(row[0], "value")
                                else str(row[0])
                            ),
                            "total": row[1],
                            "with_price": row[2],
                            "avg_price": row[3],
                        }
                        for row in portal_results
                    ],
                    "recent_activity": recent_activity.scalar(),
                    "price_stats": (
                        {
                            "min_price": price_result[0] if price_result else None,
                            "max_price": price_result[1] if price_result else None,
                            "avg_price": price_result[2] if price_result else None,
                        }
                        if price_result
                        else {}
                    ),
                    "last_updated": datetime.utcnow().isoformat(),
                }

        except Exception as e:
            self.logger.error(f"Error getting statistics: {e}")
            return {}

    async def get_properties_since(
        self, since_time: datetime
    ) -> list["PropertyListing"]:
        """
        Get properties updated since a specific time

        Args:
            since_time: Get properties updated after this time

        Returns:
            List of PropertyListing objects
        """
        try:
            async with self.async_session() as session:
                result = await session.execute(
                    select(Listing)
                    .where(Listing.last_scraped >= since_time)
                    .order_by(Listing.last_scraped.desc())
                )

                listings = result.scalars().all()
                return [listing.to_property_listing() for listing in listings]

        except Exception as e:
            self.logger.error(f"Error getting properties since {since_time}: {e}")
            return []

    async def cleanup_old_data(self, days: int = 30):
        """Remove old inactive properties"""
        try:
            async with self.async_session() as session:
                cutoff_date = datetime.utcnow() - timedelta(days=days)

                # Mark old properties as inactive
                await session.execute(
                    update(Listing)
                    .where(Listing.last_scraped < cutoff_date)
                    .values(is_active=False, status="inactive")
                )

                await session.commit()
                self.logger.info(f"Cleaned up properties older than {days} days")

        except Exception as e:
            self.logger.error(f"Error cleaning up old data: {e}")

    async def close(self):
        """Close database connections"""
        await self.async_engine.dispose()
        self.engine.dispose()


# Global database instance
db = Database()


async def init_db():
    """Initialize database with tables"""
    await db.create_tables_async()


async def get_db() -> Database:
    """Get database instance"""
    return db
