import fcntl
import multiprocessing
from pathlib import Path
import tempfile
import unittest
from repair_index_links import replace_link


def probe(path, pipe):
    with open(path, 'a+') as f:
        try:
            fcntl.lockf(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            pipe.send(False)
        except BlockingIOError:
            pipe.send(True)


class LinkTests(unittest.TestCase):
    def test_preserves_live_posix_lock(self):
        with tempfile.TemporaryDirectory() as d:
            a=Path(d)/'a'; b=Path(d)/'b'; a.touch(); b.symlink_to(a)
            with a.open('a+') as f:
                fcntl.lockf(f, fcntl.LOCK_EX)
                replace_link(a,b)
                self.assertFalse(b.is_symlink())
                parent,child=multiprocessing.Pipe()
                p=multiprocessing.Process(target=probe,args=(str(b),child));p.start()
                self.assertTrue(parent.poll(5)); self.assertTrue(parent.recv())
                p.join(5); self.assertEqual(p.exitcode,0)
                replace_link(a,b)
    def test_wrong_target_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            a=Path(d)/'a'; b=Path(d)/'b'; a.touch(); b.touch()
            with self.assertRaises(ValueError):replace_link(a,b)
