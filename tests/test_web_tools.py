from __future__ import annotations

import base64
import unittest
from unittest.mock import patch

import httpx

from wechat_agent import web_tools
from wechat_agent.web_tools import FetchedPage, WebToolError


class WebToolTests(unittest.TestCase):
    def test_normalise_url_rejects_unsafe_urls(self) -> None:
        for url in (
            "file:///etc/passwd",
            "http://user:pass@example.com/",
            "http://localhost/admin",
            "http://example.com:8080/",
            "not a url",
        ):
            with self.subTest(url=url), self.assertRaises(WebToolError):
                web_tools._normalise_url(url)

    def test_public_url_validation_blocks_private_literal_and_dns(self) -> None:
        with self.assertRaises(WebToolError):
            web_tools._validate_public_url("http://127.0.0.1/private")
        with self.assertRaises(WebToolError):
            web_tools._validate_public_url("http://[::1]/private")
        private_dns = [(2, 1, 6, "", ("10.0.0.8", 80))]
        with patch.object(web_tools.socket, "getaddrinfo", return_value=private_dns):
            with self.assertRaises(WebToolError):
                web_tools._validate_public_url("http://internal.example/")

    def test_proxy_fake_ip_is_only_allowed_for_domain_resolution(self) -> None:
        fake_dns = [(2, 1, 6, "", ("198.18.4.108", 443))]
        with patch.object(web_tools.socket, "getaddrinfo", return_value=fake_dns):
            self.assertEqual(
                web_tools._validate_public_url("https://www.python.org/"),
                "https://www.python.org/",
            )
        with self.assertRaises(WebToolError):
            web_tools._validate_public_url("https://198.18.4.108/")

    def test_fetch_revalidates_redirect_target(self) -> None:
        checked: list[str] = []

        def validate(url: str) -> str:
            checked.append(url)
            if "127.0.0.1" in url:
                raise WebToolError("blocked redirect")
            return web_tools._normalise_url(url)

        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(302, headers={"location": "http://127.0.0.1/secret"})

        with patch.object(web_tools, "_validate_public_url", side_effect=validate):
            with httpx.Client(transport=httpx.MockTransport(handler)) as client:
                with self.assertRaisesRegex(WebToolError, "blocked redirect"):
                    web_tools._fetch_text(client, "https://example.com/start", accepted_types=("text/html",))
        self.assertEqual(checked, ["https://example.com/start", "http://127.0.0.1/secret"])

    def test_fetch_rejects_large_and_non_text_responses(self) -> None:
        def validate(url: str) -> str:
            return web_tools._normalise_url(url)

        def large_handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"content-type": "text/html", "content-length": str(web_tools.MAX_RESPONSE_BYTES + 1)},
                content=b"small",
            )

        def image_handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, headers={"content-type": "image/png"}, content=b"png")

        with patch.object(web_tools, "_validate_public_url", side_effect=validate):
            with httpx.Client(transport=httpx.MockTransport(large_handler)) as client:
                with self.assertRaisesRegex(WebToolError, "size limit"):
                    web_tools._fetch_text(client, "https://example.com/", accepted_types=("text/html",))
            with httpx.Client(transport=httpx.MockTransport(image_handler)) as client:
                with self.assertRaisesRegex(WebToolError, "content type"):
                    web_tools._fetch_text(client, "https://example.com/image", accepted_types=("text/html",))

    def test_bing_parser_filters_and_unwraps_links(self) -> None:
        target = "https://docs.example.com/article?id=1"
        encoded = base64.urlsafe_b64encode(target.encode()).decode().rstrip("=")
        html = f"""
        <ol>
          <li class="b_algo"><h2><a href="{target}">Direct result</a></h2>
            <div class="b_caption"><p>Useful direct snippet.</p></div></li>
          <li class="b_algo"><h2><a href="https://www.bing.com/ck/a?u=a1{encoded}">Wrapped</a></h2></li>
          <li class="b_algo"><h2><a href="http://127.0.0.1/private">Private</a></h2></li>
        </ol>
        """
        self.assertEqual(
            web_tools._parse_bing_results(html, 10),
            [{"title": "Direct result", "url": target, "snippet": "Useful direct snippet."}],
        )

    def test_html_cleaning_removes_navigation_scripts_and_ads(self) -> None:
        cleaned = web_tools._clean_html(
            """
            <html><head><title> Example title </title><meta name="description" content="Useful"></head>
            <body><header>Header</header><main><h1>Article</h1><p>Text.</p>
            <div class="advertisement"><p>Buy now</p></div><script>steal()</script>
            <ul><li>Point one</li></ul></main><footer>Footer</footer></body></html>
            """,
            "text/html",
        )
        self.assertEqual(cleaned["title"], "Example title")
        self.assertIn("# Article", cleaned["content"])
        self.assertIn("- Point one", cleaned["content"])
        self.assertNotIn("Buy now", cleaned["content"])
        self.assertNotIn("Header", cleaned["content"])

    def test_web_search_returns_untrusted_parsed_results(self) -> None:
        def fake_fetch(_client: httpx.Client, url: str, *, accepted_types: tuple[str, ...]) -> FetchedPage:
            self.assertTrue(url.startswith("https://www.bing.com/search?"))
            return FetchedPage(
                url,
                url,
                200,
                "text/html",
                '<li class="b_algo"><h2><a href="https://example.com/a">A</a></h2><p>Snippet</p></li>',
            )

        with patch.object(web_tools, "_fetch_text", side_effect=fake_fetch):
            result = web_tools.web_search("python sync", count=3)
        self.assertTrue(result["content_is_untrusted"])
        self.assertEqual(result["result_count"], 1)
        self.assertEqual(result["results"][0]["url"], "https://example.com/a")

    def test_fetch_pages_keeps_partial_success(self) -> None:
        def fake_fetch(_client: httpx.Client, url: str, *, accepted_types: tuple[str, ...]) -> FetchedPage:
            if "broken" in url:
                raise WebToolError("request timed out")
            return FetchedPage(url, url, 200, "text/html", "<article><p>Clean text.</p></article>")

        with patch.object(web_tools, "_fetch_text", side_effect=fake_fetch):
            result = web_tools.fetch_web_pages(["https://example.com/", "https://broken.example/"])
        self.assertEqual(result["success_count"], 1)
        self.assertEqual(result["error_count"], 1)
        self.assertEqual(result["pages"][0]["content"], "Clean text.")
        self.assertEqual(result["pages"][1]["error"], "request timed out")


if __name__ == "__main__":
    unittest.main()
