"""Turn-taking strategies.

Each strategy is an async function (agent, trigger) that takes at most one turn.
A condition chooses one with `turn_taking: <name>`. To add a strategy, write a
function here and add it to STRATEGIES.

What a strategy can use from the agent:
  agent.ask(runnable, stage, instruction)   call the model (retries, traces)
  agent.act(action, context_seq, cooldown)  carry out the model's chosen action
  agent.task.render(name, **variables)      render a prompt template
  agent.typing_note()                       who is typing right now
  agent.cooling_down()                      whether the agent spoke very recently
  agent.trace(**details)                    record something in the session log
  agent.settings, agent.rng, agent.last_seq
"""


import asyncio
from uuid import uuid4


async def decide_then_act(agent, trigger):
    """
    Pause, decide whether to speak (one model call), then decide what to do (second call)

    a new message abandons the turn in progress
    """

    if not agent.in_round:
        return

    epoch = agent.round_epoch
    turn_id = uuid4().hex[:8]
    

    def still_current():
        return agent.in_round and agent.round_epoch == epoch

    # wait for `think_time`
    await asyncio.sleep(agent.rng.uniform(*agent.settings.think_time))

    # check that round still in process
    if not still_current():
        return

    # if in cooling down period return
    if agent.cooling_down():
        agent.trace(stage="gate", outcome="cooldown", context_seq=agent.last_seq, turn_id=turn_id)
        return

    context_seq = agent.last_seq

    # decide how to respond to most recent message
    if trigger.kind == "message":
        decision = await agent.ask(agent.decider, "decide",
                                   agent.render_decide(trigger), turn_id=turn_id)
        if not decision or not decision.speak or not still_current():
            return

    silence = trigger.kind == "silence"

    # set action based on whether there is silence or decision to take a turn
    action = await agent.ask(agent.actor, "silence_turn" if silence else "turn",
                             agent.render_turn(silence), turn_id=turn_id)

    # check if action to be taken AND round still in process
    if action and still_current():
        cooldown = (agent.settings.cooldown_after_silence if silence
                    else agent.settings.cooldown_after_reply)
        await agent.act(action, context_seq, cooldown, epoch=epoch, turn_id=turn_id)
        

STRATEGIES = {
    "decide_then_act": decide_then_act,
}
    
    
def render_decide(self, trigger) -> str:
    return self.task.render("decide",
                            participant=self.id,
                            last_sender=trigger.sender,
                            last_message=trigger.text,
                            typing_note=self.typing_note(),
                            status_note=self.status_note())

def render_turn(self, silence: bool) -> str:
    return self.task.render("turn",
                            situation=self.task.render("silence") if silence else "",
                            typing_note=self.typing_note(),
                            status_note=self.status_note(),
                            max_words=self.settings.max_words)
