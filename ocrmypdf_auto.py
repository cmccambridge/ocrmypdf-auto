import sys
import time
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from plumbum import local, BG
from watchdog.observers import Observer
from watchdog.events import PatternMatchingEventHandler

current_files = {}

threadpool = None

INPUT_BASE = local.path('/input')
OUTPUT_BASE = local.path('/output')
OCRMYPDF = local['ocrmypdf']

def run_ocrmypdf(path):
    out_path = OUTPUT_BASE / (path - INPUT_BASE)
    future = OCRMYPDF['--deskew', path, out_path] & BG
    future.wait()
    logging.info(future.stdout)

def process_ocr_file(ocrfile):
    ocrfile.process()

class OcrFile(object):

    NEW = 'NEW'
    QUEUED = 'QUEUED'
    SLEEPING = 'SLEEPING'
    ACTIVE = 'ACTIVE'
    DONE = 'DONE'

    COALESCING_DELAY = timedelta(seconds=5.0)

    def __init__(self, path):
        self.path = path
        self.last_touch = None
        self.future = None
        self.state = OcrFile.NEW
        self.touch()

    def __str__(self):
        return self.__repr__()

    def __repr__(self):
        return "<OcrFile [{}] {} last_touch={} future={}>".format(
                self.state,
                self.path,
                self.last_touch,
                self.future)

    def enqueue(self):
        assert self.state in [OcrFile.NEW, OcrFile.ACTIVE]
        self.state = OcrFile.QUEUED
        logging.info('OcrFile Enqueued: [%s] %s', self.state, self.path)
        self.future = threadpool.submit(process_ocr_file, self)

    def touch(self):
        logging.info('OcrFile Touched: [%s] %s', self.state, self.path)
        self.last_touch = datetime.now()
        if self.state == OcrFile.NEW:
            self.enqueue()

    def cancel(self):
        logging.info('OcrFile Canceled: [%s] %s', self.state, self.path)
        self.last_touch = None
        if self.state == OcrFile.QUEUED:
            self.future.cancel()
            self.done()

    def done(self):
        logging.info('OcrFile Done: [%s] %s', self.state, self.path)
        self.state = OcrFile.DONE
        del current_files[self.path]

    def process(self, skip_delay=False):
        """ocrmypdf processing task"""
        logging.info('Processing: [%s] %s', self.state, self.path)

        # Coalescing sleep as long as file is still being modified
        while not skip_delay:
            # Check for cancellation
            if self.last_touch is None:
                logging.info('Processing canceled: [%s] %s', self.state, self.path)
                self.done()
                return

            due = self.last_touch + OcrFile.COALESCING_DELAY
            wait_span = due - datetime.now()
            if wait_span <= timedelta(0):
                break;
            logging.info('Processing sleeping for %f: [%s] %s', wait_span.total_seconds(), self.state, self.path)
            self.state = OcrFile.SLEEPING
            time.sleep(wait_span.total_seconds())

        # Actually run ocrmypdf
        self.state = OcrFile.ACTIVE
        self.last_touch = None
        logging.info('Running ocrmypdf: [%s] %s', self.state, self.path)
        run_ocrmypdf(self.path)

        # Check for modification during sleep
        if self.last_touch is not None:
            logging.info('Modified during ocrmypdf execution: [%s] %s', self.state, self.path)
            self.enqueue()
            return

        # We're done
        self.done()


class AutoOcrEventHandler(PatternMatchingEventHandler):
    """
    Matches files to process through ocrmypdf and kicks off processing
    """

    def __init__(self):
        super(AutoOcrEventHandler, self).__init__(patterns = ["*.pdf"], ignore_directories=True)

    def touch_file(self, path):
        if path in current_files:
            current_files[path].touch()
        else:
            current_files[path] = OcrFile(path)

    def delete_file(self, path):
        if path in current_files:
            current_files[path].cancel()

    def on_moved(self, event):
        logging.info("File moved: %s -> %s", event.src_path, event.dest_path)
        self.delete_file(local.path(event.src_path))
        self.touch_file(local.path(event.dest_path))

    def on_created(self, event):
        logging.info("File created: %s", event.src_path)
        self.touch_file(local.path(event.src_path))

    def on_deleted(self, event):
        logging.info("File deleted: %s", event.src_path)
        self.delete_file(local.path(event.src_path))

    def on_modified(self, event):
        logging.info("File modified: %s", event.src_path)
        self.touch_file(local.path(event.src_path))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format='%(asctime)s - %(message)s',
                        datefmt='%Y-%m-%d %H:%M:%S')
    path = sys.argv[1] if len(sys.argv) > 1 else INPUT_BASE

    # Create a ThreadPooLExecutor for the OCR tasks
    with ThreadPoolExecutor(max_workers=3) as tp:
        threadpool = tp

        # Create our OCR file watchdog event handler
        event_handler = AutoOcrEventHandler()
        
        # Schedule watchdog to observe the input path
        observer = Observer()
        observer.schedule(event_handler, path, recursive=True)
        observer.start()
        
        logging.info("Ready.")
        # Wait in the main thread to be killed
        try:
            while True:
                time.sleep(1)
                for path in current_files:
                    print(current_files[path])
        except KeyboardInterrupt:
            pass

        ocrfiles = [ocrfile for _, ocrfile in current_files.items()]
        for ocrfile in ocrfiles:
            ocrfile.cancel()

        observer.unschedule_all()
        observer.stop()
        observer.join()

