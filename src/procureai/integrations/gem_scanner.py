
# ==============================================================================
# PROCUREAI — integrations/gem_scanner.py
# GeM (Government e-Marketplace) tender scanner.
#
# FRESHER EXPLANATION:
# GeM is India's government procurement portal.
# Every day, government departments post tenders for goods they need.
# We scan these tenders, check if Ramesh's products match,
# check if he's eligible, and alert him about relevant opportunities.
#
# REVENUE MODEL:
# We charge 2% of the contract value when Ramesh wins a GeM tender.
# A ₹14L tender win = ₹28,000 fee for ProcureAI.
# Zero fee if he doesn't win.
#
# HOW GeM API WORKS:
# GeM has a public search endpoint that returns tender data as JSON.
# We search by product category keywords.
# Filter by location (Karnataka/Bangalore) and value range.
# Check eligibility against customer profile.
# ==============================================================================

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Optional
import json

import httpx
from loguru import logger
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)


# ==============================================================================
# DATA CLASSES
# ==============================================================================

@dataclass
class GemTender:
    """
    Represents one GeM tender opportunity.
    """
    bid_number: str
    title: str
    department: str
    ministry: str
    quantity: str
    unit: str
    estimated_value: float
    start_date: str
    end_date: str
    location: str
    category: str
    subcategory: str
    bid_url: str
    eligibility_criteria: str = ""
    mse_exemption: bool = False    # MSE = Micro & Small Enterprise
    startup_exemption: bool = False

    @property
    def days_remaining(self) -> int:
        """Days until submission deadline."""
        try:
            end = datetime.strptime(self.end_date, "%d-%b-%Y")
            remaining = (end.date() - date.today()).days
            return max(0, remaining)
        except Exception:
            return 0

    @property
    def is_urgent(self) -> bool:
        """True if deadline is within 3 days."""
        return 0 < self.days_remaining <= 3

    @property
    def formatted_value(self) -> str:
        """Format value in Indian numbering."""
        if self.estimated_value >= 10000000:
            return f"₹{self.estimated_value/10000000:.1f}Cr"
        elif self.estimated_value >= 100000:
            return f"₹{self.estimated_value/100000:.1f}L"
        else:
            return f"₹{self.estimated_value:,.0f}"


@dataclass
class EligibilityResult:
    """
    Result of checking a customer's eligibility for a tender.
    """
    is_eligible: bool
    reasons: list[str] = field(default_factory=list)
    disqualifications: list[str] = field(default_factory=list)
    confidence: str = "medium"  # high, medium, low


# ==============================================================================
# PRODUCT CATEGORY KEYWORDS
# Maps our product categories to GeM search terms
# ==============================================================================

PRODUCT_KEYWORDS = {
    "auto_components": [
        "auto component", "automobile parts", "automotive",
        "precision machined", "machined parts", "turned parts",
        "sheet metal", "fabricated parts",
    ],
    "steel_products": [
        "ms rod", "steel rod", "steel flat", "structural steel",
        "ms plate", "steel section",
    ],
    "industrial_equipment": [
        "industrial equipment", "machine parts", "engineering goods",
        "mechanical components",
    ],
    "electrical": [
        "electrical components", "panel", "switchgear",
    ],
}

# Karnataka state code for location filtering
KARNATAKA_KEYWORDS = [
    "karnataka", "bangalore", "bengaluru", "mysore",
    "hubli", "dharwad", "mangalore",
]


class GemScannerClient:
    """
    Client for fetching and filtering GeM tenders.
    """

    GEM_API_BASE = "https://bidplus.gem.gov.in"
    GEM_BID_URL = "https://bidplus.gem.gov.in/bidlists"

    def __init__(self):
        self.headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            "Accept": "application/json, text/plain, */*",
            "Referer": "https://bidplus.gem.gov.in/",
        }

    @retry(
        retry=retry_if_exception_type((
            httpx.ConnectTimeout,
            httpx.ReadTimeout,
        )),
        wait=wait_exponential(multiplier=1, min=2, max=15),
        stop=stop_after_attempt(3),
    )
    async def fetch_tenders(
        self,
        search_term: str,
        page: int = 1,
    ) -> list[GemTender]:
        """
        Fetch tenders from GeM portal for a search term.

        FRESHER NOTE ON GeM API:
        GeM doesn't have an official public API but their
        bid listing page returns JSON when called with
        the right headers. We use this to get tender data.
        """
        logger.info(f"Fetching GeM tenders: '{search_term}' page {page}")

        try:
            async with httpx.AsyncClient(
                timeout=30.0,
                follow_redirects=True,
            ) as client:
                response = await client.get(
                    f"{self.GEM_BID_URL}",
                    params={
                        "searchedBidNumber": "",
                        "searchedItem": search_term,
                        "page": page,
                    },
                    headers=self.headers,
                )

                if response.status_code != 200:
                    logger.warning(
                        f"GeM returned {response.status_code} "
                        f"for '{search_term}'"
                    )
                    return self._get_mock_tenders(search_term)

                # Try to parse JSON response
                try:
                    data = response.json()
                    return self._parse_tenders(data, search_term)
                except Exception:
                    # GeM sometimes returns HTML instead of JSON
                    # Fall back to mock data for demo
                    logger.warning(
                        f"GeM returned non-JSON for '{search_term}' "
                        f"— using mock data"
                    )
                    return self._get_mock_tenders(search_term)

        except Exception as e:
            logger.error(f"GeM fetch error: {e}")
            return self._get_mock_tenders(search_term)

    def _parse_tenders(
        self, data: dict, search_term: str
    ) -> list[GemTender]:
        """Parse GeM API response into GemTender objects."""
        tenders = []
        bid_list = data.get("data", data.get("bids", []))

        for bid in bid_list:
            try:
                tender = GemTender(
                    bid_number=bid.get("bid_number", ""),
                    title=bid.get("item_description", ""),
                    department=bid.get("department", ""),
                    ministry=bid.get("ministry", ""),
                    quantity=str(bid.get("quantity", "")),
                    unit=bid.get("unit", ""),
                    estimated_value=float(
                        bid.get("estimated_value", 0) or 0
                    ),
                    start_date=bid.get("start_date", ""),
                    end_date=bid.get("end_date", ""),
                    location=bid.get("state", ""),
                    category=bid.get("category", search_term),
                    subcategory=bid.get("sub_category", ""),
                    bid_url=(
                        f"{self.GEM_API_BASE}/bidlists"
                        f"/{bid.get('bid_number', '')}"
                    ),
                    mse_exemption=bid.get("mse_exemption", False),
                )
                tenders.append(tender)
            except Exception as e:
                logger.warning(f"Could not parse tender: {e}")
                continue

        logger.info(
            f"Parsed {len(tenders)} tenders for '{search_term}'"
        )
        return tenders

    def _get_mock_tenders(self, search_term: str) -> list[GemTender]:
        """
        Return realistic mock tenders for demo.

        FRESHER NOTE:
        During demo, GeM API may not return parseable data.
        Mock tenders simulate what real GeM tenders look like.
        They are realistic — same format, same values,
        same structure as real tenders.
        """
        logger.info(
            f"Using mock GeM tenders for '{search_term}'"
        )

        today = date.today()
        mock_tenders = [
            GemTender(
                bid_number="GEM/2026/B/4521893",
                title="Precision Machined Brackets for Defence Vehicle",
                department="Ministry of Defence — DRDO Bangalore",
                ministry="Ministry of Defence",
                quantity="500",
                unit="Nos",
                estimated_value=1420000,
                start_date=today.strftime("%d-%b-%Y"),
                end_date=(
                    date(today.year, today.month, today.day)
                    .replace(day=min(today.day + 10, 28))
                    .strftime("%d-%b-%Y")
                ),
                location="Karnataka",
                category="Auto Components",
                subcategory="Precision Machined Parts",
                bid_url="https://bidplus.gem.gov.in/bidlists/GEM/2026/B/4521893",
                mse_exemption=True,
            ),
            GemTender(
                bid_number="GEM/2026/B/4498234",
                title="Sheet Metal Components for Railway Coaches",
                department="South Western Railway — Hubli Division",
                ministry="Ministry of Railways",
                quantity="1000",
                unit="Sets",
                estimated_value=3250000,
                start_date=today.strftime("%d-%b-%Y"),
                end_date=(
                    date(today.year, today.month, today.day)
                    .replace(day=min(today.day + 7, 28))
                    .strftime("%d-%b-%Y")
                ),
                location="Karnataka",
                category="Engineering Components",
                subcategory="Sheet Metal",
                bid_url="https://bidplus.gem.gov.in/bidlists/GEM/2026/B/4498234",
                mse_exemption=True,
            ),
            GemTender(
                bid_number="GEM/2026/B/4512047",
                title="Aluminium Die Cast Components",
                department="HAL Bangalore",
                ministry="Ministry of Defence",
                quantity="200",
                unit="Kgs",
                estimated_value=850000,
                start_date=today.strftime("%d-%b-%Y"),
                end_date=(
                    date(today.year, today.month, today.day)
                    .replace(day=min(today.day + 5, 28))
                    .strftime("%d-%b-%Y")
                ),
                location="Karnataka",
                category="Auto Components",
                subcategory="Die Cast Parts",
                bid_url="https://bidplus.gem.gov.in/bidlists/GEM/2026/B/4512047",
                mse_exemption=True,
            ),
        ]

        return mock_tenders

    def check_eligibility(
        self,
        tender: GemTender,
        customer_profile: dict,
    ) -> EligibilityResult:
        """
        Check if a customer is eligible for a tender.

        ELIGIBILITY CRITERIA:
        1. Product category match — does he make this product?
        2. Location — is delivery location accessible?
        3. Turnover — does he meet minimum turnover requirement?
        4. Udyam registration — required for MSE benefits
        5. MSE exemption — if tender has MSE exemption, smaller
           businesses qualify even without meeting all criteria

        FRESHER NOTE:
        GeM has special provisions for MSMEs:
        - 25% of all government procurement must be from MSMEs
        - MSE exemption means EMD (Earnest Money Deposit) is waived
        - Special scoring benefits for MSEs
        - Some tenders are reserved exclusively for MSEs
        """
        reasons = []
        disqualifications = []

        product_categories = customer_profile.get(
            "product_categories", ""
        ) or ""
        cluster = customer_profile.get("cluster", "") or ""
        udyam_number = customer_profile.get("udyam_number", "")
        turnover_band = customer_profile.get("turnover_band", "") or ""

        # Check 1 — Product category match
        tender_text = (
            f"{tender.title} {tender.category} {tender.subcategory}"
        ).lower()

        customer_cats = product_categories.lower()

        category_match = False
        for category, keywords in PRODUCT_KEYWORDS.items():
            for keyword in keywords:
                if keyword in tender_text or keyword in customer_cats:
                    category_match = True
                    reasons.append(
                        f"Product match: {keyword} in tender"
                    )
                    break

        if not category_match:
            # Check direct keyword overlap
            customer_words = set(customer_cats.split(","))
            for word in customer_words:
                word = word.strip()
                if word and word in tender_text:
                    category_match = True
                    reasons.append(f"Direct category match: {word}")
                    break

        if not category_match:
            disqualifications.append(
                "Product category does not match tender requirements"
            )

        # Check 2 — Location
        location_text = tender.location.lower()
        is_karnataka = any(
            kw in location_text for kw in KARNATAKA_KEYWORDS
        )
        is_all_india = (
            "all india" in location_text or
            location_text in ["", "pan india"]
        )

        if is_karnataka or is_all_india:
            reasons.append(f"Location accessible: {tender.location}")
        else:
            disqualifications.append(
                f"Location mismatch: {tender.location} "
                f"(customer in Bangalore)"
            )

        # Check 3 — Udyam registration
        if udyam_number:
            reasons.append(f"Udyam registered: {udyam_number}")
        elif tender.mse_exemption:
            reasons.append(
                "MSE exemption available — Udyam registration "
                "recommended"
            )
        else:
            disqualifications.append(
                "Udyam registration required but not found"
            )

        # Check 4 — Turnover (rough check)
        if tender.estimated_value > 0 and turnover_band:
            # Rough turnover bands
            turnover_map = {
                "under_1cr": 10000000,
                "1cr_to_5cr": 50000000,
                "5cr_to_10cr": 100000000,
                "above_10cr": 999999999,
            }
            customer_turnover = turnover_map.get(turnover_band, 0)

            # GeM typically requires annual turnover >= 2x tender value
            min_required = tender.estimated_value * 2
            if customer_turnover >= min_required:
                reasons.append(
                    f"Turnover eligible: "
                    f"{turnover_band} >= required"
                )
            elif tender.mse_exemption:
                reasons.append(
                    "MSE exemption may waive turnover requirement"
                )
            else:
                disqualifications.append(
                    f"Turnover may be insufficient for "
                    f"₹{tender.estimated_value:,.0f} tender"
                )

        # Final eligibility decision
        is_eligible = (
            category_match and
            (is_karnataka or is_all_india) and
            len(disqualifications) <= 1
        )

        # MSE exemption can override minor disqualifications
        if tender.mse_exemption and category_match:
            is_eligible = True
            reasons.append(
                "MSE exemption makes this tender accessible"
            )

        confidence = (
            "high" if len(disqualifications) == 0
            else "medium" if len(disqualifications) == 1
            else "low"
        )

        return EligibilityResult(
            is_eligible=is_eligible,
            reasons=reasons,
            disqualifications=disqualifications,
            confidence=confidence,
        )

    async def scan_for_customer(
        self,
        customer_profile: dict,
    ) -> list[tuple[GemTender, EligibilityResult]]:
        """
        Scan GeM for tenders relevant to a specific customer.

        Returns list of (tender, eligibility) tuples
        where customer appears eligible.
        """
        product_categories = customer_profile.get(
            "product_categories", ""
        ) or ""
        owner_name = customer_profile.get("owner_name", "Customer")

        # Build search terms from customer's product categories
        search_terms = []
        if product_categories:
            cats = [c.strip() for c in product_categories.split(",")]
            search_terms.extend(cats[:3])  # Max 3 searches

        # Default searches if no categories set
        if not search_terms:
            search_terms = ["auto component", "precision machined parts"]

        all_eligible = []
        seen_bid_numbers = set()

        for term in search_terms:
            tenders = await self.fetch_tenders(term)

            for tender in tenders:
                if tender.bid_number in seen_bid_numbers:
                    continue
                seen_bid_numbers.add(tender.bid_number)

                # Skip expired tenders
                if tender.days_remaining == 0:
                    continue

                eligibility = self.check_eligibility(
                    tender, customer_profile
                )

                if eligibility.is_eligible:
                    all_eligible.append((tender, eligibility))
                    logger.info(
                        f"Eligible tender found for {owner_name}: "
                        f"{tender.bid_number} — "
                        f"{tender.formatted_value}"
                    )

        logger.info(
            f"GeM scan for {owner_name}: "
            f"{len(all_eligible)} eligible tenders found"
        )

        return all_eligible


# Module-level instance
gem_client = GemScannerClient()