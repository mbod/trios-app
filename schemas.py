
from typing import Literal

from pydantic import BaseModel, Field


class SpeakDecision(BaseModel):
    """
    Whether to speak next in the group chat
    """
    speak: bool = Field(description="true if you should send a message now")
    reason: str = Field(description="brief reason for the decision")



class TurnAction(BaseModel):
    """
    What to do on this turn
    """
    current_action: Literal["say", "ask", "suggest_difference", "summarize",
                            "wait", "task_complete"]
    message: str = Field(default="", description=(
        "the chat message to send, or empty string if waiting or task_complete"))


    
