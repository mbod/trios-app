"""Turn-taking strategies (the agent's participation policy).

Each strategy is an async function (agent, trigger) that takes at most one turn.
A condition chooses one per agent with `turn_taking: <name>`; the default is
act_only. To add a strategy, write a function here and add it to STRATEGIES.

Everything around a strategy is shared and fixed: what wakes the agent (round
start, messages, notices, feedback on its own commands, silence), cooldowns,
typing, and how replies reach the server. In command tasks a reply is one chat
message that may contain commands, exactly as a human types it.

What a strategy can use from the agent:
  agent.ask(runnable, stage, instruction)   call the model (retries, traces)
  agent.act(reply, context_seq, cooldown)   send the reply
  agent.render_decide(trigger), agent.render_turn(silence)
  agent.cooling_down(), agent.trace(**details)
  agent.settings, agent.rng, agent.last_seq
"""

import asyncio
from uuid import uuid4


def _turn_guard(agent):
    epoch = agent.round_epoch
    return epoch, (lambda: agent.in_round and agent.round_epoch == epoch)


async def _reply(agent, trigger, epoch, still_current, turn_id, context_seq):
    """One model call for what to say or do, then send it."""
    silence = trigger.kind == "silence"
    reply = await agent.ask(agent.actor, "silence_turn" if silence else "turn",
                            agent.render_turn(silence), turn_id=turn_id)
    if reply and still_current():
        cooldown = (agent.settings.cooldown_after_silence if silence
                    else agent.settings.cooldown_after_reply)
        await agent.act(reply, context_seq, cooldown, epoch=epoch, turn_id=turn_id)


async def act_only(agent, trigger):
    """
    One call per trigger: the reply is a message (which may hold commands),
    or nothing. Feedback on the agent's own command skips the cooldown, as a
    person would retype a command straight away.
    """
    if not agent.in_round:
        return
    epoch, still_current = _turn_guard(agent)
    turn_id = uuid4().hex[:8]

    await asyncio.sleep(agent.rng.uniform(*agent.settings.think_time))
    if not still_current():
        return
    if trigger.kind != "feedback" and agent.cooling_down():
        agent.trace(stage="gate", outcome="cooldown", context_seq=agent.last_seq, turn_id=turn_id)
        return

    await _reply(agent, trigger, epoch, still_current, turn_id, agent.last_seq)


async def decide_then_act(agent, trigger):
    """
    Pause; for a new message, decide whether to take a turn (one call, reasons
    from `speak_when`, deferring if `defer`); then the turn itself (second call).
    Round start, status changes, notices and feedback skip the decision.
    """
    if not agent.in_round:
        return
    epoch, still_current = _turn_guard(agent)
    turn_id = uuid4().hex[:8]

    await asyncio.sleep(agent.rng.uniform(*agent.settings.think_time))
    if not still_current():
        return
    if trigger.kind != "feedback" and agent.cooling_down():
        agent.trace(stage="gate", outcome="cooldown", context_seq=agent.last_seq, turn_id=turn_id)
        return

    context_seq = agent.last_seq
    if trigger.kind == "message":
        decision = await agent.ask(agent.decider, "decide",
                                   agent.render_decide(trigger), turn_id=turn_id)
        if not decision or not decision.speak or not still_current():
            return

    await _reply(agent, trigger, epoch, still_current, turn_id, context_seq)


STRATEGIES = {
    "act_only": act_only,
    "decide_then_act": decide_then_act,
}


