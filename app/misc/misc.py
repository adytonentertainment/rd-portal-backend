from app.settings.settings import get_settings

settings = get_settings()

# Stripe price tiers - now configurable via environment variables
# first element is the flat fee, second element is the price after the maximum
price_tiers = {
    "essential": [
        settings.stripe_price_essential_flat,
        settings.stripe_price_essential_extra,
    ],
    "pro": [settings.stripe_price_pro_flat, settings.stripe_price_pro_extra],
    "elite": [settings.stripe_price_elite_flat, settings.stripe_price_elite_extra],
    "enterprise": [
        settings.stripe_price_enterprise_flat,
        settings.stripe_price_enterprise_extra,
    ],
}

# Map price IDs to tier names (include BOTH test and live price IDs for dual-mode webhooks)
# Includes flat, yearly, AND extra price IDs so webhooks can identify any subscription item
price_names = {}

# Add test mode price IDs (flat)
if settings.stripe_test_price_essential_flat:
    price_names[settings.stripe_test_price_essential_flat] = "Essential"
if settings.stripe_test_price_pro_flat:
    price_names[settings.stripe_test_price_pro_flat] = "Pro"
if settings.stripe_test_price_elite_flat:
    price_names[settings.stripe_test_price_elite_flat] = "Elite"
if settings.stripe_test_price_enterprise_flat:
    price_names[settings.stripe_test_price_enterprise_flat] = "Enterprise"

# Add test mode price IDs (yearly)
if settings.stripe_test_price_essential_yearly:
    price_names[settings.stripe_test_price_essential_yearly] = "Essential"
if settings.stripe_test_price_pro_yearly:
    price_names[settings.stripe_test_price_pro_yearly] = "Pro"
if settings.stripe_test_price_elite_yearly:
    price_names[settings.stripe_test_price_elite_yearly] = "Elite"
if settings.stripe_test_price_enterprise_yearly:
    price_names[settings.stripe_test_price_enterprise_yearly] = "Enterprise"

# Add test mode price IDs (extra/metered)
if settings.stripe_test_price_essential_extra:
    price_names[settings.stripe_test_price_essential_extra] = "Essential"
if settings.stripe_test_price_pro_extra:
    price_names[settings.stripe_test_price_pro_extra] = "Pro"
if settings.stripe_test_price_elite_extra:
    price_names[settings.stripe_test_price_elite_extra] = "Elite"
if settings.stripe_test_price_enterprise_extra:
    price_names[settings.stripe_test_price_enterprise_extra] = "Enterprise"

# Add live mode price IDs (flat)
if settings.stripe_live_price_essential_flat:
    price_names[settings.stripe_live_price_essential_flat] = "Essential"
if settings.stripe_live_price_pro_flat:
    price_names[settings.stripe_live_price_pro_flat] = "Pro"
if settings.stripe_live_price_elite_flat:
    price_names[settings.stripe_live_price_elite_flat] = "Elite"
if settings.stripe_live_price_enterprise_flat:
    price_names[settings.stripe_live_price_enterprise_flat] = "Enterprise"

# Add live mode price IDs (yearly)
if settings.stripe_live_price_essential_yearly:
    price_names[settings.stripe_live_price_essential_yearly] = "Essential"
if settings.stripe_live_price_pro_yearly:
    price_names[settings.stripe_live_price_pro_yearly] = "Pro"
if settings.stripe_live_price_elite_yearly:
    price_names[settings.stripe_live_price_elite_yearly] = "Elite"
if settings.stripe_live_price_enterprise_yearly:
    price_names[settings.stripe_live_price_enterprise_yearly] = "Enterprise"

# Add live mode price IDs (extra/metered)
if settings.stripe_live_price_essential_extra:
    price_names[settings.stripe_live_price_essential_extra] = "Essential"
if settings.stripe_live_price_pro_extra:
    price_names[settings.stripe_live_price_pro_extra] = "Pro"
if settings.stripe_live_price_elite_extra:
    price_names[settings.stripe_live_price_elite_extra] = "Elite"
if settings.stripe_live_price_enterprise_extra:
    price_names[settings.stripe_live_price_enterprise_extra] = "Enterprise"

thresholds = {
    "Free": 0,  # Free tier has no scan threshold
    "Essential": settings.essential_catalog_limit,
    "Pro": settings.pro_catalog_limit,
    "Elite": settings.elite_catalog_limit,
    "Enterprise": settings.enterprise_catalog_limit,
}


def get_scan_threshold(tier_value: str, billing_interval: str = "month") -> int:
    """Get the scan threshold for a tier, multiplied by 12 for yearly subscriptions."""
    limit = thresholds.get(tier_value, 0)
    if billing_interval == "year":
        limit *= 12
    return limit


royalty_dict = {
    "Worldwide": 0.00238,
    "Europe": 0.00331,
    "Iceland": 0.00618,
    "Norway": 0.00547,
    "Monaco": 0.00521,
    "Denmark": 0.00515,
    "United": 0.00352,
    "Switzerland": 0.00473,
    "Liechtenstein": 0.00472,
    "Ireland": 0.00457,
    "Finland": 0.00444,
    "Sweden": 0.004440,
    "Austria": 0.00441,
    "Luxembourg": 0.00415,
    "Netherlands": 0.00362,
    "Andorra": 0.00359,
    "Germany": 0.00336,
    "Belgium": 0.00324,
    "France": 0.00316,
    "Cyprus": 0.00304,
    "Malta": 0.00276,
    "Estonia": 0.00268,
    "Spain": 0.00235,
    "Czech": 0.00210,
    "Lithuania": 0.00206,
    "Italy": 0.00199,
    "Slovakia": 0.00192,
    "Hungary": 0.00190,
    "Latvia": 0.00177,
    "Greece": 0.00176,
    "Portugal": 0.00162,
    "Bulgaria": 0.00159,
    "Romania": 0.00151,
    "Poland": 0.00140,
    "Oceania": 0.00447,
    "New": 0.00497,
    "Australia": 0.00396,
    "Asia": 0.00173,
    "Japan": 0.00354,
    "Israel": 0.00328,
    "Singapore": 0.00291,
    "Hong": 0.00274,
    "Lebanon": 0.00203,
    "Oman": 0.00185,
    "Bahrain": 0.001760,
    "Vietnam": 0.00163,
    "Malaysia": 0.00155,
    "Taiwan": 0.001510,
    "Qatar": 0.00145,
    "Kuwait": 0.00144,
    "India": 0.00141,
    "Thailand": 0.00116,
    "Saudi": 0.00102,
    "Jordan": 0.00099,
    "Indonesia": 0.00096,
    "Turkey": 0.00084,
    "Philippines": 0.00081,
    "North": 0.00200,
    "Panama": 0.00290,
    "Canada": 0.00273,
    "Costa": 0.00197,
    "Dominican": 0.00189,
    "Honduras": 0.00172,
    "El": 0.00146,
    "Mexico": 0.001440,
    "Nicaragua": 0.00122,
    "Guatemala": 0.00117,
    "South": 0.00156,
    "Uruguay": 0.00248,
    "Ecuador": 0.001630,
    "Paraguay": 0.00153,
    "Peru": 0.001460,
    "Brazil": 0.00129,
    "Chile": 0.00126,
    "Colombia": 0.00104,
    "Bolivia": 0.000990,
    "Argentina": 0.00085,
    "Africa": 0.00094,
    "Egypt": 0.000960,
    "Morocco": 0.00080,
    "Algeria": 0.00070,
    "Tunisia": 0.000700,
}
