IMAGE_NAME := opencode-agent-isolated
IMAGE_REF := localhost/$(IMAGE_NAME):latest

.PHONY: install build shell opencode clean volumes reset images

install:
	./install.sh

build:
	podman build -t $(IMAGE_NAME) . --no-cache
	podman save $(IMAGE_NAME) | msb load

shell:
	./run.sh bash

opencode:
	./run.sh opencode

clean:
	podman rmi $(IMAGE_NAME) || true
	msb rmi $(IMAGE_REF) || true

volumes:
	@msb volume ls

reset:
	./run.sh --reset

images:
	@msb images -q | grep 'localhost/$(IMAGE_NAME)' || true
