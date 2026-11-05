"""How reliably can a small model tell "my change is already on main" (agreement) from "someone made the same
edit, but mine was relative to what I saw" (a lost increment)?

    PYTHONPATH=workspace/src python3 workspace/eval/confirm_probe.py [model ...]

Two ways of asking are compared on cases where agreement is right (a semicolon, a rename) and wrong (a counter,
a version bump): a yes/no question to the model, and "re-apply your task to the current line" with the answer
compared by the program. Needs an OpenAI-compatible endpoint (AGENTTRUNK_LLM_URL, AGENTTRUNK_LLM_HOST).
"""
import os
import re
import statistics as st
import sys
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src"))
from cmws_ws.agents import OpenAICompatModel
URL = os.environ.get("AGENTTRUNK_LLM_URL", os.environ.get("CMWS_LLM_URL", "http://127.0.0.1:11434/v1"))
_host = os.environ.get("AGENTTRUNK_LLM_HOST", os.environ.get("CMWS_LLM_HOST"))
HOST = {"Host": _host} if _host else None

CASES = {
  "counter (agreement is WRONG)": dict(
      task="In stats.py the line `COUNT = <number>` holds a counter. Increase that number by exactly 1.",
      mine="COUNT = 0  ->  COUNT = 1", main_now="COUNT = 1", agree_is_right=False, applied_result="COUNT = 2"),
  "semicolon (agreement is RIGHT)": dict(
      task="In util.py make sure the line `x = 1` ends with a semicolon.",
      mine="x = 1  ->  x = 1;", main_now="x = 1;", agree_is_right=True, applied_result="x = 1;"),
  "version bump (agreement is WRONG)": dict(
      task="In version.py bump the patch number of VERSION by one.",
      mine='VERSION = "1.4.2"  ->  VERSION = "1.4.3"', main_now='VERSION = "1.4.3"', agree_is_right=False, applied_result='VERSION = "1.4.4"'),
  "rename (agreement is RIGHT)": dict(
      task="In app.py rename the variable `tmp` to `buffer`.",
      mine="tmp = []  ->  buffer = []", main_now="buffer = []", agree_is_right=True, applied_result="buffer = []"),
}

def yesno(c):
    return (f"Your task was: {c['task']}\nYour change was: {c['mine']}\n"
            f"Meanwhile main already contains an identical change. Is your task's goal met? Answer with one word: yes or no.")

def reapply(c):
    return (f"Your task was: {c['task']}\nYou made this change on an older copy: {c['mine']}\n"
            f"Main has moved. The line on the current main is: {c['main_now']}\n"
            f"If you now carried out your task again on the current main, what would the line be afterwards? "
            f"Answer with that line only.")

def ask(model, prompt):
    r = model.chat([{"role": "user", "content": prompt}], [])
    return r.content.strip(), r.usage.prompt_tokens, r.usage.completion_tokens

N = 8
for name in (sys.argv[1:] or ["eco:latest", "quality:latest"]):
    m = OpenAICompatModel(name, URL, timeout=300, temperature=0.7, headers=HOST)
    print(f"\n=== {name}")
    for label, c in CASES.items():
        ok_yn = ok_ra = 0; toks_yn = []; toks_ra = []
        for _ in range(N):
            a, p, q = ask(m, yesno(c)); toks_yn.append(p + q)
            said_yes = a.lower().startswith("yes") or ("yes" in a.lower() and "no" not in a.lower()[:3])
            ok_yn += (said_yes == c["agree_is_right"])
            a, p, q = ask(m, reapply(c)); toks_ra.append(p + q)
            line = a.strip().strip("`").splitlines()[0].strip().strip("`") if a.strip() else ""
            agrees = (line == c["main_now"])          # decided by comparing, not by asking
            ok_ra += (agrees == c["agree_is_right"])
        print(f"{label:36} yes/no correct {ok_yn}/{N} (~{st.mean(toks_yn):.0f} tok) | re-apply+compare correct {ok_ra}/{N} (~{st.mean(toks_ra):.0f} tok)")
