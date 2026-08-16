"""Stripe Service for handling both test and live mode subscriptions"""

import logging
import os
from datetime import datetime, timedelta
from typing import Any, Dict, Optional

import stripe
from app.models.models import Subscription, SubscriptionTier, User
from app.settings import get_settings
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)
settings = get_settings()


class StripeService:
    """Service for managing Stripe operations in both test and live modes"""

    def __init__(self, mode: str = None):
        """
        Initialize Stripe with test or live keys based on mode

        Args:
            mode: 'test' or 'live', defaults to STRIPE_MODE env var
        """
        mode = mode.upper() if mode else settings.stripe_mode.upper()
        if not mode in ["TEST", "LIVE"]:
            raise Exception("The mode for stripe needs to be either TEST or LIVE.")

        self.mode = mode

        # Select credentials and price IDs based on the active mode (TEST or LIVE)
        if self.mode == "TEST":
            self.api_key = settings.stripe_test_secret_key
            self.publishable_key = settings.stripe_test_public_key
            self.webhook_secret = settings.stripe_test_webhook_secret

            self.price_ids = {
                SubscriptionTier.ESSENTIAL: {
                    "flat": settings.stripe_test_price_essential_flat,
                    "extra": settings.stripe_test_price_essential_extra,
                    "yearly": settings.stripe_test_price_essential_yearly,
                },
                SubscriptionTier.PRO: {
                    "flat": settings.stripe_test_price_pro_flat,
                    "extra": settings.stripe_test_price_pro_extra,
                    "yearly": settings.stripe_test_price_pro_yearly,
                },
                SubscriptionTier.ELITE: {
                    "flat": settings.stripe_test_price_elite_flat,
                    "extra": settings.stripe_test_price_elite_extra,
                    "yearly": settings.stripe_test_price_elite_yearly,
                },
                SubscriptionTier.ENTERPRISE: {
                    "flat": settings.stripe_test_price_enterprise_flat,
                    "extra": settings.stripe_test_price_enterprise_extra,
                    "yearly": settings.stripe_test_price_enterprise_yearly,
                },
            }
        else:
            self.api_key = settings.stripe_live_secret_key
            self.publishable_key = settings.stripe_live_public_key
            self.webhook_secret = settings.stripe_live_webhook_secret

            self.price_ids = {
                SubscriptionTier.ESSENTIAL: {
                    "flat": settings.stripe_live_price_essential_flat,
                    "extra": settings.stripe_live_price_essential_extra,
                    "yearly": settings.stripe_live_price_essential_yearly,
                },
                SubscriptionTier.PRO: {
                    "flat": settings.stripe_live_price_pro_flat,
                    "extra": settings.stripe_live_price_pro_extra,
                    "yearly": settings.stripe_live_price_pro_yearly,
                },
                SubscriptionTier.ELITE: {
                    "flat": settings.stripe_live_price_elite_flat,
                    "extra": settings.stripe_live_price_elite_extra,
                    "yearly": settings.stripe_live_price_elite_yearly,
                },
                SubscriptionTier.ENTERPRISE: {
                    "flat": settings.stripe_live_price_enterprise_flat,
                    "extra": settings.stripe_live_price_enterprise_extra,
                    "yearly": settings.stripe_live_price_enterprise_yearly,
                },
            }

        # Set the Stripe API key
        stripe.api_key = self.api_key
        logger.info(f"StripeService initialized in {self.mode} mode")

    def get_stripe_mode_for_user(self, user: User, db: Session) -> str:
        """
        Get appropriate Stripe mode based on user's existing subscriptions

        Args:
            user: User object
            db: Database session

        Returns:
            'test' or 'live' mode string
        """
        # Check if user has an active subscription for the current mode
        active_subscription = user.get_active_subscription(self.mode.lower())

        if active_subscription:
            # Use the mode from their active subscription
            existing_mode = active_subscription.stripe_mode or "test"
            logger.debug(
                f"User {user.id} has active subscription in {existing_mode} mode"
            )
            return existing_mode.upper()

        # If no subscription for current mode, check if they have any subscription at all
        if user.subscription and len(user.subscription) > 0:
            # They have a subscription but not for the current mode
            # Use current global mode (they'll need to create new subscription)
            logger.debug(
                f"User {user.id} has subscription in different mode, using global mode: {self.mode}"
            )
            return self.mode

        # New users get current global mode
        logger.debug(f"User {user.id} is new, using global mode: {self.mode}")
        return self.mode

    def get_price_id(self, tier: SubscriptionTier, price_type: str = "flat") -> str:
        """
        Get Stripe price ID for the given tier in current mode

        Args:
            tier: SubscriptionTier enum
            price_type: 'flat' for base price or 'extra' for additional charges

        Returns:
            Stripe price ID string
        """
        tier_prices = self.price_ids.get(tier)
        if not tier_prices:
            logger.error(f"No price IDs configured for tier {tier} in {self.mode} mode")
            raise ValueError(f"No price IDs configured for tier {tier}")

        price_id = tier_prices.get(price_type)
        if not price_id:
            logger.error(
                f"No {price_type} price ID configured for tier {tier} in {self.mode} mode"
            )
            raise ValueError(f"No {price_type} price ID configured for tier {tier}")

        logger.debug(
            f"{price_type.capitalize()} price ID for {tier} in {self.mode} mode: {price_id}"
        )
        return price_id

    def get_price_ids(self, tier: SubscriptionTier) -> Dict[str, str]:
        """
        Get both flat and extra Stripe price IDs for the given tier

        Args:
            tier: SubscriptionTier enum

        Returns:
            Dictionary with 'flat' and 'extra' price IDs
        """
        tier_prices = self.price_ids.get(tier)
        if not tier_prices:
            logger.error(f"No price IDs configured for tier {tier} in {self.mode} mode")
            raise ValueError(f"No price IDs configured for tier {tier}")

        return tier_prices

    def create_checkout_session(
        self,
        tier: SubscriptionTier,
        customer_id: str,
        success_url: str,
        cancel_url: str,
        extra_quantity: int = 0,
        metadata: Dict[str, Any] = None,
        interval: str = "month",
    ) -> stripe.checkout.Session:
        """
        Create a Stripe checkout session with flat fee and optional extra charges

        Args:
            tier: Subscription tier
            customer_id: Stripe customer ID
            success_url: URL to redirect on success
            cancel_url: URL to redirect on cancel
            extra_quantity: Quantity for extra/additional charges (e.g., extra scans)
            metadata: Optional metadata to attach to the session
            interval: Billing interval - "month" or "year"

        Returns:
            Stripe checkout session object
        """
        price_ids = self.get_price_ids(tier)

        # Use yearly price ID for annual billing, flat for monthly
        if interval == "year" and price_ids.get("yearly"):
            flat_price_id = price_ids["yearly"]
        else:
            flat_price_id = price_ids["flat"]

        line_items = [{"price": flat_price_id, "quantity": 1}]

        # Always include the extra/metered price so Stripe tracks usage from the start
        if price_ids.get("extra"):
            line_items.append({"price": price_ids["extra"]})

        session_params = {
            "customer": customer_id,
            "payment_method_types": ["card"],
            "line_items": line_items,
            "mode": "subscription",
            "success_url": success_url,
            "cancel_url": cancel_url,
        }

        if metadata:
            session_params["metadata"] = metadata
            # Also add mode to metadata for tracking
            session_params["metadata"]["stripe_mode"] = self.mode
            # IMPORTANT: Pass metadata to the subscription itself
            session_params["subscription_data"] = {"metadata": metadata}

        try:
            session = stripe.checkout.Session.create(**session_params)
            logger.info(
                f"Created checkout session {session.id} in {self.mode} mode with {len(line_items)} line items"
            )
            return session
        except Exception as e:
            logger.error(f"Failed to create checkout session: {e}")
            raise

    def create_checkout_session_simple(
        self,
        tier: SubscriptionTier,
        customer_id: str,
        success_url: str,
        cancel_url: str,
        metadata: Dict[str, Any] = None,
    ) -> stripe.checkout.Session:
        """
        Create a simple Stripe checkout session with only flat fee (backward compatibility)

        Args:
            tier: Subscription tier
            customer_id: Stripe customer ID
            success_url: URL to redirect on success
            cancel_url: URL to redirect on cancel
            metadata: Optional metadata to attach to the session

        Returns:
            Stripe checkout session object
        """
        return self.create_checkout_session(
            tier=tier,
            customer_id=customer_id,
            success_url=success_url,
            cancel_url=cancel_url,
            extra_quantity=0,
            metadata=metadata,
        )

    def update_subscription_items(
        self, subscription_id: str, tier: SubscriptionTier, extra_quantity: int = 0
    ) -> stripe.Subscription:
        """
        Update subscription items to change extra quantity (e.g., for additional scans)

        Args:
            subscription_id: Stripe subscription ID
            tier: Current subscription tier
            extra_quantity: New quantity for extra charges

        Returns:
            Updated subscription object
        """
        try:
            # Get current subscription
            subscription = stripe.Subscription.retrieve(subscription_id)

            price_ids = self.get_price_ids(tier)

            # Find existing items
            flat_item = None
            extra_item = None

            for item in subscription["items"]["data"]:
                if item["price"]["id"] == price_ids["flat"]:
                    flat_item = item
                elif item["price"]["id"] == price_ids["extra"]:
                    extra_item = item

            # Update subscription items
            items = []

            # Always keep the flat fee
            if flat_item:
                items.append({"id": flat_item["id"], "quantity": 1})

            # Update or add extra item
            if extra_quantity > 0:
                if extra_item:
                    # Update existing extra item
                    items.append({"id": extra_item["id"], "quantity": extra_quantity})
                else:
                    # Add new extra item
                    items.append(
                        {"price": price_ids["extra"], "quantity": extra_quantity}
                    )
            elif extra_item:
                # Remove extra item if quantity is 0
                items.append({"id": extra_item["id"], "deleted": True})

            # Update the subscription
            updated_subscription = stripe.Subscription.modify(
                subscription_id,
                items=items,
                proration_behavior="create_prorations",  # This will prorate the changes
            )

            logger.info(
                f"Updated subscription {subscription_id} with extra quantity: {extra_quantity}"
            )
            return updated_subscription

        except Exception as e:
            logger.error(f"Failed to update subscription {subscription_id}: {e}")
            raise

    def cancel_subscription(self, subscription_id: str) -> stripe.Subscription:
        """
        Cancel a Stripe subscription

        Args:
            subscription_id: Stripe subscription ID

        Returns:
            Cancelled subscription object
        """
        try:
            subscription = stripe.Subscription.delete(subscription_id)
            logger.info(f"Cancelled subscription {subscription_id}")
            return subscription
        except Exception as e:
            logger.error(f"Failed to cancel subscription {subscription_id}: {e}")
            raise

    def get_subscription_details(
        self, subscription_id: str, tier: SubscriptionTier
    ) -> Dict[str, Any]:
        """
        Get detailed subscription information including flat and extra charges

        Args:
            subscription_id: Stripe subscription ID
            tier: Current subscription tier

        Returns:
            Dictionary with subscription details including:
            - flat_quantity: Always 1 for base subscription
            - extra_quantity: Number of extra items (e.g., additional scans)
            - flat_price: Price per period for flat fee
            - extra_price: Price per item for extras
            - total: Total subscription amount
        """
        try:
            subscription = stripe.Subscription.retrieve(subscription_id)
            price_ids = self.get_price_ids(tier)

            details = {
                "subscription_id": subscription_id,
                "status": subscription["status"],
                "flat_quantity": 0,
                "extra_quantity": 0,
                "flat_price": 0,
                "extra_price": 0,
                "total": 0,
                "currency": "usd",
            }

            # Parse subscription items
            for item in subscription["items"]["data"]:
                price_id = item["price"]["id"]
                if price_id == price_ids["flat"]:
                    details["flat_quantity"] = item["quantity"]
                    details["flat_price"] = (
                        item["price"]["unit_amount"] / 100
                    )  # Convert from cents
                elif price_id == price_ids["extra"]:
                    details["extra_quantity"] = item["quantity"]
                    details["extra_price"] = (
                        item["price"]["unit_amount"] / 100
                    )  # Convert from cents

            # Calculate total
            details["total"] = (
                details["flat_quantity"] * details["flat_price"]
                + details["extra_quantity"] * details["extra_price"]
            )

            # Add currency from subscription
            if subscription["items"]["data"]:
                details["currency"] = subscription["items"]["data"][0]["price"][
                    "currency"
                ]

            logger.info(
                f"Retrieved details for subscription {subscription_id}: {details}"
            )
            return details

        except Exception as e:
            logger.error(
                f"Failed to get subscription details for {subscription_id}: {e}"
            )
            raise

    def create_billing_portal_session(
        self, customer_id: str, return_url: str
    ) -> stripe.billing_portal.Session:
        """
        Create a billing portal session for customer to manage subscription

        Args:
            customer_id: Stripe customer ID
            return_url: URL to return to after portal session

        Returns:
            Billing portal session object
        """
        try:
            session = stripe.billing_portal.Session.create(
                customer=customer_id,
                return_url=return_url,
            )
            logger.info(f"Created billing portal session for customer {customer_id}")
            return session
        except Exception as e:
            logger.error(f"Failed to create billing portal session: {e}")
            raise

    def verify_webhook_signature(
        self, payload: bytes, signature: str
    ) -> Optional[stripe.Event]:
        """
        Verify webhook signature and return event

        Args:
            payload: Raw request body
            signature: Stripe signature header

        Returns:
            Stripe event if verified, None otherwise
        """
        try:
            logger.info(
                f"Attempting webhook verification in {self.mode} mode - signature present: {bool(signature)}, payload size: {len(payload) if payload else 0}"
            )
            event = stripe.Webhook.construct_event(
                payload, signature, self.webhook_secret
            )
            logger.info(f"Webhook verified in {self.mode} mode: {event['type']}")
            return event
        except ValueError as e:
            logger.error(f"Invalid webhook payload in {self.mode} mode: {e}")
            return None
        except stripe.error.SignatureVerificationError as e:
            logger.error(
                f"Invalid webhook signature in {self.mode} mode: {e} - Signature: {signature[:50] if signature else 'MISSING'}"
            )
            return None

    @staticmethod
    def verify_webhook_dual_mode(
        payload: bytes, signature: str
    ) -> tuple[Optional[stripe.Event], Optional[str]]:
        """
        Try to verify webhook with both test and live secrets

        Args:
            payload: Raw request body
            signature: Stripe signature header

        Returns:
            Tuple of (event, mode) where mode is 'test' or 'live'
        """
        # Try live mode first
        service_live = StripeService(mode="live")
        event = service_live.verify_webhook_signature(payload, signature)
        if event:
            return event, "live"

        # Try test mode
        service_test = StripeService(mode="test")
        event = service_test.verify_webhook_signature(payload, signature)
        if event:
            return event, "test"

        logger.error("Webhook verification failed for both test and live modes")
        return None, None

    @staticmethod
    def transition_test_subscriptions(db: Session, grace_period_days: int = 14) -> int:
        """
        Set transition date for all test subscriptions

        Args:
            db: Database session
            grace_period_days: Days until test subscriptions expire

        Returns:
            Number of subscriptions updated
        """
        transition_date = datetime.utcnow() + timedelta(days=grace_period_days)

        # Find all test subscriptions without a transition date
        test_subs = (
            db.query(Subscription)
            .filter(
                Subscription.stripe_mode == "test",
                Subscription.mode_transition_date.is_(None),
            )
            .all()
        )

        count = 0
        for sub in test_subs:
            sub.mode_transition_date = transition_date
            count += 1

        db.commit()

        logger.info(
            f"Set transition date for {count} test subscriptions to {transition_date}"
        )
        return count

    def is_subscription_valid(self, subscription: Subscription) -> bool:
        """
        Check if a subscription is still valid considering test/live mode

        Args:
            subscription: Subscription object

        Returns:
            True if subscription is valid, False otherwise
        """
        # Live subscriptions are always valid (unless cancelled through Stripe)
        if subscription.stripe_mode == "live":
            return True

        # Test subscriptions are valid until transition date
        if subscription.mode_transition_date:
            if datetime.utcnow() > subscription.mode_transition_date:
                logger.info(f"Test subscription {subscription.id} has expired")
                return False

        return True
