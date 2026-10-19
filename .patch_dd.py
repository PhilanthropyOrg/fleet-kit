p = 'scripts/denial_asks.py'
s = open(p).read()
anchor = 'GH_VERB = re.compile(r"(?:^|[\\s;&|(])gh\\s+([a-z-]+)(?:\\s+([a-z-]+))?")\n'
assert anchor in s
s = s.replace(anchor, anchor + (
    "# A gh verb inside a quoted argument is text, not a call: jefe's ack note quoting the denied\n"
    "# command (\"Keep the deny ... gh issue create\") was itself refused and filed as asks #156/#157.\n"
    "QUOTED = re.compile(r\"'[^']*'|\\\"(?:[^\\\"\\\\\\\\]|\\\\\\\\.)*\\\"\", re.S)\n"))
old = '        m = GH_VERB.search(cmd)'
assert old in s
s = s.replace(old, '        m = GH_VERB.search(QUOTED.sub("\'\'", cmd))')
open(p, 'w').write(s)

t = 'scripts/test_never_silent_8215.py'
s = open(t).read()
old = '        {"tool_name": "Bash", "tool_use_id": "d", "tool_input": {"command": "gh pr merge 1"}}]}'
assert old in s
s = s.replace(old, '''        {"tool_name": "Bash", "tool_use_id": "d", "tool_input": {"command": "gh pr merge 1"}},
        {"tool_name": "Bash", "tool_use_id": "e", "tool_input": {"command":
            'python3 fleet_msg.py ack --id 1386 --note "Keep. vp denies gh issue create on purpose"'}},
        {"tool_name": "Bash", "tool_use_id": "f", "tool_input": {"command":
            "python3 fleet_msg.py ack --id 1388 --note 'jefe denies gh pr merge only'"}}]}''')
open(t, 'w').write(s)

import sys
sys.path.insert(0, 'scripts')
import denial_asks as d
r = d.gh_denials({'permission_denials': [
    {'tool_name': 'Bash', 'tool_input': {'command': 'python3 x.py --note "a gh pr merge b \\"q\\" c"'}},
    {'tool_name': 'Bash', 'tool_input': {'command': 'cd /x && gh issue create --title "y" --body "gh pr merge"'}}]})
assert [x['verb'] for x in r] == ['gh issue create'], r
print('ok', r)
