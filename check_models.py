

import asyncio
from dotenv import load_dotenv
_ = load_dotenv()


from config import ModelRegistry
from llm import build_chat_model, structured
from schemas import SpeakDecision


async def check(name, spec):
    runnable = structured(build_chat_model(spec), SpeakDecision, spec)
    result = await runnable.ainvoke([
        {"role": "system", "content": "You are Participat A in a group chat."},
        {"role": "user", "content": "B: A, which item is in your top-left cell?"},
        {"role": "user", "content": "Decide whether you should speak next"}
    ])

    print("-"*50)
    print(f"{name:8} parsed={result['parsed']} error={result['parsing_error']}")
    print()

async def main():

    REFS = ["openai:gpt-5-nano", "anthropic:claude-haiku-5-5",
            "google_genai:gemini-3.5-flash-lite",
            "gpt-6-luna-low-effort"]

    registry = ModelRegistry()
    
    for ref in REFS:
        try:
            await check(ref, registry.resolve(ref))
        except Exception as e:
            print(f"{ref:8} FAILED: {type(e).__name__}: {e}")

asyncio.run(main())


            
        
