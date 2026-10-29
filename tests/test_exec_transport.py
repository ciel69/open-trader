import io
import json
import socket
import unittest

import _bootstrap  # noqa: F401
from tb.executor.transport import make_transport


class Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class Opener(object):
    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def __call__(self, req, timeout=None):
        self.calls.append((req.get_method(), timeout))
        r = self.script.pop(0)
        if isinstance(r, Exception):
            raise r
        return Resp(json.dumps(r).encode())


class TransportTests(unittest.TestCase):
    def test_get_retries_once_on_timeout(self):
        o = Opener([socket.timeout("t"), {"retCode": 0}])
        self.assertEqual(make_transport(opener=o)("GET", "http://x", {}, None), {"retCode": 0})
        self.assertEqual(len(o.calls), 2)
        self.assertEqual(o.calls[0][1], 4.0)

    def test_get_gives_up_after_second_failure(self):
        o = Opener([socket.timeout("t"), socket.timeout("t")])
        with self.assertRaises(socket.timeout):
            make_transport(opener=o)("GET", "http://x", {}, None)
        self.assertEqual(len(o.calls), 2)

    def test_post_never_retried(self):
        o = Opener([socket.timeout("t"), {"retCode": 0}])
        with self.assertRaises(socket.timeout):
            make_transport(opener=o)("POST", "http://x", {}, "{}")
        self.assertEqual(len(o.calls), 1)
        self.assertEqual(o.calls[0][1], 9.0)


if __name__ == "__main__":
    unittest.main()
