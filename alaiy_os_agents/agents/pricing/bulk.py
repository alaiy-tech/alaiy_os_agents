# Copyright (c) 2026, Alaiy and contributors
# For license information, please see license.txt
"""Bulk price comparison: many products, still one OS Agent Run each.

Modeled on `agents/listing/bulk.py` — see that module's docstring for the
chunking rationale, which applies unchanged here. The one real difference is
that this agent has no channel and no image stage, so there is no
`_already_enriched`/`_images_pending` bookkeeping and no "Generating Images"
parked status: a batch closes the moment every row's run has finished.
"""

import json

import frappe
from frappe.utils import now_datetime

BATCH_DOCTYPE = "Price Comparison Bulk Run"
ITEM_DOCTYPE = "Price Comparison Bulk Run Item"

DEFAULT_BATCH_SIZE = 5
# One product: the agent's LLM turns plus a couple of page fetches.
PER_ITEM_TIMEOUT = 600

PENDING_STATES = ("Pending", "Running")

PROGRESS_EVENT = "price_comparison_bulk_run_progress"
DONE_EVENT = "price_comparison_bulk_run_done"


def enqueue_chunks(batch, rows=None):
	"""Split `rows` (default: every Pending row) into chunks, one worker job each.

	Returns the number of jobs enqueued.
	"""
	doc = frappe.get_doc(BATCH_DOCTYPE, batch)
	names = rows if rows is not None else [row.name for row in doc.items if row.status == "Pending"]
	size = max(1, int(doc.batch_size or DEFAULT_BATCH_SIZE))
	chunks = [names[i : i + size] for i in range(0, len(names), size)]

	for n, chunk in enumerate(chunks):
		frappe.enqueue(
			"alaiy_os_agents.agents.pricing.bulk.run_chunk",
			queue="long",
			timeout=len(chunk) * PER_ITEM_TIMEOUT,
			job_id=f"pricing-bulk-run::{batch}::{n}::{chunk[0]}",
			batch=batch,
			rows=chunk,
			enqueue_after_commit=True,
		)
	return len(chunks)


def run_chunk(batch, rows):
	"""Worker entry point: compare this chunk's products, one at a time."""
	try:
		agent, options = _request(batch)
	except Exception as e:
		frappe.log_error(title=f"Bulk price comparison {batch}: could not start")
		for row in rows:
			_set_row(row, {"status": "Failed", "error": _error_summary(str(e))})
		_finalize(batch)
		return

	_mark_running(batch)

	for row in rows:
		if frappe.db.get_value(BATCH_DOCTYPE, batch, "status") == "Cancelled":
			_set_row(row, {"status": "Cancelled", "error": "Batch cancelled."})
			continue
		_run_row(batch, row, agent, options)

	_finalize(batch)


def _run_row(batch, row, agent, options):
	product = frappe.db.get_value(ITEM_DOCTYPE, row, "product")

	try:
		run = _create_run(agent, {"product": product, **options})
		_set_row(row, {"status": "Running", "run": run, "error": None})

		from alaiy_os.engine.executor import run_queued

		run_queued(run)

		status, error = frappe.db.get_value("OS Agent Run", run, ["status", "error"])
		row_status = "Success" if status == "Success" else "Failed"
		_set_row(row, {"status": row_status, "error": _error_summary(error)})
	except Exception as e:
		frappe.db.rollback()
		frappe.log_error(title=f"Bulk price comparison {batch}: {product} failed")
		row_status = "Failed"
		_set_row(row, {"status": row_status, "error": _error_summary(str(e))})

	_publish(batch, row, product, row_status)


def _create_run(agent, payload):
	"""An OS Agent Run for one product, queued but not enqueued — this worker runs it."""
	enabled = frappe.db.get_value("OS Agent Registry", agent, "is_enabled")
	if enabled is None:
		frappe.throw(f"Agent {agent} does not exist.")
	if not enabled:
		frappe.throw(f"Agent {agent} is disabled.")

	run = frappe.get_doc(
		{
			"doctype": "OS Agent Run",
			"agent": agent,
			"trigger_type": "API",
			"status": "Queued",
			"input": json.dumps(payload, indent=1),
		}
	).insert(ignore_permissions=True)
	frappe.db.commit()
	return run.name


def _request(batch):
	"""What every product in `batch` shares: the agent id and any notes."""
	from alaiy_os_agents.agents.pricing.meta import build_agent_meta

	meta = build_agent_meta()
	doc = frappe.get_doc(BATCH_DOCTYPE, batch)

	options = {}
	if doc.notes:
		options["notes"] = doc.notes
	return meta["agent_id"], options


def _set_row(row, values):
	frappe.db.set_value(ITEM_DOCTYPE, row, values, update_modified=False)
	frappe.db.commit()


def _mark_running(batch):
	if frappe.db.get_value(BATCH_DOCTYPE, batch, "status") == "Queued":
		frappe.db.set_value(BATCH_DOCTYPE, batch, "status", "Running", update_modified=False)
		frappe.db.commit()


def _finalize(batch):
	"""Close the batch once no row is left to run. No image stage to wait on."""
	statuses = [
		row.status
		for row in frappe.get_all(
			ITEM_DOCTYPE,
			filters={"parent": batch, "parenttype": BATCH_DOCTYPE},
			fields=["status"],
		)
	]
	if any(status in PENDING_STATES for status in statuses):
		return

	failed = statuses.count("Failed")
	succeeded = statuses.count("Success")
	skipped = statuses.count("Skipped")

	if frappe.db.get_value(BATCH_DOCTYPE, batch, "status") == "Cancelled":
		status = "Cancelled"
	elif not failed:
		status = "Completed"
	elif not succeeded:
		status = "Failed"
	else:
		status = "Completed with Errors"

	frappe.db.set_value(
		BATCH_DOCTYPE,
		batch,
		{
			"status": status,
			"succeeded": succeeded,
			"failed": failed,
			"skipped": skipped,
			"ended_at": now_datetime(),
		},
		update_modified=False,
	)
	frappe.db.commit()
	frappe.publish_realtime(
		DONE_EVENT,
		{"batch": batch, "status": status, "succeeded": succeeded, "failed": failed, "skipped": skipped},
		doctype=BATCH_DOCTYPE,
		docname=batch,
	)


def _publish(batch, row, product, status):
	"""Per-product progress, so a UI can follow a batch without polling."""
	frappe.publish_realtime(
		PROGRESS_EVENT,
		{"batch": batch, "row": row, "product": product, "status": status},
		doctype=BATCH_DOCTYPE,
		docname=batch,
	)


def _error_summary(text):
	if not text:
		return None
	lines = [line.strip() for line in text.strip().splitlines() if line.strip()]
	return lines[-1][:500] if lines else None
