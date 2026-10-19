"""The demo seed is what every screenshot and smoke test starts from; make sure it still builds every state the screens show."""
from datetime import date, timedelta
from io import StringIO

from django.core.management import call_command
from vendor_review_days import ended

from apps.accounts import people
from apps.accounts.backends import find_account
from apps.accounts.models import User
from apps.contracts.models import Contract
from apps.credentials.models import Technician
from apps.demo.management.commands import seed_demo
from apps.equipment.models import Asset
from apps.facility.services import get_settings
from apps.recalls import services as rc
from apps.recalls.models import AlertMatch
from apps.tenants.context import tenant_context
from apps.tenants.models import Tenant
from apps.workorders.models import WorkOrder, WoType


def test_seed_demo_builds_the_demo_tenant(db):
    out = StringIO()
    call_command("seed_demo", stdout=out)
    tenant = Tenant.objects.get(slug="riverside")
    assert "Sign in as kim@riverside.example" in out.getvalue()
    with tenant_context(tenant):
        assert User.objects.filter(tenant=tenant).count() == 14 and Contract.objects.count() == 6
        assert rc.group_counts() == {"all": 4, "action": 2, "progress": 1, "closed": 1}
        in_progress = AlertMatch.objects.get(status=AlertMatch.Status.IN_PROGRESS)
        p = rc.progress(in_progress)
        # 60 pumps of ours and, slice 29, the vendor loaner pump on site when the batch was opened (its work order went to its owner)
        assert p["total"] == 61 and p["completed"] == 20 and WorkOrder.objects.filter(type=WoType.RECALL, alert=in_progress.alert).count() == 61
        closed = AlertMatch.objects.get(status=AlertMatch.Status.CLOSED)
        assert closed.closed_on == date.today() - timedelta(days=40) and closed.disposition_note.startswith("Gaskets replaced")
        s = get_settings()
        assert s.pk and s.portal_hotline == "ext. 4400" and s.repair_budget_monthly == 52000 and s.portal_require_callback
    call_command("seed_demo", stdout=StringIO())  # a second run is a no-op
    assert Tenant.objects.count() == 2  # Riverside and Kim's North Campus (slice 22)


def test_seed_demo_gives_kim_a_second_facility(db, client):
    """Slice 22: Kim directs Riverside North Campus too, as the same person: linked, joined with her one password, and signed in
    to Riverside last, so a sign-in lands there and the facility menu switches to the North Campus."""
    out = StringIO()
    call_command("seed_demo", stdout=out)
    riverside, north = Tenant.objects.get(slug="riverside"), Tenant.objects.get(slug="riverside-north")
    assert north.name == "Riverside North Campus" and "Seeded Riverside North Campus: 16 devices, 2 technicians" in out.getvalue()
    kim = User.objects.get(username="kim@riverside.example")
    there = User.objects.get(tenant=north, email="kim@riverside.example")
    assert kim.person and there.person == kim.person and there.username == "kim@riverside.example@riverside-north"
    assert people.is_joined(there) and there.password == kim.password and there.check_password("DemoPass-2026")
    assert find_account("kim@riverside.example") == kim
    assert [(m["name"], m["current"], m["invited"]) for m in people.facility_menu(kim)] == [
        ("Riverside North Campus", False, False), ("Riverside Regional Medical Center", True, False)]
    with tenant_context(north):
        assert there.role.slug == "director" and Asset.objects.count() == 16 and Technician.objects.count() == 2
        assert WorkOrder.objects.count() == 7 and WorkOrder.objects.filter(status="closed").count() == 4
        assert WorkOrder.objects.filter(type=WoType.PM, status="closed").exclude(pm_result="").count() == 2
        assert User.objects.filter(tenant=north).count() == 3
        # slice 27: its medium and low risk PMs are on time by the end of their due month, its default policy line saying so
        s = get_settings()
        assert (s.pm_window_high, s.pm_window_other, s.pm_window_other_days) == ("due_date", "due_month", None)
        assert s.policy_medium_low == "AEM allowed, complete by the end of the due month"
    with tenant_context(riverside):
        assert get_settings().pm_window_other == "due_date"  # Riverside keeps the default
    with tenant_context(riverside):
        assert User.objects.filter(tenant=riverside).count() == 14  # the North Campus's staff are its own
    assert client.post("/login/", {"username": "kim@riverside.example", "password": "DemoPass-2026"}).status_code == 302
    r = client.post("/account/facility/", {"account": there.pk, "screen": "workorders"})
    assert r.status_code == 302 and int(client.session["_auth_user_id"]) == there.pk
    call_command("seed_demo", stdout=StringIO())  # a second run adds nothing
    assert User.objects.filter(email="kim@riverside.example").count() == 2
    with tenant_context(north):
        assert Asset.objects.count() == 16


def test_seed_demo_gives_the_survey_binder_a_few_deliberate_items(db):
    """Slice 25: work done by technicians credentialed for the device, one credential that lapsed and was renewed with a repair done
    in the lapse, a reason on every late life-support and high-risk PM but the latest, four new devices (one gap, one finding: slice
    26), one model past its yearly risk review, and recall matches dated by when their notice was published."""
    from django.utils import timezone

    from apps.core.days import local_day
    from apps.credentials.models import Credential
    from apps.credentials.services import qualification
    from apps.equipment.models import AddedAs, RiskClass
    from apps.pm.services import missed_pms
    from apps.reports.survey import CHECK, FINDING, GAP, default_period, inspections, inventory

    call_command("seed_demo", stdout=StringIO())
    with tenant_context(Tenant.objects.get(slug="riverside")):
        today = timezone.localdate()
        lapse = Credential.objects.select_related("technician").get(technician__name=seed_demo.LAPSE[0], scope=seed_demo.LAPSE[1],
                                                                    value=seed_demo.LAPSE[2])
        expired = today - timedelta(days=seed_demo.LAPSE_EXPIRED_DAYS_AGO)
        renewed = today - timedelta(days=seed_demo.LAPSE_RENEWED_DAYS_AGO)
        assert [(r.history_type, local_day(r.history_date), r.expires_on) for r in lapse.history.order_by("history_date")] == [
            ("+", lapse.issued_on, expired), ("~", renewed, lapse.expires_on)]
        in_lapse = WorkOrder.objects.filter(assigned_to=lapse.technician, asset__device_model__category=seed_demo.LAPSE[2],
                                            completed_on__gt=expired, completed_on__lt=renewed)
        assert in_lapse.filter(problem=seed_demo.LAPSE_PROBLEM, status="closed").count() == 1
        work = WorkOrder.objects.filter(assigned_to__isnull=False, vendor_service=False).select_related("asset__device_model", "assigned_to")
        work = work.prefetch_related("assigned_to__credentials")
        assert all(qualification(w.assigned_to, w.asset, w.opened_on).ok for w in work)  # with the lapse renewed: everyone's credentialed
        late = missed_pms(today).filter(asset__device_model__risk_class__in=(RiskClass.LIFE_SUPPORT, RiskClass.HIGH)).order_by("due_on", "number")
        assert len(late) >= 2 and all(w.late_reason for w in late[: len(late) - 1]) and late[len(late) - 1].late_reason == ""
        p = default_period(today)
        new = inspections.build(p, None)
        assert [(g.kind, g.record) for g in new.gaps] == [(FINDING, "CE-11004"), (GAP, "CE-11003")]
        figures = {f.label: f.value for f in new.figures}
        # slice 29: three of the new devices are temporary (two rentals and the loaner, inspected the day they came); the demo unit was
        # entered as already on site
        assert figures["New devices added"] == 7 and figures["Inspected before first use"] == 4 and figures["Waiting: never in service yet"] == 1
        assert figures["In use before its incoming inspection"] == 1 and figures["In service with no incoming inspection passed"] == 1
        assert figures["Failed incoming inspections"] == 1 and figures["Inspected after first use"] == 0
        assert figures["Entered as already in use"] == 1
        assert Asset.objects.filter(added_as=AddedAs.NEW).count() == 7 and Asset.objects.filter(added_as=AddedAs.NEW, ownership="owned").count() == 4
        # the waiting device: no gap; slice 29, the demo unit has no owner's PM date (a medium-risk check)
        assert [(g.kind, g.record) for g in inventory.build(p, None).gaps] == [(CHECK, "T-0103"), (CHECK, "Zoll R Series Plus")]
        assert [g.record for g in new.gaps + inventory.build(p, None).gaps if g.kind == FINDING] == ["CE-11004"]
        assert all(local_day(m.created_at) == m.alert.published_on for m in AlertMatch.objects.select_related("alert"))


def test_seed_demo_adds_its_new_devices_through_the_incoming_inspection(db):
    """Slice 26: CE-11001 waits (its inspection open and nobody's), CE-11002 failed and passed its re-inspection (the evidence, the
    failed one listed), CE-11004 was put in use before its inspection in an emergency and inspected the next day, CE-11003 came in
    service with none. Every inspection completed with the model's checklist and its leakage reading."""
    from django.utils import timezone

    from apps.equipment.models import AssetStatus
    from apps.pm.dates import add_months
    from apps.reports.survey import default_period
    from apps.reports.survey import inspections as section
    from apps.workorders import inspections
    from apps.workorders.models import InspectionResult, Priority, WoStatus

    call_command("seed_demo", stdout=StringIO())
    with tenant_context(Tenant.objects.get(slug="riverside")):
        today = timezone.localdate()
        days ={tag: today - timedelta(days=ago) for tag, _model, _dept, ago, _stage in seed_demo.NEW_DEVICES}
        waiting, failed, used, in_use = (Asset.objects.get(tag=t) for t in ("CE-11001", "CE-11002", "CE-11004", "CE-11003"))
        assert (waiting.awaiting_inspection, waiting.status, waiting.next_pm_on) == (True, AssetStatus.OUT_OF_SERVICE, None)
        open_ = inspections.open_inspection(waiting)
        assert open_.opened_on == days["CE-11001"] and open_.assigned_to_id is None and list(inspections.waiting_for_inspector()) == [open_]
        first = WorkOrder.objects.get(asset=failed, type=WoType.INSPECTION, follow_up_of__isnull=True)
        again = first.follow_ups.get()
        inspected = days["CE-11002"] + timedelta(days=seed_demo.INSPECTED_AFTER_DAYS)
        passed_on = inspected + timedelta(days=seed_demo.REINSPECTED_AFTER_DAYS)
        assert (first.inspection_result, first.status, first.completed_on) == (InspectionResult.FAILED, WoStatus.CLOSED, inspected)
        assert first.checklist_results[seed_demo.LEAKAGE]["reading"] == seed_demo.INCOMING_FAIL_READING
        assert (again.inspection_result, again.status, again.completed_on, again.assigned_to_id) == (
            InspectionResult.PASSED, WoStatus.CLOSED, passed_on, first.assigned_to_id)
        assert again.resolution.startswith("Incoming inspection passed per ") and all(s["result"] == "pass" for s in again.checklist_results)
        assert (failed.awaiting_inspection, failed.status, failed.installed_on) == (False, AssetStatus.IN_SERVICE, passed_on)
        assert failed.next_pm_on == add_months(passed_on, failed.pm_interval_months) and failed.serial.endswith("S")
        assert inspections.state(failed).passed == again
        done = WorkOrder.objects.get(asset=used, type=WoType.INSPECTION)
        assert (done.inspection_result, done.completed_on, done.priority) == (InspectionResult.PASSED, days["CE-11004"] + timedelta(days=1),
                                                                              Priority.HIGH)
        assert (used.awaiting_inspection, used.status) == (False, AssetStatus.IN_SERVICE)
        assert used.next_pm_on == add_months(done.completed_on, used.pm_interval_months)
        [use] = inspections.uses_before([used.pk])[used.pk]
        assert (use.on, use.reason, use.by.username) == (days["CE-11004"], "emergency", "kim@riverside.example")
        assert not in_use.awaiting_inspection and not WorkOrder.objects.filter(asset=in_use).exists()
        # ours (slice 29: the rentals' and the loaner's are done to the rental checklist, test_seed_demo_adds_its_temporary_devices)
        for wo in WorkOrder.objects.filter(type=WoType.INSPECTION, status=WoStatus.CLOSED, asset__ownership="owned"):
            assert all(s["reading"] for s in wo.checklist_results if s["measure"]) and len(wo.checklist_results) == len(seed_demo.CHECKLIST)
        s = section.build(default_period(today), None)
        rows = {r[0]: r for r in s.table("new_devices").rows()}
        assert [tag for tag in rows if tag.startswith("CE-")] == ["CE-11002", "CE-11004", "CE-11003", "CE-11001"]
        assert rows["CE-11002"][5:12] == [again.number, "Closed", "Passed", again.assigned_to.name, passed_on, passed_on, None]
        assert rows["CE-11001"][4:8] == ["Awaiting inspection", open_.number, "Open", ""]
        assert rows["CE-11004"][11:] == [1, "Emergency clinical need"]
        assert list(s.table("failed").rows()) == [[first.number, "CE-11002", inspected, first.assigned_to.name, again.number, "Closed", passed_on]]
        finding = s.gaps[0].text
        assert finding.startswith("CE-11004 was in use 1 day before its incoming inspection: Emergency clinical need (approved by Kim Alvarez;")


def test_seed_demo_adds_its_temporary_devices(db):
    """Slice 29: a rental bed with its owner's PM date, a vendor loaner pump standing in for one of ours out for vendor repair (on site
    when the pumps' recall batch was opened, so its recall work order is its owner's), a demo monitor entered as already on site, past
    its due back date with no owner's PM date, and a rental ventilator returned to its owner; the new ones inspected on the rental
    checklist the day they came, each dated as it happened, and each where the Overview and the binder say it is."""
    from django.utils import timezone

    from apps.core.days import local_day
    from apps.equipment import services as eq
    from apps.equipment.models import AddedAs, AssetStatus, Ownership, ReturnCleaning, ReturnData, SupportType
    from apps.reports.services import attention_items, overview_kpis
    from apps.reports.survey import default_period, inventory
    from apps.workorders.inspections import TEMPORARY_INCOMING
    from apps.workorders.models import InspectionResult, WoStatus

    call_command("seed_demo", stdout=StringIO())
    with tenant_context(Tenant.objects.get(slug="riverside")):
        today = timezone.localdate()
        vent, bed, loaner, demo = (Asset.objects.get(tag=t) for t in ("T-0098", "T-0101", "T-0102", "T-0103"))
        assert [a.ownership for a in (vent, bed, loaner, demo)] == [Ownership.RENTAL, Ownership.RENTAL, Ownership.LOANER, Ownership.DEMO]
        assert all(a.support_type == SupportType.OWNER and a.next_pm_on is None and a.acquisition_cost == 0 for a in (vent, bed, loaner, demo))
        assert (vent.status, eq.status_label(vent), vent.returned_on, vent.return_cleaning, vent.return_data) == (
            AssetStatus.RETIRED, "Returned to owner", today - timedelta(days=35), ReturnCleaning.DECONTAMINATED, ReturnData.CLEARED)
        assert vent.history.order_by("-history_date", "-history_id").first().history_change_reason == eq.RETURNED_REASON
        assert local_day(vent.history.order_by("-history_date", "-history_id").first().history_date) == vent.returned_on
        assert (bed.status, bed.owner_pm_due_on, bed.due_back_on) == (AssetStatus.IN_SERVICE, today + timedelta(days=150), today + timedelta(days=20))
        for a in (vent, bed, loaner):
            wo = WorkOrder.objects.get(asset=a, type=WoType.INSPECTION)
            assert (wo.inspection_result, wo.status, wo.completed_on, wo.opened_on) == (InspectionResult.PASSED, WoStatus.CLOSED, a.arrived_on,
                                                                                         a.arrived_on)
            assert len(wo.checklist_results) == len(TEMPORARY_INCOMING.checklist) and wo.assigned_to_id is not None
            first = a.history.order_by("history_date", "history_id").first()
            assert (first.history_type, local_day(first.history_date), local_day(a.created_at), a.added_as) == ("+", a.arrived_on, a.arrived_on,
                                                                                                                 AddedAs.NEW)
        ours = loaner.stands_in_for
        assert (ours.ownership, ours.status, loaner.status, loaner.due_back_on) == (Ownership.OWNED, AssetStatus.OUT_OF_SERVICE,
                                                                                  AssetStatus.IN_SERVICE, None)
        repair = WorkOrder.objects.get(asset=ours, type=WoType.REPAIR, status=WoStatus.IN_PROGRESS)
        assert (repair.vendor_service, repair.vendor_name, repair.tagged_out) == (True, "BD field service", True)
        recall = WorkOrder.objects.get(asset=loaner, type=WoType.RECALL)
        assert (recall.vendor_service, recall.vendor_name, recall.assigned_to_id, recall.status) == (True, "BD", None, WoStatus.OPEN)
        assert (demo.added_as, demo.status, demo.owner_pm_due_on, demo.arrived_on) == (AddedAs.EXISTING, AssetStatus.IN_SERVICE, None,
                                                                                       today - timedelta(days=40))
        assert demo.due_back_on == today - timedelta(days=12) and not WorkOrder.objects.filter(asset=demo).exists()
        # the Overview: ours on the active-devices tile; the demo unit past due back on the attention list, the loaner not (ours is out)
        ours_active = Asset.objects.filter(ownership=Ownership.OWNED, status__in=Asset.ACTIVE_STATUSES).count()
        assert overview_kpis(today.year, today.month, today)["active_devices"] == ours_active
        lines = [(i["asset"], i["right"]) for i in attention_items(today) if i.get("asset", "").startswith("T-")]
        assert lines == [("T-0103", "Past due back 12 d")]
        # the binder's temporary table: the three on site, by tag
        s = inventory.build(default_period(today), None)
        rows = {r[0]: r for r in s.table("temporary").rows()}
        assert list(rows) == ["T-0101", "T-0102", "T-0103"]
        assert rows["T-0101"][10:] == [bed.arrived_on, bed.owner_pm_due_on] and rows["T-0103"][10:] == [inventory.ALREADY_ON_SITE, None]
        assert {f.label: f.value for f in s.figures}["Temporary devices on site"] == 3


def test_seed_demo_drafts_last_years_program_evaluation(db):
    """Slice 32: a draft evaluation of the 12 months ending at the end of last month, drafted by the CE manager through the services
    after everything else (its figures count the demo's work: Cadence's records begin inside the period, and the previous 12 months
    have no records), two of this period's objectives with their results, one for the next period, and two parts of the text. Riverside
    only; nothing approved, so the binder has no evaluation item."""
    from django.db.models import Min
    from django.utils import timezone

    from apps.evaluations import indicators
    from apps.evaluations.models import NARRATIVE_FIELDS, ObjectiveKind, ProgramEvaluation, Status
    from apps.pm.dates import add_months
    from apps.reports.survey import default_period, program

    call_command("seed_demo", stdout=StringIO())
    with tenant_context(Tenant.objects.get(slug="riverside")):
        today = timezone.localdate()
        author = User.objects.get(username=f"{seed_demo.EVALUATION_AUTHOR}@riverside.example")
        ev = ProgramEvaluation.objects.select_related("snapshot").get()
        start = add_months(today.replace(day=1), -12)
        assert (ev.period_start, ev.period_end, ev.status, ev.version) == (start, today.replace(day=1) - timedelta(days=1), Status.DRAFT, 1)
        assert ev.number == f"EV-{ev.period_end:%y}-0001" and ev.created_by == author and author.role.slug == "manager"
        assert {h.history_user_id for h in ev.history.all()} == {author.pk}
        data = ev.snapshot.data
        assert (ev.snapshot.taken_on, ev.snapshot.taken_by) == (today, author)
        first = WorkOrder.objects.aggregate(first=Min("opened_on"))["first"]
        assert data["records_begin"] == first.isoformat() and first > ev.period_start  # the page says where the records begin
        assert data["previous"]["source"] == "counted" and data["previous"]["values"] == {}  # nothing in Cadence the year before
        figures = {i["key"]: i for i in data["indicators"]}
        assert figures["repairs_completed"]["value"] > 0 and figures["pm_high"]["den"] > 0 and figures["incidents_recorded"]["value"] >= 1
        assert figures["recalls_received"]["value"] >= 1 and data["tables"]["aem"][0]["model"] == "Philips IntelliVue MX750"
        # taken by someone who may see every area, so nothing in it depends on who took it; the director may refresh it too
        assert indicators.missing_for(author) == [] and indicators.cut(data, author)["left_out"] == []
        assert indicators.can_refresh(User.objects.get(username="kim@riverside.example"))
        assert {f: getattr(ev, f) for f in NARRATIVE_FIELDS if getattr(ev, f)} == seed_demo.EVALUATION_TEXT
        review = [(o.text, o.target, o.result, o.comment) for o in ev.objectives.filter(kind=ObjectiveKind.REVIEW)]
        assert review == [tuple(o) for o in seed_demo.EVALUATION_REVIEW_OBJECTIVES]
        assert [(o.text, o.target, o.result) for o in ev.objectives.filter(kind=ObjectiveKind.NEXT)] == [
            (*seed_demo.EVALUATION_NEXT_OBJECTIVES[0], "")]
        s = program.build(default_period(today), None)
        assert s.gaps == [] and list(s.table("evaluations").rows()) == []  # a draft: nothing approved, no check
    with tenant_context(Tenant.objects.get(slug="riverside-north")):
        assert not ProgramEvaluation.objects.exists()
    call_command("seed_demo", stdout=StringIO())  # a second run drafts nothing more
    with tenant_context(Tenant.objects.get(slug="riverside")):
        assert ProgramEvaluation.objects.count() == 1


def test_seed_demo_signs_off_a_philips_review(db):
    """Slice 34: one signed-off review of Philips for the 12 months ending yesterday (its closed monitor repair inside them), drafted
    and signed off today by the CE manager through apps.evaluations.vendor_reviews, just before the program evaluation (which stays made
    last), every field from VENDOR_REVIEW_FIELDS, its corrective action plan due VENDOR_REVIEW_PLAN_DAYS ahead with no result. Its
    figures are what vendor_figures counts now (never constants), and its summary holds no count. Riverside only; a second run adds
    nothing."""
    from django.utils import timezone

    from apps.evaluations import vendor_figures
    from apps.evaluations import vendor_reviews as vr
    from apps.evaluations.models import ProgramEvaluation, ReviewStatus, VendorReview, VendorReviewSnapshot
    from apps.reports.survey import default_period, vendors

    call_command("seed_demo", stdout=StringIO())
    with tenant_context(Tenant.objects.get(slug="riverside")):
        today = timezone.localdate()
        manager = User.objects.get(username=f"{seed_demo.EVALUATION_AUTHOR}@riverside.example")
        review = VendorReview.objects.select_related("snapshot").get()
        end = ended(today - timedelta(days=1))  # Feb 27 on a Feb 29, as the seed does
        assert (review.vendor_names, review.vendor_keys, review.period_start, review.period_end) == (
            seed_demo.VENDOR_REVIEW_NAMES, ["philips"], vr.period_start_of(end), end)
        assert (review.status, review.signed_on, review.signed_by, review.created_by, manager.role.slug) == (
            ReviewStatus.APPROVED, today, manager, manager, "manager")
        assert review.number == f"VR-{end:%y}-0001" and {h.history_user_id for h in review.history.all()} == {manager.pk}
        assert {f: getattr(review, f) for f in seed_demo.VENDOR_REVIEW_FIELDS} == seed_demo.VENDOR_REVIEW_FIELDS
        assert (review.plan_due_on, review.plan_result, review.plan_closed_on) == (
            today + timedelta(days=seed_demo.VENDOR_REVIEW_PLAN_DAYS), "", None)
        assert not any(ch.isdigit() for ch in review.summary)  # the figures have the counts
        repair = WorkOrder.objects.get(checks_reference=seed_demo.VENDOR_REPORT_NUMBER)
        assert review.period_start <= repair.completed_on <= review.period_end and repair.vendor_name == "Philips"
        # the figures as taken, counted again now (one snapshot: taken when drafted, signed off on it)
        assert VendorReviewSnapshot.objects.count() == 1 and (review.snapshot.taken_on, review.snapshot.taken_by) == (today, manager)
        fresh = vendor_figures.snapshot(review.vendor_keys, review.period_start, review.period_end, today=today)
        assert review.snapshot.data == fresh
        jobs = vendor_figures.by_key(fresh)["jobs_completed"]
        assert jobs["value"] == WorkOrder.objects.filter(vendor_service=True, vendor_name="Philips", status__in=("completed", "closed"),
                                                         completed_on__range=(review.period_start, review.period_end)).count() >= 1
        assert vendor_figures.by_key(fresh)["contracts_in_period"]["value"] >= 1
        # made before the program evaluation, which stays last
        ev = ProgramEvaluation.objects.select_related("snapshot").get()
        assert review.created_at < ev.created_at and review.snapshot.taken_at < ev.snapshot.taken_at
        # the binder: Philips's last review, and a plan not yet due
        s = vendors.build(default_period(today), None)
        rows = {r[0]: r for r in s.table("vendors").rows()}
        assert rows["Philips"][-2:] == [review.number, today]
        assert not any("corrective action plan" in g.text or "no signed-off review of the 12 months" in g.text for g in s.gaps)
    with tenant_context(Tenant.objects.get(slug="riverside-north")):
        assert not VendorReview.objects.exists()
    call_command("seed_demo", stdout=StringIO())  # a second run adds nothing
    with tenant_context(Tenant.objects.get(slug="riverside")):
        assert VendorReview.objects.count() == 1 and VendorReviewSnapshot.objects.count() == 1


def test_seed_demo_keeps_the_ce_departments_test_equipment(db):
    """Slice 35: the CE department's test equipment, added through apps.testequipment.services before the work that records it, each
    item entered TE_ADDED_DAYS_AGO and each certificate the day it came back (no "on file since" anywhere): an electrical safety analyzer
    per technician, held by them, calibrated before the work began and due months from now, on every one of its holder's in-house PMs,
    incoming inspections, and repairs checked and ready for use; the ProSim 8 due within 60 days; the defibrillator analyzer past due,
    on the defibrillators' PMs up to its due day and on one after it; the infusion device analyzer found out of tolerance last week, the
    pumps' PMs since its previous certificate not reviewed. The binder: two gaps and one finding, nothing else. Riverside only; a second
    run adds nothing."""
    from django.db.models import Min, Q
    from django.utils import timezone

    from apps.core.days import local_day
    from apps.reports.survey import FINDING, GAP, default_period, testequipment
    from apps.testequipment import rules
    from apps.testequipment.models import AsFound, Calibration, Kind, Status, TestEquipment, TestEquipmentUse, ToleranceReview
    from apps.vendors.rules import fmt
    from apps.workorders.models import RepairCheck

    call_command("seed_demo", stdout=StringIO())
    with tenant_context(Tenant.objects.get(slug="riverside")):
        today = timezone.localdate()
        items = {i.tag: i for i in TestEquipment.objects.select_related("held_by")}
        esas = [f"TE-{101 + i}" for i in range(len(seed_demo.TECHS))]
        assert sorted(items) == [*esas, "TE-201", "TE-301", "TE-401"] and all(t.startswith("TE-") for t in items)
        assert all(i.status == Status.IN_USE and i.interval_months == 12 for i in items.values())
        assert [items[t].held_by.name for t in esas] == [name for name, *_rest in seed_demo.TECHS]
        assert all(items[t].kind == Kind.ELECTRICAL_SAFETY for t in esas) and items["TE-201"].held_by is None
        # dated as it happened: every item entered TE_ADDED_DAYS_AGO, each certificate the day it came back
        added = today - timedelta(days=seed_demo.TE_ADDED_DAYS_AGO)
        assert {(h.history_type, local_day(h.history_date)) for h in TestEquipment.history.all()} == {("+", added)}
        cals = list(Calibration.objects.all())
        assert len(cals) == len(esas) + 4 and not any(c.entered_in_error for c in cals)
        entered = rules.entered_days(cals)
        assert all(entered[c.pk] == c.calibrated_on for c in cals)
        now = rules.standings(items.values(), today)
        first_work = WorkOrder.objects.aggregate(first=Min("completed_on"))["first"]
        for t in esas:
            cal = items[t].calibrations.get()
            assert now[items[t].pk].state == rules.IN_DATE and cal.calibrated_on < first_work and cal.due_on > today
        assert rules.matches(now[items["TE-201"].pk], "due_soon", today) and now[items["TE-201"].pk].due_on > today
        due = today - timedelta(days=seed_demo.DEFIB_ANALYZER_DUE_DAYS_AGO)
        assert (now[items["TE-301"].pk].state, now[items["TE-301"].pk].due_on) == (rules.PAST_DUE, due)
        found = items["TE-401"].calibrations.order_by("-calibrated_on").first()
        assert (found.calibrated_on, found.as_found, found.adjusted, found.needs_review) == (
            today - timedelta(days=seed_demo.INFUSION_FOUND_DAYS_AGO), AsFound.OUT_OF_TOLERANCE, True, True)
        assert not ToleranceReview.objects.exists()

        # each technician's analyzer on every in-house PM, inspection, and checked repair they completed; nothing else records test
        # equipment (no vendor work, no repair not returned to use, no recall work)
        counted = WorkOrder.objects.filter(rules.counted_work_q()).filter(
            Q(type__in=(WoType.PM, WoType.INSPECTION)) | Q(type=WoType.REPAIR, repair_check=RepairCheck.PASSED))
        held = {items[t].held_by_id: items[t].pk for t in esas}
        uses = {}
        for u in TestEquipmentUse.objects.all():
            uses.setdefault(u.work_order_id, set()).add(u.test_equipment_id)
        assert counted.count() > 100
        for wo in counted:
            assert held[wo.assigned_to_id] in uses.get(wo.pk, set()), wo.number
        assert set(uses) == set(counted.values_list("pk", flat=True))
        # the analyzers for the models' PMs: the defibrillator analyzer once past due on one PM only
        for tag in ("TE-201", "TE-301", "TE-401"):
            mine = WorkOrder.objects.filter(test_equipment_uses__test_equipment=items[tag]).select_related("asset__device_model")
            assert mine and all(w.type == WoType.PM and seed_demo.TE_FOR_MODELS[w.asset.device_model.model] == tag for w in mine), tag
        defib_pms = WorkOrder.objects.filter(rules.counted_work_q(), type=WoType.PM, asset__device_model__model="R Series Plus")
        after = defib_pms.filter(completed_on__gt=due)
        assert after.count() >= 1 and after.filter(test_equipment_uses__test_equipment=items["TE-301"]).count() == 1
        assert defib_pms.filter(completed_on__lte=due).exclude(test_equipment_uses__test_equipment=items["TE-301"]).count() == 0
        [late] = after.filter(test_equipment_uses__test_equipment=items["TE-301"])
        pumps = WorkOrder.objects.filter(rules.counted_work_q(), type=WoType.PM, asset__device_model__model="Alaris 8015 PCU",
                                         completed_on__lte=found.calibrated_on)
        in_window = pumps.filter(completed_on__gt=today - timedelta(days=seed_demo.INFUSION_PREVIOUS_DAYS_AGO))
        assert in_window.count() >= 2 and set(in_window.filter(test_equipment_uses__test_equipment=items["TE-401"])) == set(in_window)

        # the binder: the defibrillator analyzer in use past due (a gap) and used once past due (a finding); the infusion analyzer's
        # certificate not reviewed (a gap); no check (all the work with readings names its test equipment)
        s = testequipment.build(default_period(today), None)
        assert [(g.kind, g.record) for g in s.gaps] == [(GAP, "TE-301"), (GAP, "TE-401"), (FINDING, "TE-301")]
        assert s.gaps[0].text.startswith(f"TE-301 (Fluke Biomedical Impulse 7000DP) is in use: calibration was due {fmt(due)}.")
        assert s.gaps[1].text.startswith(f"TE-401 (Fluke Biomedical IDA-5): certificate KCL-41877 ({fmt(found.calibrated_on)}) found it out "
                                         f"of tolerance, and the {in_window.count()} work orders done with it after its previous passed "
                                         f"certificate")
        assert s.gaps[2].text.startswith(f"TE-301 (Fluke Biomedical Impulse 7000DP) was used on 1 work order while past due, "
                                         f"{fmt(late.completed_on)} (calibration was due {fmt(due)}).")
        figures = {f.label: f.value for f in s.figures}
        assert figures["Test equipment"] == len(items) and figures["Work with readings naming no test equipment"] == 0
        assert figures["Work with readings before test equipment was entered"] == 0
        work = rows_of_table(s, "work")
        assert {r[0] for r in work} == {late.number} | {w.number for w in in_window if w.completed_on >= default_period(today).start}
        assert all(r[6] is None for r in work)  # nothing on file since: each certificate entered the day it came back
    with tenant_context(Tenant.objects.get(slug="riverside-north")):
        assert not TestEquipment.objects.exists() and not TestEquipmentUse.objects.exists()
    call_command("seed_demo", stdout=StringIO())  # a second run adds nothing
    with tenant_context(Tenant.objects.get(slug="riverside")):
        assert TestEquipment.objects.count() == len(items) and Calibration.objects.count() == len(cals)


def rows_of_table(section, key) -> list:
    return [list(r) for r in section.table(key).rows()]


def test_a_database_seeded_before_slice_22_gets_the_north_campus(db, monkeypatch):
    monkeypatch.setattr(seed_demo.Command, "_north_campus", lambda self, tenant, opts: None)
    call_command("seed_demo", stdout=StringIO())
    assert not Tenant.objects.filter(slug="riverside-north").exists()
    monkeypatch.undo()
    out = StringIO()
    call_command("seed_demo", stdout=out)
    assert "already seeded" in out.getvalue() and "Seeded Riverside North Campus" in out.getvalue()
    assert people.is_joined(User.objects.get(email="kim@riverside.example", tenant__slug="riverside-north"))
