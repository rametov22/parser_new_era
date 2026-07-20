from unittest.mock import Mock

from django.test import SimpleTestCase, override_settings
from selenium.common.exceptions import WebDriverException

from .chrome_utils import apply_vavada_trust_cookie
from .release_quality import has_pirated_release
from .tasks.vavada_serials import _episode_number


class VavadaReleaseQualityTests(SimpleTestCase):
    def test_detects_theatrical_release_labels(self):
        labels = (
            "TS",
            "HDTS",
            "CAMRip",
            "HDCAM",
            "TeleSync",
            "DVD Screener",
            "экранка",
            "камрип",
        )

        for label in labels:
            with self.subTest(label=label):
                self.assertTrue(has_pirated_release(label))

    def test_ignores_ts_inside_names_and_urls(self):
        self.assertFalse(has_pirated_release("MVO | TVShows"))
        self.assertFalse(has_pirated_release("DTS-HD MA"))
        self.assertFalse(
            has_pirated_release(
                {
                    "label": "WEB-DL",
                    "url": "https://cdn.example/video/segment.ts",
                }
            )
        )

    def test_normal_track_makes_release_non_pirated(self):
        self.assertFalse(
            has_pirated_release(
                [
                    "DUB | Звук с TS",
                    "MVO | TVShows",
                    "(EN) Оригинал",
                ]
            )
        )

    def test_all_pirated_tracks_make_release_pirated(self):
        self.assertTrue(
            has_pirated_release(
                [
                    "DUB | Звук с TS",
                    "DUB | Официальный (TC)",
                ]
            )
        )


class VavadaEpisodeNumberTests(SimpleTestCase):
    def test_reads_episode_label(self):
        self.assertEqual(_episode_number("9 эпизод"), 9)
        self.assertEqual(_episode_number("12 серия"), 12)

    def test_ignores_age_rating_in_audio_track(self):
        self.assertIsNone(_episode_number("MVO | HDrezka Studio (18+)"))


class VavadaTrustCookieTests(SimpleTestCase):
    @override_settings(
        VAVADA_WD_TRUST_COOKIE="",
        VAVADA_WD_APPROVAL_COOKIE="",
    )
    def test_skips_cookie_when_value_is_empty(self):
        driver = Mock()

        self.assertFalse(apply_vavada_trust_cookie(driver))
        driver.execute_cdp_cmd.assert_not_called()

    @override_settings(
        VAVADA_WD_TRUST_COOKIE="test-trust-token",
        VAVADA_WD_APPROVAL_COOKIE="test-approval-token",
    )
    def test_installs_cookie_for_obrut_domain(self):
        driver = Mock()
        driver.execute_cdp_cmd.return_value = {"success": True}

        self.assertTrue(apply_vavada_trust_cookie(driver))
        self.assertEqual(driver.execute_cdp_cmd.call_count, 2)
        driver.execute_cdp_cmd.assert_any_call(
            "Network.setCookie",
            {
                "name": "wd_trust",
                "value": "test-trust-token",
                "domain": ".obrut.show",
                "path": "/",
                "secure": True,
                "httpOnly": True,
                "sameSite": "None",
                "priority": "High",
            },
        )
        driver.execute_cdp_cmd.assert_any_call(
            "Network.setCookie",
            {
                "name": "wd_approval",
                "value": "test-approval-token",
                "domain": ".obrut.show",
                "path": "/",
                "secure": True,
                "httpOnly": True,
                "sameSite": "None",
                "priority": "High",
                "partitionKey": {
                    "topLevelSite": "https://iframe.cloud",
                    "hasCrossSiteAncestor": True,
                },
            },
        )

    @override_settings(
        VAVADA_WD_TRUST_COOKIE="test-trust-token",
        VAVADA_WD_APPROVAL_COOKIE="test-approval-token",
    )
    def test_raises_when_chrome_rejects_cookie(self):
        driver = Mock()
        driver.execute_cdp_cmd.side_effect = [
            {"success": True},
            {"success": False},
        ]

        with self.assertRaises(WebDriverException):
            apply_vavada_trust_cookie(driver)

    @override_settings(
        VAVADA_WD_TRUST_COOKIE="test-trust-token",
        VAVADA_WD_APPROVAL_COOKIE="",
    )
    def test_requires_both_verification_cookies(self):
        driver = Mock()

        with self.assertRaisesMessage(
            WebDriverException,
            "Both VAVADA_WD_TRUST_COOKIE and VAVADA_WD_APPROVAL_COOKIE are required",
        ):
            apply_vavada_trust_cookie(driver)
        driver.execute_cdp_cmd.assert_not_called()
