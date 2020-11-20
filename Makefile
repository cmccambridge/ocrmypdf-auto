MKDIR_P := mkdir -p

.PHONY: all clean images test release

all: images test

clean:
	-rm -f ocrmypdf-auto
	-docker image rm ocrmypdf-auto

images: ocrmypdf-auto

ocrmypdf-auto: Dockerfile src/*
	docker build -t ocrmypdf-auto . && touch ocrmypdf-auto

test_venv: .test_venv/touch

.test_venv/touch: tests/requirements.txt
	test -d .test_venv || python3 -m venv .test_venv
	. .test_venv/bin/activate; pip install -Ur tests/requirements.txt
	touch .test_venv/touch

test: ocrmypdf-auto test_venv tests/docker/*.py
	$(MKDIR_P) test-results/docker
	. .test_venv/bin/activate; pytest --junit-xml=test-results/docker/results.xml tests/

release:
ifndef RELEASE_TAG
	$(error Missing RELEASE_TAG)
endif
	docker pull cmccambridge/ocrmypdf-auto:beta
	docker tag cmccambridge/ocrmypdf-auto:beta cmccambridge/ocrmypdf-auto:$(RELEASE_TAG)
	docker push cmccambridge/ocrmypdf-auto:$(RELEASE_TAG)
	docker tag cmccambridge/ocrmypdf-auto:beta quay.io/cmccambridge/ocrmypdf-auto:$(RELEASE_TAG)
	docker push quay.io/cmccambridge/ocrmypdf-auto:$(RELEASE_TAG)