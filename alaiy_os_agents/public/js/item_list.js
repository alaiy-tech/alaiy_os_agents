// Adds "Create Listings" to the Item list's Actions menu. ERPNext owns the list's
// settings, so this extends its onload instead of replacing it.
(() => {
	const settings = (frappe.listview_settings["Item"] ||= {});
	const onload = settings.onload;

	settings.onload = function (listview) {
		onload && onload.call(this, listview);
		listview.page.add_actions_menu_item(__("Create Listings"), () => create_listings(listview));
	};

	function create_listings(listview) {
		const products = listview.get_checked_items(true);
		frappe.call({
			method: "alaiy_os_agents.agents.listing.api.get_registrable_channels",
			callback: ({ message: channels }) => {
				if (!channels.length) {
					frappe.msgprint(__("No sales channel on this site can list a new Item."));
					return;
				}
				frappe.prompt(
					[
						{
							fieldname: "channel",
							fieldtype: "Select",
							label: __("Channel"),
							options: channels,
							default: channels[0].value,
							reqd: 1,
						},
					],
					({ channel }) => {
						frappe.call({
							method: "alaiy_os_agents.agents.listing.api.bulk_enrich",
							args: { products, channel, register: 1 },
							freeze: true,
							freeze_message: __("Starting the listing agent..."),
							callback: ({ message: r }) => {
								const errors = Object.entries(r.errors || {});
								frappe.msgprint({
									title: __("Create Listings"),
									indicator: errors.length ? "orange" : "green",
									message:
										__("Listing {0} Items in {1}.", [
											r.resolved.length,
											frappe.utils.get_form_link("Listing Bulk Enrich", r.batch, true),
										]) +
										errors
											.map(([item, error]) => `<br>${item}: ${frappe.utils.escape_html(error)}`)
											.join(""),
								});
								listview.clear_checked_items();
							},
						});
					},
					__("Create Listings"),
					__("Start")
				);
			},
		});
	}
})();
