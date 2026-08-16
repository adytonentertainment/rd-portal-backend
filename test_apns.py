#!/usr/bin/env python3
"""
Test script for APNs push notifications
Run: ENVIRONMENT=DEVELOPMENT python test_apns.py <device_token>
"""
import sys
import asyncio
import os

# Set environment if not set
if not os.getenv("ENVIRONMENT"):
    os.environ["ENVIRONMENT"] = "DEVELOPMENT"

from aioapns import APNs, NotificationRequest, PushType

async def send_test_notification(device_token: str):
    """Send a test push notification to an iOS device"""
    from app.settings.settings import get_settings
    settings = get_settings()

    print(f"APNs Configuration:")
    print(f"  Key ID: {settings.apns_key_id}")
    print(f"  Team ID: {settings.apns_team_id}")
    print(f"  Bundle ID: {settings.apns_bundle_id}")
    print(f"  Key Path: {settings.apns_key_path}")
    print(f"  Use Sandbox: {settings.apns_use_sandbox}")
    print()

    if not all([settings.apns_key_id, settings.apns_team_id, settings.apns_key_path, settings.apns_bundle_id]):
        print("ERROR: APNs not fully configured")
        return

    # Check if key file exists
    if not os.path.exists(settings.apns_key_path):
        print(f"ERROR: Key file not found at {settings.apns_key_path}")
        return

    print(f"Sending push to device: {device_token[:20]}...")

    try:
        client = APNs(
            key=settings.apns_key_path,
            key_id=settings.apns_key_id,
            team_id=settings.apns_team_id,
            topic=settings.apns_bundle_id,
            use_sandbox=settings.apns_use_sandbox,
        )

        payload = {
            "aps": {
                "alert": {
                    "title": "Test Notification",
                    "body": "This is a test push from Verax!"
                },
                "sound": "default",
                "badge": 1,
            },
            "type": "test"
        }

        request = NotificationRequest(
            device_token=device_token,
            message=payload,
            push_type=PushType.ALERT,
        )

        response = await client.send_notification(request)

        if response.is_successful:
            print("SUCCESS! Push notification sent.")
        else:
            print(f"FAILED: {response.description}")

    except Exception as e:
        print(f"ERROR: {e}")

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: ENVIRONMENT=DEVELOPMENT python test_apns.py <device_token>")
        print()
        print("To get the device token, run the iOS app and look for the printed token")
        print("in the Xcode console after it registers for push notifications.")
        sys.exit(1)

    device_token = sys.argv[1]
    asyncio.run(send_test_notification(device_token))
