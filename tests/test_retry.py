from app.services.retry import compute_backoff


def test_fixed_backoff_is_constant():
    assert [compute_backoff("fixed", 10, 300, a) for a in (1, 2, 3)] == [10, 10, 10]


def test_linear_backoff_grows_linearly():
    assert [compute_backoff("linear", 10, 300, a) for a in (1, 2, 3)] == [10, 20, 30]


def test_exponential_backoff_doubles():
    assert [compute_backoff("exponential", 5, 300, a) for a in (1, 2, 3, 4)] == [5, 10, 20, 40]


def test_backoff_is_capped_at_max_delay():
    assert compute_backoff("exponential", 5, 60, 10) == 60
    assert compute_backoff("linear", 50, 120, 100) == 120


def test_unknown_strategy_falls_back_to_fixed():
    assert compute_backoff("garbage", 7, 300, 3) == 7
