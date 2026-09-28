from datetime import date, timedelta

from apps.contracts.models import Contract, ContractType
from apps.equipment.models import Asset
from apps.reports.services import attention_items, nav_counts, overview_page, work_orders_opened_by_type
from apps.web import charts
from apps.web.overview import fleet_strip, kpi_tiles, pm_trend_chart
from apps.workorders.services import assign, change_status, create_service_request, create_work_order

TODAY = date.today()


def _kinds(items):
    return [(it["rail"], it.get("asset") or it.get("wo") or it.get("contract") or it.get("recall")) for it in items]


def test_attention_list_follows_the_mock_order(ctx, dept, vent, pump, vent_model, pump_recall, techs):
    vent.next_pm_on = TODAY - timedelta(days=4)  # life-support PM overdue
    vent.save()
    expired = Contract.objects.create(reference="OLD", vendor="V", type=ContractType.OEM, start_on=TODAY - timedelta(days=400),
                                      end_on=TODAY - timedelta(days=2))
    expired.add_assets([pump])
    ending = Contract.objects.create(reference="SOON", vendor="V", type=ContractType.OEM, start_on=TODAY - timedelta(days=300),
                                     end_on=TODAY + timedelta(days=10))
    Contract.objects.create(reference="LATER", vendor="V", type=ContractType.OEM, start_on=TODAY, end_on=TODAY + timedelta(days=60))  # outside 30 days
    sr = create_service_request(asset=pump, department=dept, problem="Won't power on", urgency="critical")
    crit = create_work_order(asset=vent, type="repair", priority="critical", problem="Alarm")
    assign(crit, technician=techs["dana"])
    waiting = create_work_order(asset=pump, type="repair", priority="normal", problem="Keypad", opened_on=TODAY - timedelta(days=9))
    assign(waiting, technician=techs["dana"])
    change_status(waiting, "awaiting_parts")
    create_work_order(asset=pump, type="repair", priority="normal", problem="Waiting, but recent")
    items = attention_items()
    assert _kinds(items) == [("crit", vent.tag), ("warn", str(pump_recall.id)), ("warn", str(expired.id)), ("warn", str(ending.id)),
                             ("crit", sr.work_order.number), ("crit", crit.number), ("warn", waiting.number)]
    assert items[1]["right"] == "1 devices"  # active pumps on the recalled model


def test_high_risk_overdue_is_capped_at_three(ctx, dept, pump_model):
    for i in range(5):
        Asset.objects.create(tag=f"P{i}", device_model=pump_model, department=dept, next_pm_on=TODAY - timedelta(days=i + 1))
    assert [it["asset"] for it in attention_items()] == ["P4", "P3", "P2"]  # most overdue first


def test_nav_counts_flag_unassigned_portal_requests(ctx, dept, vent, pump):
    create_work_order(asset=pump, type="pm", priority="normal", problem="PM")
    counts = nav_counts()
    assert (counts["equipment"], counts["workorders"], counts["workorders_hot"]) == (2, 1, False)
    create_service_request(asset=vent, department=dept, problem="Alarm", urgency="normal")
    assert nav_counts()["workorders_hot"] is True


def test_nav_counts_contracts_badge(ctx, vent, pump):
    assert nav_counts()["contracts"] is None  # no badge when nothing needs attention
    ending = Contract.objects.create(reference="SOON", vendor="V", type=ContractType.OEM, start_on=TODAY - timedelta(days=300),
                                     end_on=TODAY + timedelta(days=10))
    expired_empty = Contract.objects.create(reference="OLD", vendor="V", type=ContractType.OEM, start_on=TODAY - timedelta(days=400),
                                            end_on=TODAY - timedelta(days=2))
    Contract.objects.create(reference="LATER", vendor="V", type=ContractType.OEM, start_on=TODAY, end_on=TODAY + timedelta(days=60))
    assert nav_counts()["contracts"] == 1  # ending soon counts; an expired contract with no devices does not
    expired_empty.add_assets([pump])
    counts = nav_counts()
    assert counts["contracts"] == 2 and counts["contracts_hot"] is True
    assert ending.end_on > TODAY


def test_opened_by_type_groups_months_and_folds_minor_types(ctx, vent):
    create_work_order(asset=vent, type="pm", priority="normal", problem="PM", opened_on=TODAY)
    create_work_order(asset=vent, type="safety", priority="normal", problem="Safety", opened_on=TODAY)
    create_work_order(asset=vent, type="repair", priority="normal", problem="Last month", opened_on=TODAY.replace(day=1) - timedelta(days=1))
    data = work_orders_opened_by_type(TODAY.year, TODAY.month)
    by_key = {s["key"]: s["values"] for s in data["series"]}
    assert len(data["months"]) == 6 and data["months"][-1] == (TODAY.year, TODAY.month)
    assert by_key["pm"][-1] == 1 and by_key["other"][-1] == 1 and by_key["repair"][-2:] == [1, 0]


def test_overview_page_skips_deltas_without_history(ctx, vent):
    data = overview_page(TODAY.year, TODAY.month)
    assert data["prev"] is None
    tiles = kpi_tiles(data)
    assert len(tiles) == 8 and not any("prior month" in text for t in tiles for text, _ in t["parts"])
    create_work_order(asset=vent, type="repair", priority="normal", problem="Old", opened_on=TODAY - timedelta(days=62))
    assert overview_page(TODAY.year, TODAY.month)["prev"] is not None


def test_fleet_strip_percentages_exclude_retired(ctx):
    strip = fleet_strip({"compliant": 3, "pm_due": 1, "pm_overdue": 0, "open_recall": 0, "in_repair": 0, "out_of_service": 0, "retired": 6})
    assert strip["active"] == 4 and strip["retired"] == 6
    assert [round(s["pct"]) for s in strip["segments"][:2]] == [75, 25]


def test_pm_chart_leaves_gaps_for_months_with_nothing_due():
    series = [{"year": 2026, "month": m, "due": 0 if m == 2 else 4, "on_time": 4, "rate": 100.0} for m in range(1, 4)]
    chart = pm_trend_chart(series, series)
    assert chart["paths"][0]["d"].count("M") == 2  # the gap starts a new segment
    assert chart["grid"][0]["label"] == "90%"


def test_nice_max_rounds_up_to_readable_steps():
    assert [charts.nice_max(v) for v in (0, 7, 13, 24, 230)] == [1, 10, 20, 25, 250]


def test_hbars_benchmark_marker_stays_inside_the_chart_when_every_bar_is_below_it():
    c = charts.hbars([("A", 2.0), ("B", 0.5)], fmt=lambda v: f"{v:.1f}%", marker=6.0)
    assert c["left"] < c["marker_x"] <= c["w"] - 66 and c["marker_title"] == "Benchmark: 6.0%"
    assert all(r["bar_w"] < c["marker_x"] - c["left"] for r in c["rows"])
    assert charts.hbars([("A", 2.0)], fmt=str)["marker_x"] is None


def test_donut_arcs_cover_the_circle_in_order():
    import math

    c = charts.donut([{"label": "a", "value": 3, "color": "x", "text": "3"}, {"label": "b", "value": 1, "color": "y", "text": "1"}], center="4")
    circ = 2 * math.pi * c["r"]
    assert c["arcs"][0]["dash"] == f"{0.75 * circ:.2f} {0.25 * circ:.2f}" and c["arcs"][0]["offset"] == "0.00"
    assert c["arcs"][1]["dash"] == f"{0.25 * circ:.2f} {0.75 * circ:.2f}" and c["arcs"][1]["offset"] == f"{-0.75 * circ:.2f}"
    assert c["arcs"][1]["title"] == "b: 1" and c["center"] == "4"
    assert charts.donut([], center="$0")["arcs"] == []  # nothing to draw, nothing divides by zero


# --- recall integration points (slice 6) --------------------------------------------------------

def test_recall_tile_links_only_when_given_a_url(ctx, pump_recall):
    data = overview_page(TODAY.year, TODAY.month)
    static = next(t for t in kpi_tiles(data) if t["label"] == "Recall alerts received")
    assert "url" not in static and static["value"] == "1" and ("1 need action", "") in static["parts"]
    linked = next(t for t in kpi_tiles(data, recalls_url="/recalls/") if t["label"] == "Recall alerts received")
    assert linked["url"] == "/recalls/"


def test_overview_links_recall_tile_and_attention_item(client, make_user, pump, pump_recall):
    client.force_login(make_user("director"))
    body = client.get("/").content.decode()
    assert '<a class="kpi" href="/recalls/">' in body
    assert f'<a class="li" href="/recalls/?match={pump_recall.id}">' in body and pump_recall.alert.title in body
    assert f'hx-get="/recalls/?match={pump_recall.id}"' not in body  # the Recalls screen is a page, not a drawer
