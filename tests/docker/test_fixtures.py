import pytest
from plumbum import local

# def test_ocr_tmpvols(ocr_tmpvols):
#     for vol in ['input', 'output', 'config', 'archive', 'temp']:
#         assert vol in ocr_tmpvols

#     for vol, details in ocr_tmpvols.items():
#         assert details['mode'] == 'rw'
#         bind = local.path(details['bind'])
#         assert bind.is_dir()
#         (bind / "test.txt").touch()
