import tomllib
 
SCHEDULABLE = {"todo"}
SATISFIED   = {"done", "review"}      # SPEC 4.2: review satisfies downstream depends
AUTONOMOUS  = {"impl", "tester"}      # ADR 0001: architect/reviewer stay human-gated
 
with open("tasks.toml", "rb") as f:
    tasks = tomllib.load(f)["task"]
status = {t["id"]: t["status"] for t in tasks}
 
ready = [t for t in tasks
         if t["status"] in SCHEDULABLE
         and t["routing"] in AUTONOMOUS
         and all(status.get(d) in SATISFIED for d in t["depends"])]
 
for t in sorted(ready, key=lambda t: (t["phase"], t["id"])):
    print(f'{t["id"]:6} phase {t["phase"]}  '
          f'{len(t["touches"])} touches / {len(t["exit_criteria"])} criteria  '
          f'{t["title"][:52]}')
 
blocked_by = {t["id"]: [d for d in t["depends"] if status.get(d) not in SATISFIED]
              for t in tasks if t["status"] == "todo"}
print("\nnot ready:")
for tid, missing in blocked_by.items():
    if missing:
        print(f'  {tid:6} waiting on {", ".join(missing)}')
