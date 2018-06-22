import fnmatch
import logging
import os
import sys
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from functools import partial
from plumbum import local, BG
from watchdog.observers import Observer
from watchdog.events import PatternMatchingEventHandler
from docker_support import DockerSignalMonitor

logger = logging.getLogger('ocrmypdf-auto')

class OcrmypdfConfigParsingError(Exception):
    pass

class OcrmypdfConfig(object):

    def __init__(self, input_path, output_path, config_file=None):
        self.logger = logger.getChild('config')
        self.input_path = local.path(input_path)
        self.output_path = local.path(output_path)
        temp_dir = os.getenv('OCR_TEMP_DIR')
        self.temp_dir = local.path(temp_dir) if temp_dir else None
        self.options = {}
        self.set_default_options()
        if config_file:
            self.parse_config_file(config_file)

    def set_default_options(self):
        langs = os.getenv('OCR_LANGUAGES')
        if langs is not None:
            self.options['--language'] = langs

    def parse_config_file(self, config_file):
        try:
            logging.debug('Parsing config %s', config_file)
            with local.path(config_file).open('r') as file:
                for line in file:
                    line = line.strip()
                    if len(line) == 0 or line[0] == '#':
                        continue
                    parts = line.split()
                    if len(parts) > 2:
                        raise OcrmypdfConfigParsingError('More than one option and one value in line: {}'.format(line))
                    opt = parts[0]
                    val = parts[1] if len(parts) > 1 else None
                    if opt[0] != '-':
                        raise OcrmypdfConfigParsingError('Line must start with an option (-f, --foo): {}'.format(line))
                    self.options[opt] = val
        except IOError as io:
            str = 'IOError parsing {}: {}'.format(config_file, io)
            logger.debug(str)
            raise OcrmypdfConfigParsingError(str)

    def get_ocrmypdf_arguments(self):
        args = []
        for arg, val in self.options.items():
            args.append(arg)
            if val is not None:
                args.append(val)
        args.append(self.input_path)
        args.append(self.output_path)
        return args

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

    _OCRMYPDF = local['ocrmypdf']

    COALESCING_DELAY = timedelta(seconds=try_float(os.getenv('OCR_PROCESSING_DELAY'), 3.0))

    def __init__(self, input_path, output_path, submit_func, done_callback, config_file=None, delete_input_on_success=False):
        self.logger = logger.getChild('task')
        self.input_path = input_path
        self.output_path = output_path
        self.submit_func = submit_func
        self.done_callback = done_callback
        self.config_file = config_file
        self.delete_input_on_success = delete_input_on_success
        self.last_touch = None
        self.future = None
        self.state = OcrTask.NEW
        self.touch()

    def __str__(self):
        return self.__repr__()

    def __repr__(self):
        return "<OcrTask [{}] {}->{} last_touch={} future={}>".format(
                self.state,
                self.input_path,
                self.output_path,
                self.last_touch,
                self.future)

    def enqueue(self):
        assert self.state in [OcrTask.NEW, OcrTask.ACTIVE]
        self.state = OcrTask.QUEUED
        self.logger.debug('OcrTask Enqueued: [%s] %s', self.state, self.input_path)
        self.future = self.submit_func(self._safe_process)

    def touch(self):
        self.logger.debug('OcrTask Touched: [%s] %s', self.state, self.input_path)
        self.last_touch = datetime.now()
        if self.state == OcrTask.NEW:
            self.enqueue()

    def cancel(self):
        self.logger.debug('OcrTask Canceled: [%s] %s', self.state, self.input_path)
        self.last_touch = None
        if self.state == OcrTask.QUEUED:
            self.future.cancel()
            self.done()

    def done(self):
        self.logger.debug('OcrTask Done: [%s] %s', self.state, self.input_path)
        self.state = OcrTask.DONE
        if self.done_callback:
            self.done_callback(self)

    def _safe_process(self):
        try:
            self.process()
        except BaseException as e:
            self.logger.error('Error in OcrTask.process: %s', traceback.format_exc())
            self.done()

    def process(self, skip_delay=False):
        """ocrmypdf processing task"""
        self.logger.warn('Processing: %s -> %s', self.input_path, self.output_path)
        start_ts = datetime.now()

        # Coalescing sleep as long as file is still being modified
        while not skip_delay:
            # Check for cancellation
            if self.last_touch is None:
                self.logger.info('Processing canceled: %s', self.input_path)
                self.done()
                return

            due = self.last_touch + OcrTask.COALESCING_DELAY
            wait_span = due - datetime.now()
            if wait_span <= timedelta(0):
                break;
            self.logger.debug('Sleeping for %f: [%s] %s', wait_span.total_seconds(), self.state, self.input_path)
            self.state = OcrTask.SLEEPING
            time.sleep(wait_span.total_seconds())

        # Actually run ocrmypdf
        self.state = OcrTask.ACTIVE
        self.last_touch = None
        input_mtime_before = datetime.fromtimestamp(os.path.getmtime(self.input_path))
        self.logger.info('Running ocrmypdf: %s', self.input_path)

        # Build a command line from the provided configuration
        config = OcrmypdfConfig(self.input_path, self.output_path, self.config_file)
        if not self.output_path.parent.exists():
            self.logger.debug('Mkdir: %s', self.output_path.parent)
            self.output_path.parent.mkdir()
        ocrmypdf = OcrTask._OCRMYPDF.__getitem__(config.get_ocrmypdf_arguments()).with_env(TMPDIR=config.temp_dir)

        # Execute ocrmypdf, accepting ANY return code so that we can process the return code AND outputs
        (rc, stdout, stderr) = ocrmypdf.run(retcode=None)
        if rc == 0:
            self.logger.debug('ocrmypdf succeeded with stdout [%s] and stderr [%s]', stdout, stderr)
        else:
            self.logger.info('ocrmypdf failed with rc %d stdout [%s] and stderr [%s]', rc, stdout, stderr)

        runtime = datetime.now() - start_ts
        self.logger.warn('Processing complete in %f seconds with status %d: %s', round(runtime.total_seconds(), 2), rc, self.input_path)

        # Check for modification during sleep
        if self.last_touch is not None:
            self.logger.info('Modified during ocrmypdf execution: [%s] %s', self.state, self.input_path)
            self.enqueue()
            return

        # Delete input file on success, if so configured
        if rc == 0 and self.delete_input_on_success:
            output_mtime = datetime.fromtimestamp(os.path.getmtime(self.output_path))
            input_mtime_after = datetime.fromtimestamp(os.path.getmtime(self.input_path))
            # Sanity tests to avoid deleting on some kind of failed job or when the input
            # has changed during processing:
            # 1. Input file's modification time MUST be identical to that captured at job start
            # 2. Output file's modification time MUST be more recent than the input file's
            #    modification time captured at job start
            if output_mtime < input_mtime_before:
                self.logger.warn('Not deleting input file. Output file %s was modified earlier [%s] than input file %s was modified [%s]',
                                 self.output_path, output_mtime,
                                 self.input_path, input_mtime_before)
            elif input_mtime_after != input_mtime_before:
                self.logger.warn('Not deleting input file. Input file %s modification time after OCR task [%s] is not equal to before task [%s]',
                                 self.input_path,
                                 input_mtime_after, input_mtime_before)
            else:
                self.input_path.delete()
                self.logger.debug('Deleted input file after successful OCR: %s', self.input_path)

        # We're done
        self.done()


class AutoOcrWatchdogHandler(PatternMatchingEventHandler):
    """
    Matches files to process through ocrmypdf and kicks off processing
    """

    MATCH_PATTERNS = ['*.pdf']

    def __init__(self, file_touched_callback, file_deleted_callback):
        super(AutoOcrWatchdogHandler, self).__init__(patterns = AutoOcrWatchdogHandler.MATCH_PATTERNS, ignore_directories=True)
        self.logger = logger.getChild('watchdog_handler')
        self.file_touched_callback = file_touched_callback
        self.file_deleted_callback = file_deleted_callback

    def touch_file(self, path):
        if self.file_touched_callback:
            self.file_touched_callback(path)

    def delete_file(self, path):
        if self.file_deleted_callback:
            self.file_deleted_callback(path)

    def on_moved(self, event):
        self.logger.debug("File moved: %s -> %s", event.src_path, event.dest_path)
        self.delete_file(local.path(event.src_path))
        self.touch_file(local.path(event.dest_path))

    def on_created(self, event):
        self.logger.debug("File created: %s", event.src_path)
        self.touch_file(local.path(event.src_path))

    def on_deleted(self, event):
        self.logger.debug("File deleted: %s", event.src_path)
        self.delete_file(local.path(event.src_path))

    def on_modified(self, event):
        self.logger.debug("File modified: %s", event.src_path)
        self.touch_file(local.path(event.src_path))


class AutoOcrSchedulerException(Exception):
    pass

class AutoOcrScheduler(object):

    SINGLE_FOLDER = 'single_folder'
    MIRROR_TREE = 'mirror_tree'

    OUTPUT_MODES = [SINGLE_FOLDER, MIRROR_TREE]

    def __init__(self, config_dir, input_dir, output_dir, output_mode, delete_input_on_success=False, process_existing_files=False):
        self.logger = logger.getChild('scheduler')

        self.config_dir = local.path(config_dir)
        self.input_dir = local.path(input_dir)
        self.output_dir = local.path(output_dir)
        if self.input_dir == self.output_dir:
            raise AutoOcrSchedulerException('Invalid configuration. Input and output directories must not be the same to avoid recursive OCR invocation!')
        self.output_mode = output_mode.lower()
        if self.output_mode not in AutoOcrScheduler.OUTPUT_MODES:
            raise AutoOcrSchedulerException('Invalid output mode: {}. Must be one of: {}'.format(self.output_mode, ','.join(AutoOcrScheduler.OUTPUT_MODES)))
        self.delete_input_on_success = delete_input_on_success

        self.current_tasks = {}
        self.current_outputs = set()

        # Create a Threadpool to run OCR tasks on
        self.threadpool = ThreadPoolExecutor(max_workers=3)

        # Wire up an AutoOcrWatchdogHandler
        watchdog_handler = AutoOcrWatchdogHandler(self.on_file_touched, self.on_file_deleted)

        # Schedule watchdog to observe the input directory
        self.observer = Observer()
        self.observer.schedule(watchdog_handler, self.input_dir, recursive=True)
        self.observer.start()
        self.logger.warn('Watching %s', self.input_dir)

        # Process existing files in input directory, if requested
        if process_existing_files:
            self.threadpool.submit(self.walk_existing_files)

    def shutdown(self):
        # Shut down the feed of incoming watchdog events
        if self.observer:
            self.logger.debug('Shutting down filesystem watchdog...')
            self.observer.unschedule_all()
            self.observer.stop()

        # Cancel all outstanding cancelable tasks
        self.logger.debug('Canceling all %d in-flight tasks...', len(self.current_tasks))
        tasks = [task for _, task in self.current_tasks.items()]
        for task in tasks:
            task.cancel()

        # Wait for the threadpool to clean up
        if self.threadpool:
            self.logger.debug('Shutting down threadpool...')
            self.threadpool.shutdown()
            self.threadpool = None

        # Wait for the watchdog to clean up
        if self.observer:
            self.logger.debug('Cleaning up filesystem watchdog...')
            self.observer.join()
            self.observer = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.shutdown()
        return False

    def _map_output_path(self, input_path):
        if self.output_mode == AutoOcrScheduler.MIRROR_TREE:
            return self.output_dir / (input_path - self.input_dir)
        else:
            assert self.output_mode == AutoOcrScheduler.SINGLE_FOLDER
            output_path = self.output_dir / (input_path.name)
            unique = 1
            if output_path.exists() or output_path in self.current_outputs:
                suffix = '.{}.{}{}'.format(datetime.now().strftime('%Y%m%d'), unique, output_path.suffix)
                output_path = output_path.with_suffix(suffix)

            while output_path.exists() or output_path in self.current_outputs:
                unique = unique + 1
                output_path = output_path.with_suffix('.{}{}'.format(unique, output_path.suffix), depth=2)
            return output_path

    def _get_config_path(self, input_path):
        assert (input_path - self.input_dir)[0] != '..'
        config_path = input_path.parent / 'ocr.config'
        while True:
            if config_path.exists():
                return config_path
            if config_path.parent == self.input_dir:
                break
            config_path = config_path.parent.parent / 'ocr.config'

        config_path = self.config_dir / 'ocr.config'
        if config_path.exists():
            return config_path
        return None

    def queue_path(self, path):
        output_path = self._map_output_path(path)
        config_file = self._get_config_path(path)
        task = OcrTask(path,
                       output_path,
                       self.threadpool.submit,
                       self.on_task_done,
                       config_file=config_file,
                       delete_input_on_success=self.delete_input_on_success)
        self.current_tasks[path] = task
        self.current_outputs.add(output_path)

    def walk_existing_files(self):
        self.logger.debug('Enumerating existing input files...')
        def keep_file(file):
            return any([fnmatch.fnmatch(file, pattern) for pattern in AutoOcrWatchdogHandler.MATCH_PATTERNS])
        for file in self.input_dir.walk(filter=keep_file):
            self.on_file_touched(file)

    def on_file_touched(self, path):
        if path in self.current_tasks:
            self.current_tasks[path].touch()
        else:
            self.queue_path(path)

    def on_file_deleted(self, path):
        if path in self.current_tasks:
            self.current_tasks[path].cancel()

    def on_task_done(self, task):
        self.current_outputs.remove(task.output_path)
        del self.current_tasks[task.input_path]


if __name__ == "__main__":
    logging_level = os.getenv('OCR_VERBOSITY')
    if logging_level is None:
        logging_level = logging.WARNING
    elif getattr(logging, logging_level.upper(), None) is not None:
        logging_level = getattr(logging, logging_level.upper())
    else:
        logging_level = int(try_float(logging_level, logging.WARNING))

    if logging_level < logging.WARNING:
        logging.basicConfig(level=logging_level,
                            format='%(asctime)s [%(threadName)s] - %(message)s',
                            datefmt='%Y-%m-%d %H:%M:%S')
    else:
        logging.basicConfig(level=logging_level,
                            format='%(asctime)s - %(message)s',
                            datefmt='%Y-%m-%d %H:%M:%S')

    docker_monitor = DockerSignalMonitor()

    config_dir = local.path(os.getenv('OCR_CONFIG_DIR', '/config'))
    input_dir = local.path(os.getenv('OCR_INPUT_DIR', '/input'))
    output_dir = local.path(os.getenv('OCR_OUTPUT_DIR', '/output'))
    output_mode = os.getenv('OCR_OUTPUT_MODE', AutoOcrScheduler.MIRROR_TREE)
    delete_input_on_success = (os.getenv('OCR_DELETE_INPUT_ON_SUCCESS', 'nope') == 'DELETE MY FILES')
    process_existing_files = (os.getenv('OCR_PROCESS_EXISTING', '0').lower() in ['1', 'y', 'yes', 't', 'true', 'on'])

    # Run an AutoOcrScheduler until terminated
    with AutoOcrScheduler(config_dir,
                          input_dir,
                          output_dir,
                          output_mode,
                          delete_input_on_success=delete_input_on_success,
                          process_existing_files=process_existing_files) as scheduler:
        # Wait in the main thread to be killed
        signum = docker_monitor.wait_for_exit()
        logger.warn('Signal %d (%s) Received. Shutting down...', signum, DockerSignalMonitor.SIGNUMS_TO_NAMES[signum])

