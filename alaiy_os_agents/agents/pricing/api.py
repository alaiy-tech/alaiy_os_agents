# Copyright (c) 2026, Alaiy and contributors
# For license information, please see license.txt
"""Starting a bulk price comparison. The one place a batch is built.

Simpler than `agents/listing/api.py::bulk_enrich`: there is no channel to
resolve and nothing to register — every product is just an `Item` that either
exists in the catalogue or does not.
"""

import json

import frappe
from frappe.utils import cint

from alaiy_os_agents.agents.pricing.bulk import BATCH_DOCTYPE, DEFAULT_BATCH_SIZE


@frappe.whitelist()
def bulk_compare(products, notes=None, batch_size=None):
	"""
	Compare prices for many products at once. Returns
	`{batch, items, jobs, resolved, errors}`.

	`products` is a list of item codes (or a JSON array, as REST callers
	send it). Each one is validated against the catalogue with its own
	savepoint, so a handful of typos in a fifty-row selection reduces the
	batch rather than refusing it outright — same discipline as
	`agents/listing/api.py::bulk_enrich`.

	The work happens on workers: open the returned batch to follow it.
	"""
	if not frappe.has_permission("OS Agent Run", "create"):
		frappe.throw("Not permitted.", frappe.PermissionError)
	if isinstance(products, str):
		products = json.loads(products)
	if not products:
		frappe.throw("bulk_compare needs at least one product.")

	resolved, errors = [], {}
	for n, product in enumerate(products):
		savepoint = f"pricing_bulk_{n}"
		frappe.db.savepoint(savepoint)
		try:
			if not frappe.db.exists("Item", product):
				frappe.throw(f"'{product}' is not a known item code.")
		except Exception as e:
			frappe.db.rollback(save_point=savepoint)
			errors[product] = str(e)
			continue
		if product not in resolved:
			resolved.append(product)

	if not resolved:
		frappe.throw(
			"None of these products could be compared: "
			+ "; ".join(f"{product}: {error}" for product, error in errors.items())
		)

	batch = frappe.new_doc(BATCH_DOCTYPE)
	batch.batch_size = cint(batch_size) or DEFAULT_BATCH_SIZE
	batch.notes = notes
	for product in resolved:
		batch.append("items", {"product": product})
	batch.insert()

	return {**batch.start(), "resolved": resolved, "errors": errors}
