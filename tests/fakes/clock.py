"""FakeClock: управляемое время; передаётся везде вместо time.time (06 п. 6.12)."""


class FakeClock(object):
    def __init__(self, t0=1790000000.0):
        self.t = float(t0)

    def time(self):
        return self.t

    def advance(self, s):
        self.t += float(s)
        return self.t
