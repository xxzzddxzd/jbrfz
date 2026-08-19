from __future__ import annotations

import base64
import unittest

from crumble_bot.auth import APP_BUILD, APP_VERSION
from crumble_bot.constants import FALLBACK_RESOURCE_KEY, normalize_resource_key
from crumble_bot.resource import (
    RESOURCE_MANIFEST_HEADERS,
    RESOURCE_METADATA_BODY,
    RESOURCE_METADATA_HEADERS,
)


class ResourceVersionTests(unittest.TestCase):
    def test_live_client_version_is_consistent_across_login_and_metadata(self) -> None:
        self.assertEqual(APP_VERSION, "1.1.101")
        self.assertEqual(APP_BUILD, "2026081413")
        self.assertEqual(RESOURCE_METADATA_BODY["app_version"], APP_VERSION)
        self.assertEqual(RESOURCE_METADATA_BODY["app_build"], APP_BUILD)
        self.assertEqual(
            base64.b64decode(RESOURCE_METADATA_HEADERS["X-App-Version"]).decode(),
            APP_VERSION,
        )
        self.assertEqual(
            base64.b64decode(RESOURCE_METADATA_HEADERS["X-App-Build"]).decode(),
            APP_BUILD,
        )
        expected_user_agent = f"CookieRunCrumble/{APP_BUILD} "
        self.assertTrue(
            RESOURCE_METADATA_HEADERS["User-Agent"].startswith(expected_user_agent)
        )
        self.assertTrue(
            RESOURCE_MANIFEST_HEADERS["User-Agent"].startswith(expected_user_agent)
        )

    def test_stale_resource_keys_are_replaced_by_current_fallback(self) -> None:
        self.assertEqual(FALLBACK_RESOURCE_KEY, "game-data-185237-ee36b3")
        for stale_key in (
            "",
            "dev-0000000000",
            "game-data-8319a6-a64b0c",
            "game-data-9db3ba-0ca6ad",
            "game-data-9db3ba-a069b0",
            "game-data-185237-02fbe8",
        ):
            with self.subTest(stale_key=stale_key):
                self.assertEqual(
                    normalize_resource_key(stale_key),
                    FALLBACK_RESOURCE_KEY,
                )


if __name__ == "__main__":
    unittest.main()
