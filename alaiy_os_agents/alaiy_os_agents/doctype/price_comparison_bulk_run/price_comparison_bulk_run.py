# Copyright (c) 2026, Alaiy and contributors
# For license information, please see license.txt
"""One bulk price comparison request: a list of products and the state of
each one's run. Mirrors `Listing Bulk Enrich` — see that doctype's controller
— minus anything to do with a channel or an image stage, neither of which
this agent has.
"""

import frappe
from frappe.model.document import Document
from frappe.utils import cint, now_datetime

from alaiy_os_agents.agents.pricing.bulk import DEFAULT_BATCH_SIZE, enqueue_chunks

RESTARTABLE = ("Draft", "Completed", "Completed with Errors", "Failed", "Cancelled")
IN_FLIGHT = ("Queued", "Running")


class PriceComparisonBulkRun(Document):
	def validate(self):
		self._dedupe_items()
		self.total_items = len(self.items)
		self.batch_size = max(1, cint(self.batch_size) or DEFAULT_BATCH_SIZE)

	def _dedupe_items(self):
		"""The same product twice would mean two runs racing on one result row."""
		seen, kept = set(), []
		for row in self.items:
			if row.product in seen:
				continue
			seen.add(row.product)
			row.idx = len(kept) + 1
			kept.append(row)
		if len(kept) != len(self.items):
			self.set("items", kept)

	@frappe.whitelist()
	def start(self):
		"""Queue every product in this batch. Returns {batch, items, jobs}."""
		self.check_permission("write")
		if self.status in IN_FLIGHT:
			frappe.throw(f"{self.name} is already {self.status.lower()}.")
		if not self.items:
			frappe.throw("Add at least one product before starting.")

		for row in self.items:
			row.status = "Pending"
			row.run = None
			row.error = None
		self.succeeded = self.failed = self.skipped = 0
		self.status = "Queued"
		self.started_at = now_datetime()
		self.ended_at = None
		self.save()

		return {"batch": self.name, "items": len(self.items), "jobs": enqueue_chunks(self.name)}

	@frappe.whitelist()
	def cancel_batch(self):
		"""Stop after the products already in flight."""
		self.check_permission("write")
		if self.status not in IN_FLIGHT:
			frappe.throw(f"{self.name} is not running.")
		self.db_set("status", "Cancelled", commit=True)
		return {"batch": self.name, "status": self.status}

	@frappe.whitelist()
	def retry_failed(self):
		"""Re-queue only the rows that failed or were cancelled, in this same batch."""
		self.check_permission("write")
		if self.status in IN_FLIGHT:
			frappe.throw(f"{self.name} is already {self.status.lower()}.")

		rows = [row for row in self.items if row.status in ("Failed", "Cancelled")]
		if not rows:
			frappe.throw("Nothing to retry — no failed or cancelled products.")

		for row in rows:
			row.status = "Pending"
			row.run = None
			row.error = None
		self.status = "Queued"
		self.ended_at = None
		self.save()

		jobs = enqueue_chunks(self.name, rows=[row.name for row in rows])
		return {"batch": self.name, "items": len(rows), "jobs": jobs}
