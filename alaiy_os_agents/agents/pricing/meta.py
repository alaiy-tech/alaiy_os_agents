# Copyright (c) 2026, Alaiy and contributors
# For license information, please see license.txt
"""
The pricing agent: finds competitor prices for a catalogue product, for an
admin to compare against Solist's own price.

Unlike `agents/listing`, this has no channel concept and no per-seller
override. What counts as "the same product" on the open web does not vary by
seller, and the agent writes nothing to a sales channel — it only reads a
product's own price and searches the public web, then persists a comparison
for review. So `build_agent_meta` here is the plain case: one prompt, one
schema, no discovery.

## No page-opening tool

An earlier version of this agent also had `view_competitor_page` (aliasing
`_shared.websearch.view_page`, which the listing agent still uses) and made
the model open and read a real competitor page before trusting a price.
That was more honest, but also slow and unreliable in practice: each page
open is its own model turn plus a request with up to a 30s timeout, several
retailers (chrono24.com among them) 403 a bare `requests` fetch outright, and
a run commonly spent its whole turn budget on that loop without ever
reaching `save_comparison`.

This version trusts `search_competitor_listings`'s own grounded answer and
citations directly — no independent page fetch, no per-page model turn. That
is a real accuracy tradeoff (a search-grounded price is not a price read off
the retailer's own page), traded deliberately for a run that reliably
finishes in a handful of seconds instead of 30-90s. `tools.py::save_comparison`
does a cheap best-effort `HEAD` request per citation to resolve it to its
real URL (the citations `search_competitor_listings` returns are often a
provider's redirect wrapper, not the retailer's own link) — that costs
milliseconds, not a model turn, and is not a verification step.
"""

import json
from pathlib import Path

_APP_DIR = Path(__file__).resolve().parent

AGENT_ID = "pricing"
AGENT_NAME = "Price Comparison"
AGENT_ICON = "scale"


# Not the content-writing default `agents/listing` uses (gemini-3.1-flash-lite
# is fine for prose). gemini-3.7-flash is the step up already proven in this
# codebase for reliability — see alaiy_os_thesolist/agents/listing.md's own
# override.
#
# The web search call itself (search_competitor_listings, via llm.web_search)
# is a separate, cheaper knob — see DEFAULT_WEB_SEARCH_MODEL in
# alaiy_os/engine/ai_client.py — and stays on its own default regardless of
# this setting; only the agent's own reasoning turns run on this model.
DEFAULT_MODEL = "gemini-3.7-flash"

# get_product, one search, save_comparison, with a little room for a second
# search if the first came back thin. Deliberately small: this agent trusts
# the search grounding answer directly rather than opening pages to verify it
# (see the module docstring on `view_competitor_page`'s removal) — that is
# what keeps a run to a handful of seconds and a couple of model calls
# instead of the 30-90s a page-verification loop cost.
DEFAULT_MAX_TURNS = 6

DEFAULT_DESCRIPTION = (
	"Finds competitor prices for a catalogue product on the open web, and "
	"saves a comparison against Solist's own price for an admin to review. "
	"Admin-triggered only."
)


def read_text(relpath):
	"""Read a file relative to THIS package's directory."""
	return (_APP_DIR / relpath).read_text(encoding="utf-8")


BASE_PROMPT = read_text("prompts/system.md")
BASE_SCHEMA = json.loads(read_text("schemas/output.json"))

_PKG = "alaiy_os_agents.agents.pricing"
_HANDLERS = f"{_PKG}.tools"
_WEBSEARCH = f"{_PKG}.websearch"

# ONE required string property, the item code — same reasoning as the listing
# agent's INPUT_SCHEMA: it is what lets a run be started from a plain string
# with no client-side form.
INPUT_SCHEMA = {
	"type": "object",
	"additionalProperties": False,
	"properties": {
		"product": {
			"type": "string",
			"description": "The item code to find competitor prices for. The code ALONE, looked up as a primary key.",
		},
	},
	"required": ["product"],
}


TOOL_CATALOG = {
	"get_product": {
		"description": (
			"Fetch the product's own price, brand and name from the catalogue. "
			"ALWAYS call this FIRST — its brand and name are what you build a "
			"search query from; the raw item code means nothing to a search "
			"engine."
		),
		"handler": f"{_HANDLERS}.get_product",
		"parameters_schema": {
			"type": "object",
			"properties": {
				"product": {
					"type": "string",
					"description": "The item code to read.",
				},
			},
			"required": ["product"],
		},
	},
	"search_competitor_listings": {
		"description": (
			"Search the public web for the same product on other retailers, "
			"and return a grounded answer with its sources. `query` should be "
			"phrased for a search engine: brand + model or reference number + "
			"product type, not the raw item code. Returns {answer, citations: "
			"[{title, url}]}. This is your source: extract every distinct "
			"price the answer actually states for THIS product, matched to "
			"the citation it credits that price to. There is no page-opening "
			"step — do not claim to have verified a price beyond what this "
			"answer says."
		),
		"handler": f"{_WEBSEARCH}.search_competitor_listings",
		"parameters_schema": {
			"type": "object",
			"properties": {
				"query": {
					"type": "string",
					"description": (
						"The search query, phrased for a search engine — brand, "
						"model/reference number, and product type."
					),
				},
			},
			"required": ["query"],
		},
	},
	"save_comparison": {
		"description": (
			"Persist this product's competitor prices for admin review. Call "
			"this ONCE as your FINAL action, passing the product and every "
			"competitor price the search answer clearly stated, each matched "
			"to the citation it credits that price to. It upserts by product, "
			"so re-running replaces the previous comparison rather than "
			"creating a duplicate."
		),
		"handler": f"{_HANDLERS}.save_comparison",
		"parameters_schema": {
			"type": "object",
			"properties": {
				"product": {
					"type": "string",
					"description": "The item code this comparison is for (the upsert key).",
				},
				"competitor_prices": {
					"type": "array",
					"description": "Every competitor price the search answer stated, each with its citation URL.",
					"items": {
						"type": "object",
						"properties": {
							"source_name": {"type": "string"},
							"url": {"type": "string"},
							"price": {"type": "number"},
							"currency": {"type": "string"},
						},
						"required": ["source_name", "url", "price", "currency"],
					},
				},
			},
			"required": ["product", "competitor_prices"],
		},
	},
}


def build_agent_meta():
	"""
	The registration manifest `registry.py::sync()` upserts into `OS Agent
	Registry` (and its `OS Agent Tool` child rows).

	No override discovery, unlike `agents/listing`: nothing about this agent
	varies by which seller's app is installed.
	"""
	tools = [
		dict(spec, tool_id=tool_id, connector=None) for tool_id, spec in TOOL_CATALOG.items()
	]

	return {
		"agent_id": AGENT_ID,
		"agent_name": AGENT_NAME,
		"description": DEFAULT_DESCRIPTION,
		"icon": AGENT_ICON,
		"page": None,
		"settings_doctype": None,
		"model": DEFAULT_MODEL,
		"max_turns": DEFAULT_MAX_TURNS,
		"system_prompt": BASE_PROMPT,
		"output_format": "JSON",
		"output_schema": BASE_SCHEMA,
		# Admin-triggered only — no Ask Alaiy slash command for this one.
		"chat_skill": 0,
		"skill_slug": None,
		"skill_label": None,
		"input_schema": INPUT_SCHEMA,
		"tools": tools,
		# save_comparison is the only tool that writes anything.
		"writes": ("save_comparison",),
		"input_options": [t["input_option"] for t in tools if t.get("input_option")],
		"override_app": None,
	}
