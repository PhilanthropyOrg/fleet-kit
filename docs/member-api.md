# Pinging a member (fk#1429)

Ping a member for its status, send it a command, read back whether it understood and
finished, pause/resume its cron. All five routes live on `fleet_view_server.py` — see
`README.md`'s "Driving the fleet from outside the box" for the full route list.

## Base URL

`https://dino.luckymachines.co` (port 8420 behind the tunnel).

## Auth

Read (`GET`) needs nothing. Write (`POST`) needs the shared key as an `X-Fleet-Key` header —
same key every other write route uses:

```bash
curl -X POST https://dino.luckymachines.co/api/members/marie/pause \
  -H "X-Fleet-Key: $FLEET_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"by":"reif"}'
```

`FLEET_API_KEY` lives in `FLEET_API_KEY=` in the instance's `fleet.env` (on dino:
`/home/ubuntu/fleet-kit/instances/nonprofit-atlas/fleet.env`) — gitignored, never printed here.
Read it with `grep '^FLEET_API_KEY=' <that path>` (README.md's "Where the key lives, and how to
get it" has the full instructions, including setting one for the first time).

## Ping — is it alive, what's it doing

```bash
curl -s https://dino.luckymachines.co/api/members/marie/ping | jq
```
```json
{
  "member": "marie", "enabled": true, "running": false, "running_since": null,
  "last_run": {"status": "success", "at": 1758999999.1},
  "next_due": "2026-09-29T18:33:00+00:00", "inbox_open": 0,
  "paused": false, "paused_since": null, "paused_by": null
}
```
404 for an unknown member name.

## Command — tell a member to do something now

```bash
curl -X POST https://dino.luckymachines.co/api/members/marie/command \
  -H "X-Fleet-Key: $FLEET_API_KEY" -H "Content-Type: application/json" \
  -d '{"text":"check the Q3 backlog", "from":"reif"}'
```
```json
{"ok": true, "msg_id": 42, "woke": true}
```
`woke` is `true` if this launched marie's next pass right now, or the string `"queued-busy"` if
it's waiting on marie's own cadence (she's mid-pass, disabled, paused, or woken too recently) —
either way the command is queued in her inbox.

## Message status — open → understood → done

On its next pass, the member acks the command (first word "Understood", one line of what it
will do), then closes it for good once finished, with evidence:

```bash
python3 scripts/fleet_msg.py ack  --me marie --id 42 --note "Understood: will triage the Q3 backlog"
python3 scripts/fleet_msg.py done --me marie --id 42 --note "PR #4021 -- triaged, 6 issues labeled"
```

Read the state back any time:

```bash
curl -s https://dino.luckymachines.co/api/messages/42 | jq
```
```json
{"id": 42, "state": "understood", "ack_text": "Understood: will triage the Q3 backlog",
 "acked_at": 1759000100.2, "done_text": null, "done_at": null}
```
`state` is `open` (not yet acked) → `understood` (acked, not yet done) → `done` (closed, with
evidence). A command left `understood` past its escalation window (2 of the member's own
cadences) escalates to jefe the same way an unacked message always has.

## Pause / resume

```bash
curl -X POST https://dino.luckymachines.co/api/members/marie/pause \
  -H "X-Fleet-Key: $FLEET_API_KEY" -H "Content-Type: application/json" -d '{"by":"reif"}'
```
```json
{"ok": true, "paused": true, "since": 1759003800.0, "by": "reif"}
```
```bash
curl -X POST https://dino.luckymachines.co/api/members/marie/resume \
  -H "X-Fleet-Key: $FLEET_API_KEY" -H "Content-Type: application/json" -d '{}'
```
```json
{"ok": true, "paused": false, "resumed": true}
```
Paused skips marie's scheduled AND woken passes until resumed; a pass already running is left
alone. `ping` above always shows the current `paused`/`paused_since`/`paused_by`.
