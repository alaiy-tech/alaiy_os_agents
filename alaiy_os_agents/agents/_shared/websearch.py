# Copyright (c) 2026, Alaiy and contributors
# For license information, please see license.txt
"""
The public web, for any agent in this app that needs it: finding a page and
reading what it says there, for whatever a catalog's own data doesn't carry.

Both tools go through Alaiy OS core's `ai_client` seam like everything else an
agent does — `search_competitor_listings` via `llm.web_search`, the same
provider-agnostic call `chat/websearch.py` uses for Ask Alaiy. `view_page` has
no such seam to go through: fetching and reading a page is not a model
capability, so it is plain `requests` plus a stdlib HTML strip.

## Why this lives in `_shared`, not one agent's own package

Nothing here is knowledge about any one agent's domain. Reading a page off the
web is the same act whether the caller is writing a listing or comparing a
price, so this module has no agent-specific logic and is imported by name
(`agents/listing/websearch.py` re-exports it; `agents/pricing/websearch.py`
does the same) rather than duplicated.

`research_record` is the seam a caller uses to read back what a run really
fetched — e.g. the listing agent's Shopify channel attributes values against
it in `provenance.py`. Keeping the record here, scoped by run id, is what lets
more than one agent share these tools without reimplementing them.
"""

import re
from html.parser import HTMLParser

import frappe

from alaiy_os.engine import llm
from alaiy_os.engine.context import get_agent_context

#: A generic header for fetching an external page or image ourselves, rather
#: than trusting the model to.
FETCH_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; AlaiyOS-Agents/1.0)"}

#: Where this run's web work accumulates, for the rest of the run to read.
#:
#: A write tool (e.g. `save_listing`, `save_comparison`) is the run's LAST
#: action, so by the time it needs to attribute a value the searches and page
#: fetches have already happened — but the run is still executing, which means
#: its OS Agent Run transcript has not been written yet and the engine's own
#: tool ledger is still in memory. Neither can be read back from the database
#: mid-run.
#:
#: So the tools that reach the web record what they found here, on the
#: request, and whoever needs it reads it at the end of the same run.
#: `frappe.flags` is cleared between jobs — but NOT between runs, which is the
#: case that matters here: bulk fan-out deliberately executes a whole chunk of
#: products inside one job, so without the run id below, product B could have
#: a value "sourced" to a page fetched for product A. A wrong URL is worse
#: than no URL in a feature whose entire purpose is that a source can be
#: trusted.
_RESEARCH_FLAG = "_agents_research"

#: A spec/product page has no business being longer than this once boilerplate
#: is stripped; truncating protects the turn budget from a page that is mostly
#: navigation, scripts, or an unrelated wall of related-products markup.
MAX_PAGE_CHARS = 12_000

#: Tags whose content is never page copy, so it is dropped instead of stripped-and-kept.
_SKIP_TAGS = {"script", "style", "noscript", "template", "svg"}


class _TextExtractor(HTMLParser):
	"""Bare-bones HTML-to-text: keeps tag text, drops markup and skip-tag bodies."""

	def __init__(self):
		super().__init__()
		self._skip_depth = 0
		self.chunks = []

	def handle_starttag(self, tag, attrs):
		if tag in _SKIP_TAGS:
			self._skip_depth += 1

	def handle_endtag(self, tag):
		if tag in _SKIP_TAGS and self._skip_depth:
			self._skip_depth -= 1

	def handle_data(self, data):
		if not self._skip_depth and data.strip():
			self.chunks.append(data.strip())


def _extract_text(html):
	parser = _TextExtractor()
	parser.feed(html)
	text = "\n".join(parser.chunks)
	return re.sub(r"\n{3,}", "\n\n", text).strip()


def research_record():
	"""This run's web work so far: what was searched, and what was actually read.

	`pages` holds the extracted text of every page the run really fetched, which
	is what makes a sourced value checkable rather than merely claimed — a
	value that appears in one of these was read off that page, and a URL that is
	not here was not opened, whatever the model's notes say about it.

	Scoped to the RUN, not the job. The record is stamped with the run that
	opened it and thrown away the moment a different run asks for it, so a bulk
	chunk cannot carry one product's pages into the next. Outside a run (a tool
	called from a script or a test) the id is None, which behaves as a single
	implicit run — fine, because nothing is being attributed.
	"""
	run = get_agent_context().get("run")
	record = frappe.flags.get(_RESEARCH_FLAG)
	if record is None or record.get("run") != run:
		record = {"run": run, "searches": [], "pages": []}
		frappe.flags[_RESEARCH_FLAG] = record
	return record


#: A run that asks for several unrelated specs (a catalog-spec query and a
#: wrist-fit query, say) pays one LLM turn per query if it calls this tool once
#: per question -- and turns, not the search itself, are most of a run's wall
#: clock. Small on purpose: this is for the handful of genuinely different
#: questions one run legitimately has, not a batch job.
MAX_QUERIES = 5


def _one_search(query):
	"""One grounded search -> {"query", "answer", "citations"}.

	Deliberately run on the calling thread rather than farmed out to a pool:
	both `ai_client` implementations read site config and run/billing context
	(`frappe.conf`, `frappe.local`) on every call rather than caching it at
	construction the way the thread-safe image methods do, so a bare worker
	thread here would raise or silently lose cost attribution. Sequential is
	still the win: each call is ~3-5s, but the LLM turn surrounding a batch of
	them is one turn instead of one per query, and that outer round trip -- not
	this loop -- is most of a run's wall clock.
	"""
	result = llm.web_search(query)
	return {
		"query": query,
		"answer": result.get("answer") or "",
		"citations": result.get("citations") or [],
	}


def search_competitor_listings(query=None, queries=None):
	"""
	Web-search for one or more queries and return each one's grounded answer
	plus its sources.

	Mirrors `chat/websearch.py`'s confirmation-free variant: an agent doesn't
	need to ask permission the way Ask Alaiy does, since it isn't a
	conversation with a person mid-turn — the prompt itself decides when to
	reach for this. Gated on `llm.web_search_support()` so a site whose AI
	client can't search (BYOK with no `ai_base_url`) declines cleanly instead
	of failing every call.

	Accepts either the single `query` a caller has always passed, or a
	`queries` list — several different questions asked and answered in one
	tool call (and one LLM turn) instead of one call per question. `query`
	alone still returns the bare `{query, answer, citations}` shape every
	existing caller (this agent's own prompt, the pricing agent) already
	expects; `queries` returns `{results: [...]}`, one entry per query, in the
	order given.
	"""
	if queries is not None and query is not None:
		frappe.throw("search_competitor_listings takes `query` or `queries`, not both.")

	batch = queries if queries is not None else ([query] if query is not None else [])
	batch = [str(q or "").strip() for q in batch]
	batch = [q for q in batch if q]
	if not batch:
		frappe.throw(
			"search_competitor_listings needs a `query` (or `queries`) — what should "
			"I look up?"
		)
	if len(batch) > MAX_QUERIES:
		frappe.throw(
			f"search_competitor_listings takes at most {MAX_QUERIES} queries in one "
			f"call; {len(batch)} given. Split them across calls."
		)

	if not llm.web_search_support():
		frappe.throw(
			"Web search is not available on this site (the active AI client cannot "
			"search). Do NOT retry; treat this attribute as unresolved and add it to "
			"needs_review instead."
		)

	results = [_one_search(q) for q in batch]

	record = research_record()
	for r in results:
		record["searches"].append({"query": r["query"], "citations": r["citations"]})

	if queries is None:
		return results[0]
	return {"results": results}


def view_page(url):
	"""
	Fetch an external page URL and hand back its readable text, so the model can
	read a page directly rather than trusting a search summary alone. Any fetch
	failure (bad status, timeout, unreachable host) raises and is surfaced to
	the model as an errored tool result by the executor — same as any other
	tool failure, no special handling needed here.
	"""
	import requests

	resp = requests.get(url, timeout=30, headers=FETCH_HEADERS, allow_redirects=True)
	resp.raise_for_status()

	# `url` may be a redirector rather than the real page — search grounding
	# routes (e.g. a Vertex AI `grounding-api-redirect` link) hand back a
	# wrapper URL, not the retailer's own. `resp.url` is where the request
	# actually landed after following every redirect, which is both the
	# correct provenance (a value was read off THIS page) and the link a
	# caller can hand a person to open directly, instead of a redirect that
	# outlives the search session it was minted for.
	final_url = resp.url

	text = _extract_text(resp.text)[:MAX_PAGE_CHARS]
	# Recorded only on success, and only after raise_for_status: a page that
	# 404ed is a page the run did not read, and listing it here would let a
	# value be "sourced" from a page that never loaded.
	research_record()["pages"].append({"url": final_url, "text": text})
	return {
		"_content_blocks": [
			{"type": "text", "text": f"Page content ({final_url}):\n{text}"},
		]
	}
