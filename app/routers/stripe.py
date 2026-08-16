import os
from datetime import datetime
from typing import Literal

import stripe
from app.database.session import get_session
from app.logger import get_logger
from app.misc import price_names, price_tiers, thresholds
from app.misc.misc import get_scan_threshold
from app.models.models import Subscription, SubscriptionTier, User
from app.routers.auth import get_user
from app.services.stripe_service import StripeService
from app.settings.settings import get_settings
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

settings = get_settings()
stripe.api_key = settings.stripe_api_secret_key
logger = get_logger(__name__)
stripe_router = APIRouter(prefix="/stripe", tags=["Stripe"])

# https://docs.stripe.com/billing/subscriptions/usage-based-legacy


@stripe_router.get("/checkout-session")
async def create_or_update_subscription(
    redirect_url: str = None,
    tier: Literal["essential", "pro", "elite", "enterprise"] = "pro",
    interval: Literal["month", "year"] = "month",
    user=Depends(get_user),
    db: Session = Depends(get_session),
):
    """
    Create a new subscription or update an existing one.
    If the user has an existing subscription, it will be cancelled and replaced with the new tier.
    Beta tier restrictions can be configured via BETA_ALLOWED_TIERS env variable.
    """
    if not redirect_url:
        redirect_url = settings.base_url_frontend

    if tier not in ["essential", "pro", "elite", "enterprise"]:
        raise HTTPException(
            status_code=400, detail="Please give a valid subscription type."
        )

    # Check beta tier restrictions
    beta_allowed_tiers = getattr(settings, "beta_allowed_tiers", None)
    if beta_allowed_tiers:
        allowed_tiers = [t.strip().lower() for t in beta_allowed_tiers.split(",")]
        if tier not in allowed_tiers:
            raise HTTPException(
                status_code=400,
                detail=f"This subscription tier is not available during beta. Available tiers: {', '.join(allowed_tiers)}",
            )

    # Map string tier to enum
    tier_enum = SubscriptionTier[tier.upper()]

    # Check if user has an existing subscription for the current mode
    existing_subscription = user.get_active_subscription()

    if existing_subscription:
        # Check if they're trying to change to the same tier
        if existing_subscription.tier == tier_enum:
            raise HTTPException(
                status_code=400,
                detail=f"You already have a {tier.capitalize()} subscription.",
            )

    # Ensure user has a Stripe customer ID
    if not user.stripe_customer_id:
        logger.error(f"User {user.id} ({user.username}) has no Stripe customer ID")
        raise HTTPException(
            status_code=400,
            detail="No Stripe customer found. Please contact support.",
        )

    # Initialize StripeService with appropriate mode
    stripe_service = StripeService()
    user_mode = stripe_service.get_stripe_mode_for_user(user, db)

    # Reinitialize with user's mode if different
    if user_mode != stripe_service.mode:
        stripe_service = StripeService(mode=user_mode)

    try:
        if existing_subscription:
            # Modify the existing subscription in-place by swapping the price item
            new_price_ids = stripe_service.get_price_ids(tier_enum)
            stripe_sub = stripe.Subscription.retrieve(
                existing_subscription.subscription_id
            )

            # Use yearly price for annual billing, flat for monthly
            if interval == "year" and new_price_ids.get("yearly"):
                target_price_id = new_price_ids["yearly"]
            else:
                target_price_id = new_price_ids["flat"]

            # Build the update: swap flat/yearly price, keep extra price updated
            items = []
            extra_price_id = new_price_ids.get("extra")
            has_extra_item = False
            for item in stripe_sub["items"]["data"]:
                item_interval = item["price"].get("recurring", {}).get("usage_type")
                if item_interval == "metered":
                    # This is the extra/metered price item - update to new tier's extra
                    has_extra_item = True
                    if extra_price_id:
                        items.append({"id": item["id"], "price": extra_price_id})
                    else:
                        items.append({"id": item["id"], "deleted": True})
                else:
                    # This is the flat/yearly price item - swap to new tier/interval
                    items.append(
                        {"id": item["id"], "price": target_price_id, "quantity": 1}
                    )

            # If subscription didn't have an extra item yet, add one
            if not has_extra_item and extra_price_id:
                items.append({"price": extra_price_id})

            stripe.Subscription.modify(
                existing_subscription.subscription_id,
                items=items,
                proration_behavior="create_prorations",
            )

            logger.info(
                f"Modified subscription {existing_subscription.subscription_id} "
                f"from {existing_subscription.tier.value} to {tier} for user {user.id}"
            )

            return JSONResponse(
                content={"message": f"Subscription changed to {tier.capitalize()}."},
                status_code=200,
            )
        else:
            # New subscription - create a checkout session
            checkout_session = stripe_service.create_checkout_session(
                tier=tier_enum,
                customer_id=user.stripe_customer_id,
                success_url=redirect_url,
                cancel_url=os.path.join(settings.base_url_frontend, "pricing"),
                metadata={
                    "user_id": str(user.id),
                    "tier": tier,
                    "interval": interval,
                },
                interval=interval,
            )

            return checkout_session.url
    except Exception as e:
        logger.error(f"Error processing subscription: {str(e)}")
        raise HTTPException(status_code=400, detail=str(e))


@stripe_router.get("/preview-tier-change")
async def preview_tier_change(
    tier: Literal["essential", "pro", "elite", "enterprise"] = "pro",
    interval: Literal["month", "year"] = "month",
    user: User = Depends(get_user),
    db: Session = Depends(get_session),
):
    """
    Preview what a tier change would cost, including proration details.
    Returns pricing breakdown so the frontend can show a confirmation modal.
    """
    tier_enum = SubscriptionTier[tier.upper()]

    existing_subscription = user.get_active_subscription()
    if not existing_subscription:
        raise HTTPException(status_code=400, detail="No active subscription to change.")

    if existing_subscription.tier == tier_enum:
        raise HTTPException(
            status_code=400,
            detail=f"You already have a {tier.capitalize()} subscription.",
        )

    if not user.stripe_customer_id:
        raise HTTPException(
            status_code=400,
            detail="No Stripe customer found. Please contact support.",
        )

    stripe_service = StripeService()
    user_mode = stripe_service.get_stripe_mode_for_user(user, db)
    if user_mode != stripe_service.mode:
        stripe_service = StripeService(mode=user_mode)

    try:
        new_price_ids = stripe_service.get_price_ids(tier_enum)
        stripe_sub = stripe.Subscription.retrieve(existing_subscription.subscription_id)

        # Use yearly price for annual billing, flat for monthly
        if interval == "year" and new_price_ids.get("yearly"):
            target_price_id = new_price_ids["yearly"]
        else:
            target_price_id = new_price_ids["flat"]

        # Build the items for the preview: swap flat/yearly price, update extra
        preview_items = []
        extra_price_id = new_price_ids.get("extra")
        has_extra_item = False
        for item in stripe_sub["items"]["data"]:
            item_usage_type = item["price"].get("recurring", {}).get("usage_type")
            if item_usage_type == "metered":
                has_extra_item = True
                if extra_price_id:
                    preview_items.append({"id": item["id"], "price": extra_price_id})
                else:
                    preview_items.append({"id": item["id"], "deleted": True})
            else:
                preview_items.append(
                    {"id": item["id"], "price": target_price_id, "quantity": 1}
                )

        if not has_extra_item and extra_price_id:
            preview_items.append({"price": extra_price_id})

        # Get upcoming invoice preview with the proposed changes
        upcoming = stripe.Invoice.create_preview(
            customer=user.stripe_customer_id,
            subscription=existing_subscription.subscription_id,
            subscription_details={"items": preview_items},
        )

        # Calculate the proration amount from line items
        proration_amount = 0
        for line in upcoming["lines"]["data"]:
            if line.get("proration"):
                proration_amount += line["amount"]

        # Get the new tier's recurring price
        new_price = 0
        for line in upcoming["lines"]["data"]:
            if not line.get("proration") and line["amount"] > 0:
                new_price = line["amount"]
                break

        # Get current tier's price from the existing subscription
        current_price = 0
        if stripe_sub["items"]["data"]:
            current_price = stripe_sub["items"]["data"][0]["price"].get(
                "unit_amount", 0
            )

        next_billing_date = datetime.fromtimestamp(
            stripe_sub["current_period_end"]
        ).strftime("%B %d, %Y")

        # Determine the current billing interval from the existing subscription
        current_interval = existing_subscription.billing_interval or "month"

        return JSONResponse(
            content={
                "current_tier": existing_subscription.tier.value,
                "new_tier": tier.capitalize(),
                "current_price": current_price / 100,
                "new_price": new_price / 100,
                "proration_amount": proration_amount / 100,
                "amount_due": upcoming["amount_due"] / 100,
                "next_billing_date": next_billing_date,
                "current_interval": current_interval,
                "new_interval": interval,
            },
            status_code=200,
        )
    except Exception as e:
        logger.error(f"Error previewing tier change: {str(e)}")
        raise HTTPException(status_code=400, detail=str(e))


@stripe_router.get("/billing-portal")
async def billing_portal(redirect_url: str = None, user: User = Depends(get_user)):
    if not redirect_url:
        redirect_url = settings.base_url_frontend
    try:
        # Check if user has a Stripe customer ID
        if not user.stripe_customer_id:
            logger.error(f"User {user.id} ({user.username}) has no Stripe customer ID")
            raise HTTPException(
                status_code=400,
                detail="No Stripe customer found. Please contact support.",
            )

        logger.info(
            f"Creating billing portal session for user {user.id} with customer ID {user.stripe_customer_id}"
        )

        billing_session = stripe.billing_portal.Session.create(
            customer=user.stripe_customer_id, return_url=redirect_url
        )
        return billing_session.url
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error creating billing portal session: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@stripe_router.get("/subscription")
async def get_user_subscription_data(user: User = Depends(get_user)):
    # Get the active subscription for the current stripe mode
    subscription = user.get_active_subscription()

    if not subscription:
        # User has no subscription for current mode - return free tier response
        return JSONResponse(
            content={
                "tier": "FREE",
                "scans": 0,
                "threshold": 0,
                "catalog_used": 0,
                "catalog_limit": 0,
            },
            status_code=200,
        )

    # Import the helper function to get catalog limits
    from app.routers.catalog import get_catalog_limit_for_tier

    billing_interval = subscription.billing_interval or "month"
    catalog_limit = get_catalog_limit_for_tier(subscription.tier, billing_interval)

    # Handle FREE tier subscriptions (users with explicit FREE tier in database)
    if subscription.tier == SubscriptionTier.FREE:
        return JSONResponse(
            content={
                "tier": "FREE",
                "scans": 0,
                "threshold": 0,
                "catalog_used": subscription.catalog_added_count,
                "catalog_limit": 0,
            },
            status_code=200,
        )

    return JSONResponse(
        content={
            "tier": subscription.tier.value,
            "scans": subscription.scans,
            "threshold": get_scan_threshold(subscription.tier.value, billing_interval),
            "catalog_used": subscription.catalog_added_count,
            "catalog_limit": catalog_limit,
            "billing_interval": billing_interval,
        },
        status_code=200,
    )


@stripe_router.get("/publishable-key")
async def get_publishable_key(
    user: User = Depends(get_user), db: Session = Depends(get_session)
):
    """
    Get the appropriate Stripe publishable key based on user's subscription mode
    """
    stripe_service = StripeService()
    user_mode = stripe_service.get_stripe_mode_for_user(user, db)

    # Reinitialize with user's mode if different
    if user_mode != stripe_service.mode:
        stripe_service = StripeService(mode=user_mode)

    return JSONResponse(
        content={"publishableKey": stripe_service.publishable_key, "mode": user_mode},
        status_code=200,
    )


@stripe_router.post("/webhook")
async def stripe_webhook(request: Request, db: Session = Depends(get_session)):
    content_length = int(request.headers["content-length"])
    if content_length > 1024 * 1024:
        raise HTTPException(status_code=400, detail="Content too long for webhook.")
    payload = await request.body()
    signature = request.headers.get("stripe-signature")

    # Debug logging
    logger.info(
        f"Webhook received - Signature present: {bool(signature)}, First 50 chars: {signature[:50] if signature else 'MISSING'}"
    )
    logger.info(f"All headers: {dict(request.headers)}")

    # Try to verify webhook with both test and live modes
    event, mode = StripeService.verify_webhook_dual_mode(payload, signature)

    if not event:
        raise HTTPException(status_code=400, detail="Invalid webhook signature")

    logger.info(f"Processing webhook event {event['type']} in {mode} mode")

    if event["type"] == "checkout.session.completed":
        # Handle successful checkout - this fires BEFORE subscription.created
        session = event["data"]["object"]
        customer_id = session.get("customer")

        # Get metadata from checkout session
        metadata = session.get("metadata", {})
        user_id = metadata.get("user_id")
        tier = metadata.get("tier")

        logger.info(
            f"Checkout completed for user_id: {user_id}, tier: {tier}, customer: {customer_id}"
        )

        # Find the user
        user = None
        if user_id:
            try:
                user = db.query(User).filter(User.id == int(user_id)).first()
                logger.info(f"Found user {user_id} from checkout session metadata")
            except (ValueError, TypeError) as e:
                logger.warning(f"Invalid user_id in metadata: {user_id}, error: {e}")

        # Update user's stripe_customer_id if found
        if user and customer_id and not user.stripe_customer_id:
            user.stripe_customer_id = customer_id
            db.commit()
            logger.info(
                f"Updated user {user.id} with stripe_customer_id: {customer_id}"
            )

    elif event["type"] == "customer.subscription.created":
        customer_id = event["data"]["object"]["customer"]
        subscription_id = event["data"]["object"]["id"]
        stripe_status = event["data"]["object"].get("status", "incomplete")

        # Get metadata from the subscription object (set via subscription_data in checkout)
        metadata = event["data"]["object"].get("metadata", {})
        user_id = metadata.get("user_id")
        metadata_tier = metadata.get("tier")  # e.g. "pro", "essential"
        metadata_interval = metadata.get("interval")  # e.g. "month", "year"

        # Find the flat/yearly price from subscription items (not the extra/metered price)
        items_data = event["data"]["object"]["items"]["data"]
        price_id = None
        billing_interval = "month"
        for item in items_data:
            pid = item["price"]["id"]
            if pid in price_names:
                price_id = pid
                billing_interval = (
                    item["price"].get("recurring", {}).get("interval", "month")
                )
                break

        # Fallback: if no known price found, use first item
        if not price_id and items_data:
            price_id = items_data[0]["price"]["id"]
            billing_interval = (
                items_data[0]["price"].get("recurring", {}).get("interval", "month")
            )

        # Use metadata interval if available (most reliable)
        if metadata_interval:
            billing_interval = metadata_interval

        # Try to find user by ID from metadata first, then by stripe_customer_id
        user = None
        if user_id:
            try:
                user = db.query(User).filter(User.id == int(user_id)).first()
                logger.info(f"Found user {user_id} from subscription metadata")
            except (ValueError, TypeError) as e:
                logger.warning(f"Invalid user_id in metadata: {user_id}, error: {e}")

        # Fallback to finding by stripe_customer_id
        if not user:
            user = db.query(User).filter(User.stripe_customer_id == customer_id).first()
            if user:
                logger.info(f"Found user by stripe_customer_id: {customer_id}")

        # If still not found, try to get customer email from Stripe and match by email
        if not user:
            try:
                customer = stripe.Customer.retrieve(customer_id)
                customer_email = customer.get("email")

                if customer_email:
                    user = db.query(User).filter(User.email == customer_email).first()
                    if user:
                        logger.info(f"Found user by email: {customer_email}")
            except Exception as e:
                logger.error(f"Error retrieving customer from Stripe: {e}")

        if not user:
            logger.error(
                f"Could not find user for customer {customer_id}, user_id {user_id}"
            )
            raise HTTPException(status_code=404, detail="User not found")

        # Update user's stripe_customer_id if not set
        if not user.stripe_customer_id:
            user.stripe_customer_id = customer_id
            logger.info(
                f"Updated user {user.id} with stripe_customer_id: {customer_id}"
            )

        # Determine tier: prefer metadata (most reliable), then price_id lookup
        if metadata_tier:
            try:
                tier = SubscriptionTier[metadata_tier.upper()]
                logger.info(f"Using tier from metadata: {tier.value}")
            except KeyError:
                tier = SubscriptionTier(price_names.get(price_id, "Pro"))
                logger.warning(
                    f"Invalid tier in metadata '{metadata_tier}', fell back to price lookup: {tier.value}"
                )
        else:
            tier = SubscriptionTier(price_names.get(price_id, "Pro"))

        logger.info(
            f"Resolved tier={tier.value}, interval={billing_interval}, price_id={price_id} for subscription {subscription_id}"
        )

        # Check if user already has a subscription for this mode
        existing_sub = (
            db.query(Subscription)
            .filter(Subscription.user_id == user.id, Subscription.stripe_mode == mode)
            .first()
        )

        if existing_sub:
            # Update existing subscription
            logger.info(
                f"Updating existing subscription for user {user.id} to tier {tier.value}"
            )
            existing_sub.subscription_id = subscription_id
            existing_sub.tier = tier
            existing_sub.scans = 0
            existing_sub.limit_exceeded = False
            existing_sub.stripe_status = stripe_status
            existing_sub.billing_interval = billing_interval
        else:
            # Create new subscription
            logger.info(
                f"Creating new subscription for user {user.id} with tier {tier.value}"
            )
            new_sub = Subscription(
                user_id=user.id,
                subscription_id=subscription_id,
                scans=0,
                tier=tier,
                catalog_added_count=0,
                limit_exceeded=False,
                stripe_mode=mode,
                stripe_status=stripe_status,
                billing_interval=billing_interval,
            )
            db.add(new_sub)

        db.commit()
        logger.info(
            f"Successfully processed subscription for user {user.id}, tier: {tier.value}"
        )

    elif event["type"] == "customer.subscription.deleted":
        customer_id = event["data"]["object"]["customer"]
        subscription_id = event["data"]["object"]["id"]
        db.query(Subscription).filter(
            Subscription.subscription_id == subscription_id
        ).delete()
        db.commit()
    elif event["type"] == "customer.subscription.updated":
        customer_id = event["data"]["object"]["customer"]
        subscription_id = event["data"]["object"]["id"]
        stripe_status = event["data"]["object"].get("status", "active")

        # Find the flat/yearly price from subscription items (not the extra/metered price)
        items_data = event["data"]["object"]["items"]["data"]
        price_id = None
        billing_interval = "month"
        for item in items_data:
            pid = item["price"]["id"]
            if pid in price_names:
                price_id = pid
                billing_interval = (
                    item["price"].get("recurring", {}).get("interval", "month")
                )
                break

        # Fallback: if no known price found, use first item
        if not price_id and items_data:
            price_id = items_data[0]["price"]["id"]
            billing_interval = (
                items_data[0]["price"].get("recurring", {}).get("interval", "month")
            )

        # Get metadata for tier (if available from subscription_data)
        metadata = event["data"]["object"].get("metadata", {})
        metadata_tier = metadata.get("tier")

        # Determine tier: prefer metadata, then price_id lookup
        if metadata_tier:
            try:
                new_tier = SubscriptionTier[metadata_tier.upper()]
            except KeyError:
                new_tier = SubscriptionTier(price_names.get(price_id, "Pro"))
        else:
            new_tier = SubscriptionTier(price_names.get(price_id, "Pro"))

        subscription = (
            db.query(Subscription)
            .filter(Subscription.subscription_id == subscription_id)
            .first()
        )

        if subscription:
            # Update the subscription tier, status, and billing interval
            subscription.tier = new_tier
            subscription.stripe_status = stripe_status
            subscription.billing_interval = billing_interval
            subscription.scans = 0
            subscription.catalog_added_count = 0  # Reset catalog counter on tier change
            subscription.limit_exceeded = False
            db.commit()
            logger.info(
                f"Updated subscription {subscription_id} to tier {new_tier.value}, status: {stripe_status}"
            )
        else:
            # Handle case where subscription doesn't exist yet (shouldn't happen normally)
            user = db.query(User).filter(User.stripe_customer_id == customer_id).first()
            if user:
                new_sub = Subscription(
                    user_id=user.id,
                    subscription_id=subscription_id,
                    scans=0,
                    tier=new_tier,
                    catalog_added_count=0,
                    limit_exceeded=False,
                    stripe_mode=mode,
                    stripe_status=stripe_status,
                    billing_interval=billing_interval,
                )
                db.add(new_sub)
                db.commit()
                logger.info(
                    f"Created new subscription {subscription_id} with tier {new_tier.value}, status: {stripe_status}"
                )
    elif event["type"] == "invoice.payment_succeeded":
        # This event fires when a subscription is renewed (monthly billing)
        subscription_id = event["data"]["object"].get("subscription")
        if subscription_id:
            subscription = (
                db.query(Subscription)
                .filter(Subscription.subscription_id == subscription_id)
                .first()
            )
            if subscription:
                # Reset the counters for the new billing period
                subscription.scans = 0
                subscription.catalog_added_count = 0
                subscription.limit_exceeded = False
                db.commit()
                logger.info(
                    f"Reset counters for subscription {subscription_id} on renewal"
                )
    return JSONResponse(content={"message": "Success"}, status_code=200)
