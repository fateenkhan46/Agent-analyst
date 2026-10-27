"""Deterministic transcript analysis.

This runs **whether or not an LLM is configured**, because the parts it does
are better done by code: locating speaker turns, counting them, and recording
exact line numbers so every MoM statement can cite its source. When a language
model is available it is asked to summarise and interpret *on top of* this
structure, not to re-derive it — a model asked for line numbers will invent
them.

The classifiers below are intentionally conservative. Specifically:

* A **decision** requires explicit agreement or directive language from a
  speaker. "I think we should maybe..." is not a decision; it becomes a
  discussion point. Mis-recording a hedge as a decision is the single most
  damaging error a set of minutes can make.
* An **open question** is recorded when something is asked and not answered,
  or when a speaker explicitly defers. These become the targeted questions we
  put to the user instead of inventing a definition.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

MAX_QUOTE = 400

#: "Name: text" at the start of a line, where Name looks like a person.
SPEAKER_RE = re.compile(
    r"^(?P<name>[A-Z][\w.'\-]*(?:\s+[A-Z][\w.'\-]*){0,3})\s*:\s*(?P<text>.*)$"
)
#: Header lines such as "Attendees: A (role), B" or "Date: ..."
HEADER_RE = re.compile(
    r"^(?P<key>attendees|present|participants|date|duration|subject|title|meeting|agenda)"
    r"\s*:\s*(?P<value>.+)$", re.I
)
#: "Name (Role)" inside an attendee list.
ATTENDEE_RE = re.compile(r"^(?P<name>[^(,]+?)\s*(?:\((?P<role>[^)]*)\))?$")

#: Phrases that mark a settled decision. Checked against the whole sentence.
DECISION_PATTERNS = (
    r"\bwe (?:will|should|are going to|'ll)\b",
    r"\b(?:let's|lets)\b",
    r"\bagreed\b", r"\bagree(?:d)? (?:that|to)\b",
    r"\bdecision is\b", r"\bwe decided\b", r"\bthat's (?:the )?decision\b",
    r"\buse the\b.*\bas (?:the )?system of record\b",
    r"\bis\b[^.?!]{0,60}\bnot the other way round\b",
    r"\bmust\b", r"\bdo not\b", r"\bdon't\b", r"\bnever\b", r"\balways\b",
    r"\bonly\b[^.?!]{0,40}\bthat have\b",
    r"\bfor this version\b", r"\bnot for this version\b",
    r"\bplease (?:do|use)\b", r"\bshow it as\b", r"\breport in\b",
)
#: Definitional statements: "X is Y", "X equals Y", "X means Y". These are the
#: most important decisions in a requirements meeting and carry no directive
#: language at all, so they need their own class -- "Budget Variance is Budget
#: minus Actual Cost" would otherwise be missed entirely.
DEFINITION_PATTERNS = (
    r"\bis\b[^.?!]{0,80}\bminus\b",
    r"\bis\b[^.?!]{0,80}\bdivided by\b",
    r"\bdivided by\b[^.?!]{0,40}\bnot\b[^.?!]{0,40}\bdivided by\b",
    r"\b(?:is|are)\b[^.?!]{0,60}\bsystem of record\b",
    r"\bmeans\b[^.?!]{0,60}\b(?:under|over|good|bad|budget)\b",
    r"\b(?:=|equals)\b",
    r"\bis\b[^.?!]{0,60}\bnot the other way round\b",
    r"\bshould (?:be|become)\b[^.?!]{0,60}\bhours\b",
    r"\beverything becomes\b",
    r"\buse the\b[^.?!]{0,40}\bvalues\b",
)
#: Meeting administration and framing. Never a decision, however directive.
ADMIN_PATTERNS = (
    r"\b(?:let's|lets) (?:wrap|start|begin|get started|move on|crack on)\b",
    r"\bthanks everyone\b", r"\bthank you all\b",
    r"\bthe reason we(?:'re| are) here\b",
    r"\bpart of the problem\b", r"\bthe (?:bigger )?issue is\b",
    r"\bhold on\b", r"\bwhat about\b", r"\bgood point\b",
    r"\bthat shouldn't exist\b", r"\bi have seen it done\b",
    r"\bit caused\b", r"\blast thing\b", r"\bone question\b",
)

#: Explicit confirmation by another speaker, used to raise confidence.
CONFIRMATION_PATTERNS = (
    r"^(?:agreed|noted|understood|yes|ok|okay|fine|thank you|thanks|good|please do)\b",
    r"\bnoted\b", r"\bunderstood\b", r"\bagreed\b", r"\bwill do\b",
)
#: Hedges that disqualify a sentence from being a decision.
HEDGE_PATTERNS = (
    r"\bmaybe\b", r"\bperhaps\b", r"\bprobably\b", r"\bmight\b",
    r"\bi think\b", r"\bnot sure\b", r"\bcould be\b", r"\bpossibly\b",
    r"\bi'd like\b", r"\bwe could\b",
)
#: Action-item language.
ACTION_PATTERNS = (
    r"\bi'll\b", r"\bi will\b", r"\bput (?:it|that) on the (?:action )?list\b",
    r"\bput it on the action list\b", r"\baction\b.*\bitem\b",
    r"\bi'll check\b", r"\bi'll confirm\b", r"\bcan you\b", r"\bplease send\b",
    r"\btake (?:this|that) away\b", r"\bfollow up\b", r"\bby (?:friday|monday|next week)\b",
    r"\bi'll have\b", r"\bi'll give\b", r"\bi'll build\b", r"\bi'll do\b",
    r"\bi'll flag\b", r"\bi'll keep\b", r"\bi'll need\b", r"\bi'll leave\b",
)
#: Explicit deferral -> open question.
DEFERRAL_PATTERNS = (
    r"\bopen question\b", r"\bto be confirmed\b", r"\btbc\b", r"\btbd\b",
    r"\bneed(?:s)? to (?:be )?confirm", r"\bi(?:'d| would) (?:still )?like .{0,40}confirm",
    r"\bwe(?:'ll| will) (?:have to|need to) (?:decide|work out|check)\b",
    r"\bthat's (?:an|a) open question\b", r"\bfor finance\b",
    r"\bwhat (?:date|rate|value) should\b",
)
#: Risk language.
RISK_PATTERNS = (
    r"\brisk\b", r"\bmay be incomplete\b", r"\bmight be wrong\b", r"\bconcern\b",
    r"\bworried\b", r"\bdouble count", r"\bincorrect\b",
    # "breaks" / "fails" only in a failure sense: "breaks down by category" is
    # a report layout request, not a risk.
    r"\bwould break\b", r"\bbreaks the\b", r"\bwill break\b",
    r"\bfails?\b(?!\s+safe)", r"\bfailure\b",
    r"\bgot (?:it|them) wrong\b", r"\bthe wrong (?:way|order|number)\b",
    r"\bcaused a genuine argument\b", r"\bcounted twice\b",
    r"\bsilently\b", r"\bnot recognise\b", r"\bshouldn't exist\b",
)

_DECISION_RE = re.compile("|".join(DECISION_PATTERNS), re.I)
_DEFINITION_RE = re.compile("|".join(DEFINITION_PATTERNS), re.I)
_ADMIN_RE = re.compile("|".join(ADMIN_PATTERNS), re.I)
_CONFIRM_RE = re.compile("|".join(CONFIRMATION_PATTERNS), re.I)
_HEDGE_RE = re.compile("|".join(HEDGE_PATTERNS), re.I)
_ACTION_RE = re.compile("|".join(ACTION_PATTERNS), re.I)
_DEFER_RE = re.compile("|".join(DEFERRAL_PATTERNS), re.I)
_RISK_RE = re.compile("|".join(RISK_PATTERNS), re.I)

#: Vocabulary that ties a sentence to a reporting concept, used to populate
#: ``Decision.affects`` and to seed KPI definitions.
TOPIC_KEYWORDS: dict[str, tuple[str, ...]] = {
    "identifiers": ("project code", "identifier", "project id", "codes", "normalise"),
    "budget variance": ("variance", "budget minus", "under budget", "over budget"),
    "variance percentage": ("percentage", "percent", "divided by budget", "divided by actual"),
    "labour hours": ("hours", "minutes", "timesheet", "hh:mm", "labour"),
    "currency": ("currency", "usd", "eur", "euros", "fx", "rate", "dollars"),
    "missing keys": ("no project", "unassigned", "unknown member", "not in my register",
                     "orphan", "isn't in the register"),
    "duplicates": ("duplicate", "twice", "conflict", "same timesheet id"),
    "status vocabulary": ("status", "complete", "in flight", "active", "closed",
                          "system of record"),
    "averages": ("average cost per project", "distinct count", "divide by"),
    "double counting": ("double count", "counted twice", "fact tables", "separate fact"),
    "date table": ("date table", "reporting period", "fy25", "fy24", "hard-code", "filter back"),
    "report layout": ("page", "executive", "drill", "slicer", "trend", "visual", "readable"),
    "sign-off": ("sign off", "signs off", "reconcile", "control total", "published"),
}

#: Sentences whose topic maps to a KPI the brief also names.
KPI_TOPIC_MAP = {
    "budget variance": "Budget Variance",
    "variance percentage": "Budget Variance %",
    "labour hours": "Total Labour Hours",
    "averages": "Average Cost per Project",
    "currency": "Total Cost",
}


@dataclass
class Turn:
    index: int
    line_no: int
    speaker: str
    text: str

    @property
    def quote(self) -> str:
        t = self.text.strip()
        return t if len(t) <= MAX_QUOTE else t[: MAX_QUOTE - 1] + "…"


@dataclass
class Sentence:
    turn: Turn
    text: str


@dataclass
class TranscriptAnalysis:
    line_count: int = 0
    title: str = ""
    date: str = ""
    duration: str = ""
    declared_attendees: list[tuple[str, str]] = field(default_factory=list)
    agenda: list[str] = field(default_factory=list)
    turns: list[Turn] = field(default_factory=list)
    speaker_turn_counts: dict[str, int] = field(default_factory=dict)
    speaker_roles: dict[str, str] = field(default_factory=dict)
    has_speaker_structure: bool = False
    body_lines: list[tuple[int, str]] = field(default_factory=list)

    def sentences(self) -> list[Sentence]:
        out: list[Sentence] = []
        for t in self.turns:
            for s in split_sentences(t.text):
                out.append(Sentence(turn=t, text=s))
        return out


def split_sentences(text: str) -> list[str]:
    """Split on sentence punctuation, keeping the mark so questions stay
    identifiable."""
    parts = re.split(r"(?<=[.?!])\s+", text.strip())
    return [p.strip() for p in parts if len(p.strip()) > 2]


def _normalise_person(name: str) -> str:
    return re.sub(r"\s+", " ", name).strip(" .,:;-")


def analyse(text: str) -> TranscriptAnalysis:
    """Parse a transcript into turns, headers and speaker statistics."""
    lines = text.replace("\r\n", "\n").split("\n")
    a = TranscriptAnalysis(line_count=len(lines))

    in_agenda = False
    for i, raw in enumerate(lines, start=1):
        line = raw.rstrip()
        if not line.strip():
            in_agenda = False
            continue

        header = HEADER_RE.match(line.strip())
        if header:
            key = header.group("key").lower()
            value = header.group("value").strip()
            if key in {"attendees", "present", "participants"}:
                for chunk in re.split(r",|;| and ", value):
                    chunk = chunk.strip()
                    if not chunk:
                        continue
                    m = ATTENDEE_RE.match(chunk)
                    if m:
                        nm = _normalise_person(m.group("name"))
                        role = (m.group("role") or "").strip()
                        if nm:
                            a.declared_attendees.append((nm, role))
                            if role:
                                a.speaker_roles[nm] = role
                continue
            if key == "date":
                a.date = value
                continue
            if key == "duration":
                a.duration = value
                continue
            if key in {"subject", "title", "meeting"}:
                a.title = a.title or value
                continue
            if key == "agenda":
                in_agenda = True
                if value:
                    a.agenda.append(value)
                continue

        if in_agenda and re.match(r"^\s*[-*\d.]+\s+", line):
            a.agenda.append(re.sub(r"^\s*[-*\d.]+\s+", "", line).strip())
            continue

        speaker = SPEAKER_RE.match(line.strip())
        if speaker and speaker.group("text").strip():
            name = _normalise_person(speaker.group("name"))
            # Guard against sentences that merely begin with a capitalised
            # word followed by a colon.
            if len(name.split()) <= 4 and not HEADER_RE.match(line.strip()):
                turn = Turn(
                    index=len(a.turns), line_no=i, speaker=name,
                    text=speaker.group("text").strip(),
                )
                a.turns.append(turn)
                a.speaker_turn_counts[name] = a.speaker_turn_counts.get(name, 0) + 1
                continue

        a.body_lines.append((i, line.strip()))

    a.has_speaker_structure = len(a.turns) >= 3
    if not a.title:
        first = next((t for _, t in a.body_lines), "")
        a.title = first[:120] if first and len(first) < 140 else "Stakeholder Requirements Meeting"
    return a


# --------------------------------------------------------------------------
# Classification
# --------------------------------------------------------------------------
def topics_for(text: str) -> list[str]:
    low = text.lower()
    return [topic for topic, words in TOPIC_KEYWORDS.items() if any(w in low for w in words)]


def is_question(text: str) -> bool:
    return text.rstrip().endswith("?")


def classify_decision(sentence: Sentence, following: list[Sentence]) -> tuple[bool, str]:
    """Is this sentence a settled decision? Returns (verdict, confidence).

    A sentence qualifies when it uses directive/agreement language, is not
    hedged, and is not itself a question. Confidence rises to ``high`` only
    when a *different* speaker confirms it in one of the next few turns --
    which is what actually makes a decision a decision in a meeting.
    """
    text = sentence.text
    if is_question(text) or len(text) < 12:
        return False, "low"
    if _HEDGE_RE.search(text):
        return False, "low"
    # Meeting admin and problem framing are directive-sounding but settle
    # nothing. Excluded before the positive tests so "let's wrap up" and
    # "part of the problem is ... don't use the same codes" never qualify.
    if _ADMIN_RE.search(text):
        return False, "low"
    # "I'll do X" is an action item, not a decision about the report.
    if re.match(r"^\s*i'(?:ll|m)\b", text, re.I):
        return False, "low"

    is_definition = bool(_DEFINITION_RE.search(text))
    is_directive = bool(_DECISION_RE.search(text))
    if is_definition:
        # A definition only counts when it is about something we are building;
        # otherwise it is ordinary conversation containing the word "is".
        if not topics_for(text):
            return False, "low"
    elif not is_directive:
        return False, "low"

    confirmed = any(
        s.turn.speaker != sentence.turn.speaker
        and s.turn.index <= sentence.turn.index + 3
        and _CONFIRM_RE.search(s.text)
        for s in following
    )
    if confirmed:
        return True, "high"
    # An unconfirmed definition is still worth recording, but as medium --
    # the MoM shows a "please confirm" marker against anything below high.
    return True, "medium"


def classify_action(sentence: Sentence) -> tuple[bool, str, str]:
    """Returns (is_action, owner, due)."""
    text = sentence.text
    if is_question(text):
        # "Can you send me X?" is a request; the owner is unknown until
        # someone accepts it, so it is not recorded as an owned action.
        return False, "", ""
    if not _ACTION_RE.search(text):
        return False, "", ""
    owner = ""
    if re.search(r"\bi'(?:ll|m)\b|\bi will\b|\bi'll have\b", text, re.I):
        owner = sentence.turn.speaker
    due = ""
    m = re.search(
        r"\bby (friday|monday|tuesday|wednesday|thursday|saturday|sunday|"
        r"next week|end of (?:the )?(?:week|month|day)|tomorrow|[A-Z][a-z]+ \d{1,2})\b",
        text, re.I,
    )
    if m:
        due = m.group(1)
    return True, owner, due


def is_deferral(text: str) -> bool:
    return bool(_DEFER_RE.search(text))


def classify_open_question(
    sentence: Sentence, following: list[Sentence], preceding: list[Sentence] | None = None
) -> bool:
    """A question nobody answered, or an explicit deferral.

    Two subtleties, both of which bit in testing:

    * A reply that *defers* ("that's an open question for Finance") is not an
      answer. Counting it as one would silently drop the very questions this
      system exists to surface.
    * Conversely, a deferral that directly follows an unanswered question is
      the same item as that question, so it is not recorded a second time --
      the question itself reads better in the minutes.
    """
    if is_deferral(sentence.text):
        if not is_question(sentence.text):
            for prior in reversed(preceding or []):
                if prior.turn.index < sentence.turn.index - 2:
                    break
                if is_question(prior.text):
                    return False  # already recorded as the question itself
        return True
    if not is_question(sentence.text):
        return False

    # Only the FIRST reply from another speaker decides. A wider window lets an
    # unrelated later turn ("I'm worried about the Q1 export") count as the
    # answer to a question nobody actually addressed.
    first_reply_turn: int | None = None
    reply: list[Sentence] = []
    for s in following:
        if s.turn.speaker == sentence.turn.speaker:
            continue
        if first_reply_turn is None:
            first_reply_turn = s.turn.index
        if s.turn.index != first_reply_turn:
            break
        reply.append(s)

    if not reply:
        return True
    if any(is_deferral(s.text) for s in reply):
        return True
    return not any(
        not is_question(s.text) and len(s.text) > 15 for s in reply
    )


def classify_risk(sentence: Sentence) -> bool:
    return bool(_RISK_RE.search(sentence.text)) and not is_question(sentence.text)
