from dumbledore_pregate import decide

LADDER = {"member": "dumbledore", "status": "open", "metric": "ladder_share:x", "due_ts": 9}
OPEN = {"member": "dumbledore", "status": "open", "metric": "avg_turns:marie", "due_ts": 5}


def test_green_when_three_open_incl_ladder_and_nothing_new():
    assert decide([LADDER, OPEN, OPEN], 0, since=100).startswith("green")


def test_fires_on_open_ask_fresh_verdict_room_or_no_ladder():
    assert decide([LADDER, OPEN, OPEN], 1, since=100).startswith("FIRE 1 open ask")
    hit = {"member": "dumbledore", "status": "hit", "id": 7, "resolved_ts": 200}
    assert decide([LADDER, OPEN, OPEN, hit], 0, since=100).startswith("FIRE new verdict")
    assert decide([LADDER, OPEN, OPEN, hit], 0, since=300).startswith("green")
    assert decide([LADDER, OPEN], 0, since=100).startswith("FIRE 2 open")
    assert decide([OPEN, OPEN, OPEN], 0, since=100) == "FIRE no open ladder_* outcome bet"


def test_other_members_rows_do_not_count():
    other = dict(LADDER, member="minion")
    assert decide([other, OPEN, OPEN], 0, since=100).startswith("FIRE")
