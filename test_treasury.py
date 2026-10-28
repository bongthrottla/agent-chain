import tempfile, unittest
from pathlib import Path
import treasury

class T(unittest.TestCase):
    def make(self, **kw):
        p = treasury.Policy(**kw)
        return treasury.AgentTreasury(Path(tempfile.mkdtemp()), policy=p)

    def test_monotonic(self):
        p = treasury.Policy(per_tx_max=1.0, daily_max=5.0, hard_ceiling=20.0, require_human_above=0.5)
        p.validate()

    def test_refused_negative(self):
        t = self.make(per_tx_max=1.0, daily_max=5.0, hard_ceiling=20.0, require_human_above=0.5)
        self.assertEqual(t.evaluate("addr", -1)[0], "refused")

    def test_refused_hard_ceiling(self):
        t = self.make(per_tx_max=20.0, daily_max=20.0, hard_ceiling=20.0, require_human_above=10.0)
        t._sh = lambda *a, **k: type("R",(),{"stdout":"10.0 SOL","returncode":0})()
        self.assertEqual(t.evaluate("addr", 25.0)[0], "refused")

    def test_queued_above_threshold(self):
        t = self.make(per_tx_max=1.0, daily_max=5.0, hard_ceiling=20.0, require_human_above=0.5)
        t._sh = lambda *a, **k: type("R",(),{"stdout":"10.0 SOL","returncode":0})()
        self.assertEqual(t.evaluate("addr", 0.75)[0], "queued_for_human")

    def test_auto_approved_small(self):
        t = self.make(per_tx_max=1.0, daily_max=5.0, hard_ceiling=20.0, require_human_above=0.5)
        t._sh = lambda *a, **k: type("R",(),{"stdout":"10.0 SOL","returncode":0})()
        self.assertEqual(t.evaluate("addr", 0.25)[0], "auto_approved")

if __name__ == "__main__":
    unittest.main()
