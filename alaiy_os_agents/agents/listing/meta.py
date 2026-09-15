# Copyright (c) 2026, Alaiy and contributors
# For license information, please see license.txt
"""
The listing agent: its prompt, its shared schema, its tools — and how a customer
app overrides the prompt.

There is ONE listing agent per site, `listing`, and it is not a marketplace's
agent. What Amazon wants from a listing and what Shopify wants arrive at runtime
from whichever connectors are installed, through the `listing_channels` hook —
see `channels.py` for that contract and for why the channel's fields are a tool
result rather than part of the schema here.

A customer app changes the prompt by dropping a single markdown file at:

    <customer_app>/agents/listing.md

Whatever is in that file is appended to the vanilla prompt below, so it says what
is true of that seller: who they are, their house style, their brand names. No
registration, no hook, no config — the file being there is the whole mechanism.

It may start with optional frontmatter, for the two things a prompt cannot express:

    ---
    model: claude-opus-4-8
    description: Shown in the Agents hub.
    ---
    Everything from here down is appended to the vanilla prompt.

The override is for the *seller*, not for a channel. Anything that is true of
Amazon rather than of this seller belongs in the Amazon adapter's `spec.rules`,
where every site running Amazon gets it.
"""

import json
from pathlib import Path

import frappe

_APP = "alaiy_os_agents"
_PKG = f"{_APP}.agents.listing"
_APP_DIR = Path(__file__).resolve().parent

# Where a customer app puts its override, relative to its own package directory.
OVERRIDE_PATH = ("agents", "listing.md")

# Frontmatter keys we honour. Anything else in there is a typo, so say so.
OVERRIDE_KEYS = ("model", "description")


def read_text(relpath):
	"""Read a file relative to THIS app's package directory."""
	return (_APP_DIR / relpath).read_text(encoding="utf-8")


# The one listing agent per site: the OS Agent Registry primary key, and what you
# pass as `agent` to alaiy_os.api.agents.run_agent.
AGENT_ID = "listing"
AGENT_NAME = "Listing"
AGENT_ICON = "sparkles"

DEFAULT_MODEL = "gemini-3.1-flash-lite"

# Higher than the channel packs' 8. A run now opens with get_channel_spec and may
# add get_listing_health before it reaches the work the old budget was set for,
# and save_listing can legitimately be called twice when the channel's validator
# rejects the first attempt — which is a working correction loop, not a runaway.
DEFAULT_MAX_TURNS = 12

DEFAULT_DESCRIPTION = (
	"Writes a channel-ready product listing from raw product data — for whichever "
	"sales channel the product is on — and diagnoses why a live listing was "
	"rejected or suppressed. Lands in review for an admin to approve."
)

BASE_PROMPT = read_text("prompts/system.md")
BASE_SCHEMA = json.loads(read_text("schemas/output.json"))

_HANDLERS = f"{_PKG}.tools"
# The two web-research tools are the only ones that do not dispatch through a
# channel adapter, so they keep their own module — see websearch.py.
_WEBSEARCH = f"{_PKG}.websearch"

# What the user types after the slash in Ask Alaiy, and the arguments behind it.
SKILL_SLUG = "listing"
SKILL_LABEL = "Listing"

# ONE required string property, deliberately. That is exactly the case
# `chat/skills.py:fill_from_text` handles — the words typed alongside the command
# become the argument — so `/listing ABC-123` works from a picker that knows
# nothing about this schema, and no client has to grow a form before the skill is
# usable. `channel` stays optional because the agent can almost always work it out.
INPUT_SCHEMA = {
	"type": "object",
	"additionalProperties": False,
	"properties": {
		"product": {
			"type": "string",
			"description": "The product identifier to write a listing for — a seller SKU, an item code, whatever the channel keys its products by. The code ALONE: it is looked up as a primary key, so a product name, or a name with the code inside it like 'Bumper Fastener Kit (SKU: 4125037034808)', matches nothing and ends the run.",
		},
		"channel": {
			"type": "string",
			"description": "Which sales channel to write for. Omit it unless the same identifier is listed on more than one channel, in which case the run will ask.",
		},
	},
	"required": ["product"],
}


# ── the tools ─────────────────────────────────────────────────────────────────
# Every tool the listing agent has. All of them are channel-agnostic and dispatch
# through `channels.resolve`; none of them names a marketplace.
#
# `channel` is a plain optional string on every tool rather than an enum of the
# installed channels. An enum would have to be rebuilt whenever a connector is
# installed, and a stale one would refuse a channel that exists — where a plain
# string simply reaches `channels.get`, which refuses with a message naming what
# IS available. The tool schema stays true regardless of what is on the bench.
#
# Keep new schemas to single types. The default model is reached through LiteLLM
# in front of Gemini, whose function declarations are an OpenAPI subset: a
# `"type"` union like ["string", "null"] is rejected outright and reported as a
# missing field on the node below it. That is also why `save_listing`'s `listing`
# is declared as a bare object — see its entry.

_CHANNEL_ARG = {
	"type": "string",
	"description": (
		"The channel id from get_channel_spec, copied verbatim. Omit it only if you "
		"were never given one and the product is on a single channel."
	),
}


TOOL_CATALOG = {
	"get_channel_spec": {
		"description": (
			"Return THIS channel's own listing fields and rules — what it requires "
			"beyond the shared title/description/images, how long each field may be, "
			"what it forbids, and whether it supports an image step and a health "
			"check. ALWAYS call this FIRST, before reading the product and before "
			"writing anything. The channels on an Alaiy OS site disagree about "
			"almost everything, so what you know in general about a marketplace is "
			"not what this site will accept; this tool is the binding answer. Pass "
			"the `product` you were given, or the `channel` if you were given one."
		),
		"handler": f"{_HANDLERS}.get_channel_spec",
		"parameters_schema": {
			"type": "object",
			"properties": {
				"channel": _CHANNEL_ARG,
				"product": {
					"type": "string",
					"description": "The product identifier, used to work out the channel when you were not told one.",
				},
			},
		},
	},
	"get_product": {
		"description": (
			"Fetch the product's current listing on its channel — its existing "
			"fields, its status, and its product photos as images you can actually "
			"look at. ALWAYS call this when the input contains a product "
			"identifier, and study the photos: they are the primary evidence for "
			"material, colour, pattern, construction, what is in the box, and any "
			"spec text printed onto the image or its packaging."
		),
		"handler": f"{_HANDLERS}.get_product",
		"parameters_schema": {
			"type": "object",
			"properties": {
				"product": {
					"type": "string",
					"description": "The product identifier to read.",
				},
				"channel": _CHANNEL_ARG,
			},
			"required": ["product"],
		},
	},
	"get_reference_values": {
		"description": (
			"Return the vocabulary already in use on this channel — the categories, "
			"tags, brands and search terms applied to other products on this "
			"account. Call it before finalising any field whose value should match "
			"what the catalogue already uses, so your output stays consistent "
			"instead of inventing a second spelling of an existing term. A channel "
			"that publishes none returns an empty answer with a note; that is not a "
			"failure."
		),
		"handler": f"{_HANDLERS}.get_reference_values",
		"parameters_schema": {
			"type": "object",
			"properties": {
				"channel": _CHANNEL_ARG,
				"product": {
					"type": "string",
					"description": "The product identifier, used to work out the channel when you were not told one.",
				},
			},
		},
	},
	"get_listing_health": {
		"description": (
			"Return what the channel currently says is WRONG with this listing — "
			"its status and its open issues, each with the code, severity, message "
			"and the fields it names. Call this whenever get_channel_spec reports "
			"has_health_check, and read it before writing anything: an error here "
			"is usually the reason the listing is not selling, and fixing it is the "
			"most valuable thing this run can do. Record what you fixed in "
			"`diagnosis.issues_addressed`, and anything you do not control "
			"(pricing, inventory, category approval, compliance) in "
			"`diagnosis.issues_for_admin` rather than guessing at it. A channel "
			"that does not report issues says so; leave the diagnosis empty then."
		),
		"handler": f"{_HANDLERS}.get_listing_health",
		"parameters_schema": {
			"type": "object",
			"properties": {
				"product": {
					"type": "string",
					"description": "The product identifier to diagnose.",
				},
				"channel": _CHANNEL_ARG,
			},
			"required": ["product"],
		},
	},
	"search_competitor_listings": {
		"description": (
			"Search the public web for the same product on other retailers, and "
			"return a grounded answer with its sources. Only call this if your "
			"instructions below include a competitor web-lookup step — it says "
			"when this tool applies and how to treat what it returns. `query` "
			"should be phrased for a search engine: brand + model or reference "
			"number + product type, not a copy of the input. Returns {answer, "
			"citations: [{title, url}]} — the answer comes from a model reading "
			"the live web; treat it as a source, not as fact. One citation is one "
			"shop's word for it: read more than one of them with "
			"`view_competitor_page` before trusting a specific value, per your "
			"instructions below."
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
	"view_competitor_page": {
		"description": (
			"Fetch a competitor page URL (typically one of "
			"`search_competitor_listings`'s citations) and return its text, so "
			"you can read the actual spec rather than trusting a search summary. "
			"Returns the page's extracted text, truncated to a fixed budget. A "
			"page you actually open this way is also the ONLY thing that can "
			"source a value to the web — a URL you cite without opening it earns "
			"nothing and may be reported as an unsourced claim."
		),
		"handler": f"{_WEBSEARCH}.view_page",
		"parameters_schema": {
			"type": "object",
			"properties": {
				"url": {
					"type": "string",
					"description": "The competitor page URL to fetch and read.",
				},
			},
			"required": ["url"],
		},
	},
	"view_image": {
		"description": (
			"Fetch an external image URL and show it to you as an actual image, not "
			"just a string. ALWAYS call this before writing anything if your only "
			"product evidence is a URL rather than an identifier — you cannot "
			"describe a product you have never looked at."
		),
		"handler": f"{_HANDLERS}.view_image",
		"parameters_schema": {
			"type": "object",
			"properties": {
				"image_url": {
					"type": "string",
					"description": "The image URL to fetch and look at.",
				},
			},
			"required": ["image_url"],
		},
	},
	"prepare_images": {
		"description": (
			"Run this channel's image step on the product's photos. What that step "
			"does is the channel's business, not yours — you do NOT describe the "
			"imagery you want, choose which photo leads, or reorder the result. "
			"Call it ONCE for the whole product, passing `product` and "
			"`prepare_images` copied verbatim from the input toggle (default "
			"false). Whether "
			"anything happens is decided by the tool: it runs ONLY when the product "
			"has photos AND enabled is true. Returns {images: [...]}; copy that "
			"list into the final `images` array VERBATIM and in the same order, "
			"including every field on each entry. EXPECT url TO BE null — the "
			"photos are processed in the background after this run finishes. That "
			"is success, not failure: do NOT retry, do NOT call it again, do NOT "
			"put the images in needs_review, and do NOT describe them as missing "
			"anywhere in your output. Some entries may come back with a real url "
			"because an earlier run already produced them; a mix is normal. An "
			"empty list with a note (no photos, toggle off, or no image step on "
			"this channel) is also expected — set images to [] and record the note."
		),
		"handler": f"{_HANDLERS}.prepare_images",
		"input_option": {
			"fieldname": "prepare_images",
			"label": "Prepare images",
			"description": (
				"Run the channel's image step on this product's photos. Costs money "
				"per image, and only works for photos reachable from the public "
				"internet. Turn off for a faster, text-only enrichment."
			),
			"default": 0,
		},
		"parameters_schema": {
			"type": "object",
			"properties": {
				"product": {
					"type": "string",
					"description": "The product identifier whose photos to prepare.",
				},
				"channel": _CHANNEL_ARG,
				"prepare_images": {
					"type": "boolean",
					"description": "The per-request opt-in toggle of the same name, copied verbatim from the input (default false).",
				},
				"image_urls": {
					"type": "array",
					"items": {"type": "string"},
					"description": "Only for a URL-only product with no identifier: the photo URLs to prepare, first treated as the leading image.",
				},
			},
		},
	},
	"register_product": {
		"description": (
			"Put a catalogue product onto a sales channel, so there is a listing "
			"record for it to be enriched onto. Call this ONLY when "
			"get_channel_spec or get_product told you the product is in the "
			"catalogue but not on any channel — that message says so explicitly, "
			"and names the channels that can register it. Do not call it "
			"speculatively; a product that already has a listing does not need it. "
			"**Nothing is sent to the channel.** It creates a local record in a "
			"not-live state, which is what makes the product enrichable; whether "
			"the listing is ever published stays a separate decision someone makes "
			"after reviewing the enrichment. After it returns, carry straight on "
			"with get_channel_spec and the rest of the workflow for the product it "
			"names — that identifier is the one to use from then on."
		),
		"handler": f"{_HANDLERS}.register_product",
		"parameters_schema": {
			"type": "object",
			"properties": {
				"product": {
					"type": "string",
					"description": "The catalogue product identifier to put on a channel.",
				},
				"channel": _CHANNEL_ARG,
			},
			"required": ["product"],
		},
	},
	"save_listing": {
		"description": (
			"Validate the finished listing against this channel's real rules and, if "
			"it passes, persist it for admin review. Call this ONCE as your FINAL "
			"action, after the listing is complete including any images, passing "
			"the product and the exact object you are about to return. It upserts "
			"by product, so re-running updates the existing row rather than "
			"creating a duplicate, and lands it in review. **It will refuse a "
			"listing that breaks the channel's rules** and tell you exactly what is "
			"wrong — fix those things and call it again; nothing is written until "
			"it passes. Skip this tool ONLY when there is no product identifier (a "
			"URL-only enrichment), since the record is keyed to it."
		),
		"handler": f"{_HANDLERS}.save_listing",
		"parameters_schema": {
			"type": "object",
			"properties": {
				"product": {
					"type": "string",
					"description": "The product identifier this listing is for (the upsert key).",
				},
				"channel": _CHANNEL_ARG,
				# Deliberately a bare object with no declared properties. The channel's
				# fields differ per channel and are handed over by get_channel_spec, so
				# there is no one shape to declare here — and declaring the union of
				# them would be rejected by the Gemini path anyway. What enforces the
				# shape is the channel's own validator, which runs inside this tool and
				# refuses in words the model can act on. See tools/handlers.py.
				"listing": {
					"type": "object",
					"description": "The complete listing object you are about to return, with the shared fields and every field get_channel_spec required.",
				},
			},
			"required": ["product", "listing"],
		},
	},
}


# ── the customer override ─────────────────────────────────────────────────────


def find_override():
	"""
	The installed app that overrides the listing agent, and its markdown file.

	Discovery is just "does the file exist", so a customer app needs no hook and no
	Python. Returns (app, Path) or (None, None).

	Two apps overriding one agent is a mistake worth shouting about: their prompts
	would silently concatenate in installed-app order.
	"""
	found = []
	for app in frappe.get_installed_apps():
		if app == _APP:
			continue
		path = Path(frappe.get_app_path(app, *OVERRIDE_PATH))
		if path.exists():
			found.append((app, path))

	if len(found) > 1:
		frappe.throw(
			"More than one app overrides the listing agent: "
			f"{[app for app, _ in found]}. A site has one listing agent, so leave "
			"only the customer app whose seller account this site is."
		)
	return found[0] if found else (None, None)


def parse_override(text):
	"""
	Split an override file into (frontmatter dict, prompt body).

	Frontmatter is optional, `key: value` per line between two `---` lines. Kept
	deliberately dumb — it exists only for `model` and `description`, everything else
	belongs in the prompt itself.
	"""
	meta, body = {}, text

	if text.lstrip().startswith("---"):
		stripped = text.lstrip()
		end = stripped.find("\n---", 3)
		if end != -1:
			block = stripped[3:end]
			body = stripped[end + 4 :].lstrip("-").lstrip("\n")
			for line in block.strip().splitlines():
				line = line.strip()
				if not line or line.startswith("#"):
					continue
				key, _, value = line.partition(":")
				key = key.strip()
				if key not in OVERRIDE_KEYS:
					frappe.throw(
						f"Unknown key '{key}' in an agents/listing.md frontmatter. "
						f"Supported: {', '.join(OVERRIDE_KEYS)}."
					)
				meta[key] = value.strip()

	return meta, body.strip()


def build_agent_meta():
	"""
	The registration manifest setup/install.py upserts into alaiy_os's OS Agent
	Registry (and its OS Agent Tool child rows): the vanilla agent, with this
	site's override appended to its prompt.

	No channel is read here, and that is the point. The registry row is the same on
	every site whatever connectors it has, so installing a new channel connector
	does not rewrite the agent — the channel is resolved per run instead. It also
	means this manifest can be built on a bench with no channel at all, which is
	what lets the app install before any connector does.

	Credentials are NOT part of this, and this app holds none. Everything goes
	through Alaiy OS core's `ai_client` seam, so whichever client is installed
	supplies the credential.
	"""
	app, path = find_override()
	meta, body = parse_override(path.read_text(encoding="utf-8")) if path else ({}, "")

	prompt = f"{BASE_PROMPT.rstrip()}\n\n{body}\n" if body else BASE_PROMPT
	tools = [
		dict(spec, tool_id=tool_id, connector=None) for tool_id, spec in TOOL_CATALOG.items()
	]

	return {
		"agent_id": AGENT_ID,
		"agent_name": AGENT_NAME,
		"description": meta.get("description") or DEFAULT_DESCRIPTION,
		"icon": AGENT_ICON,
		"page": None,
		# No settings DocType: the agent stores no credentials.
		"settings_doctype": None,
		"model": meta.get("model") or DEFAULT_MODEL,
		"max_turns": DEFAULT_MAX_TURNS,
		"system_prompt": prompt,
		"output_format": "JSON",
		"output_schema": BASE_SCHEMA,
		# Ask Alaiy: `/listing <product>`. See chat/skills.py — the catalogue is a
		# query over these fields, so opting in is a manifest change and nothing more.
		"chat_skill": 1,
		"skill_slug": SKILL_SLUG,
		"skill_label": SKILL_LABEL,
		"input_schema": INPUT_SCHEMA,
		"tools": tools,
		# Which of those tools change something. Named here rather than inferred,
		# because only this agent knows: `save_listing` writes the enrichment and
		# `register_product` creates the record it is written onto. registry.py
		# marks them `effect: write`, which keeps them off Ask Alaiy's
		# directly-callable surface.
		"writes": ("save_listing", "register_product"),
		# A consequence of the tools, not a separate declaration.
		"input_options": [t["input_option"] for t in tools if t.get("input_option")],
		# Not a registry field; useful to whoever is debugging why a prompt looks the
		# way it does.
		"override_app": app,
	}
