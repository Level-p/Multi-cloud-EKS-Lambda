"""The incident lifecycle (NIST SP 800-61 phases, as states an incident moves through).

  detected -> triaged -> contained -> eradicated -> recovered -> post_incident_review -> closed

Rules:
  - an incident moves one phase forward at a time (no skipping containment);
  - a non-security operational incident with nothing to eradicate may go contained -> recovered;
  - "closed" needs the post-incident review written (lessons learned and actions);
  - a recovered or reviewed incident can be reopened back to "triaged" if the problem returns.
Every transition needs a note: the note becomes a timeline post on the incident blog.
"""

STATES = ["detected", "triaged", "contained", "eradicated", "recovered", "post_incident_review", "closed"]

LABELS = {
    "detected": "Detected", "triaged": "Triaged", "contained": "Contained", "eradicated": "Eradicated",
    "recovered": "Recovered", "post_incident_review": "Post-incident review", "closed": "Closed",
}

ALLOWED = {
    "detected": {"triaged"},
    "triaged": {"contained"},
    "contained": {"eradicated", "recovered"},
    "eradicated": {"recovered"},
    "recovered": {"post_incident_review", "triaged"},
    "post_incident_review": {"closed", "triaged"},
    "closed": set(),
}

SEVERITIES = ["SEV1", "SEV2", "SEV3", "SEV4"]


class TransitionError(ValueError):
    pass


def check_transition(current: str, target: str, note: str, review: str | None) -> None:
    if target not in STATES:
        raise TransitionError(f"unknown state '{target}'")
    if target not in ALLOWED[current]:
        allowed = ", ".join(LABELS[s] for s in sorted(ALLOWED[current], key=STATES.index)) or "none"
        raise TransitionError(f"cannot move from {LABELS[current]} to {LABELS[target]} (allowed: {allowed})")
    if not note or not note.strip():
        raise TransitionError("every transition needs a note for the timeline")
    if target == "closed" and not (review and review.strip()):
        raise TransitionError("write the post-incident review (lessons learned, actions) before closing")
