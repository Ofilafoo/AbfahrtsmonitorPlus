import unittest
from unittest.mock import MagicMock, patch

import ntfy_status


class NtfyStatusTests(unittest.TestCase):
    def test_rejects_unencrypted_endpoint(self):
        with self.assertRaises(ValueError):
            ntfy_status.publish("http://example.org/topic", "", "Test", "Body", False)

    def test_publishes_one_text_message(self):
        response = MagicMock()
        response.status = 200
        with patch("ntfy_status.urllib.request.urlopen") as opener:
            opener.return_value.__enter__.return_value = response
            ntfy_status.publish("https://ntfy.sh/long-random-topic", "", "Pi-Status", "RAM 50 %", False)
            request = opener.call_args.args[0]
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.data, b"RAM 50 %")
        self.assertEqual(request.get_header("Priority"), "3")
        self.assertEqual(opener.call_args.kwargs["timeout"], 12)


if __name__ == "__main__":
    unittest.main()
