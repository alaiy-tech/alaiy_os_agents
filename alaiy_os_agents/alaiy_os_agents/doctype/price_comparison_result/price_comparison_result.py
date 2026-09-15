# Copyright (c) 2026, Alaiy and contributors
# For license information, please see license.txt
"""One product's latest price comparison. Upserted by
`agents/pricing/tools.py::save_comparison` — see that module for how the
suggested price is computed."""

from frappe.model.document import Document


class PriceComparisonResult(Document):
	pass
