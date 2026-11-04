# Copyright (c) 2026, Alaiy and contributors
# For license information, please see license.txt
"""The customer override's frontmatter: max_turns is optional.

Pure parsing, so frappe.throw is patched to raise directly: no site needed.
"""

import unittest
from unittest.mock import patch

import frappe

from alaiy_os_agents.agents.listing import meta

THROW = "alaiy_os_agents.agents.listing.meta.frappe.throw"


def _raise(message, *args, **kwargs):
	raise frappe.ValidationError(message)


class TestOverrideMaxTurns(unittest.TestCase):
	def test_no_frontmatter_keeps_the_default(self):
		parsed, _ = meta.parse_override("## STORE\n\nJust a prompt.")
		self.assertEqual(meta.override_max_turns(parsed), meta.DEFAULT_MAX_TURNS)

	def test_frontmatter_without_max_turns_keeps_the_default(self):
		parsed, _ = meta.parse_override("---\nmodel: some-model\n---\n\nPrompt.")
		self.assertEqual(meta.override_max_turns(parsed), meta.DEFAULT_MAX_TURNS)

	def test_max_turns_is_read_from_frontmatter(self):
		parsed, body = meta.parse_override("---\nmodel: some-model\nmax_turns: 20\n---\n\nPrompt.")
		self.assertEqual(meta.override_max_turns(parsed), 20)
		self.assertEqual(body, "Prompt.")

	def test_a_bad_max_turns_fails_loudly(self):
		for bad in ("twenty", "0", "-3", "12.5"):
			with self.subTest(value=bad), patch(THROW, side_effect=_raise):
				parsed, _ = meta.parse_override(f"---\nmax_turns: {bad}\n---\n\nPrompt.")
				with self.assertRaises(frappe.ValidationError):
					meta.override_max_turns(parsed)


if __name__ == "__main__":
	unittest.main()
