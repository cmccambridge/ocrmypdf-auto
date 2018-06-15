import logging
import os
import signal
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from threading import Event
from plumbum import local, BG
from watchdog.observers import Observer
from watchdog.events import PatternMatchingEventHandler

current_tasks = {}

threadpool = None

INPUT_BASE = local.path(os.getenv('OCR_INPUT_DIR', '/input'))
OUTPUT_BASE = local.path(os.getenv('OCR_OUTPUT_DIR', '/output'))
OCRMYPDF = local['ocrmypdf']

def run_ocrmypdf(path):
    out_path = OUTPUT_BASE / (path - INPUT_BASE)
    future = OCRMYPDF['--deskew', path, out_path] & BG
    future.wait()
    logging.debug(future.stdout)

def process_ocrtask(task):
    task.process()

def try_float(string, default_value):
    try:
        return float(string)
    except (ValueError, TypeError):
        return default_value

class OcrTask(object):

    NEW = 'NEW'
    QUEUED = 'QUEUED'
    SLEEPING = 'SLEEPING'
    ACTIVE = 'ACTIVE'
    DONE = 'DONE'

    COALESCING_DELAY = timedelta(seconds=try_float(os.getenv('OCR_PROCESSING_DELAY'), 3.0))

    def __init__(self, path):
        self.path = path
        self.last_touch = None
        self.future = None
        self.state = OcrTask.NEW
        self.touch()

    def __str__(self):
        return self.__repr__()

    def __repr__(self):
        return "<OcrTask [{}] {} last_touch={} future={}>".format(
                self.state,
                self.path,
                self.last_touch,
                self.future)

    def enqueue(self):
        assert self.state in [OcrTask.NEW, OcrTask.ACTIVE]
        self.state = OcrTask.QUEUED
        logging.debug('OcrTask Enqueued: [%s] %s', self.state, self.path)
        self.future = threadpool.submit(process_ocrtask, self)

    def touch(self):
        logging.debug('OcrTask Touched: [%s] %s', self.state, self.path)
        self.last_touch = datetime.now()
        if self.state == OcrTask.NEW:
            self.enqueue()

    def cancel(self):
        logging.debug('OcrTask Canceled: [%s] %s', self.state, self.path)
        self.last_touch = None
        if self.state == OcrTask.QUEUED:
            self.future.cancel()
            self.done()

    def done(self):
        logging.debug('OcrTask Done: [%s] %s', self.state, self.path)
        self.state = OcrTask.DONE
        del current_tasks[self.path]

    def process(self, skip_delay=False):
        """ocrmypdf processing task"""
        logging.info('Processing: %s', self.path)

        # Coalescing sleep as long as file is still being modified
        while not skip_delay:
            # Check for cancellation
            if self.last_touch is None:
                logging.info('Processing canceled: %s', self.path)
                self.done()
                return

            due = self.last_touch + OcrTask.COALESCING_DELAY
            wait_span = due - datetime.now()
            if wait_span <= timedelta(0):
                break;
            logging.debug('Sleeping for %f: [%s] %s', wait_span.total_seconds(), self.state, self.path)
            self.state = OcrTask.SLEEPING
            time.sleep(wait_span.total_seconds())

        # Actually run ocrmypdf
        self.state = OcrTask.ACTIVE
        self.last_touch = None
        logging.info('Running ocrmypdf: %s', self.path)
        run_ocrmypdf(self.path)
        logging.info('Finished ocrmypdf: %s', self.path)

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
        if path in current_tasks:
            current_tasks[path].touch()
        else:
            current_tasks[path] = OcrTask(path)

    def delete_file(self, path):
        if path in current_tasks:
            current_tasks[path].cancel()

    def on_moved(self, event):
        logging.debug("File moved: %s -> %s", event.src_path, event.dest_path)
        self.delete_file(local.path(event.src_path))
        self.touch_file(local.path(event.dest_path))

    def on_created(self, event):
        logging.debug("File created: %s", event.src_path)
        self.touch_file(local.path(event.src_path))

    def on_deleted(self, event):
        logging.debug("File deleted: %s", event.src_path)
        self.delete_file(local.path(event.src_path))

    def on_modified(self, event):
        logging.debug("File modified: %s", event.src_path)
        self.touch_file(local.path(event.src_path))

class DockerSignalMonitor(object):

    def __init__(self):
        self.exit_event = Event()
        signal.signal(signal.SIGINT, self.handler)
        signal.signal(signal.SIGTERM, self.handler)

    def handler(self, signum, frame):
        self.exit_event.set()

    def wait_for_exit(self):
        self.exit_event.wait()

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format='%(asctime)s - %(message)s',
                        datefmt='%Y-%m-%d %H:%M:%S')

    docker_monitor = DockerSignalMonitor()

    # Create a ThreadPooLExecutor for the OCR tasks
    with ThreadPoolExecutor(max_workers=3) as tp:
        threadpool = tp

        # Create our OCR file watchdog event handler
        event_handler = AutoOcrEventHandler()
        
        # Schedule watchdog to observe the input path
        observer = Observer()
        observer.schedule(event_handler, INPUT_BASE, recursive=True)
        observer.start()
        
        logging.info('Watching %s...', INPUT_BASE)

        # Wait in the main thread to be killed
        docker_monitor.wait_for_exit()

        logging.info('Shutting down...')

        tasks = [task for _, task in current_tasks.items()]
        for task in tasks:
            task.cancel()

        observer.unschedule_all()
        observer.stop()
        observer.join()

