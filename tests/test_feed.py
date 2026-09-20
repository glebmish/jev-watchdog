from jev_watchdog.feed import Feed


def test_publish_numbers_records_from_one():
    feed = Feed()
    assert feed.publish({"kind": "note"}) == {"kind": "note", "seq": 1}
    assert feed.publish({"kind": "note"})["seq"] == 2


def test_each_process_has_its_own_boot_id():
    assert Feed().boot != Feed().boot


def test_backlog_keeps_only_the_last_records():
    feed = Feed(backlog=3)
    for n in range(5):
        feed.publish({"n": n})
    assert [record["n"] for record in feed.subscribe().backlog] == [2, 3, 4]


def test_subscribe_since_returns_only_later_records():
    feed = Feed()
    for n in range(4):
        feed.publish({"n": n})
    assert [record["seq"] for record in feed.subscribe(since=2).backlog] == [3, 4]


def test_a_live_record_reaches_every_subscriber_and_not_the_backlog_they_hold():
    feed = Feed()
    feed.publish({"n": 0})
    first, second = feed.subscribe(), feed.subscribe()
    feed.publish({"n": 1})
    assert first.queue.get_nowait()["n"] == 1
    assert second.queue.get_nowait()["n"] == 1
    assert [record["n"] for record in first.backlog] == [0]


def test_a_subscriber_that_falls_behind_is_dropped_and_its_stream_ended():
    feed = Feed(queue_size=2)
    slow, reader = feed.subscribe(), feed.subscribe()
    for n in range(3):
        feed.publish({"n": n})
        reader.queue.get_nowait()
    assert slow.queue.get_nowait() is None  # what it had is gone: it comes back with `since`
    assert slow.queue.empty()
    feed.publish({"n": 3})
    assert slow.queue.empty()
    assert reader.queue.get_nowait()["n"] == 3


def test_unsubscribe_stops_delivery_and_may_be_repeated():
    feed = Feed()
    subscription = feed.subscribe()
    feed.unsubscribe(subscription)
    feed.unsubscribe(subscription)
    feed.publish({"n": 0})
    assert subscription.queue.empty()


def test_close_ends_every_subscriber():
    feed = Feed()
    first, second = feed.subscribe(), feed.subscribe()
    feed.close()
    assert first.queue.get_nowait() is None
    assert second.queue.get_nowait() is None
    feed.publish({"n": 0})
    assert first.queue.empty()


def test_a_subscription_to_a_closed_feed_ends_at_once():
    """Or an /events accepted during shutdown would hold the server's cleanup up."""
    feed = Feed()
    feed.close()
    late = feed.subscribe()
    assert late.queue.get_nowait() is None
    feed.publish({"n": 0})
    assert late.queue.empty()
