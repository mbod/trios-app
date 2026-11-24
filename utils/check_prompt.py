# check_prompt.py  usage: python check_prompt.py <log.jsonl> <seq>
import json, sys
from langchain_openai import ChatOpenAI
from langchain.chat_models import init_chat_model

from dotenv import load_dotenv
_ = load_dotenv()

log, seq = sys.argv[1], int(sys.argv[2])
event = next(e for e in map(json.loads, open(log)) if e["seq"] == seq)
llm = ChatOpenAI(model="gpt-6-luna", reasoning_effort="low")
llm = init_chat_model('anthropic:claude-haiku-5-5')
reply = llm.invoke([("system", event["payload"]["prompt"]),
                    ("human", "List the 9 items in your picture, by position 1-9. describe what it shows as a person looking at it would see it ")])
print(reply.content)
