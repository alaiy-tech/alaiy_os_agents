# Copyright (c) 2026, Alaiy and contributors
# For license information, please see license.txt
"""The listing agent's `view_image` sees images the site holds, not just public ones.

Produced images live in the site's private S3 bucket, or as site-relative Files, and
neither answers a plain GET. Asked to look at one, the model would get a 403 or a
"No scheme supplied" and lose the photo. The store reads them with the site's own
access; anything else is downloaded as before. The store is tested in `alaiy_os`.
"""

import unittest
from unittest.mock import patch

from alaiy_os_agents.agents.listing import tools

STORED = "https://bucket.s3.ap-south-1.amazonaws.com/images/generated/2026/10/listing-x.png"


class TestViewImage(unittest.TestCase):
	def test_an_image_the_site_holds_is_read_not_downloaded(self):
		with patch("alaiy_os.image_store.read", return_value=(b"\x89PNG", "image/png")), \
				patch("requests.get") as get:
			result = tools.view_image(STORED)
		get.assert_not_called()
		block = result["_content_blocks"][1]
		self.assertEqual(block["source"]["media_type"], "image/png")

	def test_anything_else_is_downloaded(self):
		response = type("R", (), {"content": b"jpg", "headers": {"Content-Type": "image/jpeg"},
		                           "raise_for_status": lambda self: None})()
		with patch("alaiy_os.image_store.read", return_value=None), patch("requests.get", return_value=response) as get:
			result = tools.view_image("https://cdn.supplier.example/a.jpg")
		get.assert_called_once()
		self.assertEqual(result["_content_blocks"][1]["source"]["media_type"], "image/jpeg")


if __name__ == "__main__":
	unittest.main()
