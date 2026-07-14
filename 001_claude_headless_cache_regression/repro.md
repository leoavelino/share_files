Running command:
```python
python3 repro_cache_regression.py \
      --old-bin "npx -y @anthropic-ai/claude-code@2.1.202" \
      --new-bin "npx -y @anthropic-ai/claude-code@2.1.203"
```

Output:
```
== OLD: npx -y @anthropic-ai/claude-code@2.1.202
    system[0]: text len=74
    system[1]: text len=62 [cache_control]
    system[2]: text len=26961 [cache_control]
    messages[0][0] role=user: text len=8075  <-- HOOK CONTEXT
    messages[0][1] role=user: text len=1786
    messages[0][2] role=user: text len=5905
    messages[0][3] role=user: text len=306
    messages[0][4] role=user: text len=19 [cache_control]
    VERDICT: hook context is INSIDE the final cache_control prefix (cacheable)

== NEW: npx -y @anthropic-ai/claude-code@2.1.203
    system[0]: text len=74
    system[1]: text len=62 [cache_control]
    system[2]: text len=26961 [cache_control]
    messages[0][0] role=user: text len=306
    messages[0][1] role=user: text len=19 [cache_control]
    messages[1] role=system (plain string): text len=15799  <-- HOOK CONTEXT
    VERDICT: hook context is AFTER the final cache_control breakpoint (uncacheable, re-billed every request)
```
