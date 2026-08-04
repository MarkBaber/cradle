"""Alert copy and citations (task A7).

AGENTS: architect-only. Do not author, reword, or extend medical claims here.

Two deliberate constraints on every string below:
  1. It states what the LOG shows, not what is wrong with the baby. "No feed
     recorded for 5 hours" is an observation; "your baby is underfed" is a
     diagnosis this app must never make.
  2. Escalation wording is fixed and reused verbatim, so severity always maps
     to the same advice (SPEC 1.1).

The thresholds these messages quote come from rules_config.toml, which encodes
NHS/NICE guidance. SOURCES records the guidance each rule derives from so the
UI can link out. Neither the thresholds nor these citations have been reviewed
by a clinician: see task A8.
"""

ESCALATE_ROUTINE = "discuss with your midwife/health visitor, or call 111"
ESCALATE_URGENT = "seek medical advice now - call 111 or your GP"

MESSAGES: dict[str, str] = {
    "FEED_GAP":
        "No feed recorded for {hours:.0f} hours (last was {last}). "
        "If that looks wrong, add the missed feed; if it is right, "
        + ESCALATE_ROUTINE + ".",
    "FEED_COUNT_LOW":
        "{count} feeds recorded in the last 24 hours; {expected} or more is usual "
        "at this age. Check nothing is missing from the log, then "
        + ESCALATE_ROUTINE + ".",
    "WET_NAPPY_LOW":
        "{count} wet nappies recorded in the last 24 hours; around {expected} is "
        "usual on day {day}. Wet nappies are the everyday check on how much a baby "
        "is taking in, so if the log is accurate, " + ESCALATE_ROUTINE + ".",
    "STOOL_ABSENT":
        "No dirty nappy recorded for {hours:.0f} hours (day {day}). If that is "
        "accurate, " + ESCALATE_ROUTINE + ".",
    "STOOL_COLOUR":
        "A {colour} nappy was logged. Pale or chalky, red, and black stools after "
        "the first few days are all worth checking promptly: " + ESCALATE_URGENT + ".",
    "WEIGHT_LOSS_10PC":
        "Latest weight {weight}g is {pct:.1f}% below birth weight ({birth}g). "
        "Weight loss of around a tenth is a recognised point to get feeding "
        "reviewed: " + ESCALATE_URGENT + ".",
    "WEIGHT_NOT_REGAINED":
        "Birth weight ({birth}g) has not been regained by day {day}; latest is "
        "{weight}g. Most babies are back to birth weight around two weeks. "
        + ESCALATE_ROUTINE.capitalize() + ".",
    "CENTILE_CROSS":
        "Weight has moved down {drop:.2f} standard deviations from the earlier "
        "measurement on the growth chart. A sustained drop across centile lines is "
        "worth reviewing: " + ESCALATE_ROUTINE + ".",
    "FEVER_U3M":
        "Temperature {temp:.1f} C recorded at {age} days old. A temperature of "
        "38 C or above in a baby under three months is always treated as urgent: "
        + ESCALATE_URGENT + ".",
    "WEIGH_IN_DUE":
        "No weight recorded for {days} days. Worth a weigh-in at your next "
        "clinic or health visitor appointment.",
    "MEASUREMENT_GAP":
        "Nothing logged for {hours:.0f} hours. This is about the log, not the "
        "baby - catch up whenever suits.",
}

SOURCES: dict[str, str] = {
    "FEED_GAP": "NHS: breastfeeding and bottle feeding advice for newborns",
    "FEED_COUNT_LOW": "NHS: how often to feed a newborn",
    "WET_NAPPY_LOW": "NHS: nappies and what to expect in the early days",
    "STOOL_ABSENT": "NHS: your baby's nappies",
    "STOOL_COLOUR": "NHS: baby poo colour and when to get advice",
    "WEIGHT_LOSS_10PC": "NICE CG37 / postnatal care: weight loss in the early days",
    "WEIGHT_NOT_REGAINED": "NICE CG37 / postnatal care: regaining birth weight",
    "CENTILE_CROSS": "RCPCH UK-WHO growth chart guidance: centile shifts",
    "FEVER_U3M": "NICE NG143: fever in under-5s, traffic-light assessment",
    "WEIGH_IN_DUE": "RCPCH / Red Book: routine weight monitoring",
    "MEASUREMENT_GAP": "n/a - data-entry prompt, not clinical guidance",
}

# Rules whose message must carry the urgent escalation wording.
URGENT_RULES = frozenset({"STOOL_COLOUR", "WEIGHT_LOSS_10PC", "FEVER_U3M"})


def render(rule_id: str, **fields: object) -> str:
    return MESSAGES[rule_id].format(**fields)
