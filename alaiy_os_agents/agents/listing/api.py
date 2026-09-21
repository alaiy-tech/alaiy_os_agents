# Copyright (c) 2026, Alaiy and contributors
# For license information, please see license.txt
"""Starting a bulk enrichment. The one place a batch is built.

`bulk.py` owns the fan-out; this owns getting a list of identifiers into a batch it
can fan out. Everything that can go wrong *before* a row exists is handled here — a
product that is on no channel, one the channel cannot register, a list where nothing
resolves — because a batch row is keyed on a product that must already be enrichable.

## Why one channel for the whole batch

`bulk.py` reads the channel's spec, its enriched doctype and its image step off one
adapter. Resolving a channel per row would mean a batch whose skip-check and image
wait differ line by line, and `Listing Bulk Enrich` has one `channel` field for
exactly that reason. Where the caller does not name one it is taken from the first
product that resolves, and every product not on that channel is reported in `errors`
rather than quietly starting a second batch.

## Why unresolvable products are a result, not an exception

Each resolve gets its own savepoint, so a half-built listing record is rolled back to
just before itself rather than taking every record created so far with it, and the
product is reported in `errors` instead. A sheet where twelve of fifty identifiers
are unknown should enrich thirty-eight and name the twelve: refusing the whole batch
is what pushes a caller — a person or a model — into inventing the missing rows.
"""

import json

import frappe
from frappe.utils import cint

from alaiy_os_agents.agents.listing import channels
from alaiy_os_agents.agents.listing.bulk import BATCH_DOCTYPE, DEFAULT_BATCH_SIZE


def _flag(value):
	"""A checkbox from whatever a REST caller sent — "0" is false, not a true string."""
	if isinstance(value, str):
		return 1 if value.strip().lower() in ("1", "true", "yes", "on") else 0
	return 1 if value else 0


@frappe.whitelist()
def bulk_enrich(
	products,
	channel=None,
	register=0,
	notes=None,
	batch_size=None,
	skip_enriched=0,
	**toggles,
):
	"""
	Enrich many products at once. Returns
	`{batch, items, jobs, channel, resolved, registered, errors}`.

	`products` are channel identifiers (a list, or a JSON array over REST) — a seller
	SKU on Amazon, an item code on Shopify. `channel` is optional where the products
	say which one they belong to; see the module docstring.

	`register` puts a catalogue product that is not on the channel yet onto it, via the
	adapter's own `register` handler, so a sheet of products sourced from a supplier
	can be enriched without a separate pass. Off by default: registering writes, and a
	caller asking to enrich should not create listing records as a side effect.

	Extra keyword arguments are the agent's per-request toggles — whatever
	`build_agent_meta` reports in `input_options`, e.g. `translate_images` or
	`white_bg_images` — so this signature does not name a tool either.

	The work happens on workers: open the returned batch to follow it.
	"""
	if not frappe.has_permission("OS Agent Run", "create"):
		frappe.throw("Not permitted.", frappe.PermissionError)
	if isinstance(products, str):
		products = json.loads(products)
	if not products:
		frappe.throw("bulk_enrich needs at least one product.")

	adapter = channels.get(channel) if channel else None
	resolved, registered, errors = [], [], {}

	for n, product in enumerate(products):
		savepoint = f"listing_bulk_{n}"
		frappe.db.savepoint(savepoint)
		try:
			adapter = _place(product, adapter, bool(cint(register)), registered)
		except Exception as e:
			frappe.db.rollback(save_point=savepoint)
			errors[product] = str(e)
			continue
		# Two identifiers resolving to one record would run the agent twice over the
		# same product and race on the single enrichment it writes.
		if product not in resolved:
			resolved.append(product)

	if not resolved:
		# An empty batch would sit in Draft forever, saying nothing about why.
		frappe.throw(
			"None of these products could be enriched: "
			+ "; ".join(f"{product}: {error}" for product, error in errors.items())
		)

	batch = frappe.new_doc(BATCH_DOCTYPE)
	batch.channel = adapter["channel"]
	batch.batch_size = cint(batch_size) or DEFAULT_BATCH_SIZE
	batch.skip_enriched = _flag(skip_enriched)
	batch.notes = notes
	for fieldname, value in toggles.items():
		# form_dict carries more than this signature declares (`cmd`), so only
		# arguments that are actually fields on the batch are honoured.
		if batch.meta.get_field(fieldname):
			batch.set(fieldname, _flag(value))
	for product in resolved:
		batch.append("items", {"product": product})
	batch.insert()

	return {
		**batch.start(),
		"channel": batch.channel,
		"resolved": resolved,
		"registered": registered,
		"errors": errors,
	}


def _place(product, adapter, register, registered):
	"""The adapter this product will be enriched on, registering it first if asked.

	Returns the adapter, which pins the batch's channel for every product after the
	first. Raises for a product that is not on it — caught by the caller and reported
	per product, never for the batch.
	"""
	if adapter is None:
		# No channel named and none pinned yet: let the identifier say which one it
		# is on. `resolve` throws when it is on several, which is a real ambiguity
		# only the caller can settle.
		return channels.resolve(product)

	source = adapter.get("source_doctype")
	if source and frappe.db.exists(source, product):
		return adapter

	if not register:
		frappe.throw(
			f"'{product}' is not on the {adapter['label']} channel. Pass register=1 to "
			"put it there first, if it is in the catalogue."
		)

	channels.require(adapter, "register")(product=product)
	registered.append(product)
	return adapter
