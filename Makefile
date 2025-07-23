.PHONY: lint test format docker-build k8s-deploy

lint:
	pre-commit run --all-files

format:
	black . && isort .

test:
	./run_all_tests.sh

docker-build:
	docker build -t tokenplace-relay:latest -f docker/Dockerfile.relay .

k8s-deploy:
	kubectl apply -f k8s/
