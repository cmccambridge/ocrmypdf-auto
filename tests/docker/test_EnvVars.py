import docker
import pytest

def test_OCR_LANGUAGES_empty(create_ocr_container):
    ocr = create_ocr_container(
        initial_filespecs={
            '/input/test.pdf': 'test.pdf'
        },
        environment={
            'OCR_PROCESS_EXISTING_ON_START': '1',
            'OCR_DO_NOT_RUN_SCHEDULER': '1',
            'OCR_LANGUAGES': '',
            'OCR_VERBOSITY': 'test'
        }
    )

    ocr.start()
    ocr.wait()

    results = ocr.get_ocr_results()
    assert len(results) == 1
    assert results[0]['input_file'] == '/input/test.pdf'
    assert results[0]['return_code'] == 0
