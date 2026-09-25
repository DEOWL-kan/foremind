import itertools
import unittest

from foremind.state import (
    BATCH, BATCH_MAIN, BATCH_SIDE, BLOCK_REASONS, PENDING, SESSION,
    IllegalTransition, can_transition, reconcile, transition,
)

ACTIVE = ("ready", "running", "review_ready", "in_review", "changes_requested",
          "approved", "awaiting_audit", "delivered")

# every transition legal without context
BATCH_PLAIN = {
    ("planned", "ready"), ("ready", "running"), ("running", "review_ready"), ("review_ready", "in_review"),
    ("in_review", "changes_requested"), ("in_review", "approved"), ("in_review", "review_ready"),
    ("changes_requested", "running"),
    ("approved", "awaiting_audit"), ("approved", "delivered"), ("approved", "changes_requested"),
    ("awaiting_audit", "delivered"), ("awaiting_audit", "changes_requested"),
    ("delivered", "merged"), ("delivered", "changes_requested"), ("merged", "cataloged"),
    ("approved", "updating"), ("delivered", "updating"),
    ("updating", "approved"), ("updating", "changes_requested"),
    ("running", "stuck"), ("stuck", "running"),
    *((s, "paused") for s in ACTIVE),
    *((s, "failed") for s in ("running", "review_ready", "in_review", "changes_requested")),
    ("failed", "ready"),
    *((s, "cancelled") for s in BATCH_MAIN + BATCH_SIDE if s not in ("merged", "cataloged", "cancelled")),
}

PENDING_ALL = {
    ("open", "deciding"), ("open", "answered"), ("open", "awaiting_local_confirm"),
    ("deciding", "answered"), ("deciding", "provisional"), ("deciding", "escalated"),
    ("escalated", "answered"), ("escalated", "awaiting_local_confirm"),
    ("awaiting_local_confirm", "answered"), ("answered", "applied"),
    ("provisional", "confirmed"), ("provisional", "overturned"), ("provisional", "overdue"),
    ("overdue", "confirmed"), ("overdue", "overturned"), ("overturned", "reverted"),
    *((s, "void") for s in ("open", "deciding", "escalated", "awaiting_local_confirm",
                            "answered", "provisional", "overdue")),
}

SESSION_ALL = {
    ("starting", "active"), ("active", "busy_tool"), ("busy_tool", "active"),
    ("active", "handoff_requested"), ("busy_tool", "handoff_requested"),
    ("handoff_requested", "handed_off"), ("handoff_requested", "busy_tool"), ("lost", "active"),
    *((s, "lost") for s in ("starting", "active", "busy_tool", "handoff_requested", "handed_off")),
    *((s, "closed") for s in ("starting", "active", "busy_tool", "handoff_requested", "handed_off", "lost")),
}


def plain_edges(machine):
    return {(a, b) for a, b in itertools.product(machine.states, repeat=2) if can_transition(machine, a, b)}


class StateTest(unittest.TestCase):
    def test_state_names(self):
        self.assertEqual(set(BATCH_MAIN), {"planned", "ready", "running", "review_ready", "in_review",
                                           "changes_requested", "approved", "awaiting_audit", "delivered",
                                           "merged", "cataloged", "cancelled"})
        self.assertEqual(set(BATCH_SIDE), {"updating", "blocked", "stuck", "paused", "failed"})
        self.assertEqual(set(BLOCK_REASONS), {"pending", "quota", "dependency", "violation"})

    def test_exact_plain_transition_sets(self):
        self.assertEqual(plain_edges(BATCH), BATCH_PLAIN)
        self.assertEqual(plain_edges(PENDING), PENDING_ALL)
        self.assertEqual(plain_edges(SESSION), SESSION_ALL)

    def test_all_legal_transitions_return_target(self):
        for machine, edges in ((BATCH, BATCH_PLAIN), (PENDING, PENDING_ALL), (SESSION, SESSION_ALL)):
            for a, b in edges:
                self.assertEqual(transition(machine, a, b), b)

    def test_terminal_states_are_final(self):
        ctxs = [{}, {"reason": "pending", "prior": "ready"}, {"prior": "running", "session_alive": True}]
        for machine, terminal in ((BATCH, {"cataloged", "cancelled"}),
                                  (PENDING, {"applied", "confirmed", "reverted", "void"}),
                                  (SESSION, {"closed"})):
            self.assertEqual(machine.terminal, terminal)
            for t, to, ctx in itertools.product(terminal, machine.states, ctxs):
                self.assertFalse(can_transition(machine, t, to, **ctx), (t, to, ctx))

    def test_blocked_enter_needs_reason(self):
        for s in ACTIVE:
            self.assertFalse(can_transition(BATCH, s, "blocked"))
            self.assertFalse(can_transition(BATCH, s, "blocked", reason="lunch"))
            for r in BLOCK_REASONS:
                self.assertTrue(can_transition(BATCH, s, "blocked", reason=r))
        self.assertFalse(can_transition(BATCH, "planned", "blocked", reason="pending"))
        self.assertFalse(can_transition(BATCH, "merged", "blocked", reason="pending"))

    def test_blocked_returns_to_prior(self):
        for r in ("pending", "quota", "dependency"):
            for prior in ACTIVE:
                self.assertTrue(can_transition(BATCH, "blocked", prior, reason=r, prior=prior))
                other = "running" if prior != "running" else "ready"
                self.assertFalse(can_transition(BATCH, "blocked", other, reason=r, prior=prior))
            self.assertFalse(can_transition(BATCH, "blocked", "running", reason=r))  # prior missing
            self.assertFalse(can_transition(BATCH, "blocked", "planned", reason=r, prior="planned"))

    def test_blocked_violation_goes_to_running(self):
        self.assertTrue(can_transition(BATCH, "blocked", "running", reason="violation", prior="approved"))
        self.assertFalse(can_transition(BATCH, "blocked", "approved", reason="violation", prior="approved"))

    def test_paused_exit(self):
        self.assertTrue(can_transition(BATCH, "paused", "running", prior="running", session_alive=True))
        self.assertTrue(can_transition(BATCH, "paused", "ready", prior="running", session_alive=False))
        self.assertFalse(can_transition(BATCH, "paused", "running", prior="running", session_alive=False))
        self.assertFalse(can_transition(BATCH, "paused", "ready", prior="running", session_alive=True))
        self.assertFalse(can_transition(BATCH, "paused", "running", prior="running"))  # liveness unknown
        self.assertTrue(can_transition(BATCH, "paused", "in_review", prior="in_review"))
        self.assertFalse(can_transition(BATCH, "paused", "approved", prior="in_review"))
        self.assertFalse(can_transition(BATCH, "paused", "planned", prior="planned"))

    def test_user_mode_goes_through_running(self):  # §20 I3: no ready -> review_ready shortcut
        for mode in ("user", "auto"):
            self.assertFalse(can_transition(BATCH, "ready", "review_ready", mode=mode))
        self.assertTrue(can_transition(BATCH, "ready", "running", mode="user"))

    def test_reconcile_only_verifiable_facts(self):  # §20 I2, M1-2 r2 MF-A
        self.assertEqual(BATCH.facts, {"merged"})
        for a, b in (("running", "approved"), ("changes_requested", "approved"), ("in_review", "approved"),
                     ("approved", "delivered"), ("delivered", "cataloged"), ("delivered", "delivered"),
                     ("merged", "approved"), ("running", "running"), ("approved", "changes_requested"),
                     ("approved", "cancelled"), ("approved", "blocked"), ("paused", "merged"),
                     ("running", "nowhere"), ("cataloged", "merged")):
            with self.assertRaises(IllegalTransition, msg=(a, b)):
                reconcile(BATCH, a, b)
        with self.assertRaises(IllegalTransition):  # no facts defined
            reconcile(PENDING, "open", "answered")

    def test_reconcile_reports_skipped_states(self):
        fact, skipped = reconcile(BATCH, "running", "merged")
        self.assertEqual(fact, "merged")
        self.assertEqual(skipped, ("review_ready", "in_review", "changes_requested",
                                   "approved", "awaiting_audit", "delivered"))
        self.assertEqual(reconcile(BATCH, "approved", "merged"), ("merged", ("awaiting_audit", "delivered")))
        self.assertEqual(reconcile(BATCH, "awaiting_audit", "merged"), ("merged", ("delivered",)))
        self.assertEqual(reconcile(BATCH, "delivered", "merged"), ("merged", ()))
        # on a side branch the caller passes state_prior
        self.assertIn("approved", reconcile(BATCH, "running", "merged")[1])  # was paused/updating from running
        self.assertEqual(reconcile(BATCH, "approved", "merged")[1][0], "awaiting_audit")  # was updating from approved

    def test_side_branches_do_not_nest(self):
        for a, b in itertools.product(BATCH_SIDE, repeat=2):
            if a != b:
                self.assertFalse(can_transition(BATCH, a, b, reason="pending", prior="running",
                                                session_alive=True), (a, b))

    def test_illegal_examples(self):
        for machine, a, b in ((BATCH, "planned", "running"), (BATCH, "running", "approved"),
                              (BATCH, "review_ready", "approved"), (BATCH, "merged", "cancelled"),
                              (BATCH, "changes_requested", "approved"), (BATCH, "stuck", "ready"),
                              (BATCH, "running", "updating"), (BATCH, "ready", "ready"),
                              (BATCH, "ready", "nowhere"), (PENDING, "open", "provisional"),
                              (PENDING, "overturned", "void"), (PENDING, "answered", "open"),
                              (SESSION, "starting", "busy_tool"), (SESSION, "lost", "busy_tool"),
                              (SESSION, "handed_off", "active")):
            self.assertFalse(can_transition(machine, a, b), (a, b))

    def test_illegal_transition_message(self):
        with self.assertRaises(IllegalTransition) as cm:
            transition(BATCH, "planned", "merged")
        self.assertIn("planned", str(cm.exception))
        self.assertIn("merged", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
