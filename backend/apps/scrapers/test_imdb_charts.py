from decimal import Decimal
from unittest.mock import Mock, patch

from django.test import SimpleTestCase

from .imdb_charts import (
    ImdbChartDataError,
    ImdbChartItem,
    fetch_imdb_chart_html,
    items_from_imdb_ids,
    parse_imdb_chart_items,
    refresh_kmax_imdb_top_10_cache,
    sync_imdb_top_10_week_chart,
)


class ImdbChartParserTests(SimpleTestCase):
    def test_parses_imdb_top_items_from_summary_list(self):
        html = """
        <ul>
          <li class="ipc-metadata-list-summary-item">
            <a href="/title/tt22084616/?ref_=sr_i_1">poster</a>
            <a href="/title/tt22084616/?ref_=sr_t_1">
              <h4 class="ipc-title__text">1. Человек-паук: Новый день</h4>
            </a>
            <div class="dli-title-metadata">
              <li>2026</li><li>2h 25m</li><li>PG-13</li>
            </div>
            <span class="ipc-rating-star--rating">8.2</span>
            <span class="ipc-rating-star--voteCount">&nbsp;(105K)</span>
          </li>
          <li class="ipc-metadata-list-summary-item">
            <a href="/title/tt33764258/?ref_=sr_i_2">poster</a>
            <a href="/title/tt33764258/?ref_=sr_t_2">
              <h4 class="ipc-title__text">2. Одиссея</h4>
            </a>
            <div class="dli-title-metadata">
              <li>2026</li><li>2h 53m</li>
            </div>
            <span class="ipc-rating-star--rating">8,5</span>
            <span class="ipc-rating-star--voteCount">&nbsp;(1.2M)</span>
          </li>
        </ul>
        """

        items = parse_imdb_chart_items(html, limit=10)

        self.assertEqual([item.imdb_id for item in items], ["tt22084616", "tt33764258"])
        self.assertEqual(items[0].position, 1)
        self.assertEqual(items[0].title, "Человек-паук: Новый день")
        self.assertEqual(items[0].year, 2026)
        self.assertEqual(items[0].imdb_rating, Decimal("8.2"))
        self.assertEqual(items[0].vote_count, 105000)
        self.assertEqual(items[1].imdb_rating, Decimal("8.5"))
        self.assertEqual(items[1].vote_count, 1200000)

    def test_falls_back_to_unique_title_links(self):
        html = """
        <a href="/title/tt11111111/?ref_=x">one</a>
        <a href="/title/tt11111111/?ref_=y">duplicate</a>
        <a href="/title/tt22222222/?ref_=z">two</a>
        """

        items = parse_imdb_chart_items(html, limit=10)

        self.assertEqual([item.imdb_id for item in items], ["tt11111111", "tt22222222"])
        self.assertEqual([item.position for item in items], [1, 2])

    @patch("apps.scrapers.imdb_charts.fetch_imdb_chart_html_with_browser")
    @patch("apps.scrapers.imdb_charts._build_session")
    def test_fetch_uses_browser_when_requests_gets_waf_challenge(
        self,
        build_session,
        browser_fetch,
    ):
        response = Mock()
        response.text = """
        <script>
          window.awsWafCookieDomainList = ['imdb.com'];
        </script>
        <script src="https://token.awswaf.com/challenge.js"></script>
        <div id="challenge-container"></div>
        """
        response.raise_for_status.return_value = None
        session = Mock()
        session.get.return_value = response
        build_session.return_value = session
        browser_fetch.return_value = '<a href="/title/tt11111111/">one</a>'

        with self.settings(
            IMDB_BROWSER_FALLBACK_ENABLED=True,
            IMDB_BROWSER_WAIT_SECONDS=7,
        ):
            html = fetch_imdb_chart_html("https://www.imdb.com/search/title/", timeout=30)

        self.assertIn("tt11111111", html)
        browser_fetch.assert_called_once_with(
            "https://www.imdb.com/search/title/",
            timeout=30,
            wait_seconds=7,
        )

    def test_builds_items_from_explicit_ids(self):
        items = items_from_imdb_ids(
            [" tt11111111 ", "bad", "tt11111111", "tt22222222"]
        )

        self.assertEqual([item.imdb_id for item in items], ["tt11111111", "tt22222222"])
        self.assertEqual([item.position for item in items], [1, 2])

    @patch("apps.scrapers.imdb_charts.fetch_imdb_chart_html")
    def test_uses_settings_fallback_ids_when_imdb_is_challenged(self, fetch):
        fetch.side_effect = ImdbChartDataError(
            "IMDb returned a challenge page instead of chart HTML"
        )
        with self.settings(
            IMDB_TOP_10_WEEK_FALLBACK_IDS=("tt11111111", "tt22222222")
        ):
            result = sync_imdb_top_10_week_chart(
                dry_run=True,
                push_to_kmax=False,
            )

        self.assertEqual(result["source"], "fallback_ids")
        self.assertEqual(
            [item["imdb_id"] for item in result["items"]],
            ["tt11111111", "tt22222222"],
        )
        self.assertIn("challenge", result["warning"])

    @patch("apps.scrapers.imdb_charts.KMAX_INTERNAL_URL", "https://kmax.example")
    @patch("apps.scrapers.imdb_charts.KMAX_INTERNAL_TOKEN", "secret")
    @patch("apps.scrapers.imdb_charts.requests.post")
    def test_posts_top_ids_to_kmax_internal_cache(self, post):
        response = Mock()
        response.json.return_value = {"cached": True, "count": 2}
        post.return_value = response

        result = refresh_kmax_imdb_top_10_cache(
            [
                ImdbChartItem(imdb_id="tt11111111", position=1, title="One"),
                ImdbChartItem(imdb_id="tt22222222", position=2, title="Two"),
            ]
        )

        self.assertEqual(result, {"cached": True, "count": 2})
        post.assert_called_once()
        _, kwargs = post.call_args
        self.assertEqual(
            kwargs["json"]["imdb_ids"],
            ["tt11111111", "tt22222222"],
        )
        self.assertEqual(
            kwargs["headers"]["Authorization"],
            "Bearer secret",
        )
