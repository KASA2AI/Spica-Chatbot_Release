"""ActiveDomainRouter unit contract (OO migration Phase 8-c1, 设计裁决 1).

Pins: publish/current/retract lifecycle, priority selection, the tie rule
(latest publish wins + WARNING once at publish time -- ties are a config
error but never raise), the in-memory NO-THROW contract (the controller's
binding sink must never be able to break start/stop), and the domain-filtered
``current_for`` read (the only router read galgame-only closures may use).
"""

import unittest

from spica.host.domain_router import ActiveDomainRouter


class LifecycleTest(unittest.TestCase):
    def test_empty_router_current_is_none(self):
        self.assertIsNone(ActiveDomainRouter().current())

    def test_publish_current_retract(self):
        router = ActiveDomainRouter()
        binding = object()
        router.publish("galgame", binding)
        self.assertIs(router.current(), binding)
        router.retract("galgame")
        self.assertIsNone(router.current())

    def test_republish_replaces(self):
        router = ActiveDomainRouter()
        first, second = object(), object()
        router.publish("galgame", first)
        router.publish("galgame", second)
        self.assertIs(router.current(), second)

    def test_current_for_is_domain_filtered(self):
        router = ActiveDomainRouter()
        game, watch = object(), object()
        router.publish("galgame", game, priority=0)
        router.publish("cowatch", watch, priority=5)
        self.assertIs(router.current(), watch)          # priority owns the turn
        self.assertIs(router.current_for("galgame"), game)  # filtered read unaffected
        self.assertIs(router.current_for("cowatch"), watch)
        self.assertIsNone(router.current_for("browser"))


class PriorityTest(unittest.TestCase):
    def test_highest_priority_wins(self):
        router = ActiveDomainRouter()
        low, high = object(), object()
        router.publish("galgame", low, priority=0)
        router.publish("cowatch", high, priority=10)
        self.assertIs(router.current(), high)
        router.retract("cowatch")
        self.assertIs(router.current(), low)  # falls back to the survivor

    def test_tie_latest_publish_wins_and_warns_once(self):
        router = ActiveDomainRouter()
        first, second = object(), object()
        router.publish("galgame", first, priority=0)
        with self.assertLogs("spica.host.domain_router", level="WARNING") as logs:
            router.publish("cowatch", second, priority=0)  # tie -> config error
        self.assertEqual(len(logs.output), 1)  # WARNING exactly once, at publish time
        self.assertIn("priority tie", logs.output[0])
        self.assertIs(router.current(), second)  # latest publish wins the tie


class NoThrowContractTest(unittest.TestCase):
    def test_retract_of_unknown_domain_is_a_noop(self):
        router = ActiveDomainRouter()
        router.retract("never-published")  # must not raise
        self.assertIsNone(router.current())

    def test_publish_retract_never_throw_under_repeated_use(self):
        # The sink contract (设计裁决 6): in-memory, no-throw -- the controller
        # calls these at publish-LAST/clear-FIRST and MUST never be broken.
        router = ActiveDomainRouter()
        for i in range(100):
            router.publish("galgame", object(), priority=i % 3)
            router.retract("galgame")
            router.retract("galgame")  # double retract: still a no-op
        self.assertIsNone(router.current())


if __name__ == "__main__":
    unittest.main()
