# Copyright (c) 2026, Alaiy and contributors
# For license information, please see license.txt
"""
One agent per installed connector, built here rather than by the connector.

`agents/listing` takes its channel knowledge from the `listing_channels` hook and
owns the agent itself. This is the same seam for a different job: a connector
declares the questions it can answer and the tools that answer them, and the
agent — its model, its prompt, its turn budget, its output contract — is built
here, once, for all of them.

    connector app              this package
    ─────────────              ────────────
    hooks.py                   exports()      reads the hook
      connector_agents = [..]  build()        one export -> one manifest
    agent_export.py            prompts/       the ONE prompt they share
      export() -> {tools, ...}

## Why the connector does not own the agent

It used to. Each connector wrote a `pack_meta.py` holding a model id, a turn
budget and its own `prompts/pack.md`, and upserted its own `OS Agent Registry`
row on migrate. Three connectors meant three prompts, three model choices and
three copies of the same reconcile loop, drifting apart on their own schedules.

None of that is channel knowledge. What Amazon can be asked and what Shopify can
be asked genuinely differ — that is in `tools` and `description`, which still come
from the connector. Which model answers, how many turns it gets and what shape it
replies in are decisions about *this product*, and they belong in one file.

A connector that ships no agent simply does not register the hook, exactly as one
that serves no listing channel does not register `listing_channels`. The
dependency points one way: this app looks for connectors, connectors do not look
for this app.

## What a connector may still say, and where

The prompt is this app's. What goes *inside* it at `{{channel_rules}}` is the
connector's: the facts about that channel which no single tool description can
hold because they govern several tools at once.

Amazon is the worked example. "Revenue here is gross merchandise value, never a
payout" is true of seven tools; "when Amazon's live figure disagrees with the
synced one, say so and by how much" is a rule about two tools *together*, and the
reason they disagree — different day boundaries, different marketplace coverage —
belongs to neither of them alone. Pushed into tool descriptions those rules get
copied five times and drift; dropped, an agent reports gross revenue as earnings.

It is a fragment, not a prompt. The connector does not decide the structure, the
reply contract, the model or the turn budget, and cannot remove what this file
says — it adds its own facts at one fixed point. A connector with nothing to add
leaves it out and the template closes over the gap.

## One channel per agent, and never two in one context

Each agent gets exactly the tools of the connector it was built for, so nothing
here can see two channels at once. That is not tidiness, it is the whole defence
against a real failure: Amazon adjudicates listings and publishes an issues feed;
Shopify does not review products at all, and what looks like the same tool there
(`get_listing_gaps`) is Alaiy OS's own judgement. A model holding both at once
reports one as the other. Held apart, it cannot.

The place that *does* see every channel at once is the chat turn that aggregates
the replies — so `OUTPUT_SCHEMA` makes `channel` required and the prompt requires
the summary to name its own channel. The router relays per channel; it never
merges a total across them.

## Discovery

`registry.py` walks `agents/` and calls `build_agent_meta()` on each package. This
one is the exception that returns *many* — a bench has as many connector agents as
it has connectors — so it exposes `build_agent_metas()` instead, which `registry`
accepts in its place. Nothing else in `agents/` needs that, and nothing else
should use it.
"""

import json
from pathlib import Path

import frappe

_APP_DIR = Path(__file__).resolve().parent

#: The list hook a connector registers to declare its agent. One entry per
#: connector, each a dotted path to a no-argument callable returning one export.
HOOK = "connector_agents"

#: Tool-calling and summarising, not reasoning: every turn is one read over a
#: connector's own client, and the answer is the rows. The cheapest capable model
#: is the right one, and it is chosen once here rather than per connector — a
#: fan-out asks several of these at a time, so this number is multiplied by however
#: many channels a bench has before anyone notices it.
MODEL = "claude-haiku-4-5-20251001"

#: A worst-case answer is: narrow the question, read a listing, read its sales,
#: reply. Ten leaves room for one wrong turn without letting a model page through
#: a whole catalogue on a vague question — and a vague question is exactly what
#: reaches these agents, because the router fans one out to every channel.
MAX_TURNS = 10

DEFAULT_ICON = "plug"

#: What every connector agent replies with. JSON rather than prose because the
#: caller is usually another model merging several of these into one table: prose
#: would have to be re-read and re-parsed to be compared, and a number lifted out
#: of a paragraph cannot be checked against the tool call that produced it.
#:
#: `channel` is required for the reason in the module docstring. `items` is
#: deliberately unconstrained — a listing row, an order row and a category row
#: share no fields, and a schema pretending otherwise would force every connector
#: to flatten into a shape that fits none of them.
OUTPUT_SCHEMA = {
	"type": "object",
	"required": ["channel", "summary", "items"],
	"properties": {
		"channel": {
			"type": "string",
			"description": "The sales channel this answer is about, e.g. 'Amazon' or 'Shopify'.",
		},
		"summary": {
			"type": "string",
			"description": (
				"The answer in prose, naming its own channel. Say what was found, how "
				"many, and anything the numbers do not say for themselves."
			),
		},
		"items": {
			"type": "array",
			"description": (
				"The rows behind the summary, if the question has any. Whatever fields "
				"the tools returned — do not invent, rename or compute new ones."
			),
			"items": {"type": "object"},
		},
	},
}

#: Every connector agent takes this, plus whatever its own export declares. The
#: task is the router's words, not the user's: it is what this agent was asked for
#: after the router decided which channel the question belongs to.
TASK_PROPERTY = {
	"task": {
		"type": "string",
		"description": (
			"What to find out, as a complete instruction. Self-contained: this agent "
			"cannot see the conversation it came from."
		),
	},
}


def read_text(relpath):
	"""Read a file relative to THIS package's directory."""
	return (_APP_DIR / relpath).read_text(encoding="utf-8")


BASE_PROMPT = read_text("prompts/connector.md")


def exports():
	"""Every registered connector's export, in hook order.

	A broken export is logged and skipped rather than raised. `agents/listing`'s
	channel seam fails closed instead, and the difference is what a failure costs:
	there, degrading open means enriching a product against whichever channel
	happened to load and writing that to a live catalogue. Here it means one
	connector has no agent — the others still answer, and a migrate that would
	otherwise have installed none of them installs the rest.

	`registry.agents()` does not swallow import errors for the agents in this app,
	for the opposite reason: those are ours, and their bugs should stop our migrate.
	A connector is someone else's app.
	"""
	found = []
	for entry in frappe.get_hooks(HOOK) or []:
		try:
			export = frappe.get_attr(entry)()
		except Exception:
			frappe.log_error(title=f"Connector agent export {entry} failed to load")
			continue
		if export:
			found.append(export)
	return found


def build(export):
	"""One connector export as the manifest `registry.py` upserts.

	The connector supplies what only it knows — which channel, what it can be
	asked, which tools answer, what arguments they take. Everything else is this
	file's, and is the same for every connector on the bench.
	"""
	agent_id = export["agent_id"]
	label = export["label"]

	return {
		"agent_id": agent_id,
		"agent_name": label,
		"description": export["description"],
		"icon": export.get("icon") or DEFAULT_ICON,
		"model": MODEL,
		"max_turns": MAX_TURNS,
		"system_prompt": system_prompt(export),
		"output_format": "JSON",
		"output_schema": OUTPUT_SCHEMA,
		"input_schema": input_schema(export),
		# Reachable from Ask Alaiy, both as `/amazon-sp-api` and — the point of all
		# this — as something `chat/agents.py` can hand a job to in prose.
		#
		# `chat_skill` is a claim that an agent's tools enforce their own
		# permissions, and these do: every handler is a whitelisted entry point that
		# gates on the doctype it reads before it reads it. It is also what takes
		# these tools OFF the flat chat surface (`chat/tools.py` offers only rows
		# whose agent has `chat_skill = 0`), which is the other half of the move.
		#
		# `agent_id` is a connector's own identifier and may hold underscores
		# (`amazon_sp_api`); `skill_slug` feeds `/<slug>` and OS Agent Registry
		# only accepts lowercase letters, digits and single hyphens.
		"chat_skill": 1,
		"skill_slug": agent_id.replace("_", "-"),
		"skill_label": label,
		# A connector agent exists because someone installed and configured that
		# connector, so it is on from the start — unlike the agents this app ships,
		# which arrive with an upgrade nobody asked for and start disabled. This is
		# also what keeps a migration from silently switching off a pack that was
		# already answering questions.
		"enabled_on_insert": True,
		"tools": export["tools"],
		"writes": export.get("writes") or (),
	}


def system_prompt(export):
	"""The shared prompt, named for this channel, with the connector's rules in it.

	Two substitutions and nothing else. `{{channel}}` is the label, everywhere it
	appears; `{{channel_rules}}` is the connector's own fragment, at the one point
	the template puts it — after the isolation rules, which it must not be able to
	weaken, and before the reply contract, which it must not be able to redefine.

	A connector with no rules gets the surrounding blank lines collapsed rather
	than a hole in the middle of the prompt, because a stray empty section reads to
	a model as a section it failed to receive.
	"""
	rules = (export.get("rules") or "").strip()
	prompt = BASE_PROMPT.replace("{{channel}}", export["label"])
	if rules:
		return prompt.replace("{{channel_rules}}", rules)
	return prompt.replace("\n\n{{channel_rules}}\n", "").replace("{{channel_rules}}", "")


def input_schema(export):
	"""`task`, plus whatever the connector declares, in one flat object.

	Flat rather than a nested `params`: `chat/skills.py::validate_args` already
	validates a skill's arguments against this schema and says which are missing in
	words the model can act on, and `/amazon_sp_api` on the same row renders a form
	from it. Nesting would need a second schema, a second validator and a second
	error voice for the same arguments.

	A connector declaring nothing gets `task` alone, which is a complete agent.
	"""
	declared = export.get("input_schema") or {}
	properties = {**TASK_PROPERTY, **(declared.get("properties") or {})}
	required = ["task"] + [key for key in (declared.get("required") or []) if key != "task"]

	return {
		"type": "object",
		# Open, unlike the agents in this app: a router filling a field a connector
		# declared in a version this bench has not migrated to yet should get a thin
		# answer, not a refused run.
		"additionalProperties": True,
		"properties": properties,
		"required": required,
	}


def build_agent_metas():
	"""Every connector agent on this bench. `registry.py` calls this.

	Plural, and the only manifest builder in `agents/` that is — see the module
	docstring.
	"""
	return [build(export) for export in exports()]


if __name__ == "__main__":
	# All a bare interpreter can do: nothing, without a site to read hooks from.
	# `bench --site <site> execute
	#  alaiy_os_agents.agents.connector.meta.build_agent_metas` is the smoke test.
	print(json.dumps(OUTPUT_SCHEMA, indent=2))
