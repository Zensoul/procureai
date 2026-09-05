
# ==============================================================================
# PROCUREAI — integrations/mcx.py
# Commodity price fetcher for weekly price pulse feature.
#
# FRESHER EXPLANATION:
# The price pulse feature tells Ramesh:
# "You paid ₹188/kg for MS Rod. Market rate: ₹174/kg. You overpaid."
#
# To say "market rate is ₹174/kg" we need actual market data.
# This file fetches that data from two sources:
#
# SOURCE 1 — MCX (Multi Commodity Exchange India)
# MCX is India's largest commodity exchange.
# Steel futures trade here — contracts to buy/sell steel
# at a future date at today's agreed price.
# Steel futures price correlates with physical MS rod price.
# MCX data is public — no API key required.
# We scrape their website using httpx.
#
# SOURCE 2 — Our cluster database (internal)
# Actual prices from enrolled customers.
# "Ramesh paid ₹188, Suresh paid ₹182, Venkatesh paid ₹179"
# Average: ₹183 — this IS the Bommasandra cluster price.
# More accurate than MCX because it's local, physical, actual.
# Available only after we have 5+ customers buying same material.
#
# PRIORITY:
# Cluster data > MCX data
# Use MCX as benchmark when cluster data is thin.
# Use cluster data when we have enough (≥5 data points).
#
# MATERIAL MAPPING:
# Ramesh buys "MS Rod IS 2062 E250" — a specific product.
# MCX trades "Steel Long" futures — a generic category.
# We maintain a mapping between product names and MCX contracts.
# ==============================================================================

from dataclasses import dataclass
from decimal import Decimal
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

from procureai.config import get_settings

settings = get_settings()


# ==============================================================================
# DATA CLASSES
# Clean objects representing price data
# ==============================================================================

@dataclass
class CommodityPrice:
    """
    Price data for one commodity from one source.

    FRESHER NOTE ON @dataclass:
    @dataclass automatically generates __init__, __repr__,
    and __eq__ methods from the class attributes.
    Instead of writing:
        def __init__(self, material_name, price_per_kg, ...):
            self.material_name = material_name
            ...
    You just declare the fields and @dataclass handles the rest.
    """
    material_name: str
    price_per_kg: Decimal
    price_per_tonne: Decimal
    source: str          # "mcx", "cluster", "manual"
    data_date: str       # YYYY-MM-DD
    sample_count: int    # How many transactions (for cluster data)
    is_estimated: bool   # True if MCX futures (not physical price)

    @property
    def display_price(self) -> str:
        """
        Formatted price for use in alert messages.
        "₹188/kg" or "₹1,88,000/tonne"
        """
        return f"₹{self.price_per_kg:,.0f}/kg"


@dataclass
class PriceComparison:
    """
    Comparison between what a customer paid and the market rate.
    This is the core output of the price pulse feature.
    """
    material_name: str
    customer_price: Decimal      # What Ramesh paid
    market_price: Decimal        # MCX or cluster average
    difference: Decimal          # customer - market (positive = overpaying)
    difference_pct: Decimal      # percentage overpayment
    potential_saving: Decimal    # difference × quantity
    quantity: Decimal            # How much was purchased
    unit: str
    market_source: str           # "mcx" or "cluster"
    is_overpaying: bool          # True if customer_price > market_price

    @property
    def alert_line(self) -> str:
        """
        One-line summary for the price pulse alert.
        "MS Rod: You paid ₹188/kg | Market: ₹174/kg | Saving: ₹4,200"
        """
        if self.is_overpaying:
            return (
                f"{self.material_name}: "
                f"You paid ₹{self.customer_price:,.0f}/kg | "
                f"Market: ₹{self.market_price:,.0f}/kg | "
                f"Potential saving: ₹{self.potential_saving:,.0f}"
            )
        return (
            f"{self.material_name}: "
            f"You paid ₹{self.customer_price:,.0f}/kg | "
            f"Market: ₹{self.market_price:,.0f}/kg | "
            f"Good price!"
        )


# ==============================================================================
# MATERIAL TO MCX MAPPING
# Maps common manufacturing materials to MCX contract names.
#
# FRESHER NOTE:
# MCX doesn't have a contract for "MS Rod IS 2062 E250".
# It has contracts for "Steel Long" and "Steel Flat".
# MS rods fall under "Steel Long".
# This mapping lets us find the right MCX price for any material.
# ==============================================================================

MATERIAL_MCX_MAP = {
    # Steel products → Steel Long futures
    "ms rod": "STEELLONG",
    "ms flat": "STEELLONG",
    "ms angle": "STEELLONG",
    "ms channel": "STEELLONG",
    "steel rod": "STEELLONG",
    "tmt bar": "STEELLONG",

    # Flat steel → Steel Flat futures
    "hr coil": "STEELFLAT",
    "cr coil": "STEELFLAT",
    "ms sheet": "STEELFLAT",
    "ms plate": "STEELFLAT",

    # Aluminium
    "aluminium": "ALUMINIUM",
    "aluminum": "ALUMINIUM",
    "al rod": "ALUMINIUM",

    # Copper
    "copper": "COPPER",
    "copper wire": "COPPER",

    # Lead
    "lead": "LEAD",

    # Zinc
    "zinc": "ZINC",
}

# Physical to futures price adjustment factors.
# MCX futures prices are slightly different from physical market prices.
# These factors convert MCX futures price to estimated physical price.
# Based on historical spread analysis.
FUTURES_TO_PHYSICAL_FACTOR = {
    "STEELLONG": Decimal("1.08"),   # Physical ~8% above futures
    "STEELFLAT": Decimal("1.06"),   # Physical ~6% above futures
    "ALUMINIUM": Decimal("1.05"),
    "COPPER": Decimal("1.03"),
    "LEAD": Decimal("1.04"),
    "ZINC": Decimal("1.04"),
}


# ==============================================================================
# MCX PRICE CLIENT
# ==============================================================================

class MCXPriceClient:
    """
    Fetches commodity prices from MCX India.

    FRESHER NOTE:
    MCX doesn't provide a public API. We fetch prices by
    scraping their public market data endpoints.
    This is legal for publicly available market data.

    We also maintain manual price overrides for when
    MCX scraping fails — ensuring the price pulse feature
    always has data even if MCX is temporarily unavailable.
    """

    # MCX public data endpoint
    MCX_BASE_URL = "https://www.mcxindia.com"

    # Fallback prices when MCX is unavailable
    # Updated manually based on market knowledge
    # Format: contract_name → price per tonne in INR
    FALLBACK_PRICES = {
        "STEELLONG": Decimal("52000"),   # ~₹52/kg
        "STEELFLAT": Decimal("55000"),   # ~₹55/kg
        "ALUMINIUM": Decimal("215000"),  # ~₹215/kg
        "COPPER": Decimal("820000"),     # ~₹820/kg
        "LEAD": Decimal("185000"),       # ~₹185/kg
        "ZINC": Decimal("250000"),       # ~₹250/kg
    }

    def _get_mcx_contract(self, material_name: str) -> Optional[str]:
        """
        Find the MCX contract name for a material.
        Case-insensitive, partial match supported.

        Example:
            "MS Rod IS 2062" → matches "ms rod" → "STEELLONG"
            "HR Coil 2mm" → matches "hr coil" → "STEELFLAT"
        """
        material_lower = material_name.lower().strip()

        # Try exact match first
        if material_lower in MATERIAL_MCX_MAP:
            return MATERIAL_MCX_MAP[material_lower]

        # Try partial match
        for key, contract in MATERIAL_MCX_MAP.items():
            if key in material_lower or material_lower in key:
                return contract

        logger.warning(
            f"No MCX contract mapping for material: {material_name}. "
            f"Will use fallback price."
        )
        return None

    @retry(
        retry=retry_if_exception_type((
            httpx.ConnectTimeout,
            httpx.ReadTimeout,
        )),
        wait=wait_exponential(multiplier=1, min=2, max=15),
        stop=stop_after_attempt(3),
    )
    async def fetch_mcx_price(
        self, contract: str
    ) -> Optional[Decimal]:
        """
        Fetch current price for an MCX contract.
        Returns price per tonne in INR.
        Returns None if fetch fails.

        FRESHER NOTE:
        We return None instead of raising an exception here
        because price fetch failure is non-critical.
        The caller can fall back to the manual price.
        An ITC alert is critical — must succeed.
        A price pulse is helpful — can use fallback data.
        """
        try:
            async with httpx.AsyncClient(
                timeout=15.0,
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36"
                    )
                },
                follow_redirects=True,
            ) as client:
                # MCX public market watch endpoint
                response = await client.get(
                    f"{self.MCX_BASE_URL}/MarketData/GetMarketWatch",
                    params={"Type": "SPOT"},
                )

                if response.status_code != 200:
                    logger.warning(
                        f"MCX returned {response.status_code} "
                        f"for {contract}"
                    )
                    return None

                data = response.json()
                return self._extract_price_from_response(data, contract)

        except httpx.RequestError as e:
            logger.warning(f"MCX fetch failed for {contract}: {e}")
            return None
        except Exception as e:
            logger.warning(f"MCX parse error for {contract}: {e}")
            return None

    def _extract_price_from_response(
        self, data: dict, contract: str
    ) -> Optional[Decimal]:
        """
        Extract the last traded price from MCX API response.

        FRESHER NOTE:
        API responses can be messy and change format without notice.
        We use .get() with defaults everywhere — never assume a key exists.
        If the format changes, we return None and fall back gracefully
        instead of crashing the entire price pulse run.
        """
        try:
            items = data.get("data", data.get("Data", []))
            for item in items:
                symbol = item.get("Symbol", item.get("symbol", ""))
                if contract.upper() in symbol.upper():
                    ltp = item.get(
                        "LastTradePrice",
                        item.get("LTP", item.get("ltp", 0))
                    )
                    if ltp and float(ltp) > 0:
                        return Decimal(str(ltp))
        except Exception as e:
            logger.warning(f"Error extracting price: {e}")
        return None

    def _get_fallback_price(self, contract: str) -> Decimal:
        """
        Returns the manual fallback price when MCX is unavailable.
        These are periodically updated to stay near market reality.
        """
        price = self.FALLBACK_PRICES.get(contract, Decimal("50000"))
        logger.info(
            f"Using fallback price for {contract}: "
            f"₹{price:,}/tonne"
        )
        return price

    async def get_material_price(
        self, material_name: str
    ) -> CommodityPrice:
        """
        Get current market price for a material.

        This is the main method called by the price engine.
        Returns a CommodityPrice regardless of whether MCX
        data was available or fallback was used.

        Flow:
        1. Find MCX contract for this material
        2. Fetch MCX price
        3. If MCX fails → use fallback price
        4. Convert from per-tonne to per-kg
        5. Apply physical/futures adjustment factor
        6. Return CommodityPrice object

        FRESHER NOTE ON UNIT CONVERSION:
        MCX prices are in Rupees per tonne (1000 kg).
        Ramesh buys in kg.
        per_kg_price = per_tonne_price / 1000
        """
        from datetime import date

        contract = self._get_mcx_contract(material_name)

        if contract:
            # Try fetching live MCX price
            price_per_tonne = await self.fetch_mcx_price(contract)
            source = "mcx_live"
            is_estimated = True

            if price_per_tonne is None:
                # MCX unavailable — use fallback
                price_per_tonne = self._get_fallback_price(contract)
                source = "mcx_fallback"
        else:
            # Unknown material — use generic steel fallback
            contract = "STEELLONG"
            price_per_tonne = self._get_fallback_price(contract)
            source = "fallback_generic"
            is_estimated = True

        # Apply physical/futures spread adjustment
        adjustment = FUTURES_TO_PHYSICAL_FACTOR.get(
            contract, Decimal("1.05")
        )
        adjusted_price_per_tonne = (
            price_per_tonne * adjustment
        ).quantize(Decimal("1"))

        # Convert to per-kg
        price_per_kg = (
            adjusted_price_per_tonne / Decimal("1000")
        ).quantize(Decimal("0.01"))

        logger.info(
            f"Price for {material_name}: "
            f"₹{price_per_kg}/kg "
            f"(source: {source})"
        )

        return CommodityPrice(
            material_name=material_name,
            price_per_kg=price_per_kg,
            price_per_tonne=adjusted_price_per_tonne,
            source=source,
            data_date=date.today().isoformat(),
            sample_count=0,
            is_estimated=is_estimated,
        )

    def calculate_price_comparison(
        self,
        material_name: str,
        customer_price_per_kg: Decimal,
        market_price: CommodityPrice,
        quantity: Decimal,
        unit: str,
    ) -> PriceComparison:
        """
        Compare what the customer paid against market price.

        FRESHER NOTE:
        This is the core calculation of Feature 3.
        Given:
        - Ramesh paid ₹188/kg
        - Market rate: ₹174/kg
        - He bought 300kg

        We calculate:
        - Difference: ₹188 - ₹174 = ₹14/kg overpayment
        - Percentage: 14/174 × 100 = 8.05% above market
        - Total saving opportunity: ₹14 × 300 = ₹4,200
        """
        market_price_kg = market_price.price_per_kg
        difference = customer_price_per_kg - market_price_kg

        # Avoid division by zero
        if market_price_kg > 0:
            difference_pct = (
                difference / market_price_kg * Decimal("100")
            ).quantize(Decimal("0.1"))
        else:
            difference_pct = Decimal("0")

        # Potential saving = overpayment per kg × quantity
        # Only meaningful for kg purchases
        if unit.lower() == "kg":
            potential_saving = (difference * quantity).quantize(
                Decimal("0.01")
            )
        else:
            potential_saving = Decimal("0")

        return PriceComparison(
            material_name=material_name,
            customer_price=customer_price_per_kg,
            market_price=market_price_kg,
            difference=difference,
            difference_pct=difference_pct,
            potential_saving=potential_saving,
            quantity=quantity,
            unit=unit,
            market_source=market_price.source,
            is_overpaying=difference > Decimal("0"),
        )


# ==============================================================================
# MODULE-LEVEL CLIENT INSTANCE
# ==============================================================================

mcx_client = MCXPriceClient()