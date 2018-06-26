[ocrmypdfurl]: https://github.com/jbarlow83/OCRmyPDF

# cmccambridge/ocrmypdf-auto

This container monitors an input file directory for PDF documents to process, and automatically invokes [`OCRmyPDF`][ocrmypdfurl] on each file.

It uses `inotify` to monitor the input directory efficiently, and is fairly configurable.

## Usage

Quick & Easy:
```
docker create \
  -v <input directory>:/input \
  -v <output directory>:/output \
  -v <appdata/config directory>:/config \
  cmccambridge/ocrmypdf-auto
```

Full Custom:
```
docker create \
  -v <input directory>:/input \
  -v <output directory>:/output \
  -v <appdata/config directory>:/config \
  -v <temp directory>:/ocrtemp \
  -v <archvie directory>:/archive \
  -e OCR_OUTPUT_MODE=MIRROR_FOLDERS \
  -e OCR_PROCESS_EXISTING_ON_START=1 \
  -e OCR_ACTION_ON_SUCCESS=ARCHIVE_INPUT_FILES \
  cmccambridge/ocrmypdf-auto
```

## Volumes

### Required Volumes

A few volumes are required for normal operation of `ocrmypdf-auto`:

|Volume|Description|
|---|---|
|`/input`|Directory to monitor for input files|
|`/output`|Directory to write OCR'ed PDF output to|
|`/config`|Directory to store the master `ocr.config` OCRmyPDF configuration file|

## Optional Volumes

A few additional volumes may be mounted for advanced configuration:
|Volume|Description|
|---|---|
|`/ocrtemp`|Directory to use for all OCRmyPDF temporary files, e.g. to select a ramdisk or a local cache|
|`/archive`|Directory to use for archiving input files after successful processing, in `ARCHIVE_INPUT_FILES` output mode|

## Environment Variables and Global Configuration

`ocrmypdf-auto` can be configured with a few top-level / global parameters that affect the overall operations of the container:

|Environment Variable|Description|
|---|---|
|`OCR_OUTPUT_MODE` | Controls the output directory layout: <br /> `MIRROR_FOLDERS` - (Default) Mirror the directory structure of the input directory, i.e. for an input file `/input/foo/bar.pdf` create an output file `/output/foo/bar.pdf`. <br /> `SINGLE_FOLDER` - Collect all output files in a single flat folder, i.e. for an input file `/input/foo/bar.pdf` create an output file `/output/bar.pdf`.|
|`OCR_PROCESS_EXISTING_ON_START` | Set to `1` to enable processing of any files in the input directory when the container is launched. <br/> Set to `0` (Default) or unset to ignore existing files until they are modified.|
|`OCR_ACTION_ON_SUCCESS` | Controls the action (if any) to perform after successful OCR processing: <br /> `NOTHING` - (Default) Do nothing. Input files remain in place where they were found. <br /> `ARCHIVE_INPUT_FILES` - Archive input files by **moving** them (overwriting existing files!) to the `/archive` Volume <br /> `DELETE_INPUT_FILES` - Delete the input file after successful processing.|
|`OCR_VERBOSITY` | Control the verbosity of debug logging. Accepts python `logging` levels, e.g. `warn` (Default), `info`, `debug`, etc.|
|`USERMAP_UID` | Set the UID that the OCR tools will run as.|
|`USERMAP_GID` | Set the GID that the OCR tools will run as.|

## OCRmyPDF Configuration Files

`ocrmypdf-auto` supports flexible configuration of the `ocrmypdf` binary itself, by allowing you to specify command line options in text files, one option per line.

Example:
```
# ocrmypdf-auto Config File
#
# The contents of this file are exactly one command-line option per line,
# including the "value" following the option, if any.
#
# Any blank lines or lines BEGINNING with a '#' are ignored

# Common OCRmyPDF options (see ocrmypdf --help for full current list!)

# Deskew the input file before OCR (and rebuild the output file with the skew correction)
--deskew

# Enable automatic page rotation based on tesseract orientation and script detection
--rotate-pages

# Configure automatic rotation threshold (in arbitrary units defined by tesseract)
--rotate-pages-threshold 4
```

Notes:
* The syntax is very simplistic, as described in the default `ocr.config` file that is created when the container is started with a new or empty `/config` Volume.
* It is recommended for consistency of behavior that you specify the `--skip-text` option, which will cause `OCRmyPDF` NOT to consider it an error when an input page already contains text.

### `ocr.config` File Selection

To support flexible configuration of OCRmyPDF from a single container, `ocrmypdf-auto` supports a "hierarchy" of configuration files. For a given input file, `ocrmypdf-auto` will first examine the file's own directory for an `ocr.config` to read. If found, that file (and that file _alone_) will be used to configure OCRmyPDF. If no `ocr.config` is present in the input directory, the parent directory will be searched next, and then the parent's parent, and so on up to the base `/input` Volume. If no `ocr.config` is found at any level of the directory hierarchy, then a final location is examined at `/config/ocr.config`.

Note that only a _single_ `ocr.config` file is used for a given OCRmyPDF invocation, so all desired command-line options must be specified in the corresponding configuration file. (There is no mechanism to "override" configuration from parent directories.)

For example, in this `/input` Volume hierarchy:
```
/input
├── foo
│   ├── bar
│   │   ├── bar.pdf
│   │   └── ocr.config
│   ├── baz
│   │   └── baz.pdf
│   ├── foo.pdf
│   └── ocr.config
├── qux.pdf
└── quux
    └── quuux
        └── quuuux.pdf
```

The following `ocr.config` file selections would be made:
|PDF file|`ocr.config` file|Notes|
|---|---|---|
|`/input/foo/bar/bar.pdf`|`/input/foo/bar/ocr.config`|Same directory as `bar.pdf`|
|`/input/foo/baz/baz.pdf`|`/input/foo/ocr.config`|No config file in `baz`, so parent's is selected|
|`/input/foo/foo.pdf`|`/input/foo/ocr.config`|Same directory as `foo.pdf`|
|`/input/qux.pdf`|`/config/ocr.config`|No config file exists in this directory, which is the `/input` Volume root|
|`/input/quux/quuux/quuuux.pdf`|`/config/ocr.config`|No config file in the same directory nor any parent all the way to the `/input` Volume root|

### TODO

* Create unRAID community application template
* Add automatic OCR language pack installation (via tesseract language packages)
* Build an Alpine-based image for size reduction (requires tesseract v4 to be supported in Alpine for best quality results)
