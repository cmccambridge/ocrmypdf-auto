import signal
from threading import Event

class DockerSignalMonitor(object):

    SIGNUMS_TO_NAMES = { getattr(signal, signame) : signame for signame in ['SIGINT', 'SIGTERM'] }

    def __init__(self):
        self.exit_event = Event()
        self.signum = None
        signal.signal(signal.SIGINT, self.handler)
        signal.signal(signal.SIGTERM, self.handler)

    def handler(self, signum, frame):
        self.signum = signum
        self.exit_event.set()

    def wait_for_exit(self):
        self.exit_event.wait()
        return self.signum
