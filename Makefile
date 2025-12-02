.PHONY: setup run-dev test docker-build docker-up docker-down docker-logs format lint clean help

help:
	@echo "Available commands:"
	@echo "  make setup          - Setup development environment"
	@echo "  make run-dev        - Run service locally in development mode"
	@echo "  make test           - Run tests"
	@echo "  make docker-build   - Build Docker image"
	@echo "  make docker-up      - Start service with Docker Compose"
	@echo "  make docker-down    - Stop service with Docker Compose"
	@echo "  make docker-logs    - View Docker logs"
	@echo "  make format         - Format code with black and isort"
	@echo "  make lint           - Run linters"
	@echo "  make clean          - Clean up generated files"

setup:
	python3 -m venv venv
	./venv/bin/pip install --upgrade pip
	./venv/bin/pip install -r requirements.txt
	./venv/bin/pip install -r requirements-dev.txt
	@echo "Setup complete! Activate venv with: source venv/bin/activate"

run-dev:
	./venv/bin/uvicorn src.main:app --reload --host 0.0.0.0 --port 8000

test:
	./venv/bin/pytest tests/ -v --asyncio-mode=auto

docker-build:
	docker build -f docker/Dockerfile -t transcription-service:latest .

docker-up:
	docker-compose -f docker/docker-compose.yml up -d

docker-down:
	docker-compose -f docker/docker-compose.yml down

docker-logs:
	docker-compose -f docker/docker-compose.yml logs -f

format:
	./venv/bin/black src/ tests/
	./venv/bin/isort src/ tests/

lint:
	./venv/bin/pylint src/
	./venv/bin/mypy src/

clean:
	find . -type d -name "__pycache__" -exec rm -rf {} +
	find . -type f -name "*.pyc" -delete
	find . -type d -name "*.egg-info" -exec rm -rf {} +
	find . -type d -name ".pytest_cache" -exec rm -rf {} +
	find . -type d -name ".mypy_cache" -exec rm -rf {} +
	rm -rf logs/*.log
