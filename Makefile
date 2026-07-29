.PHONY: lint test format desktop-parity-checks desktop-build desktop-clean docker-build k8s-deploy

lint:
	pre-commit run --all-files

format:
	black . && isort .

test:
	./run_all_tests.sh

desktop-parity-checks:
	python desktop-tauri/scripts/run_desktop_parity_checks.py

desktop-build:
	python3 desktop-tauri/scripts/build_local.py

desktop-clean:
	python3 desktop-tauri/scripts/clean_local_build.py --all

docker-build:
	docker build -t tokenplace-relay:latest -f docker/Dockerfile.relay .

k8s-deploy:
	kubectl apply -f k8s/
