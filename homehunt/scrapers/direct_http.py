"""
Direct HTTP scraper for Rightmove properties
Based on validated testing showing 100% success rate for Rightmove individual property pages
"""

import json
import re
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, urlparse

from bs4 import BeautifulSoup

from homehunt.core.models import ExtractionMethod, Portal, ScrapingResult

from .base import BaseScraper, ScraperError


def _extract_page_model(html: str) -> Optional[Dict[str, Any]]:
    """
    Extract `window.PAGE_MODEL = {...}` or `window.__PAGE_MODEL = {...}` from
    raw HTML using balanced-brace JSON termination.

    Earlier non-greedy regex variants terminated at the first '}' inside the
    nested JSON, silently producing invalid snippets. This walker finds the
    matching closing brace by tracking depth, ignoring braces inside strings
    and respecting backslash escapes.

    Rightmove migrated some pages from `window.PAGE_MODEL` to
    `window.__PAGE_MODEL` (double underscore) with a dehydrated state
    structure (top-level keys are `data` and `encoding`, where `data` is a
    JSON-stringified array of items with integer index references). On those
    pages the parsed dict here looks like {"data": "[...]", "encoding": ...}
    and most callers must fall back to a different extraction path. This
    function still parses both shapes; downstream callers decide what to do.

    Returns the parsed dict, or None if the marker is absent or the JSON
    fails to parse.
    """
    m = re.search(r"window\.(?:__)?PAGE_MODEL\s*=\s*", html)
    if not m:
        return None
    start = m.end()
    if start >= len(html) or html[start] != "{":
        return None
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(html)):
        ch = html[i]
        if esc:
            esc = False
            continue
        if ch == "\\":
            esc = True
            continue
        if ch == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(html[start:i + 1])
                except (json.JSONDecodeError, ValueError):
                    return None
    return None


class DirectHTTPScraper(BaseScraper):
    """
    Direct HTTP scraper optimized for Rightmove properties
    Achieves 100% success rate through validated extraction patterns
    """
    
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        
        # Update headers for better success rate
        self.client.headers.update({
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-GB,en;q=0.5",
            "Accept-Encoding": "gzip, deflate, br",
            "Connection": "keep-alive",
            "Upgrade-Insecure-Requests": "1",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Cache-Control": "max-age=0",
        })
    
    def get_portal(self) -> Portal:
        return Portal.RIGHTMOVE
    
    def get_extraction_method(self) -> ExtractionMethod:
        return ExtractionMethod.DIRECT_HTTP
    
    async def scrape_search_page(self, url: str) -> List[str]:
        """
        Scrape search page to discover property URLs
        Note: Direct HTTP has limited success with Rightmove search pages due to anti-bot measures
        """
        try:
            response = await self.make_request(url)
            soup = BeautifulSoup(response.text, 'html.parser')
            
            property_urls = []
            
            # Look for property links in search results
            property_links = soup.find_all('a', href=re.compile(r'/properties/\d+'))
            
            for link in property_links:
                href = link.get('href')
                if href and href.startswith('/properties/'):
                    full_url = f"https://www.rightmove.co.uk{href}"
                    # Remove query parameters for clean URL
                    clean_url = full_url.split('?')[0]
                    if clean_url not in property_urls:
                        property_urls.append(clean_url)
            
            self.logger.info(f"Found {len(property_urls)} property URLs via direct HTTP")
            return property_urls
            
        except Exception as e:
            self.logger.error(f"Error scraping search page {url}: {e}")
            raise ScraperError(f"Direct HTTP search page scraping failed: {e}")
    
    async def scrape_property(self, url: str) -> ScrapingResult:
        """
        Scrape individual Rightmove property page
        
        Args:
            url: Rightmove property URL
            
        Returns:
            ScrapingResult with property data
        """
        property_id = self.extract_property_id(url)
        
        try:
            response = await self.make_request(url)
            soup = BeautifulSoup(response.text, 'html.parser')
            
            # Extract property data using validated patterns
            property_data = self._extract_rightmove_data(soup, url)
            
            return ScrapingResult(
                url=url,
                success=True,
                portal=self.get_portal(),
                property_id=property_id,
                data=property_data,
                response_time=response.elapsed.total_seconds() if hasattr(response, 'elapsed') else None,
                content_length=len(response.text),
                extraction_method=self.get_extraction_method(),
            )
            
        except Exception as e:
            self.logger.error(f"Error scraping property {url}: {e}")
            return ScrapingResult(
                url=url,
                success=False,
                portal=self.get_portal(),
                property_id=property_id,
                error=str(e),
                extraction_method=self.get_extraction_method(),
            )
    
    def _extract_rightmove_data(self, soup: BeautifulSoup, url: str) -> Dict[str, Any]:
        """Extract property data from Rightmove page using validated patterns"""
        data = {}
        
        try:
            # Extract title (primary source of structured data)
            title = self._extract_title(soup)
            if title:
                data["title"] = title
                
                # Extract structured data from title
                title_data = self._parse_title_data(title)
                data.update(title_data)
            
            # Extract price
            price = self._extract_price(soup)
            if price:
                data["price"] = price
            
            # Extract property details
            details = self._extract_property_details(soup)
            data.update(details)
            
            # Extract address
            address = self._extract_address(soup)
            if address:
                data["address"] = address
            
            # Extract postcode
            postcode = self._extract_postcode(soup, address)
            if postcode:
                data["postcode"] = postcode
            
            # Extract area
            area = self._extract_area(soup, address)
            if area:
                data["area"] = area
            
            # Extract description
            description = self._extract_description(soup)
            if description:
                data["description"] = description
            
            # Extract features
            features = self._extract_features(soup)
            if features:
                data["features"] = features
            
            # Extract agent info
            agent_info = self._extract_agent_info(soup)
            data.update(agent_info)
            
            # Extract images
            images = self._extract_images(soup)
            if images:
                data["images"] = images

            # Extract size in square feet from PAGE_MODEL.propertyData.displaySize
            size_sqft = self._extract_size_sqft(soup)
            if size_sqft is not None:
                data["size_sqft"] = size_sqft

            # Extract floorplan URLs from PAGE_MODEL.propertyData.floorplans
            floorplan_urls = self._extract_floorplan_urls(soup)
            if floorplan_urls:
                data["floorplan_urls"] = floorplan_urls

            # Extract first EPC certificate graph URL from
            # PAGE_MODEL.propertyData.epcGraphs (Wave 1.7).
            epc_graph_url = self._extract_epc_graphs(soup)
            if epc_graph_url:
                data["epc_graph_url"] = epc_graph_url

            # Include raw HTML for coordinate extraction
            data["raw_content"] = str(soup)

            # Run NLP-style feature extraction over description and feature bullets.
            # Important: never let the NLP fallback overwrite a structured value
            # already populated by an earlier extractor (e.g. size_sqft from
            # propertyData.sizings). Merge only non-None NLP values.
            from homehunt.feature_extractor import extract_all as _extract_all
            nlp = _extract_all(
                description=data.get("description", "") or "",
                features=data.get("features", []) or [],
                title=data.get("title", "") or "",
            )
            for key, value in nlp.items():
                if value is None:
                    continue
                if data.get(key) is not None:
                    continue
                data[key] = value

            return data

        except Exception as e:
            self.logger.error(f"Error extracting Rightmove data: {e}")
            return {}
    
    def _extract_title(self, soup: BeautifulSoup) -> Optional[str]:
        """Extract page title"""
        # Try main title
        title_tag = soup.find('title')
        if title_tag:
            return title_tag.get_text().strip()
        
        # Try h1 tag
        h1_tag = soup.find('h1')
        if h1_tag:
            return h1_tag.get_text().strip()
        
        return None
    
    def _parse_title_data(self, title: str) -> Dict[str, Any]:
        """Parse structured data from title"""
        data = {}
        
        # Extract bedrooms
        bed_match = re.search(r'(\d+)\s*bedroom', title, re.IGNORECASE)
        if bed_match:
            data["bedrooms"] = int(bed_match.group(1))
        
        # Extract property type
        type_patterns = [
            r'bedroom\s+(flat|apartment|house|studio|maisonette|bungalow)',
            r'(\d+)\s*bed\s+(flat|apartment|house|studio|maisonette|bungalow)',
            r'(flat|apartment|house|studio|maisonette|bungalow)',
        ]
        
        for pattern in type_patterns:
            type_match = re.search(pattern, title, re.IGNORECASE)
            if type_match:
                # Get the property type (last group)
                data["property_type"] = type_match.group(type_match.lastindex).lower()
                break
        
        # Extract location from title
        location_match = re.search(r'(?:for rent in|to rent in|in)\s+(.+?)(?:\s*\||$)', title, re.IGNORECASE)
        if location_match:
            location = location_match.group(1).strip()
            data["address"] = location
        
        return data
    
    def _extract_price(self, soup: BeautifulSoup) -> Optional[str]:
        """Extract price from various possible locations"""
        # Common price selectors
        price_selectors = [
            'span[data-testid="price"]',
            '.propertyHeaderPrice',
            '[class*="price"]',
            'span:contains("£")',
        ]
        
        for selector in price_selectors:
            if ':contains(' in selector:
                # Handle contains selector manually
                elements = soup.find_all('span')
                for elem in elements:
                    text = elem.get_text().strip()
                    if '£' in text and ('pcm' in text.lower() or 'per month' in text.lower()):
                        return text
            else:
                element = soup.select_one(selector)
                if element:
                    text = element.get_text().strip()
                    if '£' in text:
                        return text
        
        # Look for price in script tags (JSON-LD)
        script_tags = soup.find_all('script', type='application/ld+json')
        for script in script_tags:
            try:
                data = json.loads(script.string)
                if isinstance(data, dict) and 'offers' in data:
                    offers = data['offers']
                    if isinstance(offers, dict) and 'price' in offers:
                        return f"£{offers['price']} pcm"
            except:
                continue
        
        return None
    
    def _extract_property_details(self, soup: BeautifulSoup) -> Dict[str, Any]:
        """Extract property details like bedrooms, bathrooms, etc."""
        details = {}
        
        # Look for property details in various locations
        detail_selectors = [
            '[data-testid="property-details"]',
            '.property-details',
            '[class*="details"]',
            '.propertyKeyFeatures',
        ]
        
        for selector in detail_selectors:
            container = soup.select_one(selector)
            if container:
                text = container.get_text().lower()
                
                # Extract bedrooms
                bed_match = re.search(r'(\d+)\s*bedroom', text)
                if bed_match and 'bedrooms' not in details:
                    details["bedrooms"] = int(bed_match.group(1))
                
                # Extract bathrooms
                bath_match = re.search(r'(\d+)\s*bathroom', text)
                if bath_match:
                    details["bathrooms"] = int(bath_match.group(1))
                
                # Extract furnished status
                if 'furnished' in text and 'unfurnished' not in text:
                    details["furnished"] = "Furnished"
                elif 'unfurnished' in text:
                    details["furnished"] = "Unfurnished"
                elif 'part furnished' in text or 'partially furnished' in text:
                    details["furnished"] = "Part Furnished"
        
        return details
    
    def _extract_size_sqft(self, soup: BeautifulSoup) -> Optional[int]:
        """
        Extract internal floor area in square feet.

        Primary: PAGE_MODEL propertyData.sizings (modern Rightmove pages
        carry an array of size objects; the relevant one has unit "sqft" or "sqm").
        Fallback: scan visible text for "sq ft" or "sq m" patterns.
        """
        from homehunt.size_parser import parse_size_sqft

        raw_html = str(soup)

        # Primary: PAGE_MODEL JSON. Use balanced-brace extraction. Earlier
        # non-greedy regex variants terminated at the first '}' inside the
        # nested JSON and silently failed to parse, so propertyData.sizings
        # was never reached on listings where it was populated.
        data = _extract_page_model(raw_html)
        if data is not None:
            try:
                prop = data.get("propertyData", {}) or {}
                # Modern path: propertyData.sizings is a list of
                # {unit, minimumSize, maximumSize}.
                sizings = prop.get("sizings") or []
                for s in sizings:
                    unit = (s.get("unit") or "").lower()
                    max_size = s.get("maximumSize") or s.get("minimumSize")
                    if not max_size:
                        continue
                    if unit in ("sqft", "sq_ft", "sq ft"):
                        return int(round(float(max_size)))
                    if unit in ("sqm", "sq_m", "sq m"):
                        return int(round(float(max_size) * 10.764))
                # Older path: propertyData.text.displaySize is a free-text string.
                text_block = prop.get("text") or {}
                parsed = parse_size_sqft(text_block.get("displaySize"))
                if parsed is not None:
                    return parsed
                parsed = parse_size_sqft(prop.get("displaySize"))
                if parsed is not None:
                    return parsed
            except (AttributeError, TypeError, ValueError):
                pass

        # Fallback: scan visible page text. Cap at 200 chars near "sq ft" / "sq m".
        text = soup.get_text(separator=" ", strip=True)
        for token in ("sq ft", "sqft", "sq m", "sqm"):
            idx = text.lower().find(token)
            if idx != -1:
                window = text[max(0, idx - 40): idx + len(token) + 5]
                parsed = parse_size_sqft(window)
                if parsed is not None:
                    return parsed
        return None

    def _extract_epc_graphs(self, soup: BeautifulSoup) -> Optional[str]:
        """
        Extract the first EPC certificate image URL.

        Tries the legacy PAGE_MODEL.propertyData.epcGraphs path first. If
        the page uses Rightmove's newer dehydrated `window.__PAGE_MODEL`
        format where `propertyData` is an integer index reference, that path
        yields nothing and we fall back to a regex on the raw HTML matching
        any media.rightmove.co.uk EPC graph URL. This is robust to dehydration
        scheme changes because the URL pattern is stable.

        Returns the first non-empty URL string, or None if no candidate URL
        is found.
        """
        raw_html = str(soup)
        data = _extract_page_model(raw_html)
        if isinstance(data, dict):
            try:
                prop = data.get("propertyData") or {}
                if isinstance(prop, dict):
                    graphs = prop.get("epcGraphs") or []
                    if graphs:
                        first = graphs[0] or {}
                        url = first.get("url") or ""
                        if url:
                            return url
            except (AttributeError, TypeError):
                pass
        # Fallback: regex on raw HTML. Pattern matches the EPC graph
        # subdirectory under media.rightmove.co.uk.
        epc_match = re.search(
            r"https?://media[0-9]*\.rightmove\.co\.uk/[^\"'\\<>\s]*epc[^\"'\\<>\s]*\.(?:jpe?g|png|gif)",
            raw_html,
            re.IGNORECASE,
        )
        return epc_match.group(0) if epc_match else None

    def _extract_floorplan_urls(self, soup: BeautifulSoup) -> list[str]:
        """
        Extract floorplan image URLs.

        Tries the legacy PAGE_MODEL.propertyData.floorplans path first. If
        the page uses Rightmove's newer dehydrated `window.__PAGE_MODEL`
        format, that path yields nothing and we fall back to a regex on the
        raw HTML matching any media.rightmove.co.uk property-floorplan URL.
        Both candidate sets are then filtered to Rightmove-hosted only, so
        Foxtons URLs (HTML wrappers, not images) never reach the caller.
        """
        raw_html = str(soup)
        data = _extract_page_model(raw_html)
        urls: list[str] = []
        if isinstance(data, dict):
            try:
                prop = data.get("propertyData") or {}
                if isinstance(prop, dict):
                    floorplans = prop.get("floorplans") or []
                    for fp in floorplans:
                        url = fp.get("url") or fp.get("resizedImageUrl") or fp.get("src") or ""
                        if url:
                            urls.append(url)
            except (AttributeError, TypeError):
                pass
        # Fallback: regex over raw HTML. Pattern matches Rightmove's
        # property-floorplan subdirectory. We deduplicate after filtering.
        if not urls:
            for hit in re.findall(
                r"https?://media[0-9]*\.rightmove\.co\.uk/[^\"'\\<>\s]*property-floorplan[^\"'\\<>\s]*\.(?:jpe?g|png|gif)",
                raw_html,
                re.IGNORECASE,
            ):
                if hit not in urls:
                    urls.append(hit)
        # Filter to Rightmove-hosted only and dedupe.
        from homehunt.floorplan_vision import filter_floorplan_urls
        return filter_floorplan_urls(urls)

    def _extract_address(self, soup: BeautifulSoup) -> Optional[str]:
        """Extract property address"""
        # Look for address in various locations
        address_selectors = [
            '[data-testid="property-address"]',
            '.property-address',
            'h1[class*="address"]',
            '[class*="address"]',
        ]
        
        for selector in address_selectors:
            element = soup.select_one(selector)
            if element:
                address = element.get_text().strip()
                if address and len(address) > 5:  # Basic validation
                    return address
        
        # Try to extract from title if not found
        title = self._extract_title(soup)
        if title:
            address_match = re.search(r'(?:for rent in|to rent in|in)\s+(.+?)(?:\s*\||$)', title, re.IGNORECASE)
            if address_match:
                return address_match.group(1).strip()
        
        return None
    
    def _extract_postcode(self, soup: BeautifulSoup, address: Optional[str] = None) -> Optional[str]:
        """Extract postcode from page or address"""
        # UK postcode pattern
        postcode_pattern = r'([A-Z]{1,2}\d[A-Z\d]?\s*\d[A-Z]{2})'
        
        # Look in address first
        if address:
            match = re.search(postcode_pattern, address)
            if match:
                return match.group(1).upper()
        
        # Look in page content
        text = soup.get_text()
        match = re.search(postcode_pattern, text)
        if match:
            return match.group(1).upper()
        
        return None
    
    def _extract_area(self, soup: BeautifulSoup, address: Optional[str] = None) -> Optional[str]:
        """Extract area/district from address"""
        if not address:
            return None
        
        # Remove postcode to get area
        postcode_pattern = r'([A-Z]{1,2}\d[A-Z\d]?\s*\d[A-Z]{2})'
        address_without_postcode = re.sub(postcode_pattern, '', address).strip()
        
        # Extract last part as area
        parts = [part.strip() for part in address_without_postcode.split(',') if part.strip()]
        if len(parts) >= 2:
            return parts[-1]
        
        return None
    
    def _extract_description(self, soup: BeautifulSoup) -> Optional[str]:
        """
        Extract property description.

        Primary: PAGE_MODEL propertyData.text.description (structured JSON)
        Fallback: HTML selectors for older page formats
        """
        raw_html = str(soup)

        # Primary: PAGE_MODEL JSON
        for pattern in [
            r'window\.PAGE_MODEL\s*=\s*(\{.*?\});\s*</script>',
            r'window\.PAGE_MODEL\s*=\s*(\{.*?\})\s*;?\s*(?:window|const|var|</script>)',
        ]:
            m = re.search(pattern, raw_html, re.DOTALL)
            if m:
                try:
                    data = json.loads(m.group(1))
                    text_block = data.get("propertyData", {}).get("text") or {}
                    desc = text_block.get("description", "")
                    if desc and len(desc) > 20:
                        # Strip HTML tags from description
                        desc_clean = re.sub(r'<[^>]+>', ' ', desc)
                        desc_clean = re.sub(r'\s+', ' ', desc_clean).strip()
                        return desc_clean
                except (json.JSONDecodeError, AttributeError):
                    pass

        # Fallback: HTML selectors
        description_selectors = [
            '[data-testid="property-description"]',
            '.property-description',
            '[class*="description"]',
            '.propertyDetailDescription',
        ]
        for selector in description_selectors:
            element = soup.select_one(selector)
            if element:
                description = element.get_text().strip()
                description = re.sub(r'\s+', ' ', description)
                if description and len(description) > 20:
                    return description

        return None
    
    def _extract_features(self, soup: BeautifulSoup) -> List[str]:
        """
        Extract property key-feature bullets.

        Primary: PAGE_MODEL propertyData.keyFeatures (string array)
        Fallback: HTML selectors for older page formats
        """
        raw_html = str(soup)

        # Primary: PAGE_MODEL JSON
        for pattern in [
            r'window\.PAGE_MODEL\s*=\s*(\{.*?\});\s*</script>',
            r'window\.PAGE_MODEL\s*=\s*(\{.*?\})\s*;?\s*(?:window|const|var|</script>)',
        ]:
            m = re.search(pattern, raw_html, re.DOTALL)
            if m:
                try:
                    data = json.loads(m.group(1))
                    kf = data.get("propertyData", {}).get("keyFeatures") or []
                    if isinstance(kf, list) and kf:
                        return [str(f).strip() for f in kf if str(f).strip()]
                except (json.JSONDecodeError, AttributeError):
                    pass

        # Fallback: HTML selectors
        features: List[str] = []
        feature_selectors = [
            '[data-testid="property-features"]',
            '.property-features',
            '[class*="features"]',
            '.propertyKeyFeatures',
        ]
        for selector in feature_selectors:
            containers = soup.select(selector)
            for container in containers:
                list_items = container.find_all('li')
                if list_items:
                    for item in list_items:
                        text = item.get_text().strip()
                        if text and 2 < len(text) < 100:
                            features.append(text)
                else:
                    items = container.find_all(['p', 'span'])
                    for item in items:
                        text = item.get_text().strip()
                        if text and 2 < len(text) < 100:
                            features.append(text)

        return list(dict.fromkeys(features))
    
    def _extract_agent_info(self, soup: BeautifulSoup) -> Dict[str, Any]:
        """Extract estate agent information"""
        agent_info = {}
        
        # Look for agent name
        agent_selectors = [
            '[data-testid="agent-name"]',
            '.agent-name',
            '[class*="agent"]',
            '.contactBranchName',
        ]
        
        for selector in agent_selectors:
            element = soup.select_one(selector)
            if element:
                agent_name = element.get_text().strip()
                if agent_name and len(agent_name) > 2:
                    agent_info["agent_name"] = agent_name
                    break
        
        # Look for phone number
        phone_selectors = [
            '[data-testid="agent-phone"]',
            '.agent-phone',
            '[class*="phone"]',
            'a[href^="tel:"]',
        ]
        
        for selector in phone_selectors:
            element = soup.select_one(selector)
            if element:
                if selector.endswith('tel:"]'):
                    phone = element.get('href', '').replace('tel:', '')
                else:
                    phone = element.get_text().strip()
                
                # Clean phone number
                phone = re.sub(r'[^\d\s\+\-\(\)]', '', phone)
                if phone and len(phone) > 8:
                    agent_info["agent_phone"] = phone
                    break
        
        return agent_info
    
    def _extract_images(self, soup: BeautifulSoup) -> List[str]:
        """
        Extract property photo URLs.

        Primary: window.PAGE_MODEL JSON (propertyData.images[].srcUrl)
        Fallback: <img> tags filtered to /property-photo/ paths
        """
        import json as _json

        # Primary: window.PAGE_MODEL contains structured image data on property pages
        raw_html = str(soup)
        for pattern in [
            r'window\.PAGE_MODEL\s*=\s*(\{.*?\});\s*</script>',
            r'window\.PAGE_MODEL\s*=\s*(\{.*?\})\s*;?\s*(?:window|const|var|</script>)',
        ]:
            m = re.search(pattern, raw_html, re.DOTALL)
            if m:
                try:
                    data = _json.loads(m.group(1))
                    prop = data.get("propertyData", {})
                    # Try multiple known image paths
                    images_raw = (
                        prop.get("images")
                        or prop.get("propertyImages", {}).get("images")
                        or prop.get("photos")
                        or []
                    )
                    if images_raw:
                        urls = []
                        for img in images_raw:
                            src = img.get("srcUrl") or img.get("url") or img.get("src") or ""
                            if src and "property-photo" in src:
                                urls.append(src)
                        if urls:
                            return urls[:10]
                except (_json.JSONDecodeError, AttributeError):
                    pass

        # Fallback: <img> tags, filtered to /property-photo/ paths only (excludes logos/icons)
        images = []
        for img in soup.find_all("img"):
            src = img.get("src") or img.get("data-src") or ""
            if "property-photo" in src and "rightmove.co.uk" in src:
                images.append(src)

        return list(dict.fromkeys(images))[:10]
    
    def extract_property_id(self, url: str) -> Optional[str]:
        """Extract Rightmove property ID from URL"""
        try:
            # Rightmove URL pattern: /properties/123456
            match = re.search(r'/properties/(\d+)', url)
            if match:
                return match.group(1)
            return None
        except Exception as e:
            self.logger.error(f"Error extracting property ID from {url}: {e}")
            return None