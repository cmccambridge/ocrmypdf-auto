import logging
import tarfile

from io import BytesIO

import docker
import pytest

@pytest.fixture
def create_volume():
    docker_client = docker.from_env()
    volumes = []

    def _create_volume(name=None, **kwargs):
        """Create a docker volume with the given name (or None for
           an anonymous volume), which will be automatically deleted
           at the end of the test
        """
        volume = docker_client.volumes.create(name=name, **kwargs)
        volumes.append(volume)
        return volume

    yield _create_volume

    for volume in volumes:
        volume.remove(force=True)


@pytest.fixture
def create_container(request, create_volume):
    default_image_name = getattr(request.module, 'DOCKER_IMAGE', 'ocrmypdf-auto')

    docker_client = docker.from_env()
    containers = []
    
    def _create_container(image_name=default_image_name, anonymous_volumes=None, command=None, **kwargs):
        """Create a docker container with the given parameters that will
           be automatically cleaned up at the end of the test
        """
        if anonymous_volumes:
            anon_volumes = {create_volume().id : mount for mount in anonymous_volumes}
            if 'volumes' not in kwargs:
                kwargs['volumes'] = {}
            if isinstance(kwargs['volumes'], dict):
                kwargs['volumes'].update({ vol_id : { 'bind': mount, 'mode': 'rw' } for vol_id, mount in anon_volumes.items() })
            else:
                kwargs['volumes'].extend([f'{vol_id}:{mount}' for vol_id, mount in anon_volumes.items()])

        container = docker_client.containers.create(image_name, command=command, **kwargs)
        containers.append(container)
        return container
    
    yield _create_container

    for container in containers:
        container.stop()
        container.remove(force=True)

def create_tar_archive(filespecs):
    tar_stream = BytesIO()
    with tarfile.open(fileobj=tar_stream, mode='w') as tar:
        for archive_name, local_path in filespecs.items():
            tar.add(name=local_path, arcname=archive_name)
    tar_stream.seek(0)
    return tar_stream

class OcrContainerHelper:
    def __init__(self, container, initial_filespecs=None):
        self.logger = logging.getLogger('OcrContainerHelper')
        self.container = container
        self.put_files(initial_filespecs)

    def start(self):
        return self.container.start()

    def stop(self):
        return self.container.stop()

    def logs(self):
        return self.container.logs()

    def wait(self, timeout=None):
        result = self.container.wait(timeout=timeout)
        rc = int(result['StatusCode'])
        self.logger.log(
            (logging.WARNING if rc != 0 else logging.DEBUG),
            'Container exited with status %d. Logs:\n%s',
            rc,
            self.logs()
        )
        return rc

    def cleanup(self):
        self.stop()

    def get_ocr_results(self):
        results = []
        for line in self.container.logs().splitlines():
            if b'TEST\0OCR_PROCESS_RESULT\0' not in line:
                continue
            (input_file, output_file, rc, duration) = line.split(b'\0')[2:]
            results.append({
                'input_file': str(input_file, encoding='utf-8'),
                'output_file': str(output_file, encoding='utf-8'),
                'return_code': int(rc),
                'duration': float(duration)
            })
        return results

    def put_files(self, filespecs):
        with create_tar_archive(filespecs) as tar:
            self.container.put_archive('/', tar)

    def get_file_names(self):
        pass

    def get_file(self, path):
        pass

@pytest.fixture
def create_ocr_container(create_container, create_volume):
    ocr_helpers = []

    def _create_ocr_container(initial_filespecs=None, **kwargs):
        mounts = ['/input', '/output', '/config', '/archive', '/ocrtemp']

        container = create_container(
            image_name='ocrmypdf-auto',
            anonymous_volumes = mounts,
            **kwargs
        )
        helper = OcrContainerHelper(container, initial_filespecs=initial_filespecs)
        ocr_helpers.append(helper)
        return helper

    yield _create_ocr_container

    for helper in ocr_helpers:
        helper.cleanup()