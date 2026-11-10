"""
Regression tests for Bitbucket client UTF-8 handling.

Bug: file content and diffs fetched from Bitbucket were being mangled
because `requests` falls back to ISO-8859-1 when a `text/*` response has
no explicit charset (per old RFC 2616 behavior) -- exactly what
Bitbucket's raw diff/src endpoints send. Left as `response.text`, that
silently turns em dashes, curly quotes, and accented names into mojibake
before the content ever reaches the LLM, producing false "corrupted
file" findings.
"""
from unittest import TestCase
from unittest.mock import patch, MagicMock

from pr_reviewer.clients.bitbucket import BitbucketClient, _decode_utf8

# Text containing an em dash, an ellipsis, a copyright sign, and an
# accented name -- the characters called out in the bug report.
SAMPLE_TEXT = "released — with more…\n© Zoë Corp\n"
UTF8_BYTES = SAMPLE_TEXT.encode("utf-8")


def _fake_response(content: bytes, content_type: str = "text/plain"):
    response = MagicMock()
    response.status_code = 200
    response.content = content
    response.headers = {"content-type": content_type}
    # Mirror real `requests` behavior: encoding is None when charset is
    # unspecified for a text/* content type, and .text falls back to
    # ISO-8859-1 in that case -- this is the bug we're regression-testing.
    response.encoding = None
    response.apparent_encoding = "utf-8"
    response.text = content.decode("ISO-8859-1")
    response.raise_for_status = MagicMock()
    return response


class TestDecodeUtf8Helper(TestCase):
    def test_decodes_valid_utf8_bytes_directly(self):
        response = _fake_response(UTF8_BYTES)
        self.assertEqual(_decode_utf8(response), SAMPLE_TEXT)

    def test_falls_back_to_apparent_encoding_on_invalid_utf8(self):
        latin1_only_bytes = "café".encode("latin-1")
        response = _fake_response(latin1_only_bytes)
        response.apparent_encoding = "latin-1"
        self.assertEqual(_decode_utf8(response), "café")


class TestBitbucketClientUtf8Content(TestCase):
    def setUp(self):
        self.client = BitbucketClient(
            workspace="ws",
            repo_slug="repo",
            api_token="token",
            email="user@example.com",
        )

    @patch("pr_reviewer.clients.bitbucket.requests.get")
    def test_get_pull_request_diff_preserves_utf8(self, mock_get):
        mock_get.return_value = _fake_response(UTF8_BYTES)

        diff = self.client.get_pull_request_diff(1)

        self.assertEqual(diff, SAMPLE_TEXT)
        self.assertNotIn("â", diff)

    @patch("pr_reviewer.clients.bitbucket.requests.get")
    def test_get_file_content_preserves_utf8(self, mock_get):
        mock_get.return_value = _fake_response(UTF8_BYTES)

        content = self.client.get_file_content("src/file.py", ref="main")

        self.assertEqual(content, SAMPLE_TEXT)
        self.assertNotIn("â", content)
        self.assertNotIn("Â©", content)
