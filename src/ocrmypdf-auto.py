import concurrent.futures
import fnmatch
import logging
import os
import requests
import sys
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from functools import partial
from plumbum import local, BG
from watchdog.observers import Observer
from watchdog.observers.polling import PollingObserver
from watchdog.events import PatternMatchingEventHandler
from docker_support import DockerSignalMonitor

logger = logging.getLogger('ocrmypdf-auto')
test_logger = logging.getLogger('test-ocrmypdf-auto')

def test_log(msg, *args):
    test_logger.critical('TEST\0' + msg, *args)

class AutoOcrError(Exception):
    pass

class OcrmypdfConfigParsingError(AutoOcrError):
    pass

class OcrmypdfConfig(object):

    _OCRMYPDF = local['ocrmypdf']

    def __init__(self, input_path, output_path, config_file=None):
        self.logger = logger.getChild('config')
        self.input_path = local.path(input_path)
        self.output_path = local.path(output_path)
        temp_dir = os.getenv('OCR_TEMP_DIR', '/ocrtemp')
        self.temp_dir = local.path(temp_dir) if temp_dir else None
        self.options = {}
        self.set_default_options()
        if config_file:
            self.parse_config_file(config_file)

    def set_default_options(self):
        langs = os.getenv('OCR_LANGUAGES')
        if langs:
            langs = '+'.join(langs.split())
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
        args.append('--')
        args.append(self.input_path)
        args.append(self.output_path)
        return args

    def get_ocrmypdf_command(self):
        return OcrmypdfConfig._OCRMYPDF.__getitem__(self.get_ocrmypdf_arguments()).with_env(temp_dir=self.temp_dir)


def try_float(string, default_value):
    try:
        return float(string)
    except (ValueError, TypeError):
        return default_value

class OcrTaskError(AutoOcrError):
    pass

class OcrTask(object):

    NEW = 'NEW'
    QUEUED = 'QUEUED'
    SLEEPING = 'SLEEPING'
    ACTIVE = 'ACTIVE'
    DONE = 'DONE'

    ON_SUCCESS_DO_NOTHING = 'nothing'
    ON_SUCCESS_DELETE_INPUT = 'delete_input_files'
    ON_SUCCESS_ARCHIVE = 'archive_input_files'
    SUCCESS_ACTIONS = [ON_SUCCESS_DO_NOTHING, ON_SUCCESS_DELETE_INPUT, ON_SUCCESS_ARCHIVE]

    COALESCING_DELAY = timedelta(seconds=try_float(os.getenv('OCR_PROCESSING_DELAY'), 3.0))

    def __init__(self, input_path, output_path, submit_func, done_callback, config_file=None, success_action=ON_SUCCESS_DO_NOTHING, archive_path=None, notify_url=''):
        self.logger = logger.getChild('task')
        self.input_path = input_path
        self.output_path = output_path
        self.submit_func = submit_func
        self.done_callback = done_callback
        self.config_file = config_file
        self.success_action = success_action
        if self.success_action not in OcrTask.SUCCESS_ACTIONS:
            raise OcrTaskError('Invalid success action: {}. Must be one of {}'.format(self.success_action, ', '.join(OcrTask.SUCCESS_ACTIONS)))
        self.archive_path = archive_path
        if self.success_action == OcrTask.ON_SUCCESS_ARCHIVE and not self.archive_path:
            raise OcrTaskError('Archive path required for success action {}'.format(self.success_action))
        self.notify_url = notify_url
        self.last_touch = None
        self.future = None
        self.state = OcrTask.NEW
        self.touch()

    def __str__(self):
        return self.__repr__()

    def __repr__(self):
        return "<OcrTask [{}] {}->{} last_touch={}>".format(
                self.state,
                self.input_path,
                self.output_path,
                self.last_touch)

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
        """OCR processing task"""
        self.logger.warning('Processing: %s -> %s', self.input_path, self.output_path)
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

        # Enter the active state and capture the last modified timestamp of the input file to
        # validate when OCR is completed
        self.state = OcrTask.ACTIVE
        self.last_touch = None
        input_mtime_before = datetime.fromtimestamp(os.path.getmtime(self.input_path))
        self.logger.info('Running OCRmyPDF: %s with config file: %s', self.input_path, self.config_file)

        # Build a command line from the provided configuration
        config = OcrmypdfConfig(self.input_path, self.output_path, self.config_file)
        if not self.output_path.parent.exists():
            self.logger.debug('Mkdir: %s', self.output_path.parent)
            self.output_path.parent.mkdir()
        ocrmypdf = config.get_ocrmypdf_command()

        # Execute ocrmypdf, accepting ANY return code so that we can process the return code AND outputs
        (rc, stdout, stderr) = ocrmypdf.run(retcode=None)
        if rc == 0:
            self.logger.debug('OCRmyPDF succeeded with stdout [%s] and stderr [%s]', stdout, stderr)
        else:
            self.logger.info('OCRmyPDF failed with rc %d stdout [%s] and stderr [%s]', rc, stdout, stderr)

        runtime = datetime.now() - start_ts
        self.logger.warning('Processing complete in %f seconds with status %d: %s', round(runtime.total_seconds(), 2), rc, self.input_path)
        test_log('OCR_PROCESS_RESULT\0%s\0%s\0%d\0%f', self.input_path, self.output_path, rc, round(runtime.total_seconds(), 2))

        # Check for modification during sleep
        if self.last_touch is not None:
            self.logger.info('Modified during OCR execution: [%s] %s', self.state, self.input_path)
            self.enqueue()
            return

        # Perform success action as configured, unless file was modified since OCR
        if rc == 0 and self.success_action in [OcrTask.ON_SUCCESS_DELETE_INPUT, OcrTask.ON_SUCCESS_ARCHIVE]:
            output_mtime = datetime.fromtimestamp(os.path.getmtime(self.output_path))
            input_mtime_after = datetime.fromtimestamp(os.path.getmtime(self.input_path))

            # Sanity tests to avoid deleting or archiving on some kind of failed job or when
            # the input has changed during processing:
            # 1. Input file's modification time MUST be identical to that captured at job start
            # 2. Output file's modification time MUST be more recent than the input file's
            #    modification time captured at job start
            if output_mtime < input_mtime_before:
                self.logger.warning('Not %s input file. Output file %s was modified earlier [%s] than input file %s was modified [%s]',
                                 'deleting' if self.success_action == OcrTask.ON_SUCCESS_DELETE_INPUT else 'archiving',
                                 self.output_path, output_mtime,
                                 self.input_path, input_mtime_before)
            elif input_mtime_after != input_mtime_before:
                self.logger.warning('Not %s input file. Input file %s modification time after OCR task [%s] is not equal to before task [%s]',
                                 'deleting' if self.success_action == OcrTask.ON_SUCCESS_DELETE_INPUT else 'archiving',
                                 self.input_path,
                                 input_mtime_after, input_mtime_before)
            else:
                if self.success_action == OcrTask.ON_SUCCESS_DELETE_INPUT:
                    self.input_path.delete()
                    self.logger.debug('Deleted input file after successful OCR: %s', self.input_path)
                else:
                    if not self.archive_path.parent.exists():
                        self.logger.debug('Mkdir: %s', self.archive_path.parent)
                        self.archive_path.parent.mkdir()
                    self.input_path.move(self.archive_path)
                    self.logger.debug('Archived input file after successful OCR: %s -> %s', self.input_path, self.archive_path)

        # Notify if notification url is set and run was successful
        if rc == 0  and self.notify_url:
            # Build json
            output_data = {
                "pdf": self.output_path
            }

            # Check if there is a txt file from --sidecar
            # Apparently, it just tacks on .txt so files are called something.pdf.txt in the end
            txt_path = self.output_path + '.txt'
            if os.path.isfile(txt_path):
                output_data['txt'] = txt_path

            # Post json to notification url
            # The json parameter will encode it and set the Content-Type header
            r = requests.post(self.notify_url, json = output_data)
            self.logger.debug('Sent notification to %s and got response code %s', self.notify_url, r.status_code)

        # We're done
        self.done()


class AutoOcrWatchdogHandler(PatternMatchingEventHandler):
    """
    Matches files to process through OCR and kicks off processing
    """

    MATCH_PATTERNS = ['*.pdf', '*.jpg']

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


class AutoOcrSchedulerError(AutoOcrError):
    pass

class AutoOcrScheduler(object):

    SINGLE_FOLDER = 'single_folder'
    MIRROR_TREE = 'mirror_tree'

    OUTPUT_MODES = [SINGLE_FOLDER, MIRROR_TREE]

    def __init__(
            self,
            config_dir,
            input_dir,
            output_dir,
            output_mode,
            success_action=OcrTask.ON_SUCCESS_DO_NOTHING,
            archive_dir=None,
            notify_url='',
            process_existing_files=False,
            run_scheduler=True,
            polling_observer=False,
        ):
        self.logger = logger.getChild('scheduler')

        self.config_dir = local.path(config_dir)
        self.input_dir = local.path(input_dir)
        self.output_dir = local.path(output_dir)
        if self.input_dir == self.output_dir:
            raise AutoOcrSchedulerError('Invalid configuration. Input and output directories must not be the same to avoid recursive OCR invocation!')
        self.output_mode = output_mode.lower()
        if self.output_mode not in AutoOcrScheduler.OUTPUT_MODES:
            raise AutoOcrSchedulerError('Invalid output mode: {}. Must be one of: {}'.format(self.output_mode, ', '.join(AutoOcrScheduler.OUTPUT_MODES)))
        self.success_action = success_action.lower()
        if self.success_action not in OcrTask.SUCCESS_ACTIONS:
            raise AutoOcrSchedulerError('Invalid success action: {}. Must be one of {}'.format(self.success_action, ', '.join(OcrTask.SUCCESS_ACTIONS)))
        self.archive_dir = local.path(archive_dir) if archive_dir else None
        if self.success_action == OcrTask.ON_SUCCESS_ARCHIVE and not self.archive_dir:
            raise AutoOcrSchedulerError('Archive directory required for success action {}'.format(self.success_action))

        self.notify_url = notify_url
        self.current_tasks = {}
        self.walk_existing_task = None
        self.current_outputs = set()

        # Create a Threadpool to run OCR tasks on
        self.threadpool = ThreadPoolExecutor(max_workers=3)

        # Wire up an AutoOcrWatchdogHandler
        watchdog_handler = AutoOcrWatchdogHandler(self.on_file_touched, self.on_file_deleted)

        # Schedule watchdog to observe the input directory
        if run_scheduler:
            self.observer = PollingObserver() if polling_observer else Observer()
            self.observer.schedule(watchdog_handler, self.input_dir, recursive=True)
            self.observer.start()
            self.logger.warning('Watching %s', self.input_dir)
        else:
            self.observer = None
            self.logger.warning('Not watching %s', self.input_dir)

        # Process existing files in input directory, if requested
        if process_existing_files:
            self.walk_existing_task = self.threadpool.submit(self.walk_existing_files)

    def shutdown(self):
        # Shut down the feed of incoming watchdog events
        if self.observer:
            self.logger.debug('Shutting down filesystem watchdog...')
            self.observer.unschedule_all()
            self.observer.stop()

        # Cancel all outstanding cancelable tasks
        if self.walk_existing_task:
            self.logger.debug('Canceling walk existing files task...')
            self.walk_existing_task.cancel()
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

    def _map_archive_path(self, input_path):
        return self.archive_dir / (input_path - self.input_dir)

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
        archive_file = self._map_archive_path(path)
        task = OcrTask(path,
                       output_path,
                       self.threadpool.submit,
                       self.on_task_done,
                       config_file=config_file,
                       success_action=self.success_action,
                       archive_path=archive_file,
                       notify_url=self.notify_url)
        self.current_tasks[path] = task
        self.current_outputs.add(output_path)

    def walk_existing_files(self):
        self.logger.debug('Enumerating existing input files...')
        def keep_file(file):
            return any([fnmatch.fnmatch(file, pattern) for pattern in AutoOcrWatchdogHandler.MATCH_PATTERNS])
        for file in self.input_dir.walk(filter=keep_file):
            self.on_file_touched(file)
        self.walk_existing_task = None

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

    def wait_for_idle(self):
        if self.walk_existing_task:
            self.logger.debug('Waiting for walk existing files to complete...')
            concurrent.futures.wait([self.walk_existing_task])
        while self.current_tasks:
            self.logger.debug('Waiting for %d tasks to complete...', len(self.current_tasks))
            concurrent.futures.wait([task.future for _, task in self.current_tasks.items()])


if __name__ == "__main__":
    logging_level = os.getenv('OCR_VERBOSITY')

    test_logging = (logging_level and str(logging_level).lower() == 'test')
    if test_logging:
        # Output DEBUG level alongside test output
        logging_level = logging.DEBUG
    elif not logging_level:
        logging_level = logging.WARNING
    elif getattr(logging, logging_level.upper(), None):
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
    
    if not test_logging:
        test_logger.propagate = False

    docker_monitor = DockerSignalMonitor()

    YES_LIKE_VALUES = ['1', 'y', 'yes', 't', 'true', 'on']
    config_dir = local.path(os.getenv('OCR_CONFIG_DIR', '/config'))
    input_dir = local.path(os.getenv('OCR_INPUT_DIR', '/input'))
    output_dir = local.path(os.getenv('OCR_OUTPUT_DIR', '/output'))
    output_mode = os.getenv('OCR_OUTPUT_MODE', AutoOcrScheduler.MIRROR_TREE)
    notify_url = os.getenv('OCR_NOTIFY_URL', '')
    action_on_success = (os.getenv('OCR_ACTION_ON_SUCCESS', OcrTask.ON_SUCCESS_DO_NOTHING))
    archive_dir = local.path(os.getenv('OCR_ARCHIVE_DIR', '/archive'))
    process_existing_files = (os.getenv('OCR_PROCESS_EXISTING_ON_START', '0').lower() in YES_LIKE_VALUES)
    single_shot_mode = (os.getenv('OCR_DO_NOT_RUN_SCHEDULER', '0').lower() in YES_LIKE_VALUES)
    polling_observer = (os.getenv('OCR_USE_POLLING_SCHEDULER', '0').lower() in YES_LIKE_VALUES)

    if single_shot_mode and not process_existing_files:
        logger.error(
            'Setting OCR_DO_NOT_RUN_SCHEDULER without OCR_PROCESS_EXISTING_ON_START '
            'results in nothing happening!'
        )

    # Run an AutoOcrScheduler
    with AutoOcrScheduler(config_dir,
                          input_dir,
                          output_dir,
                          output_mode,
                          success_action=action_on_success,
                          archive_dir=archive_dir,
                          notify_url=notify_url,
                          process_existing_files=process_existing_files,
                          run_scheduler=(not single_shot_mode),
                          polling_observer=polling_observer) as scheduler:
        if single_shot_mode:
            # Wait for all initial (process existing) tasks to complete
            scheduler.wait_for_idle()
            logger.warning('No remaining OCR tasks, and scheduler not started. Shutting down...')
        else:
            # Wait in the main thread to be terminated
            signum = docker_monitor.wait_for_exit()
            logger.warning('Signal %d (%s) Received. Shutting down...', signum, DockerSignalMonitor.SIGNUMS_TO_NAMES[signum])