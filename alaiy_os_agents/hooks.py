app_name = "alaiy_os_agents"
app_title = "Alaiy OS Agents"
app_publisher = "Alaiy"
app_description = "The default agents Alaiy OS ships — installed everywhere, each enabled per site"
app_email = "mail@alaiy.com"
app_license = "agpl-3.0"

# ---------------------------------------------------------------------------
# Dependencies
# ---------------------------------------------------------------------------
# The agent engine, OS Agent Registry and OS Agent Run all live in alaiy_os.
#
# Nothing else, and no connector. An agent here reaches whatever a site happens to
# have through a seam of its own — the listing agent takes its channels from the
# `listing_channels` hook — so this app installs on a bench with no sales channel,
# no supplier and no marketplace credentials at all. That is what makes it
# shippable by default rather than something a deployment opts into.
required_apps = ["alaiy_os"]

# ---------------------------------------------------------------------------
# Installation / migration
# ---------------------------------------------------------------------------
# registry.sync() walks agents/ and upserts every agent it finds, on install and
# on every migrate, so editing a prompt and migrating reconciles it onto the site.
#
# Each agent lands **disabled**. The code ships with the app; whether a site runs
# any given agent is a decision made in the Agents settings screen, and it is
# `is_enabled` that Ask Alaiy reads — see registry.py.
#
# The exception is a connector agent, which lands enabled: it exists only because
# a connector was installed and configured, and asking a site to choose the same
# connector twice is not a safety property. `agents/connector/meta.py` asks for
# that, and registry.py's `enabled_on_insert` is how.
#
# ---------------------------------------------------------------------------
# Seams this app reads
# ---------------------------------------------------------------------------
# `listing_channels`   — what a marketplace needs from a listing, for the ONE
#                        listing agent. See agents/listing/channels.py.
# `connector_agents`   — a connector's questions and the tools that answer them.
#                        One agent is built per entry, here rather than there:
#                        the connector owns what can be asked, this app owns the
#                        model, the prompt, the turn budget and the reply shape.
#                        See agents/connector/meta.py.
#
# Neither is declared here. Both point the same way — this app looks for
# connectors, connectors do not look for this app — so a bench with no connector
# at all installs and runs exactly as it does today, with no agent registered
# from either seam and nothing to configure.
after_install = ["alaiy_os_agents.registry.sync"]
after_migrate = ["alaiy_os_agents.registry.sync"]

# ---------------------------------------------------------------------------
# Ask Alaiy
# ---------------------------------------------------------------------------
# chat_tool_sources: contributes `listing_bulk_enrich_from_csv`, which turns an
# attached spreadsheet into one bulk enrich batch (agents/listing/chat_tools.py ->
# api.bulk_enrich -> bulk.py). The chat's own listing route is `run_agent`, which
# enriches a single product; without this, a model handed a fifty-row sheet has no
# correct move and improvises one from the handful of rows its file-reader previews.
# This tool reads the file server-side, so the rows never pass through the model and
# cannot be invented.
#
# Withheld from a user who cannot create an OS Agent Run, and from a site with the
# listing agent disabled. That second gate is the source's own: core's seam filters
# only `OS Agent Tool` rows on `is_enabled`, and a `chat_tool_sources` contribution
# goes straight through — so without it, disabling the agent would stop being a
# complete off switch. See chat_tools.source().
chat_tool_sources = ["alaiy_os_agents.agents.listing.chat_tools.source"]

# ---------------------------------------------------------------------------
# Uninstallation
# ---------------------------------------------------------------------------
# Remove every agent's registry row. OS Agent Run history is kept.
before_uninstall = ["alaiy_os_agents.registry.unregister"]
