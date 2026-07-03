# The upstream scaffold tests/test_agent.py imports a non-existent `agent` package
# (leftover from the af-deep-research template) and errors at collection. It is
# unrelated to the ui/ persistence work; ignore it so tests/ui/ collects cleanly.
collect_ignore = ["test_agent.py"]
