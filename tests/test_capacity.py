"""REQ-5 (seats: successors count and go first) and REQ-6 (the machine's load before opening a seat)."""
import subprocess
import unittest
from unittest import mock

from foremind import config, lock
from foremind.supervisor import machine

from test_supervisor import Base, sv

READ = machine.read  # Base stands in for it
MAX3 = '[quota]\nprobe_command = ["/usr/bin/true"]\n[supervisor]\nmax_seats = 3\n'

VM_STAT = """Mach Virtual Memory Statistics: (page size of 16384 bytes)
Pages free:                              1000.
Pages active:                            934205.
Pages inactive:                          2000.
Pages speculative:                         96.
Pages throttled:                              0.
"""


class SeatsTest(Base):
    def test_a_returned_batch_waits_for_a_seat_then_goes_before_new_batches(self):  # finding 35
        self.plan("p", {}, {"state": "delivered"}, {}, {})
        self.hold("p.1", "fm-a")
        self.set_state("p.4", "paused")
        self.tick()
        self.assertEqual(self.jobs.cmds(), [["seat", "p.3"]], "p.2's seat was released: 2 of 2 in use")
        self.set_state("p.2", "running")  # the controller returns it
        self.set_state("p.4", "planned")
        self.tick(30)
        self.tick(60)
        self.assertEqual(len(self.jobs.started), 1, "no successor over max_seats")
        self.assertEqual([(e["batch"], e["used"], e["cap"]) for e in self.events("successor_deferred")],
                         [("p.2", 2, 2)], "once per wait")
        self.set_state("p.1", "merged")
        lock.release(self.root, "p.1", "fm-a")
        self.tick(90)
        self.assertEqual(self.jobs.cmds(), [["seat", "p.3"], ["_successor", "p.2"]], "the successor before p.4")
        self.assertEqual(self.header("p.4")["state"], "ready")

    def test_a_context_handoff_successor_takes_its_predecessors_seat(self):
        self.user_config('[quota]\nprobe_command = ["/usr/bin/true"]\n[supervisor]\nmax_seats = 1\n')
        self.plan("p")
        self.carrier.idle = False
        self.hold("p.1", "fm-s", handoff_requested=True)
        self.log.append("handoff_written", batch="p.1", session="fm-s")
        self.machine.update(load1=100.0)
        self.tick(60)
        self.assertEqual(self.jobs.cmds(), [["_successor", "p.1"]])
        self.assertEqual((self.events("successor_deferred"), self.events("seat_deferred")), ([], []))

    def test_a_successor_after_a_stuck_successor_takes_the_seat_its_predecessor_still_holds(self):  # r1, m2b.8 r3
        self.plan("p", {}, {})
        self.hold("p.1", "fm-a")
        self.hold("p.2", "fm-p", state="stuck")
        self.log.append("seat_opened", batch="p.2", session="fm-s", successor=True, predecessor="fm-p", worktrees={})
        self.log.append("batch_state", batch="p.2", frm="running", to="stuck", session="fm-s")
        self.log.append("sv_close", batch="p.2", session="fm-s", carrier="fake", confirmed=True, how="absent")
        t = sv.Tick(self.root, config.load(self.root), self.now)
        t.load()
        self.assertEqual(t.free_seats(), (2, 2, 0), "p.2 counts once: in used, not waiting as well")
        self.machine.update(load1=100.0)
        self.tick()
        self.assertEqual(self.jobs.cmds(), [["_successor", "p.2"]])
        self.assertEqual(self.events("successor_deferred"), [])

    def test_a_successor_for_an_exited_holder_whose_lock_did_not_break_waits_for_the_machine(self):  # m2e REQ-9
        self.plan("p", {}, {})
        self.hold("p.1", "fm-a")
        self.hold("p.2", "fm-p", state="stuck")
        self.log.append("sv_close", batch="p.2", session="fm-p", carrier="fake", confirmed=True, how="absent")
        self.machine.update(load1=100.0)
        with mock.patch.object(lock, "break_lock", side_effect=lock.LockError("not broken")):
            self.tick()
            self.assertEqual((self.jobs.started, len(self.events("seat_deferred"))), ([], 1))
            self.machine.update(load1=0.0)
            self.tick(30)
        self.assertEqual(self.jobs.cmds(), [["_successor", "p.2"]])
        self.assertEqual(self.events("successor_deferred"), [], "2 of 2 in use, p.2's seat among them")


class MachineTest(Base):
    def test_a_busy_machine_defers_seats_once_until_one_opens(self):
        self.plan("p", {}, {})
        self.machine.update(load1=6.4)  # 0.8 x 8
        self.tick()
        self.tick(30)
        self.assertEqual(self.jobs.started, [])
        self.assertEqual([{k: e[k] for k in ("reason", "load1", "cpus", "avail_mb", "max_load_per_cpu",
                                             "min_free_mem_mb")} for e in self.events("seat_deferred")],
                         [{"reason": "load", "load1": 6.4, "cpus": 8, "avail_mb": 65536, "max_load_per_cpu": 0.8,
                           "min_free_mem_mb": 2048}])
        self.machine.update(load1=1.0, avail_mb=2047.0)
        self.tick(60)
        self.assertEqual(len(self.events("seat_deferred")), 1, "memory now, still the same deferment")
        self.machine.update(avail_mb=4096.0)
        self.tick(90)
        self.assertEqual(self.jobs.cmds(), [["seat", "p.1"], ["seat", "p.2"]])
        self.log.append("seat_opened", batch="p.1", session="fm-s", successor=False, worktrees={})
        self.user_config(MAX3)
        self.plan("q")
        self.machine.update(avail_mb=100.0)
        self.tick(120)
        self.assertEqual([e["reason"] for e in self.events("seat_deferred")], ["load", "memory"])

    def test_the_thresholds_are_configurable(self):
        self.user_config('[quota]\nprobe_command = ["/usr/bin/true"]\n[supervisor]\nmax_load_per_cpu = 2\n')
        self.plan("p")
        self.machine.update(load1=15.0)
        self.tick()
        self.assertEqual(self.jobs.cmds(), [["seat", "p.1"]])

    def test_a_successor_for_a_batch_nobody_holds_waits_for_the_machine(self):
        self.plan("p", {"state": "running"})
        self.machine.update(load1=100.0)
        self.tick()
        self.assertEqual((self.jobs.started, len(self.events("seat_deferred"))), ([], 1))
        self.machine.update(load1=0.0)
        self.tick(30)
        self.assertEqual(self.jobs.cmds(), [["_successor", "p.1"]])

    def test_an_unreadable_figure_does_not_limit_and_is_recorded_once(self):
        self.plan("p", {}, {})
        self.machine.update(avail_mb=None, errors={"memory": "OSError: no vm_stat"})
        self.tick()
        self.user_config(MAX3)
        self.plan("q")
        self.tick(30)
        self.assertEqual(self.jobs.cmds(), [["seat", "p.1"], ["seat", "p.2"], ["seat", "q.1"]])
        self.assertEqual([(e["what"], e["error"]) for e in self.events("machine_unreadable")],
                         [("memory", "OSError: no vm_stat")])

    def test_read(self):
        self.assertAlmostEqual(machine.parse_vm_stat(VM_STAT), 3096 * 16384 / 2 ** 20)
        self.assertEqual(machine.parse_meminfo("MemTotal: 8 kB\nMemAvailable:    2097152 kB\n"), 2048)

        def gone():
            raise subprocess.CalledProcessError(1, "vm_stat")
        m = READ(loadavg=lambda: (3.0, 2.0, 1.0), cpu_count=lambda: None, avail=gone)
        self.assertEqual((m["load1"], m["cpus"], m["avail_mb"]), (3.0, None, None))
        self.assertEqual(sorted(m["errors"]), ["cpus", "memory"])
        live = READ()  # this host: a figure or its error, never a raise
        for what, key in (("load", "load1"), ("cpus", "cpus"), ("memory", "avail_mb")):
            self.assertTrue(isinstance(live[key], (int, float)) or what in live["errors"], what)


if __name__ == "__main__":
    unittest.main()
