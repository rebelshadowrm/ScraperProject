"""Retain explicit source cancellations without deleting historical event facts."""
from copy import deepcopy
from common.validation import validate_event_data


def retain_cancellation(entry,details,*,observed_at):
    if not entry or details.get('cancelled') is not True or not validate_event_data(deepcopy(entry.get('event_data',{})))[0]:return False
    entry['source_details']={**deepcopy(entry.get('source_details') or {}),**deepcopy(details),'cancelled':True}
    entry.update(acquisition_refreshed_at=observed_at,last_attempt_status='success')
    entry.pop('source_review_required',None);entry.pop('source_review_reason',None)
    return True
