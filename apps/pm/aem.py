"""
The AEM program (slice 14): alternative equipment maintenance intervals for device models, proposed with the model's failure
history and approved by the Equipment Management Committee. The only code that changes AemDecision rows and, through them,
DeviceModel.aem_interval_months (equipment.services.update_device_model refuses a direct change).

TODO(slice 14, part B): evidence, propose, approve, reject, withdraw, end, and the model drawer's AEM tab.
"""


def model_changed(device_model, changed: list[str], by=None) -> None:
    """Called by equipment.services.update_device_model, inside its transaction, after a model's risk class or OEM interval
    changed (`changed` names the fields). TODO(part B): a model that became life support leaves AEM, and so on."""
    return None
