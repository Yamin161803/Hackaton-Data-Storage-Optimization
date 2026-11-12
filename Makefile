.PHONY: setup demo test

setup:
	python -m pip install --upgrade pip
	python -m pip install -e .[dev]

demo:
	distill --help

test:
	pytest -q
