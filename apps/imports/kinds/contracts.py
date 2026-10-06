"""
Service contracts and the devices they cover (slice 23). A file has one row per contract, or one row per covered device (the link
file other systems export): the contract's reference repeats once per device, with the device's asset tag, and a row with only a
reference and a tag puts that device on a contract already here (or added by an earlier line).

A contract is found by its reference in any letter case. A new one needs a vendor, a start and an end date (create_contract); one
here takes the row's values (update_contract), a blank cell or a column not in the file keeping what Cadence has. Rows of one
contract that give it different values are applied in turn, so the last one stays, and the row says so. The contract's notes are
never imported: they are free text.

The row's device goes on the contract (add_asset). A tag not here, or a retired device, is noted and the contract row still
imports; a device on another contract that ends later than this one stays there, with a note (the later contract is the one that
covers it); otherwise it moves. Notes never name the tag: the line does.

A chunk knows only its own lines, and the check rolls each chunk back (apps.imports.services). A contract's consecutive lines are
kept in one chunk (keep_together), so in a file sorted by contract, as exports are, the check sees what the import will. Lines of
one contract spread through the file are checked chunk by chunk: a later chunk's line then reads as adding the contract again (or,
with only a reference and a tag, as a skip), while the import finds the contract the earlier chunk committed.
"""
from apps.accounts.models import Level, Module
from apps.contracts import services as contracts
from apps.contracts.models import Contract, ContractType, Coverage
from apps.equipment.models import Asset, AssetStatus

from .. import parse
from ..base import Column, Importer, RowSkip


def _words(choices, **extra) -> dict[str, str]:
    """The words a choice is written with: its slug, its label, and the other names exports give it."""
    words = {value.replace("_", " "): value for value in choices.values}
    words.update({label.lower(): value for value, label in choices.choices})
    for value, names in extra.items():
        words.update({name: value for name in names})
    return words


TYPE_WORDS = _words(ContractType, oem=("oem contract", "manufacturer", "original equipment manufacturer"),
                    third_party=("third party contract", "3rd party", "3rd-party", "isp", "iso", "independent",
                                 "independent service organization", "independent service provider"))
COVERAGE_WORDS = _words(Coverage, full=("full coverage", "comprehensive", "full service contract"),
                        parts_labor=("parts & labor", "parts/labor", "parts and labour", "parts & labour", "p&l"),
                        pm_only=("pm", "preventive maintenance", "preventive maintenance only", "pm-only", "inspection only"),
                        tm=("time & materials", "time and material", "t&m", "t & m", "t and m"))
FIELDS = {"vendor": "vendor", "type": "type", "coverage": "coverage", "start": "start_on", "end": "end_on", "annual_cost": "annual_cost"}  # column -> field
NEW_NEEDS = ("vendor", "start_on", "end_on")
COVERED, MOVED = "put on a contract", "moved from another contract"


class ContractsImporter(Importer):
    kind = "contracts"
    label = "Service contracts"
    module = Module.CONTRACTS
    level = Level.EDIT
    key = "reference"
    keep_together = True  # a contract's lines in one chunk (apps.imports.services._chunk_end)
    order = 20
    description = ("Service contracts, by reference: one row per contract, or one per covered device with its asset tag (the reference "
                   "repeats). Contracts already here are updated; a blank cell keeps what is here.")
    columns = [
        Column("reference", "Contract reference", ("contract reference", "reference", "contract number", "contract #", "contract no",
                                                   "contract id", "agreement number", "agreement #", "contract"),
               required=True, max_length=60, example="SC-2026-118"),
        Column("vendor", "Vendor", ("vendor", "vendor name", "service vendor", "service provider", "service company", "supplier", "contractor"),
               max_length=120, help="Needed for a new contract", example="Hamilton Medical"),
        Column("type", "Contract type", ("contract type", "type", "support type"), help="OEM or third party", example="OEM"),
        Column("coverage", "Coverage", ("coverage", "coverage type", "coverage level", "service level"),
               help="Full service, parts and labor, parts only, PM only, or time and materials", example="Full service"),
        Column("start", "Start date", ("start date", "start", "contract start", "contract start date", "effective date", "begin date",
                                       "coverage start"), help="Needed for a new contract", example="2026-01-01"),
        Column("end", "End date", ("end date", "end", "contract end", "contract end date", "expiration date", "expiration", "expires",
                                   "expiry date", "termination date", "coverage end"), help="Needed for a new contract", example="2026-12-31"),
        Column("annual_cost", "Annual cost", ("annual cost", "annual value", "yearly cost"), help="A year's cost, not the contract's total",
               example="48000.00"),
        Column("tag", "Asset tag", ("asset tag", "tag", "control number", "control no", "control #", "equipment #", "equipment id",
                                    "equip #", "asset id", "asset number", "ce number", "ce #"),
               max_length=40, help="One covered device per row; repeat the reference for each", example="CE-10042"),
    ]
    field_labels = {FIELDS[c.key]: c.label for c in columns if c.key in FIELDS}  # contract field -> its column's label, for notes

    def prepare(self, rows):
        """A reference repeats on purpose (one row per covered device); the same device twice under one contract, or the same
        contract twice without a device, is a problem of the file."""
        firsts, problems = {}, {}
        for i, row in enumerate(rows):
            reference = (row.get("reference") or "").lower()
            if not reference:
                continue
            pair = (reference, (row.get("tag") or "").lower())
            if pair not in firsts:
                firsts[pair] = i
            elif pair[1]:
                problems[i] = f"Asset tag {row['tag']} is also on line {firsts[pair] + 2} for this contract: one row per covered device"
            else:
                problems[i] = f"This contract is also on line {firsts[pair] + 2} without a device: one row per contract, or per covered device"
        return problems

    def apply(self, ctx, row, result):
        reference = row["reference"]
        if not reference:
            raise RowSkip("Contract reference is blank")
        contract = Contract.objects.filter(reference__iexact=reference).first()
        values, reads = self._read(row, result, new=contract is None)
        earlier = ctx.cache.setdefault("given", {}).get(reference.lower(), {})  # what this chunk's earlier lines gave the contract
        for field, value in values.items():
            if field in earlier and earlier[field] != value:
                result.warn(f"{self.field_labels[field]} differs from an earlier line of this contract: this line's value is used")
        changes = {}
        if contract is None:
            if any(field not in values for field in NEW_NEEDS):
                raise RowSkip("No contract with this reference here; a new one needs Vendor, Start date and End date")
            contract = contracts.create_contract(by=ctx.user, reference=reference, **values)
            result.outcome = result.CREATE
        else:
            changes = {field: value for field, value in values.items() if getattr(contract, field) != value}
            if changes:
                contracts.update_contract(contract, by=ctx.user, **changes)
                result.outcome = result.UPDATE
        covered = self._cover(contract, row.get("tag"), result)
        if covered and result.outcome == result.UNCHANGED:
            result.outcome = result.UPDATE
        # Recorded once the row's changes are made, so a row skipped part way leaves nothing in the summary.
        ctx.cache["given"][reference.lower()] = {**earlier, **values}
        for column, value, shown in reads:
            ctx.read_as(column, value, shown)
        if result.outcome == result.CREATE:
            ctx.add_created("Contracts", contract.reference)
        for field in changes:
            ctx.total("Changes", self.field_labels[field])
        if covered:
            ctx.total("Devices", covered)

    def _read(self, row, result, *, new: bool) -> tuple[dict, list]:
        """The row's values for the contract (a blank cell, or a column not in the file, is left out) and how its choices were read.
        A value it cannot read is left out with a note: a new contract gets the default, one here keeps its own; a new contract's
        dates are needed, so the row is skipped."""
        values, reads = {}, []
        if row.get("vendor"):
            values["vendor"] = row["vendor"]
        for key, choices, words, default in (("type", ContractType, TYPE_WORDS, ContractType.OEM), ("coverage", Coverage, COVERAGE_WORDS, Coverage.FULL)):
            label = self.column(key).label
            try:
                value = parse.parse_choice(row.get(key, ""), words, what=label.lower())
            except parse.Unreadable:
                result.warn(f"{label} not read: {default.label} used" if new else f"{label} not read: left as it was")
                continue
            if value:
                values[key] = value
                reads.append((label, row[key], choices(value).label))
        for key in ("start", "end"):
            label = self.column(key).label
            try:
                day = parse.parse_date(row.get(key, ""))
            except parse.Unreadable as e:
                note = f"{label} reads as no date" if isinstance(e, parse.Placeholder) else f"{label} not read"
                if new:
                    raise RowSkip(f"{note}: a new contract needs one") from None
                result.warn(f"{note}: left as it was")
                continue
            if day:
                values[FIELDS[key]] = day
        try:
            cost = parse.parse_money(row.get("annual_cost", ""))
        except parse.Unreadable:
            result.warn("Annual cost not read: 0 used" if new else "Annual cost not read: left as it was")
        else:
            if cost is not None:
                values["annual_cost"] = cost
        return values, reads

    def _cover(self, contract, tag: str, result) -> str | None:
        """Put the row's device on the contract. Returns how it went for the summary (COVERED, MOVED), or None when nothing moved."""
        if not tag:
            return None
        asset = Asset.objects.select_related("contract").filter(tag__iexact=tag).first()  # tags are unique in any letter case
        if asset is None:
            result.warn("No device with this asset tag here: the contract is imported without it")
            return None
        if asset.status == AssetStatus.RETIRED:
            result.warn("The device is retired: not put on the contract")
            return None
        if asset.contract_id == contract.pk:
            return None
        if asset.contract_id and asset.contract.end_on > contract.end_on:
            result.warn("The device is on another contract that ends later: left there")
            return None
        return MOVED if contracts.add_asset(contract, asset) else COVERED
