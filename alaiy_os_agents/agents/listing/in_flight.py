# Copyright (c) 2026, Alaiy and contributors
# For license information, please see license.txt
"""Which products already have a listing enrichment under way.

One product, one enrichment at a time. Two runs over the same product race on the
single enriched record they both write, and the one that finishes last wins
outright — `save_listing` rebuilds that record's image table from its own run's
output. Found in production (Z058-03285, 2026-09-29): "Enrich" and "Enrich +
images" clicked five seconds apart started two batches over the same products; the
text-only run finished last and left the draft with no photos, and a worn photo
kept afterwards was then approved as the product's entire gallery.

Both ways a run starts are checked, because neither can see the other:

- a batch row that is Pending or Running in a live batch — the batch's run does
  not exist until a worker picks the chunk up, which on a busy `long` queue is well
  after the request that asked for it has returned;
- an OS Agent Run of the listing agent still Queued or Running — the single-product
  path (e.g. The Solist's enrich_product) creates one directly, with no batch.

Anything older than STALE_AFTER_HOURS is ignored, so a worker that died mid-run
cannot lock a product out of enrichment for good.
"""

import json

import frappe
from frappe.utils import add_to_date, now_datetime

from alaiy_os_agents.agents.listing.bulk import BATCH_DOCTYPE, ITEM_DOCTYPE, PENDING_STATES
from alaiy_os_agents.agents.listing.meta import AGENT_ID

RUN_DOCTYPE = "OS Agent Run"
RUN_IN_FLIGHT = ("Queued", "Running")
# Same as Listing Bulk Enrich's own IN_FLIGHT. Not "Generating Images": by then
# every run in the batch has closed, and what is left is the image stage's own work.
BATCH_IN_FLIGHT = ("Queued", "Running")

# Well past any real run (a bulk chunk job is killed after PER_ITEM_TIMEOUT per
# row), with room for a batch that sat on a long queue before it started.
STALE_AFTER_HOURS = 6

BUSY_MESSAGE = "Already being enriched — wait for that run to finish."


def products_in_flight(products):
	"""The subset of `products` that already has an enrichment under way."""
	wanted = set(products or [])
	if not wanted:
		return set()

	since = add_to_date(now_datetime(), hours=-STALE_AFTER_HOURS)
	return _in_live_batches(wanted, since) | _in_open_runs(wanted, since)


def _in_live_batches(wanted, since):
	rows = frappe.get_all(
		ITEM_DOCTYPE,
		filters={"product": ("in", list(wanted)), "status": ("in", PENDING_STATES)},
		fields=["product", "parent"],
	)
	if not rows:
		return set()

	live = set(
		frappe.get_all(
			BATCH_DOCTYPE,
			filters={
				"name": ("in", list({row.parent for row in rows})),
				"status": ("in", BATCH_IN_FLIGHT),
				"creation": (">", since),
			},
			pluck="name",
		)
	)
	return {row.product for row in rows if row.parent in live}


def _in_open_runs(wanted, since):
	# The product lives only inside the run's JSON input, so the few open runs of
	# this agent are read and parsed here rather than matched with LIKE.
	runs = frappe.get_all(
		RUN_DOCTYPE,
		filters={"agent": AGENT_ID, "status": ("in", RUN_IN_FLIGHT), "creation": (">", since)},
		fields=["input"],
	)
	busy = set()
	for run in runs:
		try:
			product = json.loads(run.input or "{}").get("product")
		except (TypeError, ValueError, AttributeError):
			continue
		if product in wanted:
			busy.add(product)
	return busy
