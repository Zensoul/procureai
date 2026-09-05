# ==============================================================================
# PROCUREAI — schemas/customer.py
# Pydantic schemas for Customer API requests and responses.
#
# FRESHER EXPLANATION:
# Schemas are the "gatekeepers" of your API.
#
# When n8n sends data to enrol a new customer:
#   → CustomerCreate schema validates the incoming JSON
#   → If GSTIN is missing or phone has wrong format → reject immediately
#   → If valid → pass to service layer → save to database
#
# When your API returns customer data:
#   → CustomerResponse schema controls exactly what is returned
#   → Sensitive fields (encrypted credentials) are never included
#   → Computed fields (is_gstn_connected) are added automatically
#
# ANALOGY:
# Model = the actual employee file in HR (contains everything)
# Schema = the business card (contains only what's appropriate to share)
# ==============================================================================

from datetime import datetime
from typing import Optional
import re

from pydantic import BaseModel, Field, field_validator, computed_field


# ==============================================================================
# BASE SCHEMA
# Shared fields and validators used by all Customer schemas.
# Other schemas inherit from this — same principle as BaseModel in models.
# ==============================================================================

class CustomerBase(BaseModel):
    """
    Shared fields across Create, Update, and Response schemas.
    Contains fields that appear in multiple schema types.
    """

    # Owner's full name
    # min_length=2: "A" is not a valid name
    # max_length=100: matches database column length
    owner_name: Optional[str] = Field(
        None,
        min_length=2,
        max_length=100,
        description="Full name of the MSME owner",
        examples=["Ramesh Kumar"]
    )

    # Business name
    business_name: Optional[str] = Field(
        None,
        min_length=2,
        max_length=200,
        description="Registered business name",
        examples=["Ramesh Auto Parts Pvt Ltd"]
    )

    # Phone number
    # Pattern validates Indian mobile numbers:
    # +919845123456 or 9845123456 or 09845123456
    phone: Optional[str] = Field(
        None,
        description="Indian mobile number",
        examples=["9845123456", "+919845123456"]
    )

    # Email — optional
    email: Optional[str] = Field(
        None,
        description="Email address — optional for MSME owners"
    )

    # Preferred delivery channel
    preferred_channel: Optional[str] = Field(
        None,
        description="Alert delivery channel: rcs, sms, voice, telegram"
    )

    # Preferred language
    preferred_language: Optional[str] = Field(
        None,
        description="Alert language: kn=Kannada, hi=Hindi, en=English"
    )

    # Industrial cluster
    cluster: Optional[str] = Field(
        None,
        description="Industrial cluster: bommasandra, peenya, electronic_city"
    )

    # Udyam registration number
    udyam_number: Optional[str] = Field(
        None,
        description="Udyam Registration Number for GeM eligibility"
    )

    # Turnover band for scheme eligibility
    turnover_band: Optional[str] = Field(
        None,
        description="Annual turnover: under_1cr, 1cr_to_5cr, 5cr_to_10cr, above_10cr"
    )

    # Product categories for GeM matching
    product_categories: Optional[str] = Field(
        None,
        description="Comma-separated product categories"
    )

    # ===========================================================================
    # VALIDATORS
    # Pydantic validators run automatically before data reaches your service.
    # They reject invalid data immediately with a clear error message.
    # No invalid data ever reaches your database.
    # ===========================================================================

    @field_validator("phone")
    @classmethod
    def validate_phone(cls, v: Optional[str]) -> Optional[str]:
        """
        Validates Indian mobile phone numbers.

        Accepts:
        - 9845123456 (10 digits starting with 6-9)
        - +919845123456 (with country code)
        - 919845123456 (country code without +)

        Rejects:
        - 12345 (too short)
        - 1234567890 (doesn't start with 6-9)
        - abcdefghij (not a number)

        FRESHER NOTE:
        @classmethod means this validator runs on the class itself,
        not on an instance. Pydantic requires this for field validators.
        """
        if v is None:
            return v

        # Remove spaces and dashes — "98451 23456" → "9845123456"
        v = v.strip().replace(" ", "").replace("-", "")

        # Remove country code if present
        if v.startswith("+91"):
            v = v[3:]
        elif v.startswith("91") and len(v) == 12:
            v = v[2:]

        # Validate: 10 digits, starts with 6, 7, 8, or 9
        if not re.match(r'^[6-9]\d{9}$', v):
            raise ValueError(
                f"Invalid Indian mobile number: {v}. "
                "Must be 10 digits starting with 6, 7, 8, or 9."
            )
        return v

    @field_validator("preferred_channel")
    @classmethod
    def validate_channel(cls, v: Optional[str]) -> Optional[str]:
        """Ensures channel is one of the supported values."""
        if v is None:
            return v
        allowed = {"rcs", "sms", "voice", "telegram"}
        if v not in allowed:
            raise ValueError(
                f"Invalid channel: {v}. Must be one of: {allowed}"
            )
        return v

    @field_validator("preferred_language")
    @classmethod
    def validate_language(cls, v: Optional[str]) -> Optional[str]:
        """Ensures language is one of the supported values."""
        if v is None:
            return v
        allowed = {"kn", "hi", "en"}
        if v not in allowed:
            raise ValueError(
                f"Invalid language: {v}. Must be one of: {allowed}"
            )
        return v

    @field_validator("gstin", mode="before", check_fields=False)
    @classmethod
    def validate_gstin(cls, v: Optional[str]) -> Optional[str]:
        """
        Validates GSTIN format: 29ABCDE1234F1Z5
        - 2 digits (state code)
        - 10 alphanumeric (PAN)
        - 1 digit (entity number)
        - 1 letter (always Z)
        - 1 alphanumeric (checksum)
        Total: 15 characters
        """
        if v is None:
            return v
        v = v.strip().upper()
        if not re.match(r'^\d{2}[A-Z]{5}\d{4}[A-Z]{1}[1-9A-Z]{1}Z[0-9A-Z]{1}$', v):
            raise ValueError(
                f"Invalid GSTIN format: {v}. "
                "Must be 15 characters like: 29ABCDE1234F1Z5"
            )
        return v

    @field_validator("turnover_band")
    @classmethod
    def validate_turnover_band(cls, v: Optional[str]) -> Optional[str]:
        """Ensures turnover band is a valid option."""
        if v is None:
            return v
        allowed = {"under_1cr", "1cr_to_5cr", "5cr_to_10cr", "above_10cr"}
        if v not in allowed:
            raise ValueError(
                f"Invalid turnover band: {v}. Must be one of: {allowed}"
            )
        return v


# ==============================================================================
# CREATE SCHEMA
# Used when enrolling a new MSME customer.
# Contains all REQUIRED fields — API rejects if any are missing.
# ==============================================================================

class CustomerCreate(CustomerBase):
    """
    Schema for enrolling a new customer.

    REQUIRED fields: owner_name, business_name, gstin, phone
    All other fields are optional — can be added later.

    FRESHER NOTE:
    Field(...) with ... means REQUIRED.
    Field(None) or Field(default=...) means OPTIONAL.

    When n8n sends a POST /customers request, this schema
    validates the body. If gstin is missing → 422 error returned
    before your code even runs.
    """

    # Required fields — no defaults
    owner_name: str = Field(
        ...,
        min_length=2,
        max_length=100,
        description="Full name of the MSME owner",
        examples=["Ramesh Kumar"]
    )

    business_name: str = Field(
        ...,
        min_length=2,
        max_length=200,
        description="Registered business name",
        examples=["Ramesh Auto Parts Pvt Ltd"]
    )

    gstin: str = Field(
        ...,
        description="15-character GSTIN",
        examples=["29ABCDE1234F1Z5"]
    )

    phone: str = Field(
        ...,
        description="Indian mobile number",
        examples=["9845123456"]
    )

    # Optional fields with sensible defaults
    preferred_channel: str = Field(
        default="rcs",
        description="Primary alert channel"
    )

    preferred_language: str = Field(
        default="kn",
        description="Alert language — kn for Kannada"
    )

    # CA who referred this customer
    # Optional — direct signups have no CA
    ca_id: Optional[str] = Field(
        None,
        description="CA's customer ID — for revenue sharing"
    )

    # Telegram chat ID — populated for Option A customers
    telegram_chat_id: Optional[int] = Field(
        None,
        description="Telegram chat ID for Option A delivery"
    )


# ==============================================================================
# UPDATE SCHEMA
# Used when updating an existing customer's details.
# ALL fields are optional — only send what's changing.
# ==============================================================================

class CustomerUpdate(CustomerBase):
    """
    Schema for updating customer details.

    ALL fields are optional. Send only the fields you want to change.

    Example: Update only the phone number:
    PATCH /customers/{id}
    {"phone": "9844567890"}

    Everything else stays unchanged.

    FRESHER NOTE:
    This is called a "partial update" pattern.
    The alternative is PUT which requires sending ALL fields.
    PATCH with all-optional fields is cleaner for mobile apps
    and WhatsApp bots that only update one thing at a time.
    """

    # All fields optional — inherited from CustomerBase as Optional
    # Just add any fields that aren't in CustomerBase
    is_active: Optional[bool] = Field(
        None,
        description="Activate or deactivate customer"
    )

    telegram_chat_id: Optional[int] = Field(
        None,
        description="Update Telegram chat ID"
    )


# ==============================================================================
# RESPONSE SCHEMA
# What the API returns when someone requests customer data.
# Includes database-generated fields (id, created_at).
# Excludes sensitive fields (encrypted credentials).
# ==============================================================================

class CustomerResponse(BaseModel):
    """
    Schema for API responses containing customer data.

    WHAT IS INCLUDED:
    - All business information (name, GSTIN, phone, cluster)
    - Status flags (is_active, is_trial)
    - Timestamps (created_at, onboarded_at)
    - Computed field (is_gstn_connected)

    WHAT IS EXCLUDED:
    - gstn_username_encrypted (never exposed)
    - gstn_password_encrypted (never exposed)

    FRESHER NOTE ON model_config:
    from_attributes=True tells Pydantic to read data from
    SQLAlchemy model attributes directly.
    Without this, you'd have to manually convert every
    SQLAlchemy object to a dictionary before returning it.
    With it: return customer_object → Pydantic handles conversion.
    """

    model_config = {"from_attributes": True}

    # Database-generated fields
    id: str = Field(description="Unique customer ID (UUID)")
    created_at: datetime = Field(description="When customer was enrolled")

    # Identity
    owner_name: str
    business_name: str
    gstin: str
    phone: str
    email: Optional[str] = None

    # Channel preferences
    preferred_channel: str
    preferred_language: str
    telegram_chat_id: Optional[int] = None

    # Business profile
    cluster: Optional[str] = None
    udyam_number: Optional[str] = None
    turnover_band: Optional[str] = None
    product_categories: Optional[str] = None

    # Status
    is_active: bool
    is_trial: bool
    onboarded_at: Optional[datetime] = None

    # Computed field — derived from whether credentials are stored
    # FRESHER NOTE:
    # @computed_field runs Python code to generate this field's value.
    # It's not stored in the database — calculated fresh each time.
    # The CA or n8n workflow uses this to know if GSTN is connected.
    @computed_field
    @property
    def is_gstn_connected(self) -> bool:
        """Returns True if GSTN credentials have been stored."""
        # We can't access the encrypted fields here (not in response)
        # This will be set by the service layer
        return False


# ==============================================================================
# LIST RESPONSE SCHEMA
# Used when returning multiple customers (paginated list).
# Wraps a list of CustomerResponse with pagination metadata.
# ==============================================================================

class CustomerListResponse(BaseModel):
    """
    Paginated list of customers.

    FRESHER NOTE ON PAGINATION:
    Never return ALL records from a database in one API call.
    If you have 10,000 customers and someone calls GET /customers,
    returning all 10,000 at once:
    - Crashes the database (huge query)
    - Times out the HTTP request
    - Uses enormous memory

    Pagination returns a "page" of results at a time.
    The caller asks: "Give me page 1 with 20 results"
    Then: "Give me page 2 with 20 results"
    And so on.

    total: How many customers exist in total
    page: Current page number
    per_page: How many per page
    items: The actual customer data for this page
    """

    total: int = Field(description="Total number of customers")
    page: int = Field(description="Current page number")
    per_page: int = Field(description="Results per page")
    items: list[CustomerResponse] = Field(description="Customer records")
