"""
Task engines: the rules of one task

The session runs chat, timing, done signals and logging. The engine plans the
rounds, deals each one, says what each participant sees, applies task actions,
decides when a round is complete and scores it.
"""


import string

from tasks import Task, StimulusSet, Rules



class TaskEngine:
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



class ChoiceEngine(TaskEngine):
    """
    Everyone has their own picture; the answer is chosen from fixed options
    and comes with the done signal (common lines, spot the difference)
    """

    def plan_rounds(self, rng):
        order = list(self.stimuli.rounds)
        if self.rules.order == "shuffled":
            rng.shuffle(order)
        if self.rules.rounds is not None:
            if self.rules.rounds > len(order):
                raise ValueError(f"{self.rules.rounds} rounds requested; "
                                 f"{self.stimuli_name} has {len(order)}")
            order = order[:self.rules.rounds]
            
    
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
        base = {"participant": p, "grid": list(self.rules.grid)}
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
            
ENGINES = {
    "choice": ChoiceEngine,
    "matching": MatchingEngine
}

