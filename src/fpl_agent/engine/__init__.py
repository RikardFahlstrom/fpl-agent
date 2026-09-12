"""The decision engine: capture, project, recommend, and grade.

Reads the FPL API through `..api` and its own warehouse. The dependency runs one way -
storage is a leaf, projection builds on it, recommend builds on projection - and nothing
in `api/` imports back.
"""
