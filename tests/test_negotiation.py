import pytest

from hiclose.rules import Decision, Policy, decide

JACKET = Policy(list_cents=8000, floor_cents=6800, max_rounds=3)


def test_offer_above_list_is_accepted_at_list():
    assert decide(JACKET, 9000, 1) == Decision("accept", 8000)


def test_offer_at_list_is_accepted():
    assert decide(JACKET, 8000, 1) == Decision("accept", 8000)


def test_offer_between_floor_and_list_is_accepted_as_asked():
    assert decide(JACKET, 7000, 1) == Decision("accept", 7000)


def test_offer_exactly_at_floor_is_accepted():
    assert decide(JACKET, 6800, 2) == Decision("accept", 6800)


def test_lowball_gets_counter_stepping_down_each_round():
    assert decide(JACKET, 5000, 1) == Decision("counter", 7600)
    assert decide(JACKET, 5000, 2) == Decision("counter", 7200)


def test_last_round_is_a_final_offer_at_floor():
    assert decide(JACKET, 5000, 3) == Decision("final", 6800)
    assert decide(JACKET, 5000, 7) == Decision("final", 6800)


def test_counter_never_goes_below_floor():
    tight = Policy(list_cents=1000, floor_cents=999, max_rounds=3)
    for round_no in (1, 2):
        decision = decide(tight, 100, round_no)
        assert decision.price_cents >= 999


def test_counters_are_rounded_up_to_whole_units():
    scarf = Policy(list_cents=6500, floor_cents=5500, max_rounds=3)
    assert decide(scarf, 500, 1) == Decision("counter", 6200)
    assert decide(scarf, 500, 2) == Decision("counter", 5900)


def test_zero_or_negative_offer_is_rejected():
    assert decide(JACKET, 0, 1) == Decision("reject", 0)
    assert decide(JACKET, -500, 1) == Decision("reject", 0)


def test_no_decision_ever_goes_below_floor():
    for asked in range(1, 10000, 137):
        for round_no in range(1, 6):
            decision = decide(JACKET, asked, round_no)
            if decision.action != "reject":
                assert decision.price_cents >= JACKET.floor_cents


@pytest.mark.parametrize("list_cents,floor_cents", [(8000, 9000), (8000, 0), (8000, -1)])
def test_bad_policy_is_refused(list_cents, floor_cents):
    with pytest.raises(ValueError):
        Policy(list_cents=list_cents, floor_cents=floor_cents)
