"""Slice 36's demo: Riverside plans by a July fiscal year and CE recorded the manufacturers' support and where expected lives come from
on a few models (seed_demo's _replacement_planning, after every draw), so the Replacement planning report's sections and the device
drawer's outlook have something real. On PostgreSQL the rule, the report, and the drawer are read as the runtime role too."""
import re
from datetime import timedelta
from io import StringIO

import pytest
from django.core.management import call_command
from django.utils import timezone
from pg_helpers import as_app_role, needs_postgres

from apps.accounts.models import User
from apps.capital import priority
from apps.demo.management.commands import seed_demo
from apps.equipment.models import Asset, AssetStatus, DeviceModel, LifeSource
from apps.equipment.services import support_status
from apps.facility import fiscal
from apps.facility.services import get_settings
from apps.reports.fleet import REPLACE_LIMIT, report_replace
from apps.tenants.context import tenant_context
from apps.tenants.models import Tenant
from apps.workorders.models import RepairCheck, WorkOrder

DIALYSIS, VITALS, MONITOR, BED, PUMP = "2008T BlueStar", "Connex Spot", "IntelliVue MX750", "Centrella", "Alaris 8015 PCU"
DAY = timedelta(days=1)


@pytest.fixture
def seeded(db):
    call_command("seed_demo", stdout=StringIO())
    return Tenant.objects.get(slug="riverside")


def first_tag(model: str) -> str:
    return Asset.objects.filter(device_model__model=model).order_by("tag").values_list("tag", flat=True).first()


def test_riverside_plans_by_july_and_ce_recorded_support_and_life_sources_on_a_few_models(seeded):
    with tenant_context(seeded):
        today = timezone.localdate()
        assert get_settings().fiscal_year_start_month == seed_demo.FISCAL_YEAR_START == 7
        models = {dm.model: dm for dm in DeviceModel.objects.all()}
        # the beds' life lowered from MODELS' 12 to the manufacturer's 10; the pumps' life kept, its source recorded
        assert (models[BED].expected_life_years, models[BED].life_source) == (10, LifeSource.MANUFACTURER)
        assert (models[PUMP].expected_life_years, models[PUMP].life_source) == (10, LifeSource.AHA)
        assert {m for m, dm in models.items() if dm.life_source} == {BED, PUMP}
        # the dialysis machines: the sale ended, then support (each the last day of a month), the notice on file, checked recently
        dm = models[DIALYSIS]
        assert dm.sale_ended_on < dm.support_ends_on < today and dm.parts_end_on is None
        assert (dm.sale_ended_on + DAY).day == (dm.support_ends_on + DAY).day == 1
        assert re.fullmatch(r"FSN-\d{4}-014", dm.support_notice) and dm.support_checked_on == today - 12 * DAY
        assert support_status(dm, today).key == "support_ended" and support_status(dm, today).words.startswith("Manufacturer support ended ")
        # the vital signs monitors: parts available until a later day; the patient monitors: supported, checked, nothing announced
        assert models[VITALS].parts_end_on > today and support_status(models[VITALS], today).words.startswith("Parts available until ")
        assert support_status(models[MONITOR], today).key == "supported" and models[MONITOR].support_notice == ""
        recorded = {DIALYSIS, VITALS, MONITOR}
        assert {m for m, dm in models.items() if dm.support_checked_on} == recorded
        assert all(support_status(dm, today).key == "unknown" for m, dm in models.items() if m not in recorded)
        # each through update_device_model by the CE manager: the model's history says who
        manager = User.objects.get(username=f"{seed_demo.SUPPORT_RECORDED_BY}@riverside.example")
        for name in (BED, PUMP, *recorded):
            latest = DeviceModel.history.filter(id=models[name].pk).order_by("-history_date", "-history_id").first()
            assert (latest.history_type, latest.history_user_id) == ("~", manager.pk), name
    with tenant_context(Tenant.objects.get(slug="riverside-north")):  # the North Campus: a calendar fiscal year, nothing recorded
        assert fiscal.start_month() == 1
        assert not DeviceModel.objects.exclude(life_source="").exists() and not DeviceModel.objects.filter(support_checked_on__isnull=False).exists()


def test_the_replacement_report_has_something_in_every_section(client, seeded):
    with tenant_context(seeded):
        today = timezone.localdate()
        now = fiscal.current_fy(today, 7)
        r = report_replace(today)
        scored = priority.score_assets(priority.scored_assets(), today)
        assert r["scored_count"] == len(scored) == len(r["rows"]) and r["ranked_count"] > REPLACE_LIMIT == len(r["top"])

        # the top shows chips: each of the top is flagged (the dialysis machines' support, the pumps' recall); review flags only
        assert all(p.flags for p in r["top"])
        assert {f for p in scored for f in p.flags} == {priority.SUPPORT_ENDED, priority.RECALL} and not any(p.must_act for p in scored)
        dialysis = [p for p in scored if p.asset.device_model.model == DIALYSIS]
        assert len(dialysis) == 8 and all(priority.SUPPORT_ENDED in p.flags for p in dialysis)
        assert {p.suggested_fy for p in dialysis} == {now}  # support ended: the current fiscal year

        # the beds: a few past their lowered life, not all; every other fleet within its life (install dates are drawn within it)
        fleets = {f["device_model"].model: f for f in r["fleets"]}
        assert (fleets[BED]["past_life"], fleets[BED]["devices"]) == (4, 25)
        assert all(f["past_life"] == 0 for m, f in fleets.items() if m != BED)
        assert (fleets[DIALYSIS]["support"].key, fleets[VITALS]["support"].key, fleets[MONITOR]["support"].key) == (
            "support_ended", "announced", "supported")

        # the outlook: July's current fiscal year and the four after it, devices in more than one, one device not placed
        years = r["outlook"]["years"]
        assert [y["fy"] for y in years] == list(range(now, now + 5)) and sum(1 for y in years if y["devices"]) > 1
        assert years[0]["devices"] >= len(dialysis) and r["outlook"]["not_placed"] == 1
        # the vital signs monitors whose life ends after their parts do are placed in the parts' fiscal year
        vitals = [p for p in scored if p.asset.device_model.model == VITALS]
        assert max(p.suggested_fy for p in vitals) == fiscal.fiscal_year_of(DeviceModel.objects.get(model=VITALS).parts_end_on, 7)

        # what the scores do not know: the model-level gaps counted with their models; the new defibrillator waiting for its incoming
        # inspection has no install date, so it is listed apart, never ranked
        gaps = {g["key"]: g for g in r["gaps"]}
        assert (gaps[priority.LIFE_SOURCE]["models"], gaps[priority.SUPPORT_UNCHECKED]["models"]) == (8, 7)
        assert gaps[priority.LIFE_SOURCE]["devices"] > 0 and gaps[priority.SUPPORT_UNCHECKED]["devices"] > 0
        assert (gaps[priority.NO_INSTALL_DATE]["devices"], gaps[priority.NO_ESTIMATE]["devices"]) == (1, 0)
        assert [(p.asset.tag, p.unranked_reasons) for p in r["unranked"]] == [("CE-11001", (priority.NO_INSTALL_DATE,))]

        # the demo's records begin 180 days back: failures and service cost are unknown on every device (points unknown, never 0)
        for p in scored:
            assert p.component(priority.RELIABILITY).input == p.component(priority.COST).input == priority.TOO_LITTLE_HISTORY
            assert p.points_unknown >= priority.WEIGHTS[priority.RELIABILITY] + priority.WEIGHTS[priority.COST]

        # the bed found beyond repair stays retired (slice 30's binder story has no repairs gap): never a row, no must-act flag
        bed = WorkOrder.objects.get(repair_check=RepairCheck.NOT_RETURNED).asset
        assert bed.status == AssetStatus.RETIRED and bed.device_model.model == BED
        assert bed.tag not in {row[0] for row in r["rows"]} and priority.for_asset(bed, today) is None
        tag = first_tag(DIALYSIS)

    # the drawer's outlook on a flagged device
    client.force_login(User.objects.get(username="kim@riverside.example"))
    response = client.get(f"/equipment/{tag}/?tab=costs", HTTP_HX_REQUEST="true")
    outlook = response.context["costs"]["outlook"]
    assert response.status_code == 200 and priority.SUPPORT_ENDED in outlook["priority"].flags and outlook["priority"].suggested_fy == now


@needs_postgres
def test_the_seeded_plan_reads_the_same_under_the_policies(client, seeded):
    """The rule reads the models' list price history (a historical table: no policy, filtered by this facility) and every other input
    under row-level security: as the runtime role it scores the seeded fleet exactly as the owner does, and the screens answer."""
    def scores():
        return sorted((p.asset.tag, p.score, p.points_unknown, p.flags, p.gaps, p.estimate.words, p.suggested_fy, p.support.words)
                      for p in priority.score_assets(priority.scored_assets(), timezone.localdate()))

    with tenant_context(seeded):
        before = scores()
        tag = first_tag(DIALYSIS)
    assert any(row[5].startswith("List price, set ") for row in before)
    client.force_login(User.objects.get(username="kim@riverside.example"))
    as_app_role()
    with tenant_context(seeded):
        assert scores() == before
    for url in ("/reports/replace/", "/reports/replace.csv", "/print/reports/replace/", f"/equipment/{tag}/?tab=costs"):
        r = client.get(url)
        assert r.status_code == 200, url
        if hasattr(r, "streaming_content"):
            assert b"".join(r.streaming_content), url
