"""
Task engines: the rules of one task

The session runs chat, timing, done signals and logging. The engine plans the
rounds, deals each one, says what each participant sees, applies task actions,
decides when a round is complete and scores it.
"""


import string
import difflib

from tasks import Task, StimulusSet, Rules
import maze

class CommandError(ValueError):
    """A problem with what the participant typed; its text is shown to them."""

class TaskEngine:

    COMMANDS: dict[str, tuple[str, ...]] = {}
    HELP = ""
    
    def __init__(self, task: Task, stimuli_name: str, stimuli: StimulusSet, rules: Rules):
        self.task = task
        self.stimuli_name = stimuli_name
        self.stimuli = stimuli
        self.rules = rules
        self.round_id = None
        self.participants = {}

    def plan_rounds(self, rng) -> list[str]:
        """
        Round ids for the session, in order
        """
        raise NotImplementedError


    def setup_round(self, round_no: int, round_id: str, participants: list[str], rng) -> None:
        """
        Deal the round: roles and each participant's private state
        """
        self.round_id = round_id
        self.participants = participants

    def setup_log(self) -> dict:
        """
        Hidden round setup for the log (roles, targets, private labels)
        """
        return {}
        
    def materials(self) -> dict[str, str]:
        """
        Picture URL per participant, for the page and the log
        """
        return {}

    def view(self, p: str) -> dict:
        """
        What p sees beyond their picture (role, board, ...). Empty for simple tasks
        """
        return {}
        

    
    def actions(self, p: str) -> list[str]:
        """
        Task actions p's role allows now
        """
        return []


    def apply(self, p: str, action: dict) -> dict:
        """
        Validate and apply one action; return event to log. Raises if invalid
        """
        raise ValueError("this task has no actions")

    def round_complete(self, status: dict) -> bool:
        return all(s["done"] for s in status.values())
    
    def correct(self):
        """
        The rounds's correct answer, for the log
        """
        return None

    def score(self, status: dict) -> dict:
        """
        Result per participant, given their done signals and answers
        """
        return {p: {} for p in status}


    def resolve(self, word: str) -> str | None:
        for cmd, aliases in self.COMMANDS.items():
            if word == cmd or word in aliases:
                return cmd
        return None

    def command_words(self) -> list[str]:
        return [w for cmd, aliases in self.COMMANDS.items() for w in (cmd, *aliases)]

    def takes_arg(self, cmd: str, word: str) -> bool:
        """
        Is word part of this command's arguments?
        Default - everything up to the clause end
        """
        return True

    
    def command(self, p: str, cmd: str, args: list[str]) -> dict:
        """
        Apply a typed command.
        - returns
           {"feedback": ..., "notice": ...}
        - raises
           ValueError
        """
        raise ValueError("this task has no commands")

    def _ordered(self, ids: list[str], rng) -> list[str]:
        if self.rules.order == "shuffled":
            rng.shuffle(ids)
        if self.rules.rounds is not None:
            if self.rules.rounds > len(ids):
                raise ValueError(f"{self.rules.rounds} rounds requested; "
                                 f"{self.stimuli_name} has {len(ids)}")
            ids = ids[:self.rules.rounds]
        return ids

class ChoiceEngine(TaskEngine):
    """
    Everyone has their own picture; the answer is chosen from fixed options
    and comes with the done signal (common lines, spot the difference)
    """

    def plan_rounds(self, rng):
        return self._ordered(list(self.stimuli.rounds), rng)
    
    def materials(self):
        return {p: self.task.material_url(self.stimuli_name, self.round_id, p)
                for p in self.participants}


    def round_complete(self, status):
        if not all(s["done"] for s in status.values()):
            return False
        if self.rules.require_agreement:
            return len({frozenset(s["answer"] or []) for s in status.values()}) == 1
        return True
    
    def correct(self):
        return self.stimuli.rounds[self.round_id]


    def score(self, status):
        correct = self.correct()
        return {p: self.task.score(s["answer"], correct) for p, s in status.items()}


class MatchingEngine(TaskEngine):
    """
    A director describes a target grid; each matcher fills an empty grid from
    a pool holding every figure. The grid uses N of the figures; the ones left
    out rotate, so every figure keeps coming back
    """

    def __init__(self, *args):
        super().__init__(*args)
        rows, cols = self.rules.grid
        self.size = rows * cols
        self.figures = list(self.stimuli.figures)
        if self.size > len(self.figures):
            raise ValueError(f"a {rows}x{cols} grid needs {self.size} figures;"
                             f"{self.stimuli_name} has {len(self.figures)}")

        self.cycle = None

    def plan_rounds(self, rng):
        if not self.rules.rounds:
            raise ValueError("the matching task needs 'rounds' in its rules")
        return [f"trial{i}" for i in range(1, self.rules.rounds + 1)]

    def setup_round(self, round_no, round_id, participants, rng):
        super().setup_round(round_no, round_id, participants, rng)

        # figure out the initial director (or the director for whole session if not rotating)
        drole = self.rules.director
        if drole == "rotate":
            self.director = participants[(round_no - 1) % len(participants)]
        elif drole in participants:
            self.director = drole
        else:
            raise ValueError(f"director '{drole}' is not a participant")
        self.matchers = [p for p in participants if p != self.director]

        # create sample of figures from the figure population for the task/round
        n = len(self.figures)
        if self.cycle is None:
            self.cycle = rng.sample(self.figures, n)

        k = n - self.size
        start = ((round_no -1) * k) % n
        left_out = {self.cycle[(start + i) % n] for i in range(k)}
        self.target = rng.sample([f for f in self.figures if f not in left_out], self.size)

        # give private labels to the 'cards' (items) per matcher
        letters = string.ascii_lowercase[:n]

        # each matcher gets a set of cards in their 'pool'
        self.cards = {m: dict(zip(letters, rng.sample(self.figures, n))) for m in self.matchers}

        # each matcher has a board to fill with cards during round
        self.boards = {m: [None] * self.size for m in self.matchers}


    def setup_log(self):
        return {"direction": self.director, "target": self.target,
                "cards": self.cards}

    def url(self, figure: str) -> str:
        folder = self.task.dir / "stimuli" / self.stimuli_name
        ext = "png" if (folder / f"{figure}.png").is_file() else self.task.defn.materials
        return f"/stimuli/{self.task.defn.name}/{self.stimuli_name}/{figure}.{ext}"

    def view(self, p):

        if not self.participants or p not in self.participants:
            return {}
        
        base = {"participant": p, "grid": list(self.rules.grid),
                "director": self.director, "figures": len(self.figures)}
        if p == self.director:
            return {**base, "role": "director",
                    "target": [self.url(f) for f in self.target]}

        cards, board = self.cards[p], self.boards[p]     # get the card pool and board for matcher 'p'
        return {**base,
                "role": "matcher",
                "cards": {c: self.url(f) for c, f in cards.items()},
                "slots": board,
                "pool": [c for c in cards if c not in board]}


    def apply(self, p, action):
        if p not in self.boards:
            raise ValueError("only matchers can place cards")

        if action.get("type") != "place":
            raise ValueError("unknown action")
        
        card, slot = action.get("card"), action.get("slot")
        if card not in self.cards[p]:
            raise ValueError(f"no card '{card}'")
        if slot is not None and (not isinstance(slot, int) or not 1 <= slot <= self.size):
            raise ValueError(f"slot must be 1 to {self.size}, or none to take the card back")
        board = self.boards[p]
        if card in board:
            board[board.index(card)] = None    # remove selected card from matcher board
        displaced = None
        if slot is not None:
            displaced = board[slot - 1]        # and put it back in the matcher's pool
            board[slot - 1] = card             # replace with current card
        return { "action": "place", "card": card,
                 "figure": self.cards[p][card],
                 "slot": slot, "displaced": displaced }
    

    def round_complete(self, status):
        done = [status[m]["done"] for m in self.matchers]
        return any(done) if self.rules.trial_end == "first_done" else all(done)


    def correct(self):
        return self.target

    def score(self, status):
        out = {self.director: {"role": "director"}}
        for m in self.matchers:
            placed = [self.cards[m][c] if c else None for c in self.boards[m]]
            out[m] = {"role": "matcher", "board": placed, "of": self.size,
                      "correct_slots": sum(a == b for a, b in zip(placed, self.target))}
        return out 



class MazeEngine(TaskEngine):
    """
    A shared token moves through a maze.
    Each participant knows some of the cells.
    Moves are proposed and accepted under the decision rule
    """

    COMMANDS = {
                "go": ("move", "m"),
                "accept": ("yes", "ok", "agree"),
                "reject": ("no", "disagree"),
                "cancel": ()
                }

    DIRECTIONS = {"up": "up", "u": "up", "north": "up", "n": "up",
                  "down": "down", "d": "down", "south": "down",
                  "left": "left", "l": "left", "west": "left", "w": "left",
                  "right": "right", "r": "right", "east": "right", "e": "right"}

    FILLER = {"to", "the", "a", "one", "step", "please", "go", "move"}


    HELP = ("/go up|down|left|right    propose a move (also /move, /m); the same\n"
            "                          direction as an open proposal agrees with it\n"
            "/accept [direction]       agree with a proposal (also /agree, /yes)\n"
            "/reject [direction]       disagree with it (also /disagree, /no)\n"
            "                          the direction can be left out if only one is open;\n"
            "                          you back one direction at a time and can change\n"
            "/cancel                   withdraw your own proposal\n"
            "/help                     show this list")
    

    def plan_rounds(self, rng):
        return self._ordered(list(self.stimuli.mazes), rng)

    def setup_round(self, round_no, round_id, participants, rng):
        super().setup_round(round_no, round_id, participants, rng)
        spec = self.stimuli.mazes[round_id]
        self.grid = maze.parse_grid(spec["grid"])
        self.start, self.end = maze.find(self.grid, "s"), maze.find(self.grid, "e")
        self.shortest = len(maze.shortest_path(self.grid)) - 1
        known = spec.get("known") or {}
        self.known = {p: {maze.cell(t) for t in known.get(p, [])} | {self.start, self.end}
                       for p in participants}
        self.pos, self.path = self.start, [self.start]
        self.walls_found = set()
        self.moves = self.bumps = 0
        self.lives = self.rules.lives
        self.budget = self.shortest + self.rules.extra_moves
        self.proposals = {}
        self.finished = None

    def setup_log(self):
        return {"shortest": self.shortest, "budget": self.budget,
                "known": {p: sorted(maze.name(*c) for c in cs)
                             for p, cs in self.known.items()}
                }

    def needed(self) -> int:
        n = len(self.participants)
        return {"single": 1,
                "majority": n // 2 + 1,
                "all": n}[self.rules.decide]

    def _first_direction(self, args):
        words = [w.strip(".,;!?") for w in args if w not in self.FILLER][:4]
        return next((self.DIRECTIONS[w] for w in words if w in self.DIRECTIONS), None), words


        
    def _direction(self, args) -> str:
        """
        Extract direction from a move/go command
        """
        d, words = self._first_direction(args)
        if d is None:
            if not words:
                raise CommandError("which way? /go up, down, left or right")
            close = difflib.get_close_matches(words[0], list(self.DIRECTIONS), n=1)
            hint = (f" Did you mean /go {self.DIRECTIONS[close[0]]}?" if close
                    else " Use up, down, left or right.")
            raise CommandError(f"'{words[0]}' isn't a direction.{hint}")
        dr, dc = maze.DIRS[d]
        if not maze.inside(self.grid, self.pos[0] + dr, self.pos[1] + dc):
            raise CommandError(f"can't go {d}: that's the edge of the maze")
        return d

    def _open(self, args) -> str:
        """Which open proposal an /accept or /reject refers to."""
        if not self.proposals:
            raise CommandError("there's no open proposal")
        d, _ = self._first_direction(args)
        if d is not None:
            if d not in self.proposals:
                raise CommandError(f"no open proposal for {d}; open: {', '.join(self.proposals)}")
            return d
        if len(self.proposals) > 1:
            raise CommandError("which one? " + " or ".join(self.proposals))
        return next(iter(self.proposals))
    
    def _standing(self) -> str:
        """
        Look at current proposals and determine participant's vote on them
        """
        if not self.proposals:
            return "No open proposals."
        parts = []
        for d, pr in self.proposals.items():
            t = f"{d} ({pr['by']}): for {', '.join(sorted(pr['agree'])) or 'nobody'}"
            if pr["disagree"]:
                t += f"; against {', '.join(sorted(pr['disagree']))}"
            parts.append(t)
        return " · ".join(parts) + f" (needs {self.needed()})"

    
    
    def view(self, p):
        if not self.participants or p not in self.participants:
            return {}

        proposals = [{"dir": d, "by": pr["by"], "agree": sorted(pr["agree"]),
                               "disagree": sorted(pr["disagree"])}
                              for d, pr in self.proposals.items()]


        return {"participant": p,
                "size": [len(self.grid), len(self.grid[0])],
                "known": {maze.name(r,c): self.grid[r][c] for r, c in sorted(self.known[p])},
                "position": maze.name(*self.pos),
                "path": [maze.name(*c) for c in self.path],
                "walls_found": sorted(maze.name(*c) for c in self.walls_found),
                "lives": self.lives,
                "moves_left": self.budget - self.moves,
                "proposals": proposals,
                "needed": self.needed(),
                "finished": self.finished}

    def takes_arg(self, cmd, word):
        return cmd in ("go", "accept", "reject") and (word in self.DIRECTIONS or word in self.FILLER)
    
    def command(self, p, cmd, args):
        if self.finished:
            raise ValueError("this maze is finished")

        if cmd == "go":
            d = self._direction(args)
            if self.needed() == 1:
                self.proposals = {}
                return {"notice": f"{p}: {self._move(d)}"}
            if d in self.proposals:
                return self._vote(p, d, True)
            said = f"{p} proposes {d}"
            if self.rules.proposals == "single" and self.proposals:
                old_d, old = next(iter(self.proposals.items()))
                said += f" instead of {old['by']}'s {old_d}"
                self.proposals = {}
            self.proposals[d] = {"by": p, "agree": set(), "disagree": set()}
            return self._vote(p, d, True, said=said)

        if cmd == "accept":
            return self._vote(p, self._open(args), True)

        if cmd == "reject":
            d = self._open(args)
            if self.proposals[d]["by"] == p:
                raise CommandError("it's your proposal; use /cancel to withdraw it")
            return self._vote(p, d, False)

        if cmd == "cancel":
            mine = [d for d, pr in self.proposals.items() if pr["by"] == p]
            if not mine:
                raise CommandError("you have no open proposal")
            for d in mine:
                del self.proposals[d]
            return {"notice": f"{p} withdrew their proposal ({', '.join(mine)}). {self._standing()}"}

        raise CommandError(f"unknown command /{cmd}")

    def _vote(self, p, d, agree: bool, said: str | None = None) -> dict:
        pr = self.proposals[d]
        if said is None:
            if p in (pr["agree"] if agree else pr["disagree"]):
                raise CommandError(f"you already {'agree' if agree else 'disagree'} with {d}")
            changed = p in pr["agree"] or p in pr["disagree"]
            said = f"{p} {'now ' if changed else ''}{'agrees' if agree else 'disagrees'} with {d}"
        pr["disagree" if agree else "agree"].discard(p)
        pr["agree" if agree else "disagree"].add(p)
        if agree:                                  # one direction each: support moves
            for other in [o for o in self.proposals if o != d]:
                op = self.proposals[other]
                if p in op["agree"]:
                    op["agree"].discard(p)
                    if op["by"] == p:
                        del self.proposals[other]
                        said += f" (withdrawing their {other})"
        return self._maybe_move(d, said)

    def _maybe_move(self, d: str, said: str) -> dict:
        if len(self.proposals[d]["agree"]) < self.needed():
            return {"notice": f"{said}. {self._standing()}"}
        others = len(self.proposals) > 1
        self.proposals = {}
        cleared = " Other proposals cleared." if others else ""
        return {"notice": f"{said}. {self._move(d)}{cleared}"}    

    def _move(self, d: str) -> str:
        dr, dc = maze.DIRS[d]
        target = (self.pos[0] + dr, self.pos[1] + dc)       # the resulting position of selected move
        self.moves +=1
        if self.grid[target[0]][target[1]] == "x":          # check for collision with wall 'x'
            self.bumps += 1
            if self.rules.wall_reveal == "all":
                self.walls_found.add(target)
            if self.lives is not None:
                self.lives -= 1
            text = f"Moved {d}: hit a wall at {maze.name(*target)}."
            if self.rules.on_wall == "restart":                 # restart maze after collision
                self.pos, self.path = self.start, [self.start]
                text += " Back to the start."

        else:
            self.pos = target
            self.path.append(target)
            text = f"Moved {d} to {maze.name(*target)}."
            if target == self.end:
                self.finished = "goal"
                return text + " Reached the end!"

        if self.lives is not None and self.lives <= 0:
            self.finished = "no_lives"
            return text + " No lives left."

        if self.moves >= self.budget:
            self.finished = "no_moves"
            return text + " No moves left."

        lives = f" Lives left: {self.lives}." if self.lives is not None else ""
        return text + lives + f" Moves left: {self.budget - self.moves}."

    def round_complete(self, status):
        return self.finished is not None

    def score(self, status):
        result = {"finished": self.finished,
                  "reached_end": self.finished == "goal",
                  "moves": self.moves,
                  "bumps": self.bumps,
                  "shortest": self.shortest,
                  "path": [maze.name(*c) for c in self.path]}
        return {p: dict(result) for p in status}
    

        
ENGINES = {
    "choice": ChoiceEngine,
    "matching": MatchingEngine,
    "maze": MazeEngine
}

