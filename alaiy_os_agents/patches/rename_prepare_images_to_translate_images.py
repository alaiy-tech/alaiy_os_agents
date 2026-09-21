# Copyright (c) 2026, Alaiy and contributors
# For license information, please see license.txt
"""`prepare_images` on Listing Bulk Enrich is now `translate_images` — the
checkbox always meant "translate the Chinese text off supplier photos", and
now that a second, independent image toggle (`white_bg_images`) exists on the
same doctype, the old name was actively misleading rather than just vague.

`rename_field` carries the column's existing values across (a batch someone
left with the box ticked keeps meaning the same thing); the new
`white_bg_images` field is brand new and needs no migration.
"""

import frappe
from frappe.model.utils.rename_field import rename_field


def execute():
	if not frappe.db.table_exists("Listing Bulk Enrich"):
		return
	if not frappe.db.has_column("Listing Bulk Enrich", "prepare_images"):
		return
	rename_field("Listing Bulk Enrich", "prepare_images", "translate_images")
