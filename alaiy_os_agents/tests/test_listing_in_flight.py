# Copyright (c) 2026, Alaiy and contributors
# For license information, please see license.txt
"""One listing enrichment per product at a time.

What is pinned is which records count as "under way" and that bulk_enrich turns
them away, so both database reads are patched: no site needed.
"""

import datetime
import json
import unittest
import unittest.mock
from unittest.mock import patch

import frappe

from alaiy_os_agents.agents.listing import api, in_flight

GET_ALL = "alaiy_os_agents.agents.listing.in_flight.frappe.get_all"


def _fake_get_all(item_rows=(), live_batches=(), runs=()):
	def get_all(doctype, filters=None, fields=None, pluck=None):
		if doctype == in_flight.ITEM_DOCTYPE:
			return [frappe._dict(row) for row in item_rows]
		if doctype == in_flight.BATCH_DOCTYPE:
			return [name for name in live_batches if name in filters["name"][1]]
		if doctype == in_flight.RUN_DOCTYPE:
			return [frappe._dict(input=json.dumps(run) if isinstance(run, dict) else run) for run in runs]
		raise AssertionError(f"unexpected read of {doctype}")

	return get_all


class TestProductsInFlight(unittest.TestCase):
	def setUp(self):
		clock = patch.object(in_flight, "now_datetime", return_value=datetime.datetime(2026, 9, 29, 12))
		clock.start()
		self.addCleanup(clock.stop)

	def test_a_pending_row_of_a_live_batch_counts(self):
		"""The case that broke: batch one's run did not exist yet when batch two asked."""
		fake = _fake_get_all(item_rows=[{"product": "A", "parent": "B1"}], live_batches=["B1"])
		with patch(GET_ALL, side_effect=fake):
			self.assertEqual(in_flight.products_in_flight(["A", "B"]), {"A"})

	def test_a_row_of_a_batch_that_is_no_longer_live_does_not(self):
		"""Cancelled, finished or stale batches must not lock a product out."""
		fake = _fake_get_all(item_rows=[{"product": "A", "parent": "B1"}], live_batches=[])
		with patch(GET_ALL, side_effect=fake):
			self.assertEqual(in_flight.products_in_flight(["A"]), set())

	def test_an_open_single_product_run_counts(self):
		fake = _fake_get_all(runs=[{"product": "A", "channel": "shopify"}, {"product": "Z"}])
		with patch(GET_ALL, side_effect=fake):
			self.assertEqual(in_flight.products_in_flight(["A", "B"]), {"A"})

	def test_a_run_with_unreadable_input_is_ignored(self):
		fake = _fake_get_all(runs=["not json", "[1, 2]", None])
		with patch(GET_ALL, side_effect=fake):
			self.assertEqual(in_flight.products_in_flight(["A"]), set())

	def test_nothing_asked_reads_nothing(self):
		with patch(GET_ALL, side_effect=AssertionError("read")):
			self.assertEqual(in_flight.products_in_flight([]), set())


def _raise(msg, *args, **kwargs):
	# frappe.throw without a site to msgprint to.
	raise frappe.ValidationError(msg)


class TestBulkEnrichTurnsBusyProductsAway(unittest.TestCase):
	def setUp(self):
		patches = [
			patch.object(api.frappe, "has_permission", return_value=True),
			patch.object(api, "_place", side_effect=lambda product, adapter, *a: {"channel": "shopify"}),
			patch.object(api.frappe, "db", new=unittest.mock.MagicMock()),
			patch.object(api.frappe, "throw", side_effect=_raise),
		]
		for p in patches:
			p.start()
			self.addCleanup(p.stop)

	def test_every_product_busy_refuses_the_batch(self):
		with patch.object(in_flight, "products_in_flight", return_value={"A", "B"}):
			with self.assertRaises(frappe.ValidationError) as caught:
				api.bulk_enrich(["A", "B"], channel=None)
		self.assertIn(in_flight.BUSY_MESSAGE, str(caught.exception))
