from jev_watchdog.display.feed import Feed


def test_records_are_numbered_from_one_and_asked_for_by_number():
    feed = Feed()
    for n in range(4):
        feed.publish({"n": n})
    assert [record["seq"] for record in feed.since()] == [1, 2, 3, 4]
    assert [record["n"] for record in feed.since(2)] == [2, 3]
    assert feed.since(4) == []


def test_only_the_last_records_are_kept():
    feed = Feed(backlog=3)
    for n in range(5):
        feed.publish({"n": n})
    assert [record["n"] for record in feed.since()] == [2, 3, 4]


def test_each_process_has_its_own_boot_id():
    assert Feed().boot != Feed().boot
