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


async def decide_then_act(agent, trigger):
    """
    Pause, decide whether to speak (one model call), then decide what to do (second call)

    The behavior of the first pilot tests. Combined with `interrupt: cancel`,
    a new message abandons the turn in progress
    """
    await asyncio.sleep(agent.rng.uniform(*agent.settings.think_time))
    if agent.cooling_down():
        agent.trace(stage="gate", outcome="cooldown", context_seq=agent.last_seq)
        return

    if trigger.kind == "message":
        decision = await agent.ask(agent.decider, "decide", agent.task.render(
            "decide", participant=agent.id, last_sender=trigger.sender,
            last_message=trigger.text, typing_note=agent.typing_note(),
            status_note=agent.status_note()
        ))

        if not decision or not decision.speak:
            return


    silence = trigger.kind == "silence"
    context_seq = agent.last_seq
    action = await agent.ask(
        agent.actor, "silence_turn" if silence else "turn",
        agent.task.render("turn",
                          situation=agent.task.render("silence") if silence else "",
                          typing_note=agent.typing_note(),
                          status_note=agent.status_note(),
                          max_words=agent.settings.max_words))

    if action:
        cooldown = (agent.settings.cooldown_after_silence if silence
                    else agent.settings.cooldown_after_reply)
        await agent.act(action, context_seq, cooldown)



STRATEGIES = {
    "decide_then_act": decide_then_act,
}
    
    
