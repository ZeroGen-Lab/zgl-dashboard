import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

from config import ClaimConflict, change_claim, claim_status
from db_usage import initialize


class CodingClaimTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1])
        self.addCleanup(self.directory.cleanup)
        self.path = str(Path(self.directory.name).resolve() / 'usage.db')
        self.primary = str(Path(self.directory.name).resolve() / 'primary.db')
        self.ids = ('z1', 'z2', 'z3')
        initialize(self.path, self.primary)

    def change(self, key, action='claim', user='alice'):
        return change_claim(self.path, self.primary, key, user, action,
                            self.ids if key in self.ids else ())

    def test_exclusive_claim_release_and_other_users(self):
        self.change('z1')
        self.change('z1')
        with self.assertRaises(ClaimConflict):
            self.change('z2')
        self.change('deepseek')
        self.change('z2', user='bob')
        self.change('z1', 'release')
        self.change('z3')
        self.assertEqual(claim_status(self.path, 'alice')[1], {'z3', 'deepseek'})

    def test_concurrent_claims_only_one_succeeds(self):
        barrier = Barrier(2)

        def claim(key):
            barrier.wait()
            try:
                self.change(key)
                return True
            except ClaimConflict:
                return False

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(claim, ('z1', 'z2')))
        self.assertEqual(sum(results), 1)
        self.assertEqual(len(claim_status(self.path, 'alice')[1]), 1)


if __name__ == '__main__':
    unittest.main()
