"""
Find typed commands inside a chat message.

A command is /verb followed by the words it understands (the task engine says
which); it stops at the first other word, a new line, or clause punctuation.
A known verb counts anywhere after a space; an unknown one only at the start of
a line, where it gets a "did you mean" reply. A command in quotes or backticks
is a mention, not an action. Segments keep the exact text, so the message can
be shown as typed with the commands highlighted.
"""

import re

CMD = re.compile(r"(?:^|(?<=\s))/([A-Za-z]+)", re.M)
WORD = re.compile(r"([ \t]+)(\S+)")
PUNCT = ".,;:!?)"


def split_message(text: str, verbs: set[str], takes=lambda verb, word: True) -> list[dict]:
    segments, pos = [], 0
    for m in CMD.finditer(text):
        if m.start() < pos:
            continue
        verb = m.group(1).lower()
        line_start = text.rfind("\n", 0, m.start()) + 1
        at_line_start = not text[line_start:m.start()].strip()
        if verb not in verbs and not at_line_start:
            continue
        end = m.end()
        while (w := WORD.match(text, end)):
            core = w.group(2).rstrip(PUNCT)
            if not core or not takes(verb, core.lower()):
                break
            end = w.start(2) + len(core)
            if core != w.group(2):               # the word ended a clause
                break
        if m.start() > pos:
            segments.append({"text": text[pos:m.start()]})
        segments.append({"text": text[m.start():end], "cmd": True})
        pos = end
    if pos < len(text):
        segments.append({"text": text[pos:]})
    return segments


