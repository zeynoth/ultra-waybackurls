import unittest
from unittest.mock import patch, Mock
from ultrawayback import WaybackScraper
import sqlite3
import os
import tempfile

class TestWaybackScraper(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test.db")
        self.scraper = WaybackScraper(
            hosts=["example.com"],
            with_subs=False,
            output_dir=self.temp_dir,
            cache_dir=self.temp_dir,
            db_path=self.db_path,
            max_threads=1,
            max_pages=1,
            delay=0.1
        )

    def tearDown(self):
        self.scraper.session.close()
        if os.path.exists(self.db_path):
            os.remove(self.db_path)
        os.rmdir(self.temp_dir)

    def test_setup_database(self):
        """تست ایجاد دیتابیس و ستون‌ها"""
        with sqlite3.connect(self.db_path) as conn:
            c = conn.cursor()
            c.execute("PRAGMA table_info(urls)")
            columns = [col[1] for col in c.fetchall()]
            self.assertIn("keywords", columns)
            self.assertIn("url", columns)
            self.assertIn("host", columns)

    @patch("requests.Session.get")
    def test_fetch_wayback(self, mock_get):
        """تست دریافت داده از Wayback Machine"""
        mock_response = Mock()
        mock_response.json.return_value = [
            ["original", "timestamp"],
            ["http://example.com/test.html", "20230101120000"],
            ["http://example.com/page.php", "20230201120000"]
        ]
        mock_response.raise_for_status.return_value = None
        mock_get.return_value = mock_response

        result = self.scraper.fetch_wayback("example.com", 1)
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0], ("http://example.com/test.html", "20230101120000"))

    def test_filter_urls(self):
        """تست فیلتر کردن URLها"""
        urls = [
            ("http://example.com/test.html", "20230101120000"),
            ("http://example.com/style.css", "20230101120000"),
            ("http://sub.example.com/page.php", "20230201120000")
        ]
        filtered = self.scraper.filter_urls(urls, "example.com", "wayback")
        self.assertEqual(len(filtered), 1)
        self.assertEqual(filtered[0], "http://example.com/test.html")

    @patch("requests.Session.get")
    def test_extract_keywords_and_links(self, mock_get):
        """تست استخراج کلمات کلیدی و لینک‌ها"""
        mock_response = Mock()
        mock_response.text = """
        <html>
            <body>
                <p>Test content for scraping</p>
                <a href="/internal">Internal Link</a>
                <a href="http://example.com/external">External Link</a>
            </body>
        </html>
        """
        mock_response.raise_for_status.return_value = None
        mock_get.return_value = mock_response

        keywords, links = self.scraper.extract_keywords_and_links("http://example.com/test.html")
        self.assertIn("test", keywords)
        self.assertIn("http://example.com/internal", links)
        self.assertIn("http://example.com/external", links)

    @patch("requests.Session.head")
    def test_get_content_type_and_status(self, mock_head):
        """تست دریافت Content-Type و Status Code"""
        mock_response = Mock()
        mock_response.headers = {"Content-Type": "text/html"}
        mock_response.status_code = 200
        mock_response.raise_for_status.return_value = None
        mock_head.return_value = mock_response

        content_type, status_code = self.scraper.get_content_type_and_status("http://example.com/test.html")
        self.assertEqual(content_type, "text/html")
        self.assertEqual(status_code, 200)

if __name__ == "__main__":
    unittest.main()
