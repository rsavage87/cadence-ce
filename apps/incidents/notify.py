"""
Emails about device incidents (slice 28; wave 2 fills these in). To Incidents Approve holders who keep NotificationPreference.incidents
on: when an incident with a clock is recorded, and as its due date nears (SOON_WORK_DAYS work days left) and passes. Only the incident's
number, the device's tag, and the due date, with a link naming the facility (people.with_facility): never the outcome, the person, or
anything typed. Each reminder once (NotificationSent kind "incident", key "<number>:<stage>").
"""


def recorded(incident) -> None:
    """After commit: an incident with a clock was recorded."""


def send_reminders(today) -> int:
    """The daily reminders (send_staff_notifications runs it per facility): returns how many emails went out."""
    return 0
