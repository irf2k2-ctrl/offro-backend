"""The old "Pricing & GST" dashboard screen is gone; Pricing & Plans is the single place. The backend endpoints it used stay (the
merchant, influencer and customer flows read the same `pricing` document) and the role permission name is unchanged so saved roles
keep working."""
import os
import re

ROOT = os.path.dirname(os.path.abspath(__file__))
T = open(os.path.join(ROOT, "templates/admin_dashboard.html"), encoding="utf-8").read()
JS = open(os.path.join(ROOT, "static/js/pricing_plans.js"), encoding="utf-8").read()
ADMIN = open(os.path.join(ROOT, "routers/admin.py"), encoding="utf-8").read()


def test_old_menu_item_section_and_scripts_are_removed():
    for gone in ['id="section-pricing"', "showSection('pricing',this)", "function loadPricing", "function savePricing", "loadPricingConfig",
                 "savePricingConfig", "plans_editor", "gst_input", "influencer_fee_input", "conversion_rate_input", ".plan-row", "bv_pricing_msg"]:
        assert gone not in T, gone
    assert not re.search(r"""name\s*===?\s*["']pricing["']""", T)
    labels = re.findall(r">\s*(Pricing[^<]*)</a>", T)
    assert any("Pricing &amp; Plans" in x for x in labels) and not any("Pricing & GST" in x for x in labels), labels


def test_single_pricing_menu_entry():
    assert T.count("showSection('pricing_plans',this)") == 1


def test_role_permission_name_is_kept_so_saved_roles_still_work():
    assert '"Pricing & GST":     ["pricing_plans"]' in T
    assert '"Pricing & GST"' in open(os.path.join(ROOT, "routers/dashboard_auth.py"), encoding="utf-8").read()


def test_backend_endpoints_are_not_removed():
    for r in ['@router.get("/pricing")', '@router.put("/pricing")', '@router.get("/banner-pricing")', '@router.put("/banner-pricing")']:
        assert r in ADMIN, r


def test_everything_the_old_screen_edited_is_editable_in_the_new_module():
    for frag in ["fee: val('pp_f_price')", "gst_percent = val('pp_f_gst')", "price_per_day", "free_product_limit", "product_max_validity_days",
                 "plans: [{ id: e.id, price", "conversion_rate: val('pp_r_rate')", "min_withdraw_points: val('pp_r_min')", "tax_percent: val('pp_t_pct')",
                 "enabled: on"]:
        assert frag in JS, frag


def test_restyled_to_dashboard_look():
    css = open(os.path.join(ROOT, "static/css/pricing_plans.css"), encoding="utf-8").read()
    assert ".pp .pp-tab.active{background:var(--primary" in css and ".pp .pp-table th{" in css and "background:var(--primary" in css
