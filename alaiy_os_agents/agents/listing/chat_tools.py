# Copyright (c) 2026, Alaiy and contributors
# For license information, please see license.txt
"""
`listing_bulk_enrich_from_csv` — enrich a spreadsheet of products, as an Ask Alaiy tool.

Wired through core's tool seam:

    chat_tool_sources = ["alaiy_os_agents.agents.listing.chat_tools.source"]

## Why this exists

The chat model already had `run_agent`, which enriches exactly one product. Handed a
fifty-row sheet it had no correct move, and the failure mode was not an error — it
read the ten rows its file-reader previewed, invented the other forty by cycling
those ten, and wrote a file it described as complete. Nothing in the transcript said
otherwise: every tool call succeeded.

So the fix is not a bigger preview. It is that **the rows never pass through the
model at all**. This tool takes a `file_url`, reads the whole file here, and hands
the identifiers to `api.bulk_enrich`. The model chooses the file and the column; it
never transcribes a row, so it cannot fabricate one.

## Why it returns a batch and not a file

`bulk.py` fans fifty products out over background jobs with a 900s per-row timeout —
that is hours, and a chat turn cannot hold it. The tool returns the batch name and
its link, and the batch page reports per-row status as the chunks land. A tool that
blocked until it could hand back a finished CSV would time out and pin a worker.

## Why unresolvable rows are an answer, not an exception

`api.bulk_enrich` resolves each identifier under its own savepoint and reports the
ones that failed in `errors`, enriching the rest. That contract is exactly right
here: a sheet where twelve of fifty IDs are unknown should enrich thirty-eight and
name the twelve. The alternative — refusing the batch — is what pushes a model into
inventing the missing rows, which is the bug this module exists to close.
"""

import csv
import io

import frappe

from alaiy_os_agents.agents.listing import api
from alaiy_os_agents.agents.listing.meta import AGENT_ID

TOOL = "listing_bulk_enrich_from_csv"

# One run per row, each up to bulk.PER_ITEM_TIMEOUT. A sheet past this is a bulk
# import, not a chat action, and the throw says so rather than queueing for a day.
MAX_ROWS = 500

# The file is read into memory here, as the parse needs the whole thing. Well above
# any realistic product sheet — a bigger one is the desk import, not this.
MAX_BYTES = 5 * 1024 * 1024

DESCRIPTION = """\
Enrich every product in an uploaded spreadsheet with the listing agent — titles, \
bullet points, descriptions and specifications — as one background batch.

Use this for ANY request to enrich, enhance or fill in listing data for a file the \
user attached, however many rows it has. Pass the attachment's `file_url` and the \
name of the column holding the product identifiers.

This tool reads the file itself, in full. Do NOT read the sheet first and do NOT \
pass rows: a file-reading tool shows you only a preview, so any row you copy from \
one beyond that preview would be invented.

Returns the batch name, a link to watch it, and `errors` naming any identifier that \
could not be resolved. Report those honestly — they are products that were NOT \
enriched.

The work runs on background workers and is NOT finished when this returns. Report \
it as started, never as complete, and never describe listing content you have not \
read back from the batch.

For a single product, use `run_agent` with the listing agent instead.\
"""

INPUT_SCHEMA = {
	"type": "object",
	"properties": {
		# Single types only, no unions: the default model is reached through LiteLLM
		# in front of Gemini, whose function declarations are an OpenAPI subset that
		# rejects a `"type"` list outright. Same note as meta.py's tool schemas.
		"file_url": {
			"type": "string",
			"description": "The attachment's file_url, exactly as the attachment reports it.",
		},
		"column": {
			"type": "string",
			"description": (
				"Header of the column holding the product identifiers, e.g. 'Variant id'. "
				"Omit to be told which columns the file has. Matched ignoring "
				"surrounding spaces and case."
			),
		},
		"channel": {
			"type": "string",
			"description": (
				"Which sales channel to enrich for. Omit unless the products are "
				"listed on more than one, in which case this tool will ask."
			),
		},
		"register": {
			"type": "boolean",
			"description": (
				"Put products that are in the catalogue but not yet on the channel "
				"onto it first. Defaults to false. This WRITES a listing record for "
				"each one, so only pass true when the user has asked for it."
			),
		},
		"notes": {
			"type": "string",
			"description": "Free-text guidance passed to the agent for every row in the batch.",
		},
		"skip_enriched": {
			"type": "boolean",
			"description": "Skip products that already have an enriched listing. Defaults to false.",
		},
	},
	"required": ["file_url"],
}


def source():
	"""This app's contribution to the chat tool surface, or None.

	None rather than an empty tool list when this is unavailable: the seam documents
	None as how an app opts out per user, and a tool that is present but always
	refuses teaches the model to keep calling it.

	Two gates, and the first is easy to get wrong. A `chat_tool_sources` contribution
	is NOT gated on the agent being enabled — `chat/tools.py:_surface` takes tenant
	tools straight through `_provided`, and nothing downstream checks `is_enabled` for
	them. So without this check a site with the listing agent switched off would still
	be offered a bulk enrich, which `bulk._create_run` would then refuse once per row.
	Disabling an agent is meant to be a complete off switch (see registry.py), and this
	is what keeps it one here.

	This used to say that only `_pack_tools` filtered on `is_enabled`. That surface no
	longer exists, which makes this check the *only* thing standing between a disabled
	listing agent and a working bulk-enrich tool rather than the odd one out.

	The second is the user's own permission to start runs. `api.bulk_enrich` checks
	it again — this is the polite half, that one is the load-bearing half.
	"""
	if not frappe.db.get_value("OS Agent Registry", AGENT_ID, "is_enabled"):
		return None
	if not frappe.has_permission("OS Agent Run", "create"):
		return None

	return {
		"name": "listing_agent",
		"tools": [
			{
				"name": TOOL,
				"description": DESCRIPTION,
				"input_schema": INPUT_SCHEMA,
				"run": run,
			}
		],
	}


def run(arguments=None):
	"""Read the file, resolve the column, start the batch. Raises messages the model
	can act on — a missing column answers with the columns the file actually has."""
	args = arguments or {}

	file_url = (args.get("file_url") or "").strip()
	if not file_url:
		frappe.throw("file_url is required — pass the attachment's file_url.")

	headers, rows = _read_csv(file_url)

	column = _resolve_column(headers, args.get("column"))
	identifiers = _identifiers(rows, column)

	if not identifiers:
		frappe.throw(f"Every value in column '{column}' is empty — there is nothing to enrich.")
	if len(identifiers) > MAX_ROWS:
		frappe.throw(
			f"That file has {len(identifiers)} products, more than the {MAX_ROWS} one batch "
			"takes. Split it, or start the batch from the desk."
		)

	result = api.bulk_enrich(
		identifiers,
		channel=args.get("channel"),
		register=bool(args.get("register")),
		notes=args.get("notes"),
		skip_enriched=bool(args.get("skip_enriched")),
	)

	return {
		**result,
		"column": column,
		"identifiers_read": len(identifiers),
		"link": f"/app/listing-bulk-enrich/{result['batch']}",
		# Said in the payload because the distinction is the one the model got wrong:
		# a queued batch is not an enriched catalogue.
		"state": "started",
		"note": (
			f"Queued {len(result['resolved'])} of {len(identifiers)} products from column "
			f"'{column}'. The batch is running on background workers — it is NOT finished. "
			"Tell the user it has started, give them the link, and name anything in "
			"`errors` as not enriched."
		),
	}


def _read_csv(file_url):
	"""`(headers, rows)` for the whole file. Permission is the File's own.

	Parsed with the csv module rather than pandas on purpose: pandas types a column
	of long numeric ids as int64, and one empty cell in it makes the column float,
	which renders `4457730620593` as `4457730620593.0` and resolves to nothing. Ids
	are strings here from the first read to the last.
	"""
	names = frappe.get_all("File", filters={"file_url": file_url}, pluck="name", limit=1)
	if not names:
		frappe.throw(f"No file at {file_url}. Use the file_url the attachment reports.")

	file_doc = frappe.get_doc("File", names[0])
	# The generic File read permission is not enough: a file is only as private as the
	# document it hangs off, so the parent is what decides.
	if file_doc.attached_to_doctype and file_doc.attached_to_name:
		if not frappe.has_permission(file_doc.attached_to_doctype, "read", file_doc.attached_to_name):
			frappe.throw(
				"You do not have permission to read the document this file is attached to.",
				frappe.PermissionError,
			)
	elif "/private/" in (file_doc.file_url or ""):
		frappe.only_for("System Manager")

	if (file_doc.file_size or 0) > MAX_BYTES:
		frappe.throw(f"That file is larger than {MAX_BYTES // (1024 * 1024)} MB.")

	content = file_doc.get_content()
	if isinstance(content, bytes):
		for encoding in ("utf-8-sig", "utf-8", "latin-1"):
			try:
				content = content.decode(encoding)
				break
			except UnicodeDecodeError:
				continue
		else:
			frappe.throw("That file is not readable as text.")

	reader = csv.DictReader(io.StringIO(content))
	if not reader.fieldnames:
		frappe.throw("That file has no header row.")
	return list(reader.fieldnames), list(reader)


def _resolve_column(headers, wanted):
	"""The header matching `wanted`, or a throw naming every header there is.

	Headers are matched on their stripped, lowered form because an exported sheet
	routinely carries them padded ("Offer id ", " Variant id "), and a model asking
	for the column it can see should not fail on a trailing space.
	"""
	available = ", ".join(f"'{header}'" for header in headers)

	if not wanted:
		frappe.throw(
			f"Which column holds the product identifiers? This file has: {available}. "
			"Call this tool again with `column` set to one of them."
		)

	match = {(header or "").strip().lower(): header for header in headers}.get(wanted.strip().lower())
	if match is None:
		frappe.throw(f"No column called '{wanted}'. This file has: {available}.")
	return match


def _identifiers(rows, column):
	"""Every non-empty value in `column`, in file order, without repeats.

	Deduplicated here rather than left to `api.bulk_enrich`: it would resolve a
	repeated identifier twice to learn what the first pass already knew.
	"""
	seen, identifiers = set(), []
	for row in rows:
		value = (row.get(column) or "").strip()
		if value and value not in seen:
			seen.add(value)
			identifiers.append(value)
	return identifiers
